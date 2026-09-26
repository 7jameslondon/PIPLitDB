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
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

import pdfplumber
from pdfminer.fontmetrics import FONT_METRICS
from pypdf import PdfReader
from pypdf._codecs import adobe_glyphs, charset_encoding


_CID_PATTERN = re.compile(r"^\(cid:(\d+)\)$")
# A dot suffix denotes an alternate glyph outline; the Cnn base still carries
# the character code. Publishers also use the literal suffix `._`.
_CNN_PATTERN = re.compile(r"^C(\d+)(?:\.[A-Za-z0-9_]+)?$", re.IGNORECASE)
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
    descent: float | None = None


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
    use_text_origin: bool = False

    @property
    def baseline(self) -> float:
        return self.baseline_sum / self.baseline_count

    def add(self, char: Mapping[str, Any]) -> None:
        self.chars.append(char)
        size = float(char.get("size", 0.0))
        if size >= self.max_size * 0.85:
            self.baseline_sum += (
                _text_origin(char) if self.use_text_origin else float(char["bottom"])
            )
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
    reading_regions: Sequence[Mapping[str, Any]] | None = None,
    word_gap_points: float | None = None,
) -> PdfTextDocument:
    """Extract deterministic native text and geometry from *path*.

    ``glyph_overrides`` is keyed by a PDF base-font name (with or without its
    six-letter subset prefix), followed by a C-number as an integer, decimal
    string, a string such as ``"C60"``, or an exact decoded Unicode code point
    such as ``"U+F044"``.  ``"*"`` may be used as the outer fallback font
    key.  Overrides are applied before built-in mappings.

    ``reading_regions`` may identify source-reviewed page boxes in reading
    order.  Characters whose centers fall inside a region are assembled into
    lines independently of characters in other regions, preventing adjacent
    columns on a shared baseline from being merged.  Characters outside the
    configured regions remain available after the ordered region lines so the
    semantic layer can explicitly classify or exclude them.

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
    normalized_regions = _normalize_reading_regions(reading_regions)
    if word_gap_points is not None and (isinstance(word_gap_points, bool)
            or not isinstance(word_gap_points, (int, float))
            or not 0 < word_gap_points <= 3):
        raise PdfTextExtractionError('word_gap_points must be a reviewed threshold greater than 0 and at most 3')

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
                # Some PDF generators simulate bold text by painting an
                # identical glyph several times with sub-point offsets.  Those
                # are drawing instructions, not authored repeated characters.
                # Deduplicate only same-text, same-font, same-size glyphs that
                # coincide within half a point so adjacent or deliberately
                # overstruck distinct text remains untouched.
                text_page = page.dedupe_chars(
                    tolerance=0.5,
                    extra_attrs=("fontname", "size"),
                )
                lines = _extract_page_lines(
                    text_page,
                    page_number,
                    normalized_relative,
                    page_fonts[page_number - 1],
                    normalized_overrides,
                    diagnostic_rows,
                    warnings,
                    reading_regions=normalized_regions.get(page_number, ()),
                    word_gap_points=word_gap_points,
                )
                has_chars = bool(text_page.chars)
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
    *,
    reading_regions: Sequence[Mapping[str, Any]] = (),
    word_gap_points: float | None = None,
) -> list[PdfTextLine]:
    # Work from page.chars rather than extract_text_lines(return_chars=True).
    # The latter drops literal space characters; in a subset Cnn encoding a raw
    # byte 32 can map to an ordinary letter (for example, /C83 = S).
    page_chars = list(page.chars)
    ordered_lines: list[PdfTextLine] = []
    assigned: set[int] = set()
    for region in reading_regions:
        box = region["box"]
        if box[2] > float(page.width) + 1e-6 or box[3] > float(page.height) + 1e-6:
            raise PdfTextExtractionError(
                f"reading region {region['region_id']!r} exceeds PDF page {page_number}"
            )
        matching = [
            index
            for index, char in enumerate(page_chars)
            if _char_center_in_box(char, box)
        ]
        overlap = assigned.intersection(matching)
        if overlap:
            raise PdfTextExtractionError(
                f"reading region {region['region_id']!r} overlaps another configured "
                f"reading region on PDF page {page_number}"
            )
        assigned.update(matching)
        region_lines = _make_character_lines(
            [page_chars[index] for index in matching],
            page_number,
            relative_path,
            fonts,
            overrides,
            diagnostic_rows,
            warnings,
            split_column_gaps=False,
            word_gap_points=word_gap_points,
        )
        ordered_lines.extend(
            replace(line, source_region_id=region["region_id"])
            for line in region_lines
        )
        diagnostic_rows.append(
            {
                "schema_version": "1.0",
                "kind": "configured_native_reading_region",
                "status": "reviewed",
                "source_path": relative_path,
                "page": page_number,
                "region_id": region["region_id"],
                "box": list(box),
                "character_count": len(matching),
                "line_count": len(region_lines),
                "source_locator": (
                    f"page={page_number};bbox="
                    + ",".join(f"{value:.2f}" for value in box)
                ),
            }
        )

    unassigned_chars = [
        char for index, char in enumerate(page_chars) if index not in assigned
    ]
    ordered_lines.extend(
        _make_character_lines(
            unassigned_chars,
            page_number,
            relative_path,
            fonts,
            overrides,
            diagnostic_rows,
            warnings,
            word_gap_points=word_gap_points,
        )
    )
    return ordered_lines


def _has_horizontal_baseline(char: Mapping[str, Any]) -> bool:
    """PDFMiner's upright flag also permits diagonal text; inspect its baseline.

    Horizontal shear (the c matrix term) is ordinary synthetic italics and
    must remain horizontal. A nonzero b rotates the actual text baseline.
    """
    if not char.get("upright", True):
        return False
    matrix = char.get("matrix")
    if isinstance(matrix, (tuple, list)) and len(matrix) == 6:
        try:
            return abs(float(matrix[1])) <= 1e-6
        except (TypeError, ValueError):
            pass
    return True


def _make_character_lines(
    chars: Sequence[Mapping[str, Any]],
    page_number: int,
    relative_path: str,
    fonts: Mapping[str, _FontInfo],
    overrides: Mapping[str, Mapping[str, str]],
    diagnostic_rows: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    *,
    split_column_gaps: bool = True,
    word_gap_points: float | None = None,
) -> list[PdfTextLine]:
    character_groups: list[tuple[list[Mapping[str, Any]], bool]] = []
    upright_chars = [
        _with_text_origin(char, fonts)
        for char in chars
        if _has_horizontal_baseline(char)
    ]
    for spatial_line in _group_upright_chars(upright_chars):
        fragments = (
            _split_at_column_gaps(spatial_line)
            if split_column_gaps
            else [spatial_line]
        )
        for fragment in fragments:
            if fragment:
                character_groups.append((fragment, False))
    rotated_chars = [char for char in chars if not _has_horizontal_baseline(char)]
    for fragment in _group_rotated_chars(rotated_chars):
        if fragment:
            character_groups.append((fragment, True))

    lines: list[PdfTextLine] = []
    for line_chars, rotated in character_groups:
        line, rows, line_warnings = _make_line(
            line_chars,
            page_number,
            relative_path,
            fonts,
            overrides,
            rotated=rotated,
            word_gap_points=word_gap_points,
        )
        if line.plain_text:
            lines.append(line)
            diagnostic_rows.extend(rows)
            warnings.extend(line_warnings)
    return _order_page_lines(lines)


def _char_center_in_box(
    char: Mapping[str, Any], box: tuple[float, float, float, float]
) -> bool:
    center_x = (float(char["x0"]) + float(char["x1"])) / 2.0
    center_y = (float(char["top"]) + float(char["bottom"])) / 2.0
    return box[0] <= center_x <= box[2] and box[1] <= center_y <= box[3]


def _normalize_reading_regions(
    value: Sequence[Mapping[str, Any]] | None,
) -> dict[int, list[dict[str, Any]]]:
    if value is None:
        return {}
    if not isinstance(value, (list, tuple)):
        raise PdfTextExtractionError("reading_regions must be a list")
    result: dict[int, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise PdfTextExtractionError(
                f"reading_regions item {index} must be a mapping"
            )
        region_id = str(item.get("region_id", "")).strip()
        page = item.get("page")
        raw_box = item.get("box")
        if not region_id or region_id in seen_ids:
            raise PdfTextExtractionError(
                f"reading_regions item {index} requires a unique region_id"
            )
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise PdfTextExtractionError(
                f"reading_regions item {index} requires a positive page"
            )
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            raise PdfTextExtractionError(
                f"reading_regions item {index} box must contain four coordinates"
            )
        try:
            box = tuple(float(coordinate) for coordinate in raw_box)
        except (TypeError, ValueError) as exc:
            raise PdfTextExtractionError(
                f"reading_regions item {index} box must be numeric"
            ) from exc
        if not all(math.isfinite(coordinate) for coordinate in box) or not (
            0 <= box[0] < box[2] and 0 <= box[1] < box[3]
        ):
            raise PdfTextExtractionError(
                f"reading_regions item {index} box is invalid"
            )
        seen_ids.add(region_id)
        result.setdefault(page, []).append(
            {"region_id": region_id, "box": box}
        )
    return result


def _order_page_lines(lines: Sequence[PdfTextLine]) -> list[PdfTextLine]:
    """Order split native fragments by rendered row, then left to right.

    A reference marker and its citation can be split at a wide column gap.
    Font metrics may put the citation's top a fraction of a point above the
    marker even though both share one visual row; sorting by top alone then
    attaches that citation to the preceding marker.  Substantial bbox overlap
    is reliable same-row evidence, while separate body rows do not overlap.
    """

    rows: list[list[PdfTextLine]] = []
    for line in sorted(lines, key=lambda item: (item.bbox[1], item.bbox[0], item.rotated)):
        best: tuple[float, list[PdfTextLine]] | None = None
        line_height = max(0.0, line.bbox[3] - line.bbox[1])
        for row in rows:
            if row[0].rotated != line.rotated:
                continue
            overlap_ratio = max(
                (
                    max(
                        0.0,
                        min(member.bbox[3], line.bbox[3])
                        - max(member.bbox[1], line.bbox[1]),
                    )
                    / max(
                        1e-9,
                        min(
                            member.bbox[3] - member.bbox[1],
                            line_height,
                        ),
                    )
                )
                for member in row
            )
            if overlap_ratio >= 0.70 and (
                best is None or overlap_ratio > best[0]
            ):
                best = (overlap_ratio, row)
        if best is None:
            rows.append([line])
        else:
            best[1].append(line)

    rows.sort(
        key=lambda row: (
            min(member.bbox[1] for member in row),
            min(member.bbox[0] for member in row),
            row[0].rotated,
        )
    )
    return [
        line
        for row in rows
        for line in sorted(row, key=lambda member: (member.bbox[0], member.bbox[1]))
    ]


def _make_line(
    chars: Sequence[Mapping[str, Any]],
    page_number: int,
    relative_path: str,
    fonts: Mapping[str, _FontInfo],
    overrides: Mapping[str, Mapping[str, str]],
    *,
    rotated: bool,
    word_gap_points: float | None = None,
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
    word_gap = word_gap_points if word_gap_points is not None else max(0.8, median_size * 0.18)

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

    _canonicalize_scientific_multiplication_dots(tokens)
    _canonicalize_degree_celsius(tokens)
    _canonicalize_script_token_order(tokens)
    plain_text = "".join(token.text for token in tokens)
    markdown = _render_markdown(tokens)
    visible_chars = [
        char
        for char, item in zip(ordered, decoded)
        if not item.text.isspace()
    ]
    geometry_chars = visible_chars or ordered
    bbox = (
        _clean_number(min(float(char["x0"]) for char in geometry_chars)),
        _clean_number(min(float(char["top"]) for char in geometry_chars)),
        _clean_number(max(float(char["x1"]) for char in geometry_chars)),
        _clean_number(max(float(char["bottom"]) for char in geometry_chars)),
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
    # pdfplumber can emit a multi-character token containing only layout
    # whitespace (for example ``"\t\r "``).  A single raw space is different:
    # in a custom Type1 font it can be code 32 mapped through Differences to an
    # authored visible glyph, so it must continue through semantic decoding.
    layout_whitespace = bool(
        original and len(original) > 1 and original.isspace()
    )
    if layout_whitespace:
        # pdfminer can expose a single positioned text object containing
        # several layout-only whitespace controls (for example ``"\t\r "``).
        # It is still ordinary whitespace, not an unresolved scientific glyph.
        decoded = " "
    elif cid_match:
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

    if layout_whitespace:
        pass
    elif encoded_code is not None:
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
    elif (
        len(original) == 1
        and ("symbolssk" in normalized_font.casefold()
             or normalized_font.casefold() in {"advps_grti", "advps_grtbi", "advps_grtu"})
        and (symbolssk_greek := _GREEK_FROM_LATIN.get(original)) is not None
    ):
        # SymbolSSK is a Type 1 Symbol-family font whose extractable Latin
        # character codes select Greek outlines.  Unlike the Cnn subset fonts
        # handled above, these PDFs often expose the raw Latin token without a
        # /Differences entry. AdvPS Greek fonts also expose literal Latin
        # tokens in some publisher subsets; apply their established Cnn Greek
        # mapping to those literal tokens as well.
        decoded = symbolssk_greek
        diagnostic = {
            "kind": "glyph_decoding",
            "status": "resolved",
            "method": ("symbolssk_greek_transliteration" if "symbolssk" in normalized_font.casefold()
                       else "advps_greek_literal_transliteration"),
            "page": page_number,
            "font": font.name,
            "font_full_name": font.full_name,
            "encoded_code": None,
            "unicode_codepoint": f"U+{ord(original):04X}",
            "glyph_name": None,
            "semantic_code": ord(original),
            "replacement": decoded,
            "bbox": _char_bbox(char),
        }
    elif len(original) == 1 and (
        literal_override := _lookup_override(
            overrides,
            font,
            encoded_code=None,
            glyph_name=None,
            semantic_code=None,
            literal_codepoint=ord(original),
        )
    ) is not None:
        decoded = literal_override
        diagnostic = {
            "kind": "glyph_decoding",
            "status": "resolved",
            "method": "unicode_glyph_override",
            "page": page_number,
            "font": font.name,
            "font_full_name": font.full_name,
            "encoded_code": None,
            "unicode_codepoint": f"U+{ord(original):04X}",
            "glyph_name": None,
            "semantic_code": None,
            "replacement": decoded,
            "bbox": _char_bbox(char),
        }
    elif normalized_font.casefold() == "advpi1" and original in {"8", "'"}:
        decoded = {"8": "°", "'": "′"}[original]
        diagnostic = {
            "kind": "glyph_decoding", "status": "resolved",
            "method": "advpi1_literal_builtin", "page": page_number,
            "font": font.name, "font_full_name": font.full_name,
            "unicode_codepoint": f"U+{ord(original):04X}",
            "replacement": decoded, "bbox": _char_bbox(char),
        }
    elif normalized_font.casefold() == "universal-chemicalpi" and original == "3":
        decoded = "→"
        diagnostic = {
            "kind": "glyph_decoding", "status": "resolved",
            "method": "universal_chemicalpi_arrow", "page": page_number,
            "font": font.name, "font_full_name": font.full_name,
            "unicode_codepoint": "U+0033", "replacement": decoded,
            "bbox": _char_bbox(char),
        }
    elif len(original) == 1 and (
        small_cap := _literal_adobe_small_cap(original, font)
    ) is not None:
        glyph_name, decoded = small_cap
        diagnostic = {
            "kind": "glyph_decoding",
            "status": "resolved",
            "method": "adobe_small_cap",
            "page": page_number,
            "font": font.name,
            "font_full_name": font.full_name,
            "encoded_code": None,
            "unicode_codepoint": f"U+{ord(original):04X}",
            "glyph_name": glyph_name,
            "semantic_code": None,
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


# Only these exact MathematicalPi-One aliases are established. Their outlines
# are respectively a cross, horizontal stroke, prime and raised ring; unknown
# H-number aliases and identically named glyphs in other fonts remain unresolved.
_MATHEMATICAL_PI_ONE = {
    "H11001": "+",
    "H11002": "−",
    "H11032": "′",
    "H11034": "°",
}

# Exact legacy font/glyph pairs verified against rendered publisher text.
# H-number aliases are font-specific; unfamiliar aliases remain unresolved.
_LEGACY_PUBLISHER_PI = {
    "universal-greekwithmathpi": {
        "H11001": "+", "H11002": "−", "H11005": "=",
        "H11011": "∼", "H11021": "<", "H11022": ">",
        "H11032": "′", "H9251": "α", "H9252": "β",
        "H9253": "γ", "H9262": "μ",
    },
    "universal-newswithcommpi": {"H18528": "·"},
    "mathematicalpi-six": {"H11569": "*"},
}


def _adobe_small_cap(glyph_name: str) -> str | None:
    """Resolve an AGL small-cap name to its encoded capital, not a PUA glyph."""
    if not glyph_name.endswith("small"):
        return None
    private = adobe_glyphs.get("/" + glyph_name)
    capital = adobe_glyphs.get("/" + glyph_name[:-5])
    if (
        private and len(private) == 1 and unicodedata.category(private) == "Co"
        and capital and len(capital) == 1 and unicodedata.category(capital) == "Lu"
    ):
        return capital
    return None


def _literal_adobe_small_cap(value: str, font: _FontInfo) -> tuple[str, str] | None:
    # pdfminer may already have converted /Dsmall to U+F764. Require this
    # font's explicit Differences entry; a coincident private-use value alone
    # is not enough evidence to reinterpret another font's glyph.
    if unicodedata.category(value) != "Co":
        return None
    matches = {
        (name, capital)
        for name in font.differences.values()
        if adobe_glyphs.get("/" + name) == value
        and (capital := _adobe_small_cap(name)) is not None
    }
    return next(iter(matches)) if len(matches) == 1 else None


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
    if lower_name == "mathematicalpi-one" and glyph_name in _MATHEMATICAL_PI_ONE:
        return _MATHEMATICAL_PI_ONE[glyph_name], "resolved", "mathematicalpi_one_alias", None
    legacy_alias = _LEGACY_PUBLISHER_PI.get(lower_name, {}).get(glyph_name)
    if legacy_alias is not None:
        return legacy_alias, "resolved", "legacy_publisher_pi_alias", None
    if glyph_name and (small_cap := _adobe_small_cap(glyph_name)) is not None:
        return small_cap, "resolved", "adobe_small_cap", None
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
    baseline_top = statistics.median(
        float(char.source["top"]) for char in baseline_chars
    )
    baseline_bottom = statistics.median(
        float(char.source["bottom"]) for char in baseline_chars
    )
    baseline_center = statistics.median(
        (
            float(char.source["top"])
            + float(char.source["bottom"])
        )
        / 2.0
        for char in baseline_chars
    )
    origins = [_text_origin(char.source) for char in visible]
    origin_baseline = (
        statistics.median(_text_origin(char.source) for char in baseline_chars)
        if all(value is not None for value in origins) else None
    )
    for char in visible:
        size = float(char.source.get("size", 0.0))
        if size <= 0 or size > dominant_size * 0.82:
            continue
        if origin_baseline is not None:
            displacement = _text_origin(char.source) - origin_baseline
            if displacement < -dominant_size * 0.16:
                char.script = "sup"
            elif (
                displacement > dominant_size * 0.13
                or math.isclose(
                    displacement,
                    dominant_size * 0.13,
                    rel_tol=1e-6,
                    abs_tol=1e-6,
                )
            ) or (
                # Compact numeric subscripts can be lowered only 12% of the
                # body size. Require both a distinctly smaller digit and a
                # real origin shift; same-baseline small text stays ordinary.
                char.text.isdecimal()
                and size <= dominant_size * 0.70
                and displacement > dominant_size * 0.10
            ) or (
                # Word's compact scripts may lower the origin by only 8%
                # while reducing the glyph to two-thirds size. Require the
                # glyph box to share the body bottom as independent evidence;
                # a merely small or slightly shifted baseline is insufficient.
                char.text.isalnum()
                and size <= dominant_size * 0.70
                and displacement > dominant_size * 0.06
                and abs(float(char.source["bottom"]) - baseline_bottom)
                    <= dominant_size * 0.03
                and float(char.source["top"]) - baseline_top
                    > dominant_size * 0.20
            ):
                char.script = "sub"
            continue
        # Type 1 font boxes are not vertically symmetric.  A genuine lowered
        # molecular-formula digit can share almost the same ``bottom`` as the
        # baseline font even though its whole glyph is visibly displaced.
        # Comparing glyph centers detects both that case and raised citations
        # without treating merely smaller, vertically centered text as script.
        top_shift = float(char.source["top"]) - baseline_top
        bottom_shift = float(char.source["bottom"]) - baseline_bottom
        center_shift = (
            float(char.source["top"])
            + float(char.source["bottom"])
        ) / 2.0 - baseline_center
        if (
            center_shift < -dominant_size * 0.16
            or bottom_shift < -dominant_size * 0.16
        ):
            char.script = "sup"
        elif (
            center_shift > dominant_size * 0.13
            and top_shift > dominant_size * 0.16
            # Arial's compact formula digits can share or very slightly
            # exceed the surrounding glyph bottom.  Their lowered top and
            # center still distinguish them from merely small centered text.
            and bottom_shift > -dominant_size * 0.05
        ):
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

    # A superscript painted between letters of one word subscript (for
    # example I_t^0_ot) is spatially parallel to that subscript. Keep the
    # word together before the superscript in linear machine reading order.
    cursor = 0
    while cursor < len(chars):
        if chars[cursor].script not in {"sub", "sup"}:
            cursor += 1
            continue
        end = cursor + 1
        while end < len(chars) and chars[end].script in {"sub", "sup"}:
            end += 1
        group = chars[cursor:end]
        sub = [char for char in group if char.script == "sub"]
        sup = [char for char in group if char.script == "sup"]
        if (sub and sup and group[0].script == group[-1].script == "sub"
                and all(char.text.isalpha() for char in sub)
                and all(char.text.isdigit() for char in sup)):
            chars[cursor:end] = [*sub, *sup]
        cursor = end

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


def _canonicalize_scientific_multiplication_dots(
    chars: list[_DecodedChar],
) -> None:
    """Restore a raised period before angstrom units as a multiplication dot.

    Some legacy scientific PDFs draw the inter-unit multiplication mark with
    a small raised period. Geometry correctly detects that glyph as raised,
    but ``<sup>.</sup>Å`` is not the authored mathematical meaning. The exact
    period-before-angstrom context distinguishes it from sentence punctuation.
    """

    index = 0
    while index < len(chars):
        char = chars[index]
        if char.text != "." or char.script != "sup":
            index += 1
            continue
        following = index + 1
        while following < len(chars) and chars[following].text.isspace():
            following += 1
        if following >= len(chars) or chars[following].text != "Å":
            index += 1
            continue
        char.text = "·"
        char.script = None
        del chars[index + 1 : following]
        index += 1


def _canonicalize_degree_celsius(chars: list[_DecodedChar]) -> None:
    """Restore a superscript lowercase ``o`` before C as a degree symbol.

    Older scientific PDFs often draw the Celsius degree with a raised letter
    ``o`` rather than an encoded U+00B0 glyph. Geometry correctly classifies
    that source glyph as superscript, but preserving it as ``^{o}C`` would
    misstate the unit. The narrow adjacent ``superscript o`` + ``C`` context
    distinguishes this convention from an ordinary letter.
    """

    for index, char in enumerate(chars[:-1]):
        if (
            char.text == "o"
            and char.script == "sup"
            and chars[index + 1].text == "C"
            and not chars[index + 1].text.isspace()
        ):
            char.text = "°"
            char.script = None


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
            # The limited rich-text expander intentionally handles only
            # non-nested Markdown runs. Triple-asterisk Markdown is ambiguous
            # to that grammar and can expand into mismatched tags, so emit the
            # same semantics with the already-supported explicit HTML subset.
            escaped = f"<strong><em>{escaped}</em></strong>"
        elif bold:
            escaped = f"**{escaped}**"
        elif italic:
            escaped = f"*{escaped}*"
        if (rendered and rendered[-1].endswith("*")
                and escaped.startswith("*") and script is None):
            # Adjacent italic and bold runs otherwise form an ambiguous ***
            # boundary, even though neither run itself uses nested styling.
            # Keep the new run explicit so the limited Markdown expander
            # cannot turn the boundary into crossing emphasis tags.
            tag = "strong" if bold else "em"
            escaped = f"<{tag}>{_escape_markdown(text)}</{tag}>"
        if script is not None:
            escaped = f"<{script}>{escaped}</{script}>"
        rendered.append(escaped)
    return "".join(rendered).strip()


def _text_origin(char: Mapping[str, Any]) -> float | None:
    """Return the upright baseline in a consistent downward-positive frame."""
    if "_text_origin" in char:
        return float(char["_text_origin"])
    matrix = char.get("matrix")
    if not isinstance(matrix, (tuple, list)) or len(matrix) != 6:
        return None
    try:
        a, b, c, d, _x, y = (float(value) for value in matrix)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (a, b, c, d, y)):
        return None
    # Horizontal shear is used for synthetic italics and does not move the
    # baseline. A nonzero b rotates the text axis and needs the old fallback.
    if a <= 0 or d <= 0 or abs(b) > 1e-6:
        return None
    return -y


def _with_text_origin(
    char: Mapping[str, Any], fonts: Mapping[str, _FontInfo]
) -> Mapping[str, Any]:
    origin = _text_origin(char)
    if origin is None:
        return char
    name = str(char.get("fontname", ""))
    font = fonts.get(name) or fonts.get(_normalize_font_name(name))
    if font is not None and font.descent is not None and "y0" in char:
        # LTChar's matrix origin excludes an explicit PDF text rise (Ts).
        # Its y0 includes that rise and the font descent. Removing the descent
        # recovers the actual baseline and makes mixed-font symbols comparable.
        origin = -float(char["y0"]) + font.descent * float(char["size"]) / 1000.0
    return {**char, "_text_origin": origin}


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
    use_text_origin = all(_text_origin(char) is not None for char in ordered)
    for char in ordered:
        size = max(float(char.get("size", 0.0)), 1.0)
        bottom = _text_origin(char) if use_text_origin else float(char["bottom"])
        best: tuple[float, _SpatialLineCluster] | None = None
        for cluster in clusters:
            distance = abs(bottom - cluster.baseline)
            # Legacy typesetting can raise a 7-point reference or isotope by
            # just over half of the surrounding 12-point line height.  Keep
            # that small glyph on the dominant baseline, while leaving a
            # substantial margin below ordinary adjacent-line leading.
            tolerance = max(2.0, max(size, cluster.max_size) * 0.7)
            if distance <= tolerance and (best is None or distance < best[0]):
                best = (distance, cluster)
        if best is None:
            clusters.append(
                _SpatialLineCluster(
                    chars=[char],
                    max_size=size,
                    baseline_sum=bottom,
                    baseline_count=1,
                    use_text_origin=use_text_origin,
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
            descriptor = font_dict.get("/FontDescriptor")
            descriptor = descriptor.get_object() if descriptor is not None else {}
            descent = descriptor.get("/Descent") if isinstance(descriptor, Mapping) else None
            # Match pdfminer's built-in metrics for standard simple fonts,
            # which commonly omit FontDescriptor (including explicit Ts text).
            if font_dict.get("/Subtype") in {"/Type1", "/TrueType"}:
                if full_name in FONT_METRICS:
                    descent = FONT_METRICS[full_name][0].get("Descent", 0)
                elif descent is None:
                    descent = 0
            if descent is not None:
                try:
                    descent = -abs(float(descent))
                    if not math.isfinite(descent):
                        descent = None
                except (TypeError, ValueError):
                    descent = None
            info = _FontInfo(
                full_name=full_name,
                name=name,
                differences=differences,
                cnn_encoded=bool(differences)
                and all(_CNN_PATTERN.fullmatch(value) for value in differences.values()),
                bold=_font_name_is_bold(name),
                italic=_font_name_is_italic(name),
                descent=descent,
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
        raw_font = font_name.strip().lstrip("/")
        if raw_font == "*":
            normalized_font = "*"
        elif re.fullmatch(r"[A-Z]{6}\+.+", raw_font):
            # A fully qualified subset selector is intentionally kept exact.
            # Different subsets with the same base font can carry different
            # byte mappings within one PDF; callers may still use the
            # prefix-free base name as a broad fallback.
            normalized_font = raw_font
        else:
            normalized_font = _normalize_font_name(raw_font)
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
    encoded_code: int | None,
    glyph_name: str | None,
    semantic_code: int | None,
    literal_codepoint: int | None = None,
) -> str | None:
    selectors = (font.full_name.lower(), font.name.lower(), "*")
    keys: list[str] = []
    if glyph_name:
        keys.append(glyph_name.upper())
    if semantic_code is not None:
        keys.append(f"C{semantic_code}")
    if encoded_code is not None:
        keys.append(f"RAW:{encoded_code}")
    if literal_codepoint is not None:
        keys.append(f"U+{literal_codepoint:04X}")
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
    unicode_match = re.fullmatch(r"U\+([0-9A-F]{4,6})", value, re.IGNORECASE)
    if unicode_match:
        codepoint = int(unicode_match.group(1), 16)
        if codepoint <= 0x10FFFF:
            return f"U+{codepoint:04X}"
        raise PdfTextExtractionError(
            f"Unicode glyph override code point is out of range: {key!r}"
        )
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
        or re.fullmatch(r"advot[0-9a-f]+\.(?:b|bi)(?:\+[0-9a-f]+)?", lower)
        or re.search(r"(?:ttb|grtbi|hlbl)(?:$|[^a-z])", lower)
        or lower.endswith(("ttb", "ttbi", "grtbi", "hlbl"))
    )


def _font_name_is_italic(name: str) -> bool:
    lower = name.lower()
    return bool(
        "italic" in lower
        or re.fullmatch(r"advot[0-9a-f]+\.(?:i|bi)(?:\+[0-9a-f]+)?", lower)
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
