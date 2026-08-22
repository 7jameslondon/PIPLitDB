"""Deterministic native-text extraction for legacy scientific PDFs.

The extractor deliberately does not perform OCR.  It combines pdfplumber's
character geometry with the PDF font dictionaries read by pypdf so that old
Type 1 ``/Encoding /Differences`` fonts can be decoded without depending on a
viewer-specific fallback font.

Coordinates in the public models use PDF points and a top-left origin.
Unknown glyphs are never silently guessed: they become U+FFFD and are recorded
in both ``diagnostic_rows`` and ``warnings``.
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pdfplumber
from pypdf import PdfReader
from pypdf._codecs import adobe_glyphs, charset_encoding


_CID_PATTERN = re.compile(r"^\(cid:(\d+)\)$")
_CNN_PATTERN = re.compile(r"^C(\d+)$", re.IGNORECASE)
_SUBSET_PATTERN = re.compile(r"^[A-Z]{6}\+")
_REPLACEMENT = "\N{REPLACEMENT CHARACTER}"
_STANDARD_ENCODING = charset_encoding["/StandardEncoding"]
_STANDARD_REVERSE: dict[str, tuple[int, ...]] = {}
for _standard_code, _standard_character in enumerate(_STANDARD_ENCODING):
    _STANDARD_REVERSE[_standard_character] = (
        *_STANDARD_REVERSE.get(_standard_character, ()),
        _standard_code,
    )


class PdfTextExtractionError(ValueError):
    """Raised when native PDF text cannot be extracted safely."""


@dataclass(frozen=True)
class PdfTextLine:
    """One spatial text line or line fragment from a PDF page."""

    page: int
    bbox: tuple[float, float, float, float]
    plain_text: str
    markdown: str
    source_locator: str
    rotated: bool = False
    source_region_id: str | None = None
    ocr_confidence: float | None = None
    constituent_geometry: tuple[
        tuple[int, tuple[float, float, float, float]], ...
    ] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "bbox": list(self.bbox),
            "plain_text": self.plain_text,
            "markdown": self.markdown,
            "source_locator": self.source_locator,
            "rotated": self.rotated,
            **(
                {"source_region_id": self.source_region_id}
                if self.source_region_id is not None
                else {}
            ),
            **(
                {"ocr_confidence": self.ocr_confidence}
                if self.ocr_confidence is not None
                else {}
            ),
            **(
                {
                    "constituent_geometry": [
                        {"page": page, "bbox": list(bbox)}
                        for page, bbox in self.constituent_geometry
                    ]
                }
                if self.constituent_geometry
                else {}
            ),
        }


@dataclass(frozen=True)
class PdfTextPage:
    """Native-text classification and lines for one PDF page."""

    page: int
    width: float
    height: float
    classification: str
    lines: list[PdfTextLine]

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "width": self.width,
            "height": self.height,
            "classification": self.classification,
            "lines": [line.as_dict() for line in self.lines],
        }


@dataclass(frozen=True)
class PdfTextDocument:
    """Result of deterministic native-text extraction."""

    relative_path: str
    pages: list[PdfTextPage]
    diagnostic_rows: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    all_pages_classified: bool

    @property
    def lines(self) -> list[PdfTextLine]:
        """Return all lines in page order as a convenient flattened view."""

        return [line for page in self.pages for line in page.lines]

    def as_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "pages": [page.as_dict() for page in self.pages],
            "diagnostic_rows": self.diagnostic_rows,
            "warnings": self.warnings,
            "all_pages_classified": self.all_pages_classified,
        }


@dataclass(frozen=True)
class _FontInfo:
    full_name: str
    name: str
    differences: Mapping[int, str]
    cnn_encoded: bool
    bold: bool
    italic: bool


@dataclass
class _DecodedChar:
    text: str
    source: Mapping[str, Any]
    bold: bool
    italic: bool
    script: str | None = None
    diagnostic: dict[str, Any] | None = None


@dataclass
class _SpatialLineCluster:
    chars: list[Mapping[str, Any]]
    max_size: float
    baseline_sum: float
    baseline_count: int

    @property
    def baseline(self) -> float:
        return self.baseline_sum / self.baseline_count

    def add(self, char: Mapping[str, Any]) -> None:
        self.chars.append(char)
        size = float(char.get("size", 0.0))
        if size >= self.max_size * 0.85:
            self.baseline_sum += float(char["bottom"])
            self.baseline_count += 1


# The AdvPS Greek fonts use Latin character names/codes to select Greek
# outlines.  The mapping follows the conventional Symbol-font transliteration.
_GREEK_FROM_LATIN = {
    "A": "Α",
    "B": "Β",
    "C": "Χ",
    "D": "Δ",
    "E": "Ε",
    "F": "Φ",
    "G": "Γ",
    "H": "Η",
    "I": "Ι",
    "J": "ϑ",
    "K": "Κ",
    "L": "Λ",
    "M": "Μ",
    "N": "Ν",
    "O": "Ο",
    "P": "Π",
    "Q": "Θ",
    "R": "Ρ",
    "S": "Σ",
    "T": "Τ",
    "U": "Υ",
    "V": "ϖ",
    "W": "Ω",
    "X": "Ξ",
    "Y": "Ψ",
    "Z": "Ζ",
    "a": "α",
    "b": "β",
    "c": "χ",
    "d": "δ",
    "e": "ε",
    "f": "φ",
    "g": "γ",
    "h": "η",
    "i": "ι",
    "j": "ϕ",
    "k": "κ",
    "l": "λ",
    "m": "μ",
    "n": "ν",
    "o": "ο",
    "p": "π",
    "q": "θ",
    "r": "ρ",
    "s": "σ",
    "t": "τ",
    "u": "υ",
    "v": "ς",
    "w": "ω",
    "x": "ξ",
    "y": "ψ",
    "z": "ζ",
}


def extract_pdf_text(
    path: Path,
    relative_path: str,
    *,
    glyph_overrides: Mapping[str, Mapping[int | str, str]] | None = None,
) -> PdfTextDocument:
    """Extract deterministic native text and geometry from *path*.

    ``glyph_overrides`` is keyed by a PDF base-font name (with or without its
    six-letter subset prefix), followed by a C-number as an integer, decimal
    string, or string such as ``"C60"``.  ``"*"`` may be used as the outer
    fallback font key.  Overrides are applied before built-in mappings.

    The function does not discard rotated text.  Rotated runs (including, but
    not limited to, publisher download strips) are returned with
    ``PdfTextLine.rotated`` set, classified on its page, and represented by a
    diagnostic row.  A caller can then exclude a source-specific region without
    this module hardcoding a journal or record.  Rotation alone is not a warning.
    """

    source_path = Path(path)
    if not source_path.is_file():
        raise PdfTextExtractionError(f"PDF source does not exist: {source_path}")
    normalized_relative = _normalize_relative_path(relative_path)
    normalized_overrides = _normalize_overrides(glyph_overrides)

    try:
        reader = PdfReader(source_path)
        page_fonts = [_collect_page_fonts(page) for page in reader.pages]
    except Exception as exc:
        raise PdfTextExtractionError(f"could not read PDF font dictionaries: {exc}") from exc

    pages: list[PdfTextPage] = []
    diagnostic_rows: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    try:
        with pdfplumber.open(source_path) as pdf:
            if len(pdf.pages) != len(page_fonts):
                raise PdfTextExtractionError(
                    "pypdf and pdfplumber reported different page counts"
                )
            for page_number, page in enumerate(pdf.pages, start=1):
                lines = _extract_page_lines(
                    page,
                    page_number,
                    normalized_relative,
                    page_fonts[page_number - 1],
                    normalized_overrides,
                    diagnostic_rows,
                    warnings,
                )
                has_chars = bool(page.chars)
                has_upright = any(not line.rotated for line in lines)
                has_rotated = any(line.rotated for line in lines)
                if has_upright and has_rotated:
                    classification = "native_text_with_rotated_text"
                elif has_upright:
                    classification = "native_text"
                elif has_rotated:
                    classification = "rotated_text_only"
                elif has_chars:
                    classification = "unclassified"
                elif page.images:
                    classification = "image_only"
                else:
                    classification = "blank"
                if classification == "image_only":
                    warnings.append(
                        {
                            "code": "image_only_page",
                            "severity": "warning",
                            "page": page_number,
                            "source_path": normalized_relative,
                            "message": "Page has images but no native text; OCR was not performed.",
                        }
                    )
                pages.append(
                    PdfTextPage(
                        page=page_number,
                        width=_clean_number(page.width),
                        height=_clean_number(page.height),
                        classification=classification,
                        lines=lines,
                    )
                )
    except PdfTextExtractionError:
        raise
    except Exception as exc:
        raise PdfTextExtractionError(f"could not extract native PDF text: {exc}") from exc

    return PdfTextDocument(
        relative_path=normalized_relative,
        pages=pages,
        diagnostic_rows=diagnostic_rows,
        warnings=warnings,
        all_pages_classified=all(
            page.classification != "unclassified" for page in pages
        ),
    )


def _extract_page_lines(
    page: Any,
    page_number: int,
    relative_path: str,
    fonts: Mapping[str, _FontInfo],
    overrides: Mapping[str, Mapping[str, str]],
    diagnostic_rows: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> list[PdfTextLine]:
    character_groups: list[tuple[list[Mapping[str, Any]], bool]] = []

    # Work from page.chars rather than extract_text_lines(return_chars=True).
    # The latter drops literal space characters; in a subset Cnn encoding a raw
    # byte 32 can map to an ordinary letter (for example, /C83 = S).
    upright_chars = [char for char in page.chars if char.get("upright", True)]
    for spatial_line in _group_upright_chars(upright_chars):
        for fragment in _split_at_column_gaps(spatial_line):
            if fragment:
                character_groups.append((fragment, False))

    rotated_chars = [char for char in page.chars if not char.get("upright", True)]
    for fragment in _group_rotated_chars(rotated_chars):
        if fragment:
            character_groups.append((fragment, True))

    lines: list[PdfTextLine] = []
    for chars, rotated in character_groups:
        line, rows, line_warnings = _make_line(
            chars,
            page_number,
            relative_path,
            fonts,
            overrides,
            rotated=rotated,
        )
        if line.plain_text:
            lines.append(line)
            diagnostic_rows.extend(rows)
            warnings.extend(line_warnings)

    lines.sort(key=lambda line: (line.bbox[1], line.bbox[0], line.rotated))
    return lines


def _make_line(
    chars: Sequence[Mapping[str, Any]],
    page_number: int,
    relative_path: str,
    fonts: Mapping[str, _FontInfo],
    overrides: Mapping[str, Mapping[str, str]],
    *,
    rotated: bool,
) -> tuple[PdfTextLine, list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(chars, key=(lambda char: char["top"] if rotated else char["x0"]))
    decoded = [_decode_char(char, page_number, fonts, overrides) for char in ordered]
    if not rotated:
        _infer_scripts(decoded)

    axis_gap = (
        (lambda previous, current: current["top"] - previous["bottom"])
        if rotated
        else (lambda previous, current: current["x0"] - previous["x1"])
    )
    sizes = [float(char.get("size", 0.0)) for char in ordered if char.get("size")]
    median_size = statistics.median(sizes) if sizes else 8.0
    word_gap = max(0.8, median_size * 0.18)

    tokens: list[_DecodedChar] = []
    for index, item in enumerate(decoded):
        if index:
            previous = ordered[index - 1]
            if (
                tokens
                and not tokens[-1].text.isspace()
                and not item.text.isspace()
                and axis_gap(previous, ordered[index]) > word_gap
            ):
                tokens.append(
                    _DecodedChar(" ", {}, bold=False, italic=False, script=None)
                )
        if item.text.isspace():
            if tokens and not tokens[-1].text.isspace():
                tokens.append(
                    _DecodedChar(" ", {}, bold=False, italic=False, script=None)
                )
        else:
            tokens.append(item)

    while tokens and tokens[0].text.isspace():
        tokens.pop(0)
    while tokens and tokens[-1].text.isspace():
        tokens.pop()

    _canonicalize_script_token_order(tokens)
    plain_text = "".join(token.text for token in tokens)
    markdown = _render_markdown(tokens)
    bbox = (
        _clean_number(min(float(char["x0"]) for char in ordered)),
        _clean_number(min(float(char["top"]) for char in ordered)),
        _clean_number(max(float(char["x1"]) for char in ordered)),
        _clean_number(max(float(char["bottom"]) for char in ordered)),
    )
    source_locator = _source_locator(page_number, bbox)

    rows: list[dict[str, Any]] = []
    line_warnings: list[dict[str, Any]] = []
    context = _context_excerpt(plain_text)
    for item in decoded:
        if item.diagnostic is None:
            continue
        row = dict(item.diagnostic)
        row["source_path"] = relative_path
        row["source_locator"] = source_locator
        row["context"] = context
        rows.append(row)
        if row["status"] == "unresolved":
            line_warnings.append(
                {
                    "code": "unresolved_glyph",
                    "severity": "warning",
                    "page": page_number,
                    "font": row["font"],
                    "encoded_code": row.get("encoded_code"),
                    "glyph_name": row.get("glyph_name"),
                    "bbox": row["bbox"],
                    "source_path": relative_path,
                    "source_locator": source_locator,
                    "context": context,
                    "message": (
                        f"Unresolved glyph {row.get('glyph_name') or row.get('encoded_code')!r} "
                        f"in font {row['font']!r}; inserted U+FFFD."
                    ),
                }
            )

    if rotated:
        rows.append(
            {
                "kind": "rotated_text",
                "status": "marked",
                "page": page_number,
                "bbox": list(bbox),
                "source_path": relative_path,
                "source_locator": source_locator,
                "context": context,
            }
        )

    return (
        PdfTextLine(
            page=page_number,
            bbox=bbox,
            plain_text=plain_text,
            markdown=markdown,
            source_locator=source_locator,
            rotated=rotated,
        ),
        rows,
        line_warnings,
    )


def _decode_char(
    char: Mapping[str, Any],
    page_number: int,
    fonts: Mapping[str, _FontInfo],
    overrides: Mapping[str, Mapping[str, str]],
) -> _DecodedChar:
    raw_font = str(char.get("fontname", "unknown"))
    normalized_font = _normalize_font_name(raw_font)
    font = fonts.get(raw_font) or fonts.get(normalized_font)
    if font is None:
        font = _FontInfo(
            full_name=raw_font,
            name=normalized_font,
            differences={},
            cnn_encoded=False,
            bold=_font_name_is_bold(normalized_font),
            italic=_font_name_is_italic(normalized_font),
        )

    original = str(char.get("text", ""))
    cid_match = _CID_PATTERN.fullmatch(original)
    diagnostic: dict[str, Any] | None = None
    decoded = original
    encoded_code: int | None = None
    if cid_match:
        encoded_code = int(cid_match.group(1))
    elif font.cnn_encoded and len(original) == 1:
        # pdfminer/pdfplumber returns printable encoded bytes literally even
        # when a subset Type 1 font remaps that byte through /Differences.  For
        # example, a raw byte 40 may be shown as '(' although Differences[40]
        # is /C112 (the semantic 'p').  Remap every one-character byte for Cnn
        # subset encodings, not only non-printable ``(cid:n)`` fallbacks.
        candidate = ord(original)
        if candidate in font.differences:
            encoded_code = candidate
        else:
            # pdfminer applies StandardEncoding to a few printable bytes before
            # exposing them (notably byte 39 -> U+2019).  Reverse that decoding,
            # but only when it identifies exactly one byte present in this
            # font's /Differences table.
            reverse_candidates = [
                code
                for code in _STANDARD_REVERSE.get(original, ())
                if code in font.differences
            ]
            if len(reverse_candidates) == 1:
                encoded_code = reverse_candidates[0]

    if encoded_code is not None:
        glyph_name = font.differences.get(encoded_code)
        decoded, status, method, semantic_code = _resolve_encoded_glyph(
            font, encoded_code, glyph_name, overrides
        )
        # Ordinary Cnn characters resolved through Adobe StandardEncoding are
        # the common case in these subset fonts.  Keep diagnostics focused on
        # special-font decisions, explicit overrides, and unresolved glyphs.
        if method != "adobe_standard_encoding":
            diagnostic = {
                "kind": "glyph_decoding",
                "status": status,
                "method": method,
                "page": page_number,
                "font": font.name,
                "font_full_name": font.full_name,
                "encoded_code": encoded_code,
                "glyph_name": glyph_name,
                "semantic_code": semantic_code,
                "replacement": decoded,
                "bbox": _char_bbox(char),
            }
    elif _contains_unknown_or_control(original):
        decoded = _replace_unknown_or_control(original)
        diagnostic = {
            "kind": "glyph_decoding",
            "status": "unresolved",
            "method": "unknown_or_control_character",
            "page": page_number,
            "font": font.name,
            "font_full_name": font.full_name,
            "encoded_code": ord(original[0]) if original else None,
            "glyph_name": None,
            "semantic_code": None,
            "replacement": decoded,
            "bbox": _char_bbox(char),
        }

    return _DecodedChar(
        text=decoded,
        source=char,
        bold=font.bold,
        italic=font.italic,
        diagnostic=diagnostic,
    )


def _resolve_encoded_glyph(
    font: _FontInfo,
    encoded_code: int,
    glyph_name: str | None,
    overrides: Mapping[str, Mapping[str, str]],
) -> tuple[str, str, str, int | None]:
    cnn_match = _CNN_PATTERN.fullmatch(glyph_name or "")
    semantic_code = int(cnn_match.group(1)) if cnn_match else None
    override = _lookup_override(
        overrides,
        font,
        encoded_code=encoded_code,
        glyph_name=glyph_name,
        semantic_code=semantic_code,
    )
    if override is not None:
        return override, "resolved", "glyph_override", semantic_code

    lower_name = font.name.lower()
    if semantic_code is not None:
        if "advpi1" in lower_name:
            builtins = {39: "′", 56: "°"}
            if semantic_code in builtins:
                return builtins[semantic_code], "resolved", "advpi1_builtin", semantic_code
            return _REPLACEMENT, "unresolved", "unsupported_advpi1_symbol", semantic_code

        if "advps_grtu" in lower_name and semantic_code == 109:
            return "μ", "resolved", "advps_grtu_builtin", semantic_code

        if "advps_grti" in lower_name or "advps_grtbi" in lower_name:
            if 0 <= semantic_code <= 0x10FFFF:
                greek = _GREEK_FROM_LATIN.get(chr(semantic_code))
                if greek is not None:
                    return greek, "resolved", "advps_greek_transliteration", semantic_code
            return _REPLACEMENT, "unresolved", "unsupported_advps_greek_symbol", semantic_code

        if "advp4" in lower_name:
            builtins = {136: "=", 135: "+"}
            if semantic_code in builtins:
                return builtins[semantic_code], "resolved", "advp4_builtin", semantic_code
            return _REPLACEMENT, "unresolved", "unsupported_advp4_symbol", semantic_code

        if 0 <= semantic_code < len(_STANDARD_ENCODING):
            value = _STANDARD_ENCODING[semantic_code]
            if not _contains_unknown_or_control(value):
                return value, "resolved", "adobe_standard_encoding", semantic_code
        return _REPLACEMENT, "unresolved", "undefined_standard_encoding", semantic_code

    if glyph_name:
        value = adobe_glyphs.get("/" + glyph_name.lstrip("/"))
        if value and not _contains_unknown_or_control(value):
            return value, "resolved", "adobe_glyph_list", None

    return _REPLACEMENT, "unresolved", "unknown_encoding_difference", None


def _infer_scripts(chars: Sequence[_DecodedChar]) -> None:
    visible = [char for char in chars if char.text and not char.text.isspace()]
    if len(visible) < 2:
        return
    size_counts = Counter(round(float(char.source.get("size", 0.0)), 1) for char in visible)
    dominant_size = size_counts.most_common(1)[0][0]
    if dominant_size <= 0:
        return
    baseline_chars = [
        char
        for char in visible
        if abs(float(char.source.get("size", 0.0)) - dominant_size)
        <= dominant_size * 0.08
    ]
    if not baseline_chars:
        return
    baseline_bottom = statistics.median(
        float(char.source["bottom"]) for char in baseline_chars
    )
    for char in visible:
        size = float(char.source.get("size", 0.0))
        if size <= 0 or size > dominant_size * 0.82:
            continue
        shift = float(char.source["bottom"]) - baseline_bottom
        if shift < -dominant_size * 0.16:
            char.script = "sup"
        elif shift > dominant_size * 0.16:
            char.script = "sub"


def _canonicalize_script_token_order(chars: list[_DecodedChar]) -> None:
    """Put a molecular-formula subscript before a following ionic charge.

    Some legacy Type 1 PDFs paint a superscript charge before the subscript
    atom count even though the page visually reads the count first (for
    example, the content stream order ``O``, ``+``, ``3`` renders as O₃⁺).
    Geometry has already identified the scripts at this point, so the narrow
    and deterministic correction below restores semantic reading order without
    guessing from the characters alone.
    """

    index = 0
    while index < len(chars) - 1:
        charge = chars[index]
        if charge.script != "sup" or charge.text not in {"+", "-", "−"}:
            index += 1
            continue

        end = index + 1
        while (
            end < len(chars)
            and chars[end].script == "sub"
            and chars[end].text.isdigit()
        ):
            end += 1
        if end == index + 1:
            index += 1
            continue

        chars[index:end] = [*chars[index + 1 : end], charge]
        index = end


def _render_markdown(chars: Sequence[_DecodedChar]) -> str:
    if not chars:
        return ""
    runs: list[tuple[tuple[bool, bool, str | None], str]] = []
    for index, char in enumerate(chars):
        style = (char.bold, char.italic, char.script)
        if char.text.isspace():
            previous = next(
                (
                    candidate
                    for candidate in reversed(chars[:index])
                    if not candidate.text.isspace()
                ),
                None,
            )
            following = next(
                (
                    candidate
                    for candidate in chars[index + 1 :]
                    if not candidate.text.isspace()
                ),
                None,
            )
            previous_style = (
                (previous.bold, previous.italic, previous.script)
                if previous is not None
                else None
            )
            following_style = (
                (following.bold, following.italic, following.script)
                if following is not None
                else None
            )
            # Geometric word-gap spaces have no font of their own.  When the
            # visible runs on both sides share a style, keep the space inside
            # that run so phrases render as ``*two words*`` instead of the
            # machine-hostile ``*two* *words*``.
            style = (
                previous_style
                if previous_style is not None and previous_style == following_style
                else (False, False, None)
            )
        if runs and runs[-1][0] == style:
            runs[-1] = (style, runs[-1][1] + char.text)
        else:
            runs.append((style, char.text))

    rendered: list[str] = []
    for (bold, italic, script), text in runs:
        escaped = _escape_markdown(text)
        if bold and italic:
            escaped = f"***{escaped}***"
        elif bold:
            escaped = f"**{escaped}**"
        elif italic:
            escaped = f"*{escaped}*"
        if script is not None:
            escaped = f"<{script}>{escaped}</{script}>"
        rendered.append(escaped)
    return "".join(rendered).strip()


def _group_upright_chars(
    chars: Sequence[Mapping[str, Any]],
) -> list[list[Mapping[str, Any]]]:
    """Cluster upright characters into spatial baselines.

    Larger characters establish each baseline before smaller characters are
    assigned.  This keeps conservative superscripts/subscripts with their text
    line while avoiding mergers between adjacent body lines.
    """

    if not chars:
        return []
    clusters: list[_SpatialLineCluster] = []
    ordered = sorted(
        chars,
        key=lambda char: (
            -float(char.get("size", 0.0)),
            float(char["bottom"]),
            float(char["x0"]),
        ),
    )
    for char in ordered:
        size = max(float(char.get("size", 0.0)), 1.0)
        bottom = float(char["bottom"])
        best: tuple[float, _SpatialLineCluster] | None = None
        for cluster in clusters:
            distance = abs(bottom - cluster.baseline)
            tolerance = max(2.0, max(size, cluster.max_size) * 0.5)
            if distance <= tolerance and (best is None or distance < best[0]):
                best = (distance, cluster)
        if best is None:
            clusters.append(
                _SpatialLineCluster(
                    chars=[char],
                    max_size=size,
                    baseline_sum=bottom,
                    baseline_count=1,
                )
            )
        else:
            best[1].add(char)

    for cluster in clusters:
        cluster.chars.sort(key=lambda char: char["x0"])
    clusters.sort(
        key=lambda cluster: (
            min(float(char["top"]) for char in cluster.chars),
            min(float(char["x0"]) for char in cluster.chars),
        )
    )
    return [cluster.chars for cluster in clusters]


def _split_at_column_gaps(
    chars: Sequence[Mapping[str, Any]],
) -> list[list[Mapping[str, Any]]]:
    ordered = sorted(chars, key=lambda char: char["x0"])
    if len(ordered) < 2:
        return [ordered]
    sizes = [float(char.get("size", 0.0)) for char in ordered if char.get("size")]
    median_size = statistics.median(sizes) if sizes else 8.0
    threshold = max(9.0, median_size * 1.05)
    groups: list[list[Mapping[str, Any]]] = [[ordered[0]]]
    for previous, current in zip(ordered, ordered[1:]):
        if float(current["x0"]) - float(previous["x1"]) > threshold:
            groups.append([])
        groups[-1].append(current)
    return groups


def _group_rotated_chars(
    chars: Sequence[Mapping[str, Any]],
) -> list[list[Mapping[str, Any]]]:
    if not chars:
        return []
    ordered = sorted(chars, key=lambda char: (char["x0"], char["top"]))
    columns: list[list[Mapping[str, Any]]] = []
    for char in ordered:
        center = (float(char["x0"]) + float(char["x1"])) / 2.0
        for column in columns:
            reference = (float(column[0]["x0"]) + float(column[0]["x1"])) / 2.0
            tolerance = max(3.0, float(char.get("size", 0.0)))
            if abs(center - reference) <= tolerance:
                column.append(char)
                break
        else:
            columns.append([char])

    groups: list[list[Mapping[str, Any]]] = []
    for column in columns:
        column.sort(key=lambda char: char["top"])
        current: list[Mapping[str, Any]] = []
        for char in column:
            if current:
                previous = current[-1]
                size = max(float(char.get("size", 0.0)), float(previous.get("size", 0.0)))
                if float(char["top"]) - float(previous["bottom"]) > max(15.0, size * 3.0):
                    groups.append(current)
                    current = []
            current.append(char)
        if current:
            groups.append(current)
    return groups


def _collect_page_fonts(page: Any) -> dict[str, _FontInfo]:
    fonts: dict[str, _FontInfo] = {}
    visited: set[tuple[int, int] | int] = set()

    def visit_resources(resources_value: Any) -> None:
        if resources_value is None:
            return
        resources = resources_value.get_object() if hasattr(resources_value, "get_object") else resources_value
        marker = _object_marker(resources_value, resources)
        if marker in visited:
            return
        visited.add(marker)

        font_resources = resources.get("/Font", {})
        font_resources = (
            font_resources.get_object()
            if hasattr(font_resources, "get_object")
            else font_resources
        )
        for font_value in font_resources.values():
            font_dict = font_value.get_object()
            full_name = str(font_dict.get("/BaseFont", "unknown")).lstrip("/")
            name = _normalize_font_name(full_name)
            differences = _font_differences(font_dict.get("/Encoding"))
            info = _FontInfo(
                full_name=full_name,
                name=name,
                differences=differences,
                cnn_encoded=bool(differences)
                and all(_CNN_PATTERN.fullmatch(value) for value in differences.values()),
                bold=_font_name_is_bold(name),
                italic=_font_name_is_italic(name),
            )
            fonts[full_name] = info
            fonts[name] = info

        xobjects = resources.get("/XObject", {})
        xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
        for value in xobjects.values():
            try:
                xobject = value.get_object()
            except Exception:
                continue
            if "/Resources" in xobject:
                visit_resources(xobject["/Resources"])

    visit_resources(page.get("/Resources"))
    return fonts


def _font_differences(encoding_value: Any) -> dict[int, str]:
    if encoding_value is None:
        return {}
    encoding = (
        encoding_value.get_object()
        if hasattr(encoding_value, "get_object")
        else encoding_value
    )
    if not isinstance(encoding, Mapping):
        return {}
    differences = encoding.get("/Differences", [])
    result: dict[int, str] = {}
    code: int | None = None
    for item in differences:
        if isinstance(item, int):
            code = item
        elif code is not None:
            result[code] = str(item).lstrip("/")
            code += 1
    return result


def _normalize_overrides(
    overrides: Mapping[str, Mapping[int | str, str]] | None,
) -> dict[str, dict[str, str]]:
    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise PdfTextExtractionError("glyph_overrides must be a mapping")
    normalized: dict[str, dict[str, str]] = {}
    for font_name, values in overrides.items():
        if not isinstance(font_name, str) or not font_name.strip():
            raise PdfTextExtractionError("glyph override font names must be non-empty strings")
        if not isinstance(values, Mapping):
            raise PdfTextExtractionError(
                f"glyph overrides for {font_name!r} must be a mapping"
            )
        normalized_font = "*" if font_name == "*" else _normalize_font_name(font_name)
        bucket: dict[str, str] = {}
        for key, replacement in values.items():
            normalized_key = _normalize_override_key(key)
            if not isinstance(replacement, str) or not replacement:
                raise PdfTextExtractionError(
                    f"glyph override {font_name!r}/{key!r} must be a non-empty string"
                )
            if _contains_unknown_or_control(replacement):
                raise PdfTextExtractionError(
                    f"glyph override {font_name!r}/{key!r} contains a control or unknown character"
                )
            bucket[normalized_key] = replacement
        normalized[normalized_font.lower()] = bucket
    return normalized


def _lookup_override(
    overrides: Mapping[str, Mapping[str, str]],
    font: _FontInfo,
    *,
    encoded_code: int,
    glyph_name: str | None,
    semantic_code: int | None,
) -> str | None:
    selectors = (font.name.lower(), font.full_name.lower(), "*")
    keys: list[str] = []
    if glyph_name:
        keys.append(glyph_name.upper())
    if semantic_code is not None:
        keys.append(f"C{semantic_code}")
    keys.append(f"RAW:{encoded_code}")
    for selector in selectors:
        values = overrides.get(selector)
        if values is None:
            continue
        for key in keys:
            if key in values:
                return values[key]
    return None


def _normalize_override_key(key: int | str) -> str:
    if isinstance(key, bool):
        raise PdfTextExtractionError("boolean glyph override keys are not allowed")
    if isinstance(key, int):
        if key < 0:
            raise PdfTextExtractionError("glyph override codes cannot be negative")
        return f"C{key}"
    if not isinstance(key, str) or not key.strip():
        raise PdfTextExtractionError("glyph override keys must be integers or strings")
    value = key.strip()
    if value.isdecimal():
        return f"C{int(value)}"
    match = _CNN_PATTERN.fullmatch(value)
    if match:
        return f"C{int(match.group(1))}"
    raw_match = re.fullmatch(r"RAW:(\d+)", value, re.IGNORECASE)
    if raw_match:
        return f"RAW:{int(raw_match.group(1))}"
    glyph_name = value.lstrip("/")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", glyph_name):
        return glyph_name.upper()
    raise PdfTextExtractionError(f"unsupported glyph override key {key!r}")


def _normalize_relative_path(relative_path: str) -> str:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise PdfTextExtractionError("relative_path must be a non-empty string")
    value = relative_path.replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise PdfTextExtractionError("relative_path must be a safe relative path")
    if ":" in path.parts[0]:
        raise PdfTextExtractionError("relative_path must not contain a drive prefix")
    return path.as_posix()


def _normalize_font_name(name: str) -> str:
    return _SUBSET_PATTERN.sub("", name.lstrip("/"))


def _font_name_is_bold(name: str) -> bool:
    lower = name.lower()
    return bool(
        "bold" in lower
        or re.search(r"(?:ttb|grtbi|hlbl)(?:$|[^a-z])", lower)
        or lower.endswith(("ttb", "ttbi", "grtbi", "hlbl"))
    )


def _font_name_is_italic(name: str) -> bool:
    lower = name.lower()
    return bool(
        "italic" in lower
        or "oblique" in lower
        or lower.endswith(("tti", "ttbi", "grti", "grtbi"))
    )


def _contains_unknown_or_control(value: str) -> bool:
    if not value or _REPLACEMENT in value:
        return True
    return any(unicodedata.category(char) == "Cc" for char in value)


def _replace_unknown_or_control(value: str) -> str:
    if not value:
        return _REPLACEMENT
    return "".join(
        _REPLACEMENT
        if char == _REPLACEMENT or unicodedata.category(char) == "Cc"
        else char
        for char in value
    )


def _escape_markdown(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("*", "\\*")
        .replace("_", "\\_")
    )


def _source_locator(
    page_number: int, bbox: tuple[float, float, float, float]
) -> str:
    coordinates = ",".join(f"{value:.3f}" for value in bbox)
    return f"page={page_number};bbox={coordinates}"


def _char_bbox(char: Mapping[str, Any]) -> list[float]:
    return [
        _clean_number(float(char["x0"])),
        _clean_number(float(char["top"])),
        _clean_number(float(char["x1"])),
        _clean_number(float(char["bottom"])),
    ]


def _context_excerpt(value: str, limit: int = 300) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _object_marker(reference: Any, value: Any) -> tuple[int, int] | int:
    if hasattr(reference, "idnum") and hasattr(reference, "generation"):
        return (int(reference.idnum), int(reference.generation))
    return id(value)


def _clean_number(value: float) -> float:
    if not math.isfinite(value):
        raise PdfTextExtractionError("PDF geometry contains a non-finite value")
    rounded = round(value, 6)
    return 0.0 if rounded == 0 else rounded


__all__ = [
    "PdfTextDocument",
    "PdfTextExtractionError",
    "PdfTextLine",
    "PdfTextPage",
    "extract_pdf_text",
]
