"""Preserve and extract supplementary sources without modifying originals.

The public entry point, :func:`extract_supplements`, accepts the ``SourceFile``
objects returned by discovery and an existing extraction root. Every supplement
is copied byte-for-byte to a deterministic ``supplement_NNN`` directory before
any format-specific extraction is attempted. PDF extraction uses only the
document's native text layer; this module never invokes OCR.
"""

from __future__ import annotations

import hashlib
import html as html_stdlib
import io
import math
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from .caption_patterns import (
    CAPTION_PATTERN,
    normalize_caption_kind,
    normalize_caption_number,
)
from .csv_supplement import extract_csv_supplement, is_csv_supplement
from .cif_supplement import extract_cif_supplement, is_cif_supplement
from .docx_supplement import (
    DOCX_FIGURE_RENDER_DPI,
    DOCX_MEDIA_TYPE,
    LEGACY_DOC_MEDIA_TYPE,
    DocxFigureCropRequest,
    DocxFigureRenderer,
    DocxFigureRenderError,
    convert_legacy_doc_to_docx,
    extract_docx_supplement,
    render_docx_figure_crops,
)
from .html_extractor import plain_text as _html_plain_text
from .html_extractor import render_inline as _html_render_inline
from .models import (
    ContentBlock,
    FigureItem,
    SourceFile,
    SupplementExtraction,
    TableCell,
    TableItem,
    TablePart,
)
from .paths import (
    atomic_write_bytes,
    ensure_within,
    is_reparse_point,
    reject_reparse_chain,
    sha256_file,
)
from .pdf_text_extractor import PdfTextDocument, extract_pdf_text
from .pptx_supplement import (
    SlideRenderer,
    extract_pptx_supplement,
    render_powerpoint_slides,
)
from .postscript_supplement import (
    POSTSCRIPT_MEDIA_TYPE,
    POSTSCRIPT_RENDER_DPI,
    PostscriptRenderer,
    render_postscript_png,
)
from .xml_supplement import extract_xml_fields
from .xlsx_supplement import XLSX_MEDIA_TYPE, extract_xlsx_supplement
from .xls_supplement import is_xls_supplement, extract_xls_supplement

try:
    from lxml import html as lxml_html
except ModuleNotFoundError:  # pragma: no cover - exercised by CLI dependency check
    lxml_html = None


FOOTNOTE_PATTERN = re.compile(r"^\[[A-Za-z0-9]+\](?:\s|(?=[A-Z])|$)")
SPACE_PATTERN = re.compile(r"\s+")
XML_MEDIA_TYPES = frozenset({"application/xml", "text/xml"})
PPTX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "presentationml.presentation"
)


@dataclass(frozen=True)
class _PdfLine:
    text: str
    markdown: str
    line_number: int
    x0: float
    x1: float
    top: float
    bottom: float
    overlaps_figure_graphic: bool = False


@dataclass
class _PdfParagraph:
    lines: list[_PdfLine]

    @property
    def text(self) -> str:
        return _normalize_space(_join_pdf_line_values(self.lines, "text"))

    @property
    def markdown(self) -> str:
        return _normalize_space(_join_pdf_line_values(self.lines, "markdown"))

    @property
    def first_line(self) -> int:
        return self.lines[0].line_number

    @property
    def last_line(self) -> int:
        return self.lines[-1].line_number


def _normalize_space(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    # PDF text layers can expose typographic ligature code points even though
    # the authored scientific word consists of ordinary letters. Expand only
    # the standard Unicode Latin ligatures so search and copy retain the word.
    normalized = normalized.translate(
        str.maketrans(
            {
                "ﬀ": "ff",
                "ﬁ": "fi",
                "ﬂ": "fl",
                "ﬃ": "ffi",
                "ﬄ": "ffl",
                "ﬅ": "st",
                "ﬆ": "st",
            }
        )
    )
    # Legacy supporting PDFs commonly encode scientific prime marks as right
    # single quotes or acute accents and a degree sign as a masculine ordinal.
    # A quote-like mark immediately after a digit has unambiguous prime
    # semantics in these scientific labels, including when rich tags close
    # before the following visible character.
    normalized = re.sub(
        r"(?<=\d)[‘’´ʹ](?=(?:</(?:sup|strong|em)>)*(?:[A-Z\-·\s,;:.)]|$))",
        "′",
        normalized,
    )
    normalized = re.sub(r"(?<=\d)\s+ºC\b", " °C", normalized)
    normalized = SPACE_PATTERN.sub(" ", normalized).strip()
    # Positioned PDF text may expose a final punctuation glyph as a separate
    # fragment.  Joining those fragments must not fabricate a space before the
    # authored punctuation.
    normalized = re.sub(r"\s+([,;:!?])", r"\1", normalized)
    return re.sub(r"\s+\.(?!\d)", ".", normalized)


# Conventional Latin character slots in the Adobe Symbol font. Apply only to
# an explicitly named Symbol font in the authenticated legacy XHTML fragment;
# already-Unicode symbols (including an unresolved U+FFFD) remain unchanged.
_HIGHWIRE_SYMBOL_GLYPHS = str.maketrans(dict(zip(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
    "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖαβχδεφγηιϕκλµνοπθρστυϖωξψζ",
    strict=True,
)))


def _highwire_standalone_html_blocks(
    source: SourceFile,
    supplement_id: str,
    source_path: Path,
) -> list[ContentBlock] | None:
    """Recover explicit HighWire XHTML prose/caption fragments, not web pages."""

    if lxml_html is None:
        return None
    try:
        document = lxml_html.fromstring(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if (
        document.tag != "div"
        or document.get("xmlns") != "http://www.w3.org/1999/xhtml"
        or document.get("xmlns:hw") != "org.highwire.hpp"
        or set(document.attrib) != {"xmlns", "xmlns:hw"}
        or (document.text or "").strip()
        or (document.tail or "").strip()
    ):
        return None
    allowed_inline = {"b", "strong", "i", "em", "sub", "sup", "font", "a", "br"}
    if any((node.tail or "").strip() for node in document):
        return None
    children = [node for node in document if isinstance(node.tag, str)]
    if not children or not any(node.tag == "p" for node in children):
        return None
    for node in children:
        if node.tag not in {"p", "a", "br"} or (node.tail or "").strip():
            return None
        if node.tag == "br" and (len(node) or (node.text or "").strip()):
            return None
        if any(
            child.tag not in allowed_inline
            for child in node.iterdescendants()
            if isinstance(child.tag, str)
        ):
            return None
        # Hidden or styled application markup is not this archived fragment
        # dialect; fail closed instead of silently dropping authored content.
        if any(
            set(child.attrib) - ({"face"} if child.tag == "font" else
                                 {"href"} if child.tag == "a" else set())
            for child in node.iter()
            if isinstance(child.tag, str)
        ):
            return None

    def decode_font(node: Any, inherited_symbol: bool = False) -> None:
        symbol = inherited_symbol
        if node.tag == "font":
            symbol = (node.get("face") or "").strip().casefold() == "symbol"
        if symbol and node.text:
            node.text = node.text.translate(_HIGHWIRE_SYMBOL_GLYPHS)
        for child in node:
            if isinstance(child.tag, str):
                decode_font(child, symbol)
            if symbol and child.tail:
                child.tail = child.tail.translate(_HIGHWIRE_SYMBOL_GLYPHS)

    decode_font(document)
    blocks: list[ContentBlock] = []
    for node in children:
        if node.tag == "br":
            continue
        value = _html_plain_text(node)
        if not value:
            continue
        inline = [child for child in node if isinstance(child.tag, str)]
        heading = (
            node.tag == "p" and len(inline) == 1
            and inline[0].tag in {"b", "strong"}
            and not (node.text or "").strip()
            and not (inline[0].tail or "").strip()
        )
        caption = CAPTION_PATTERN.match(value)
        kind = (
            ("figure_caption" if normalize_caption_kind(caption.group("kind")).startswith("fig")
             else "table_caption" if normalize_caption_kind(caption.group("kind")).startswith("table")
             else "scheme_caption")
            if caption is not None
            else "subsection_heading" if heading else "text"
        )
        blocks.append(ContentBlock(
            block_id=f"{supplement_id}-html-block-{len(blocks) + 1:03d}",
            kind=kind,
            markdown=_html_render_inline(node),
            plain_text=value,
            source_path=source.relative_path,
            source_locator=node.getroottree().getpath(node),
        ))
    return blocks or None


def _legacy_standalone_html_table(
    source: SourceFile,
    supplement_id: str,
    source_path: Path,
) -> TableItem | None:
    """Recover one exact legacy publisher table-only HTML supplement.

    Older journal archives distributed large supporting tables as standalone
    HTML pages rather than spreadsheets.  Authenticate the narrow topology of
    one nonempty body wrapper containing exactly a numbered title paragraph,
    one table, and one authored note paragraph.  Other HTML remains preserved
    with the ordinary unsupported-format warning instead of being flattened.
    """

    if lxml_html is None:
        return None
    try:
        document = lxml_html.fromstring(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    bodies = document.xpath("//body")
    if len(bodies) != 1:
        return None
    body_children = [
        child
        for child in bodies[0]
        if isinstance(child.tag, str) and _html_plain_text(child)
    ]
    if len(body_children) != 1 or body_children[0].tag.rsplit("}", 1)[-1].lower() != "div":
        return None
    wrapper = body_children[0]
    meaningful = [
        child
        for child in wrapper
        if isinstance(child.tag, str) and _html_plain_text(child)
    ]
    if len(meaningful) != 3:
        return None
    title_node, table_node, note_node = meaningful
    if (
        title_node.tag.rsplit("}", 1)[-1].lower() != "p"
        or table_node.tag.rsplit("}", 1)[-1].lower() != "table"
        or note_node.tag.rsplit("}", 1)[-1].lower() != "p"
        or document.xpath("//img | //audio | //video | //iframe")
        or len(document.xpath("//table")) != 1
    ):
        return None
    title_plain = _normalize_space(_html_plain_text(title_node))
    title_match = re.fullmatch(
        r"Table\s+(?P<number>\d+[A-Za-z]?)\.\s+.+", title_plain, re.IGNORECASE
    )
    if title_match is None:
        return None
    row_nodes = table_node.xpath("./thead/tr | ./tbody/tr | ./tfoot/tr | ./tr")
    if len(row_nodes) < 2:
        return None
    rows: list[list[TableCell]] = []
    expected_columns: int | None = None
    for row_index, row_node in enumerate(row_nodes):
        cell_nodes = row_node.xpath("./th | ./td")
        if not cell_nodes:
            return None
        row: list[TableCell] = []
        column_count = 0
        for cell_node in cell_nodes:
            try:
                rowspan = int(cell_node.get("rowspan") or 1)
                colspan = int(cell_node.get("colspan") or 1)
            except ValueError:
                return None
            if rowspan < 1 or colspan < 1:
                return None
            cell_plain = _normalize_space(_html_plain_text(cell_node))
            cell_markup = _html_render_inline(cell_node)
            row.append(
                TableCell(
                    text=cell_plain,
                    markdown=cell_markup,
                    header=(
                        row_index == 0
                        or cell_node.tag.rsplit("}", 1)[-1].lower() == "th"
                    ),
                    rowspan=rowspan,
                    colspan=colspan,
                )
            )
            column_count += colspan
        if expected_columns is None:
            expected_columns = column_count
        elif column_count != expected_columns:
            return None
        rows.append(row)
    number = title_match.group("number")
    return TableItem(
        table_id=f"{supplement_id}_table_{number.casefold()}",
        source_id=f"table-{number}",
        label=f"Table {number}",
        title_markdown=_html_render_inline(title_node),
        title_plain=title_plain,
        parts=[
            TablePart(
                part_id=f"{supplement_id}_table_{number.casefold()}_part_001",
                rows=rows,
            )
        ],
        footnotes_markdown=[_html_render_inline(note_node)],
        footnotes_plain=[_normalize_space(_html_plain_text(note_node))],
        source_path=source.relative_path,
        source_locator=table_node.getroottree().getpath(table_node),
        source_kind="html",
    )


def _escape_literal_pdf_markup(value: str) -> str:
    """Escape unstyled pdfplumber text before rich-text conversion.

    The fallback PDF path has no reliable font-style information, so every
    Markdown/HTML control character it returns is authored literal text.  This
    keeps corresponding-author stars and scientific comparison operators from
    being reinterpreted as emphasis or tags.
    """

    return (
        value.replace("\\", "\\\\")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        # Native PDF text is not Markdown.  Escape the complete link-control
        # sequence so bracketed chemical complexes followed by charge terms
        # such as ``[PtCl(H2O)](2-n)-`` cannot become fabricated hyperlinks.
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("*", "\\*")
        .replace("_", "\\_")
    )


def _join_pdf_line_values(lines: Iterable[_PdfLine], attribute: str) -> str:
    """Join visual PDF lines without inserting spaces at hyphen continuations."""

    result = ""
    plain_result = ""
    for line in lines:
        fragment = str(getattr(line, attribute, "")).strip()
        plain_fragment = line.text.strip()
        if not fragment:
            continue
        if not result:
            result = fragment
            plain_result = plain_fragment
        elif plain_result.endswith(("-", "‐", "‑")) or (
            re.search(r"[ACGT]{12,}$", plain_result) is not None
            and (
                re.match(r"[ACGT]{6,}", plain_fragment) is not None
                or re.match(
                    r"[ACGT]{4,}-[35][’´′](?:\b|[,;:.)])", plain_fragment
                )
                is not None
            )
        ):
            # Decoded PDF lines can each carry a complete Markdown emphasis
            # wrapper even when the underlying word is hyphenated across the
            # visual line break.  Joining ``*...-*`` and ``*...*`` verbatim
            # creates an accidental ``**`` boundary and leaves malformed rich
            # text after conversion.  Continue the single italic span across
            # that source-imposed line break; preserve bold and mixed markup.
            if (
                attribute == "markdown"
                and result.endswith("*")
                and not result.endswith("**")
                and fragment.startswith("*")
                and not fragment.startswith("**")
            ):
                result = result[:-1] + fragment[1:]
            else:
                result += fragment
            plain_result += plain_fragment
        else:
            result += " " + fragment
            plain_result += " " + plain_fragment
    return result


def _natural_path_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """Sort numbered supplement filenames in human/publisher order."""

    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
        if part
    )


def _presentation_caption_semantics(
    blocks: list[ContentBlock],
    assets: list[dict[str, Any]],
    source: SourceFile,
    supplement_id: str,
) -> tuple[list[ContentBlock], list[FigureItem]]:
    """Merge captions and promote matching complete-slide renders as figures."""

    groups: dict[str, list[ContentBlock]] = {}
    for block in blocks:
        match = re.match(r"^(.*);paragraph=\d+$", block.source_locator)
        if match:
            groups.setdefault(match.group(1), []).append(block)

    caption_groups = {
        prefix: values
        for prefix, values in groups.items()
        if any(block.kind == "figure_caption" for block in values)
    }
    merged: list[ContentBlock] = []
    consumed: set[str] = set()
    for block in blocks:
        match = re.match(r"^(.*);paragraph=\d+$", block.source_locator)
        prefix = match.group(1) if match else ""
        values = caption_groups.get(prefix)
        if values is None:
            merged.append(block)
            continue
        if prefix in consumed:
            continue
        consumed.add(prefix)
        caption = next(item for item in values if item.kind == "figure_caption")
        merged.append(
            ContentBlock(
                block_id=caption.block_id,
                kind="figure_caption",
                markdown="\n".join(item.markdown for item in values if item.markdown),
                plain_text="\n".join(item.plain_text for item in values if item.plain_text),
                source_path=caption.source_path,
                source_locator=prefix,
                source_geometry=caption.source_geometry,
            )
        )

    captions_by_slide: dict[int, list[ContentBlock]] = {}
    for caption in (block for block in merged if block.kind == "figure_caption"):
        caption_slide = re.search(
            r"(?:^|;)slide=(\d+)(?:;|$)", caption.source_locator
        )
        if caption_slide:
            captions_by_slide.setdefault(int(caption_slide.group(1)), []).append(caption)

    renders_by_slide: dict[int, dict[str, Any]] = {}
    for asset in assets:
        if str(asset.get("category") or "").casefold() != "supplement_slide_render":
            continue
        try:
            slide_number = int(asset.get("presentation_slide_number") or 0)
        except (TypeError, ValueError):
            continue
        if slide_number > 0 and slide_number not in renders_by_slide:
            renders_by_slide[slide_number] = asset

    figures: list[FigureItem] = []
    promoted_slides: dict[int, str] = {}
    consumed_caption_ids: set[str] = set()
    for slide_number in sorted(renders_by_slide):
        slide_captions = captions_by_slide.get(slide_number, [])
        if len(slide_captions) != 1:
            continue
        caption = slide_captions[0]
        match = CAPTION_PATTERN.match(caption.plain_text)
        if match is None or not normalize_caption_kind(match.group("kind")).startswith("fig"):
            continue
        render = renders_by_slide[slide_number]
        figure_id = str(render.get("asset_id") or "").strip()
        output_path = str(render.get("output_path") or "").strip()
        if not figure_id or not output_path:
            continue
        label = f"Figure {normalize_caption_number(match.group('number')).upper()}"
        figures.append(
            FigureItem(
                figure_id=figure_id,
                source_id=caption.source_locator,
                label=label,
                kind="figure",
                caption_markdown=caption.markdown,
                caption_plain=caption.plain_text,
                source_path=source.relative_path,
                source_locator=caption.source_locator,
                output_path=output_path,
            )
        )
        render["category"] = "figure"
        render["label"] = label
        promoted_slides[slide_number] = figure_id
        consumed_caption_ids.add(caption.block_id)

    # Component media used by exactly one promoted slide belong to that complete
    # figure. Shared media and chart workbooks retain their supplement/table
    # parent because a single asset cannot have several semantic parents.
    for asset in assets:
        if (
            str(asset.get("category") or "").casefold() != "supplement_image"
            or asset.get("parent_id") != supplement_id
        ):
            continue
        raw_slides = asset.get("presentation_slide_numbers")
        try:
            media_slides = [int(value) for value in raw_slides]
        except (TypeError, ValueError):
            media_slides = []
        if len(media_slides) == 1 and media_slides[0] in promoted_slides:
            asset["parent_id"] = promoted_slides[media_slides[0]]

    retained: list[ContentBlock] = []
    for block in merged:
        if block.block_id in consumed_caption_ids:
            continue
        slide_match = re.search(r"(?:^|;)slide=(\d+)(?:;|$)", block.source_locator)
        slide_number = int(slide_match.group(1)) if slide_match else None
        if slide_number in promoted_slides and block.kind != "speaker_note":
            # Native text stays in record.json for machine use and keeps its
            # coordinates in extraction diagnostics, but the viewer must not
            # present decontextualized labels as ordinary linear prose.
            retained.append(
                ContentBlock(
                    block_id=block.block_id,
                    kind="figure_text",
                    markdown=block.markdown,
                    plain_text=block.plain_text,
                    source_path=block.source_path,
                    source_locator=block.source_locator,
                    source_geometry=block.source_geometry,
                )
            )
        else:
            retained.append(block)
    return retained, figures


def _consolidate_supplement_figure_captions(
    blocks: list[ContentBlock],
    figures: list[FigureItem],
) -> list[ContentBlock]:
    """Remove caption prose already represented by a structured figure.

    The exact caption-kind and source-locator match keeps unrelated caption
    blocks intact when only some supplement figures have been structured.
    """

    represented = {
        (f"{figure.kind}_caption", figure.source_locator)
        for figure in figures
        if figure.kind in {"figure", "scheme"}
    }
    represented_page_labels: set[tuple[str, str, int]] = set()
    for figure in figures:
        if figure.kind != "figure":
            continue
        label_match = _SUPPLEMENT_FIGURE_LABEL_PREFIX.match(figure.label)
        if label_match is None:
            continue
        number = normalize_caption_number(label_match.group("number")).casefold()
        for page in _source_locator_pages(figure.source_locator):
            represented_page_labels.add((figure.source_path, number, page))

    retained: list[ContentBlock] = []
    for block in blocks:
        if (block.kind, block.source_locator) in represented:
            continue
        label_match = (
            _SUPPLEMENT_FIGURE_LABEL_PREFIX.match(block.plain_text)
            if block.kind == "figure_caption"
            else None
        )
        if label_match is not None:
            number = normalize_caption_number(
                label_match.group("number")
            ).casefold()
            if any(
                (block.source_path, number, page) in represented_page_labels
                for page in _source_locator_pages(block.source_locator)
            ):
                # A reviewed complete figure on the same source page owns
                # native labels/caption fragments embedded with its pixels,
                # even when its reviewed caption has a richer locator.
                continue
        retained.append(block)
    return retained


_SUPPLEMENT_FIGURE_LABEL_PREFIX = re.compile(
    r"^(?:(?:Supplemental|Supplementary|Supporting)\s+)?"
    r"(?:Fi(?:\s+)?gures?|Figs?\.?)\s*"
    r"(?P<number>[A-Za-z]*\s*\d+(?:[A-Za-z]|[.-]\d+)*)"
    r"\s*[.:-]?\s*",
    flags=re.IGNORECASE,
)


def _source_locator_pages(locator: str) -> set[int]:
    """Return explicit single/range page numbers from a source locator."""

    pages = {
        int(value)
        for value in re.findall(r"(?:^|;)page=(\d+)(?=;|$)", locator)
    }
    for first, last in re.findall(
        r"(?:^|;)pages=(\d+)-(\d+)(?=;|$)", locator
    ):
        start = int(first)
        stop = int(last)
        if start <= stop:
            pages.update(range(start, stop + 1))
    return pages


def _supplement_figure_equivalence_key(
    figure: FigureItem,
) -> tuple[str, str, str] | None:
    """Return a strict authored-label/caption key for cross-file duplicates."""

    label_match = _SUPPLEMENT_FIGURE_LABEL_PREFIX.match(figure.label)
    if label_match is None:
        return None
    number = normalize_caption_number(label_match.group("number")).casefold()
    caption = _normalize_space(figure.caption_plain)
    caption_match = _SUPPLEMENT_FIGURE_LABEL_PREFIX.match(caption)
    if caption_match is not None:
        caption = _normalize_space(caption[caption_match.end() :])
    return (figure.kind.casefold(), number, caption)


def _consolidate_cross_file_caption_figures(
    supplements: list[SupplementExtraction],
    crop_specs: Iterable[Mapping[str, Any]] = (),
) -> None:
    """Drop a caption-only duplicate when another source supplies its pixels.

    Publishers sometimes distribute captions in a DOCX and the corresponding
    image pages in a separate PDF.  When both parsers produce the same exact
    authored figure, retain the asset-bearing representation once.  The
    source-hash-bound equivalence decision remains private in coverage
    exclusions, so no caption or provenance is silently lost.
    """

    reviewed_crop_assets = {
        (
            str(spec.get("source_path", spec.get("source", ""))).replace(
                "\\", "/"
            ),
            str(spec.get("asset_id", "")).strip(),
        )
        for spec in crop_specs
        if str(spec.get("category", "")).strip().casefold()
        == "supplement_figure"
    }
    asset_figures: dict[tuple[str, str, str], list[tuple[str, FigureItem]]] = {}
    for supplement in supplements:
        for figure in supplement.figures:
            has_pixels = bool(figure.output_path) or (
                figure.source_path,
                figure.figure_id,
            ) in reviewed_crop_assets
            if not has_pixels:
                continue
            key = _supplement_figure_equivalence_key(figure)
            if key is not None:
                asset_figures.setdefault(key, []).append(
                    (supplement.supplement_id, figure)
                )

    for supplement in supplements:
        retained: list[FigureItem] = []
        for figure in supplement.figures:
            key = _supplement_figure_equivalence_key(figure)
            matches = [
                item
                for item in asset_figures.get(key, [])
                if item[0] != supplement.supplement_id
            ]
            has_pixels = bool(figure.output_path) or (
                figure.source_path,
                figure.figure_id,
            ) in reviewed_crop_assets
            if has_pixels or len(matches) != 1:
                retained.append(figure)
                continue
            represented_supplement, represented_figure = matches[0]
            # The caption-only source is often the publisher's native legend
            # document and therefore retains richer authored inline markup
            # than the separate visual PDF.  Exact normalized plain-text
            # equivalence has already been required above, so carry that
            # richer representation onto the retained pixel-bearing figure.
            represented_figure.caption_plain = figure.caption_plain
            represented_figure.caption_markdown = figure.caption_markdown
            supplement.exclusions.append(
                {
                    "schema_version": "1.0",
                    "coverage_id": (
                        f"{supplement.supplement_id}-cross-file-caption-"
                        f"{figure.figure_id}"
                    ),
                    "content_kind": "figure_caption",
                    "source_path": figure.source_path,
                    "source_locator": figure.source_locator,
                    "status": "intentionally_excluded",
                    "reason": (
                        "An exact caption-equivalent structured figure with "
                        "local pixels is retained from another supplementary file."
                    ),
                    "evidence": (
                        f"{represented_supplement}:{represented_figure.figure_id};"
                        f"source_locator={represented_figure.source_locator}"
                    ),
                    "text_sha256": hashlib.sha256(
                        figure.caption_plain.encode("utf-8")
                    ).hexdigest(),
                }
            )
        supplement.figures = retained


def _warning(
    code: str,
    message: str,
    source: SourceFile,
    supplement_id: str,
    *,
    page: int | None = None,
) -> dict[str, Any]:
    warning: dict[str, Any] = {
        "schema_version": "1.0",
        "code": code,
        "severity": "review",
        "message": message,
        "source_path": source.relative_path,
        "supplement_id": supplement_id,
    }
    if page is not None:
        warning["page"] = page
    return warning


def _require_regular_source(source: SourceFile) -> Path:
    path = source.path
    if not path.exists():
        raise FileNotFoundError(f"supplement disappeared after discovery: {path}")
    if is_reparse_point(path):
        raise ValueError(f"supplement cannot be a reparse point: {path}")
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError(f"supplement is not a regular file: {path}")
    actual_size = path.stat().st_size
    if actual_size != source.size:
        raise RuntimeError(
            f"supplement size changed after discovery: {source.relative_path} "
            f"({source.size} -> {actual_size} bytes)"
        )
    actual_hash = sha256_file(path)
    if actual_hash != source.sha256:
        raise RuntimeError(
            f"supplement hash changed after discovery: {source.relative_path}"
        )
    return path


def _copy_verified(source: SourceFile, destination: Path, extraction_root: Path) -> None:
    source_path = _require_regular_source(source)
    destination = ensure_within(destination, extraction_root, require_exists=False)
    reject_reparse_chain(destination.parent, extraction_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    reject_reparse_chain(destination.parent, extraction_root)

    if destination.exists():
        if is_reparse_point(destination) or not stat.S_ISREG(destination.lstat().st_mode):
            raise ValueError(f"unsafe existing supplement destination: {destination}")
        if destination.stat().st_size != source.size or sha256_file(destination) != source.sha256:
            raise FileExistsError(
                f"refusing to replace a different supplement copy: {destination}"
            )
        return

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary_path = Path(temporary_name)
    digest = hashlib.sha256()
    copied_size = 0
    try:
        with source_path.open("rb") as input_stream, os.fdopen(
            descriptor, "wb"
        ) as output_stream:
            while True:
                chunk = input_stream.read(1024 * 1024)
                if not chunk:
                    break
                output_stream.write(chunk)
                digest.update(chunk)
                copied_size += len(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())

        if copied_size != source.size or digest.hexdigest() != source.sha256:
            raise RuntimeError(
                f"supplement changed while being copied: {source.relative_path}"
            )
        if destination.exists():
            raise FileExistsError(
                f"supplement destination appeared during copy: {destination}"
            )
        os.replace(temporary_path, destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise

    if destination.stat().st_size != source.size or sha256_file(destination) != source.sha256:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            f"supplement copy failed verification: {source.relative_path}"
        )


def _write_generated_asset(
    extraction_root: Path,
    relative_path: str,
    data: bytes,
) -> Path:
    """Write one generated supplement asset without replacing existing data."""

    normalized = relative_path.replace("\\", "/")
    parts = tuple(normalized.split("/"))
    if (
        not normalized
        or normalized.startswith("/")
        or any(not part or part in {".", ".."} for part in parts)
    ):
        raise ValueError(f"unsafe generated supplement asset path: {relative_path!r}")
    destination = ensure_within(
        extraction_root.joinpath(*parts), extraction_root, require_exists=False
    )
    reject_reparse_chain(destination.parent, extraction_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    reject_reparse_chain(destination.parent, extraction_root)
    if destination.exists():
        if is_reparse_point(destination) or not stat.S_ISREG(destination.lstat().st_mode):
            raise ValueError(f"unsafe existing supplement asset: {destination}")
        if destination.read_bytes() != data:
            raise FileExistsError(
                f"refusing to replace a different supplement asset: {destination}"
            )
        return destination

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise FileExistsError(
                f"supplement asset destination appeared during write: {destination}"
            ) from exc
        temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if destination.read_bytes() != data:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"supplement asset write failed verification: {destination}")
    return destination


def _reviewed_standalone_image_specs(
    raw_specs: Iterable[Mapping[str, Any]],
    supplement_sources: Iterable[SourceFile],
) -> dict[str, list[dict[str, Any]]]:
    """Validate reviewed figure semantics for standalone image supplements.

    Standalone image files are otherwise intentionally opaque: pixels are not
    OCR targets and a filename alone is insufficient evidence that an image is
    an authored figure.  These hash-pinned specs classify exact source
    snapshots and require every frame to be represented, preventing silent
    loss from multi-frame TIFF files.
    """

    sources = {source.relative_path: source for source in supplement_sources}
    required = {
        "source_path",
        "source_sha256",
        "asset_id",
        "label",
        "caption_plain",
        "caption_markdown",
        "source_locator",
        "output_path",
        "expected_frames",
        "frame",
        "reason",
        "evidence",
    }
    allowed = required | {"kind"}
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_assets: set[str] = set()
    seen_outputs: set[str] = set()
    for index, raw in enumerate(raw_specs, 1):
        if not isinstance(raw, Mapping):
            raise ValueError(
                f"standalone_image_figures item {index} must be a mapping"
            )
        unknown = set(raw) - allowed
        missing = required - set(raw)
        if unknown or missing:
            raise ValueError(
                f"standalone_image_figures item {index} has invalid fields"
            )
        spec = dict(raw)
        source_path = str(spec["source_path"]).replace("\\", "/")
        source_sha256 = str(spec["source_sha256"]).strip().casefold()
        source = sources.get(source_path)
        asset_id = str(spec["asset_id"]).strip()
        label = str(spec["label"]).strip()
        caption_plain = str(spec["caption_plain"]).strip()
        caption_markdown = str(spec["caption_markdown"]).strip()
        source_locator = str(spec["source_locator"]).strip()
        output_path = str(spec["output_path"]).replace("\\", "/").strip()
        reason = str(spec["reason"]).strip()
        evidence = str(spec["evidence"]).strip()
        kind = str(spec.get("kind", "figure")).strip().casefold()
        expected_frames = spec["expected_frames"]
        frame = spec["frame"]
        output_parts = tuple(output_path.split("/"))
        if (
            source is None
            or not (
                source.detected_format.startswith("image/")
                or source.detected_format == POSTSCRIPT_MEDIA_TYPE
                or source.detected_format == PPTX_MEDIA_TYPE
            )
            or source.sha256.casefold() != source_sha256
            or not all(
                (
                    asset_id,
                    label,
                    caption_plain,
                    caption_markdown,
                    source_locator,
                    output_path,
                    reason,
                    evidence,
                )
            )
            or kind not in {"figure", "scheme"}
            or isinstance(expected_frames, bool)
            or not isinstance(expected_frames, int)
            or expected_frames < 1
            or isinstance(frame, bool)
            or not isinstance(frame, int)
            or not 1 <= frame <= expected_frames
            or (
                source.detected_format == POSTSCRIPT_MEDIA_TYPE
                and (expected_frames != 1 or frame != 1)
            )
            or (
                source.detected_format == "image/svg+xml"
                and not output_path.casefold().endswith(".svg")
            )
            or (
                source.detected_format != "image/svg+xml"
                and not output_path.casefold().endswith(".png")
            )
            or output_path.startswith("/")
            or any(not part or part in {".", ".."} for part in output_parts)
            or asset_id in seen_assets
            or output_path.casefold() in seen_outputs
        ):
            raise ValueError(f"invalid standalone_image_figures item {index}")
        seen_assets.add(asset_id)
        seen_outputs.add(output_path.casefold())
        spec.update(
            {
                "source_path": source_path,
                "source_sha256": source_sha256,
                "asset_id": asset_id,
                "label": label,
                "caption_plain": caption_plain,
                "caption_markdown": caption_markdown,
                "source_locator": source_locator,
                "output_path": output_path,
                "kind": kind,
                "reason": reason,
                "evidence": evidence,
            }
        )
        grouped.setdefault(source_path, []).append(spec)

    for source_path, specs in grouped.items():
        expected_values = {int(spec["expected_frames"]) for spec in specs}
        frames = {int(spec["frame"]) for spec in specs}
        if len(expected_values) != 1:
            raise ValueError(
                f"standalone image frame-count disagreement: {source_path}"
            )
        expected = next(iter(expected_values))
        if frames != set(range(1, expected + 1)):
            raise ValueError(
                f"standalone image frame coverage is incomplete: {source_path}"
            )
        specs.sort(key=lambda item: int(item["frame"]))
    return grouped


def _reviewed_standalone_image_table_specs(
    raw_specs: Iterable[Mapping[str, Any]],
    supplement_sources: Iterable[SourceFile],
) -> dict[str, list[dict[str, Any]]]:
    """Validate reviewed standalone raster-table render specifications."""

    sources = {source.relative_path: source for source in supplement_sources}
    required = {
        "source_path",
        "source_sha256",
        "asset_id",
        "label",
        "source_locator",
        "output_path",
        "expected_frames",
        "frame",
        "reason",
        "evidence",
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_assets: set[str] = set()
    seen_outputs: set[str] = set()
    for index, raw in enumerate(raw_specs, 1):
        if not isinstance(raw, Mapping) or set(raw) != required:
            raise ValueError(
                f"standalone_image_tables item {index} has invalid fields"
            )
        spec = dict(raw)
        source_path = str(spec["source_path"]).replace("\\", "/")
        source_sha256 = str(spec["source_sha256"]).strip().casefold()
        source = sources.get(source_path)
        asset_id = str(spec["asset_id"]).strip()
        label = str(spec["label"]).strip()
        source_locator = str(spec["source_locator"]).strip()
        output_path = str(spec["output_path"]).replace("\\", "/").strip()
        reason = str(spec["reason"]).strip()
        evidence = str(spec["evidence"]).strip()
        expected_frames = spec["expected_frames"]
        frame = spec["frame"]
        output_parts = tuple(output_path.split("/"))
        if (
            source is None
            or not source.detected_format.startswith("image/")
            or source.detected_format == "image/svg+xml"
            or source.sha256.casefold() != source_sha256
            or not all(
                (
                    asset_id,
                    label,
                    source_locator,
                    output_path,
                    reason,
                    evidence,
                )
            )
            or isinstance(expected_frames, bool)
            or not isinstance(expected_frames, int)
            or expected_frames < 1
            or isinstance(frame, bool)
            or not isinstance(frame, int)
            or not 1 <= frame <= expected_frames
            or not output_path.casefold().endswith(".png")
            or output_path.startswith("/")
            or any(not part or part in {".", ".."} for part in output_parts)
            or asset_id in seen_assets
            or output_path.casefold() in seen_outputs
        ):
            raise ValueError(f"invalid standalone_image_tables item {index}")
        seen_assets.add(asset_id)
        seen_outputs.add(output_path.casefold())
        spec.update(
            {
                "source_path": source_path,
                "source_sha256": source_sha256,
                "asset_id": asset_id,
                "label": label,
                "source_locator": source_locator,
                "output_path": output_path,
                "reason": reason,
                "evidence": evidence,
            }
        )
        grouped.setdefault(source_path, []).append(spec)

    for source_path, specs in grouped.items():
        expected_values = {int(spec["expected_frames"]) for spec in specs}
        frames = {int(spec["frame"]) for spec in specs}
        if len(expected_values) != 1:
            raise ValueError(
                f"standalone image table frame-count disagreement: {source_path}"
            )
        expected = next(iter(expected_values))
        if frames != set(range(1, expected + 1)):
            raise ValueError(
                f"standalone image table frame coverage is incomplete: {source_path}"
            )
        specs.sort(key=lambda item: int(item["frame"]))
    return grouped


def _materialize_reviewed_standalone_image_tables(
    source: SourceFile,
    supplement_id: str,
    copied_source: Path,
    extraction_root: Path,
    specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create lossless PNG assets for reviewed standalone raster tables."""

    try:
        from PIL import Image
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency gate owns this
        raise RuntimeError(
            "Pillow is required for standalone image supplement tables"
        ) from exc

    assets: list[dict[str, Any]] = []
    with Image.open(copied_source) as opened:
        actual_frames = int(getattr(opened, "n_frames", 1))
        expected_frames = int(specs[0]["expected_frames"])
        if actual_frames != expected_frames:
            raise ValueError(
                f"standalone image table frame count changed for "
                f"{source.relative_path}: expected {expected_frames}, "
                f"found {actual_frames}"
            )
        for spec in specs:
            frame = int(spec["frame"])
            opened.seek(frame - 1)
            opened.load()
            image = opened.copy()
            if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            width, height = image.size
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=False, compress_level=6)
            png_bytes = output.getvalue()
            output_path = str(spec["output_path"])
            destination = _write_generated_asset(
                extraction_root, output_path, png_bytes
            )
            assets.append(
                {
                    "asset_id": str(spec["asset_id"]),
                    "category": "supplement_image",
                    "label": str(spec["label"]),
                    "media_type": "image/png",
                    "output_path": output_path,
                    "parent_id": supplement_id,
                    "source_locator": (
                        f"{spec['source_locator']};frame={frame};"
                        "derivative=lossless-png"
                    ),
                    "sha256": hashlib.sha256(png_bytes).hexdigest(),
                    "bytes": destination.stat().st_size,
                    "width": width,
                    "height": height,
                    "ocr_performed": False,
                }
            )
    return assets


def _materialize_reviewed_standalone_images(
    source: SourceFile,
    supplement_id: str,
    copied_source: Path,
    extraction_root: Path,
    specs: list[dict[str, Any]],
    postscript_renderer: PostscriptRenderer | None,
) -> tuple[list[FigureItem], list[dict[str, Any]]]:
    """Create full-resolution lossless PNG displays for reviewed image figures."""

    if source.detected_format == POSTSCRIPT_MEDIA_TYPE:
        if len(specs) != 1 or postscript_renderer is None:
            raise ValueError(
                "reviewed PostScript figure requires one frame and a renderer"
            )
        rendered = postscript_renderer(
            copied_source, extraction_root, POSTSCRIPT_RENDER_DPI
        )
        spec = specs[0]
        output_path = str(spec["output_path"])
        destination = _write_generated_asset(
            extraction_root, output_path, rendered.png_bytes
        )
        asset_id = str(spec["asset_id"])
        label = str(spec["label"])
        kind = str(spec["kind"])
        asset = {
            "asset_id": asset_id,
            "category": kind,
            "label": label,
            "media_type": "image/png",
            "output_path": output_path,
            "parent_id": supplement_id,
            "source_locator": (
                f"{spec['source_locator']};frame=1;"
                "derivative=lossless-png"
            ),
            "sha256": hashlib.sha256(rendered.png_bytes).hexdigest(),
            "bytes": destination.stat().st_size,
            "width": rendered.pixel_width,
            "height": rendered.pixel_height,
            "ocr_performed": False,
            "render_method": rendered.renderer,
            "renderer_version": rendered.renderer_version,
        }
        figure = FigureItem(
            figure_id=asset_id,
            source_id=label,
            label=label,
            kind=kind,
            caption_markdown=str(spec["caption_markdown"]),
            caption_plain=str(spec["caption_plain"]),
            source_path=source.relative_path,
            source_locator=f"{spec['source_locator']};frame=1",
            output_path=output_path,
        )
        return [figure], [asset]

    if source.detected_format == "image/svg+xml":
        from .html_extractor import _safe_embedded_svg

        if len(specs) != 1 or int(specs[0]["expected_frames"]) != 1:
            raise ValueError("reviewed SVG figure requires exactly one frame")
        svg_bytes = copied_source.read_bytes()
        if not _safe_embedded_svg(svg_bytes):
            raise ValueError("reviewed standalone SVG is not self-contained and safe")
        spec = specs[0]
        output_path = str(spec["output_path"])
        destination = _write_generated_asset(
            extraction_root, output_path, svg_bytes
        )
        asset_id = str(spec["asset_id"])
        label = str(spec["label"])
        kind = str(spec["kind"])
        asset = {
            "asset_id": asset_id,
            "category": kind,
            "label": label,
            "media_type": "image/svg+xml",
            "output_path": output_path,
            "parent_id": supplement_id,
            "source_locator": (
                f"{spec['source_locator']};frame=1;"
                "derivative=verified-svg-copy"
            ),
            "sha256": hashlib.sha256(svg_bytes).hexdigest(),
            "bytes": destination.stat().st_size,
            "ocr_performed": False,
        }
        figure = FigureItem(
            figure_id=asset_id,
            source_id=label,
            label=label,
            kind=kind,
            caption_markdown=str(spec["caption_markdown"]),
            caption_plain=str(spec["caption_plain"]),
            source_path=source.relative_path,
            source_locator=f"{spec['source_locator']};frame=1",
            output_path=output_path,
        )
        return [figure], [asset]

    try:
        from PIL import Image
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency gate owns this
        raise RuntimeError(
            "Pillow is required for standalone image supplement figures"
        ) from exc

    figures: list[FigureItem] = []
    assets: list[dict[str, Any]] = []
    with Image.open(copied_source) as opened:
        actual_frames = int(getattr(opened, "n_frames", 1))
        expected_frames = int(specs[0]["expected_frames"])
        if actual_frames != expected_frames:
            raise ValueError(
                f"standalone image frame count changed for {source.relative_path}: "
                f"expected {expected_frames}, found {actual_frames}"
            )
        for spec in specs:
            frame = int(spec["frame"])
            opened.seek(frame - 1)
            opened.load()
            image = opened.copy()
            if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            width, height = image.size
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=False, compress_level=6)
            png_bytes = output.getvalue()
            output_path = str(spec["output_path"])
            destination = _write_generated_asset(
                extraction_root, output_path, png_bytes
            )
            asset_id = str(spec["asset_id"])
            label = str(spec["label"])
            kind = str(spec["kind"])
            assets.append(
                {
                    "asset_id": asset_id,
                    "category": kind,
                    "label": label,
                    "media_type": "image/png",
                    "output_path": output_path,
                    "parent_id": supplement_id,
                    "source_locator": (
                        f"{spec['source_locator']};frame={frame};"
                        "derivative=lossless-png"
                    ),
                    "sha256": hashlib.sha256(png_bytes).hexdigest(),
                    "bytes": destination.stat().st_size,
                    "width": width,
                    "height": height,
                    "ocr_performed": False,
                }
            )
            figures.append(
                FigureItem(
                    figure_id=asset_id,
                    source_id=label,
                    label=label,
                    kind=kind,
                    caption_markdown=str(spec["caption_markdown"]),
                    caption_plain=str(spec["caption_plain"]),
                    source_path=source.relative_path,
                    source_locator=f"{spec['source_locator']};frame={frame}",
                    output_path=output_path,
                )
            )
    return figures, assets


def _promote_reviewed_presentation_slide_figures(
    source: SourceFile,
    supplement_id: str,
    specs: list[dict[str, Any]],
    assets: list[dict[str, Any]],
) -> list[FigureItem]:
    """Promote reviewed image-only slide renders to structured figures.

    The reviewed mapping is necessary when a presentation contains only
    rasterized slides and its authored captions are delivered in a separate
    source.  Every slide remains PowerPoint-rendered at the required size;
    embedded package media are preserved as related downloadable assets.
    """

    if not specs:
        return []
    expected_slide_count = int(specs[0]["expected_frames"])
    renders_by_slide: dict[int, dict[str, Any]] = {}
    for asset in assets:
        if str(asset.get("category", "")).casefold() not in {
            "supplement_slide_render",
            "figure",
        }:
            continue
        slide_number = asset.get("presentation_slide_number")
        if isinstance(slide_number, bool) or not isinstance(slide_number, int):
            continue
        if slide_number in renders_by_slide:
            raise ValueError("presentation contains duplicate complete-slide renders")
        renders_by_slide[slide_number] = asset

    figures: list[FigureItem] = []
    for spec in specs:
        slide_number = int(spec["frame"])
        render = renders_by_slide.get(slide_number)
        if (
            render is None
            or int(render.get("presentation_slide_count", 0)) != expected_slide_count
            or str(render.get("asset_id", "")) != str(spec["asset_id"])
            or str(render.get("output_path", "")).replace("\\", "/")
            != str(spec["output_path"])
        ):
            raise ValueError(
                "reviewed presentation figure does not match its complete-slide render"
            )
        asset_id = str(spec["asset_id"])
        label = str(spec["label"])
        kind = str(spec["kind"])
        render["category"] = kind
        render["label"] = label
        render["parent_id"] = supplement_id
        render["source_locator"] = (
            f"{spec['source_locator']};slide={slide_number};render=complete-slide"
        )
        figures.append(
            FigureItem(
                figure_id=asset_id,
                source_id=label,
                label=label,
                kind=kind,
                caption_markdown=str(spec["caption_markdown"]),
                caption_plain=str(spec["caption_plain"]),
                source_path=source.relative_path,
                source_locator=f"{spec['source_locator']};slide={slide_number}",
                output_path=str(spec["output_path"]),
            )
        )

    promoted_by_slide = {
        int(spec["frame"]): str(spec["asset_id"]) for spec in specs
    }
    for asset in assets:
        if (
            str(asset.get("category", "")).casefold() != "supplement_image"
            or asset.get("parent_id") != supplement_id
        ):
            continue
        raw_slides = asset.get("presentation_slide_numbers")
        try:
            slides = [int(value) for value in raw_slides]
        except (TypeError, ValueError):
            slides = []
        if len(slides) == 1 and slides[0] in promoted_by_slide:
            asset["parent_id"] = promoted_by_slide[slides[0]]
    return figures


def _materialize_reviewed_postscript_table(
    source: SourceFile,
    supplement_id: str,
    copied_source: Path,
    extraction_root: Path,
    table: TableItem,
    postscript_renderer: PostscriptRenderer | None,
) -> dict[str, Any]:
    """Create one complete lossless display for a reviewed EPS/PS table."""

    if postscript_renderer is None:
        raise ValueError("reviewed PostScript table requires a renderer")
    rendered = postscript_renderer(
        copied_source, extraction_root, POSTSCRIPT_RENDER_DPI
    )
    output_path = (
        Path("supplementary")
        / supplement_id
        / "tables"
        / f"{_safe_identifier(table.label)}.png"
    ).as_posix()
    destination = _write_generated_asset(
        extraction_root, output_path, rendered.png_bytes
    )
    return {
        "asset_id": table.table_id,
        "category": "supplement_table",
        "label": table.label,
        "media_type": "image/png",
        "output_path": output_path,
        "parent_id": supplement_id,
        "source_locator": f"{table.source_locator};derivative=lossless-png",
        "sha256": hashlib.sha256(rendered.png_bytes).hexdigest(),
        "bytes": destination.stat().st_size,
        "width": rendered.pixel_width,
        "height": rendered.pixel_height,
        "ocr_performed": False,
        "render_method": rendered.renderer,
        "renderer_version": rendered.renderer_version,
    }


def _reviewed_docx_figure_crop_specs(
    raw_specs: Iterable[Mapping[str, Any]],
    supplement_sources: Iterable[SourceFile],
) -> dict[str, list[dict[str, Any]]]:
    """Validate hash-pinned full-layout renders for exceptional DOCX figures."""

    sources = {source.relative_path: source for source in supplement_sources}
    required = {
        "source_path",
        "source_sha256",
        "asset_id",
        "page",
        "box",
        "output_path",
        "expected_page_count",
        "reason",
        "evidence",
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_assets: set[str] = set()
    seen_outputs: set[str] = set()
    for index, raw in enumerate(raw_specs, 1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"docx_figure_crops item {index} must be a mapping")
        if set(raw) != required:
            raise ValueError(f"docx_figure_crops item {index} has invalid fields")
        spec = dict(raw)
        source_path = str(spec["source_path"]).replace("\\", "/").strip()
        source_sha256 = str(spec["source_sha256"]).strip().casefold()
        source = sources.get(source_path)
        asset_id = str(spec["asset_id"]).strip()
        output_path = str(spec["output_path"]).replace("\\", "/").strip()
        reason = str(spec["reason"]).strip()
        evidence = str(spec["evidence"]).strip()
        page = spec["page"]
        expected_page_count = spec["expected_page_count"]
        raw_box = spec["box"]
        if (
            not isinstance(raw_box, (list, tuple))
            or len(raw_box) != 4
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in raw_box
            )
        ):
            raise ValueError(f"docx_figure_crops item {index} has an invalid box")
        box = tuple(float(value) for value in raw_box)
        pure_output = PurePosixPath(output_path)
        if (
            source is None
            or source.detected_format not in {DOCX_MEDIA_TYPE, LEGACY_DOC_MEDIA_TYPE}
            or source.sha256.casefold() != source_sha256
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", asset_id)
            or asset_id in seen_assets
            or not output_path.casefold().endswith(".png")
            or pure_output.is_absolute()
            or any(part in {"", ".", ".."} for part in pure_output.parts)
            or output_path.casefold() in seen_outputs
            or isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or isinstance(expected_page_count, bool)
            or not isinstance(expected_page_count, int)
            or expected_page_count < 1
            or page > expected_page_count
            or box[0] < 0
            or box[1] < 0
            or box[2] <= box[0]
            or box[3] <= box[1]
            or not reason
            or not evidence
        ):
            raise ValueError(f"invalid docx_figure_crops item {index}")
        seen_assets.add(asset_id)
        seen_outputs.add(output_path.casefold())
        spec.update(
            {
                "source_path": source_path,
                "source_sha256": source_sha256,
                "asset_id": asset_id,
                "output_path": output_path,
                "page": page,
                "box": box,
                "expected_page_count": expected_page_count,
                "reason": reason,
                "evidence": evidence,
            }
        )
        grouped.setdefault(source_path, []).append(spec)

    for source_path, specs in grouped.items():
        if len({int(spec["expected_page_count"]) for spec in specs}) != 1:
            raise ValueError(f"DOCX crop page-count disagreement: {source_path}")
    return grouped


def _apply_reviewed_docx_figure_crops(
    *,
    source: SourceFile,
    supplement_id: str,
    copied_source: Path,
    extraction_root: Path,
    figures: list[FigureItem],
    assets: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    specs: list[dict[str, Any]],
    renderer: DocxFigureRenderer | None,
) -> None:
    """Materialize reviewed native-layout crops for parsed DOCX figures."""

    if not specs:
        return
    if renderer is None:
        raise DocxFigureRenderError(
            "reviewed DOCX figure crops require a document renderer"
        )
    figures_by_id = {figure.figure_id: figure for figure in figures}
    if len(figures_by_id) != len(figures):
        raise ValueError("DOCX figures contain duplicate identifiers")
    assets_by_id = {
        str(asset.get("asset_id", "")): asset
        for asset in assets
        if str(asset.get("asset_id", ""))
    }
    if len(assets_by_id) != len(assets):
        raise ValueError("DOCX assets contain missing or duplicate identifiers")

    targets: dict[str, Path] = {}
    materialized_assets: set[str] = set()
    materialized_embedded_assets: dict[str, dict[str, Any]] = {}
    original_locators: dict[str, str] = {}
    requests: list[DocxFigureCropRequest] = []
    unsupported_embedded_warnings = [
        warning
        for warning in warnings
        if warning.get("code") == "docx_embedded_visual_format_unsupported"
        and warning.get("source_path") == source.relative_path
    ]
    unsupported_embedded_by_id = {
        f"{supplement_id}_embedded_visual_{number:03d}": warning
        for number, warning in enumerate(unsupported_embedded_warnings, 1)
    }
    for spec in specs:
        asset_id = str(spec["asset_id"])
        figure = figures_by_id.get(asset_id)
        asset = assets_by_id.get(asset_id)
        output_path = str(spec["output_path"])
        embedded_visual = (
            figure is None
            and asset is not None
            and asset.get("category") == "supplement_image"
            and asset.get("parent_id") in {None, supplement_id}
            and asset_id.startswith(f"{supplement_id}_embedded_visual_")
            and asset.get("media_type") == "image/png"
            and str(asset.get("output_path", "")) == output_path
            and output_path.startswith(f"figures/{supplement_id}/")
        )
        unsupported_embedded_warning = unsupported_embedded_by_id.get(asset_id)
        materialized_embedded_visual = (
            figure is None
            and asset is None
            and unsupported_embedded_warning is not None
            and output_path
            == f"figures/{supplement_id}/{asset_id.removeprefix(f'{supplement_id}_')}.png"
        )
        if figure is None and not embedded_visual and not materialized_embedded_visual:
            raise ValueError(
                f"reviewed DOCX crop {asset_id!r} does not match one parsed figure"
            )
        if asset is None:
            if materialized_embedded_visual:
                materialized_embedded_assets[asset_id] = unsupported_embedded_warning
            else:
                expected_prefix = f"figures/{supplement_id}/"
                if (
                    figure.output_path is not None
                    or not output_path.startswith(expected_prefix)
                ):
                    raise ValueError(
                        f"reviewed DOCX crop {asset_id!r} has an invalid materialized target"
                    )
                materialized_assets.add(asset_id)
        elif not embedded_visual and (
            figure.output_path != output_path
            or str(asset.get("output_path", "")) != output_path
            or str(asset.get("category", "")) != figure.kind
        ):
            raise ValueError(
                f"reviewed DOCX crop {asset_id!r} does not match one parsed figure asset"
            )
        original_locators[asset_id] = (
            figure.source_locator
            if figure is not None
            else str(asset.get("source_locator", ""))
            if asset is not None
            else str(unsupported_embedded_warning.get("source_locator", ""))
        )
        target = ensure_within(
            extraction_root.joinpath(*PurePosixPath(output_path).parts),
            extraction_root,
            require_exists=asset is not None,
        )
        reject_reparse_chain(target, extraction_root)
        if asset is not None:
            if is_reparse_point(target) or not stat.S_ISREG(target.lstat().st_mode):
                raise ValueError(f"reviewed DOCX crop target is unsafe: {output_path}")
            if str(asset.get("sha256", "")) != sha256_file(target):
                raise ValueError(
                    f"reviewed DOCX crop target hash changed before rendering: {asset_id}"
                )
        elif target.exists():
            raise ValueError(f"reviewed DOCX crop target already exists: {output_path}")
        targets[asset_id] = target
        requests.append(
            DocxFigureCropRequest(
                asset_id=asset_id,
                page=int(spec["page"]),
                box=tuple(spec["box"]),
                expected_page_count=int(spec["expected_page_count"]),
            )
        )

    scratch_parent = extraction_root.parent
    if ".building-" in extraction_root.parent.name:
        scratch_parent = extraction_root.parent.parent
    with tempfile.TemporaryDirectory(
        # LibreOffice creates a deeply nested profile tree.  Keep the private
        # scratch root at the caller's isolated staging parent so Windows path
        # limits do not prevent conversion of otherwise valid documents.
        prefix=".docx-render-",
        dir=scratch_parent,
    ) as temporary:
        temporary_root = Path(temporary).resolve(strict=True)
        rendered = renderer(
            copied_source,
            temporary_root,
            tuple(requests),
            DOCX_FIGURE_RENDER_DPI,
        )
        rendered_by_id = {item.asset_id: item for item in rendered}
        if len(rendered_by_id) != len(rendered) or set(rendered_by_id) != set(targets):
            raise DocxFigureRenderError(
                "DOCX renderer output does not match the reviewed figure requests"
            )

        prepared: dict[str, tuple[bytes, int, int, Any]] = {}
        try:
            from PIL import Image
        except ModuleNotFoundError as exc:  # pragma: no cover - dependency gate owns this
            raise RuntimeError("Pillow is required for DOCX figure rendering") from exc
        request_by_id = {request.asset_id: request for request in requests}
        for asset_id, item in rendered_by_id.items():
            request = request_by_id[asset_id]
            if (
                item.page_count != request.expected_page_count
                or item.page != request.page
                or tuple(item.box) != request.box
                or item.dpi != DOCX_FIGURE_RENDER_DPI
                or item.pixel_width < 1
                or item.pixel_height < 1
            ):
                raise DocxFigureRenderError(
                    f"DOCX renderer metadata is inconsistent for {asset_id!r}"
                )
            try:
                rendered_path = ensure_within(
                    Path(item.path).resolve(strict=True), temporary_root
                )
                reject_reparse_chain(rendered_path, temporary_root)
            except (FileNotFoundError, OSError) as exc:
                raise DocxFigureRenderError(
                    f"DOCX renderer output is missing or unsafe for {asset_id!r}"
                ) from exc
            if is_reparse_point(rendered_path) or not stat.S_ISREG(
                rendered_path.lstat().st_mode
            ):
                raise DocxFigureRenderError(
                    f"DOCX renderer output is not a regular file for {asset_id!r}"
                )
            data = rendered_path.read_bytes()
            try:
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    if image.format != "PNG" or image.size != (
                        item.pixel_width,
                        item.pixel_height,
                    ):
                        raise DocxFigureRenderError(
                            f"DOCX renderer PNG metadata disagrees for {asset_id!r}"
                        )
            except DocxFigureRenderError:
                raise
            except Exception as exc:
                raise DocxFigureRenderError(
                    f"DOCX renderer returned an invalid PNG for {asset_id!r}"
                ) from exc
            prepared[asset_id] = (
                data,
                item.pixel_width,
                item.pixel_height,
                item,
            )

        for asset_id in (str(spec["asset_id"]) for spec in specs):
            data, width, height, item = prepared[asset_id]
            target = targets[asset_id]
            atomic_write_bytes(target, data)
            if target.read_bytes() != data:
                raise RuntimeError(
                    f"reviewed DOCX figure replacement failed verification: {asset_id}"
                )
            locator = (
                f"docx-render=complete-authored-layout;page={item.page};"
                f"box-points={','.join(f'{value:g}' for value in item.box)};"
                f"renderer={item.renderer}"
            )
            figure = figures_by_id.get(asset_id)
            if asset_id in materialized_assets:
                figure.output_path = str(
                    next(
                        spec["output_path"]
                        for spec in specs
                        if str(spec["asset_id"]) == asset_id
                    )
                )
                asset = {
                    "asset_id": asset_id,
                    "category": figure.kind,
                    "label": figure.label,
                    "media_type": "image/png",
                    "output_path": figure.output_path,
                    "parent_id": supplement_id,
                }
                assets.append(asset)
                assets_by_id[asset_id] = asset
            elif asset_id in materialized_embedded_assets:
                warning = materialized_embedded_assets[asset_id]
                source_name = str(warning.get("message", "")).rsplit(": ", 1)[-1]
                source_name = source_name.rstrip(".")
                asset = {
                    "asset_id": asset_id,
                    "category": "supplement_image",
                    "label": (
                        f"Embedded DOCX visual {asset_id.rsplit('_', 1)[-1]} "
                        f"({source_name})"
                    ),
                    "media_type": "image/png",
                    "output_path": str(
                        next(
                            spec["output_path"]
                            for spec in specs
                            if str(spec["asset_id"]) == asset_id
                        )
                    ),
                    "parent_id": supplement_id,
                }
                assets.append(asset)
                assets_by_id[asset_id] = asset
            else:
                asset = assets_by_id[asset_id]
            asset.update(
                {
                    "source_locator": f"{original_locators[asset_id]};{locator}",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                    "width": width,
                    "height": height,
                    "ocr_performed": False,
                    "render_method": "reviewed_docx_pdf_crop",
                    "render_page": item.page,
                    "render_box_points": list(item.box),
                    "render_dpi": item.dpi,
                    "renderer": item.renderer,
                    "renderer_version": item.renderer_version,
                }
            )
            if figure is not None:
                figure.source_locator = f"{figure.source_locator};{locator}"

    resolved_locators = {
        original_locators[asset_id]
        for asset_id in materialized_assets | set(materialized_embedded_assets)
    }
    warnings[:] = [
        warning
        for warning in warnings
        if not (
            warning.get("code") in {
                "supplement_figure_asset_missing",
                "docx_embedded_visual_format_unsupported",
            }
            and warning.get("source_path") == source.relative_path
            and warning.get("source_locator") in resolved_locators
        )
    ]


def _box(raw: dict[str, Any]) -> tuple[float, float, float, float] | None:
    try:
        return (
            float(raw["x0"]),
            float(raw["top"]),
            float(raw["x1"]),
            float(raw["bottom"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _boxes_touch(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    *,
    margin: float,
) -> bool:
    return not (
        first[2] < second[0] - margin
        or first[0] > second[2] + margin
        or first[3] < second[1] - margin
        or first[1] > second[3] + margin
    )


def _union_box(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _is_inline_raster_glyph(
    box: tuple[float, float, float, float],
    protected_text_regions: Iterable[tuple[float, float, float, float]],
) -> bool:
    """Return whether a tiny raster is contained by a native-text line.

    Some publisher PDFs paint punctuation or other inline glyph fragments as
    very small images even though the complete line is also available in the
    native text stream.  Such images are not figure seeds: treating them as
    figures causes the whole overlapping prose line to be discarded.
    """

    width = box[2] - box[0]
    height = box[3] - box[1]
    if width > 24.0 or height > 24.0:
        return False
    return any(
        box[0] >= text_box[0] - 0.5
        and box[1] >= text_box[1] - 0.5
        and box[2] <= text_box[2] + 0.5
        and box[3] <= text_box[3] + 0.5
        for text_box in protected_text_regions
    )


def _figure_graphic_regions(
    page: Any,
    reviewed_regions: Iterable[tuple[float, float, float, float]] = (),
    protected_text_regions: Iterable[tuple[float, float, float, float]] = (),
) -> list[tuple[float, float, float, float]]:
    """Find graphics connected to raster images without treating tables as figures.

    A raster image can be accompanied by vector bonds, labels, or panel marks.
    Those nearby vector objects are folded into the image region. Vector-only
    ruled tables are intentionally not used as seeds because their native cell
    text belongs in the textual extraction.
    """

    protected = list(protected_text_regions)
    regions = [
        *[
            box
            for raw in page.images
            if (box := _box(raw)) is not None
            and not _is_inline_raster_glyph(box, protected)
        ],
        *list(reviewed_regions),
    ]
    pending = [
        box
        for raw in (*page.lines, *page.curves, *page.rects)
        if (box := _box(raw)) is not None
        # Some PDFs render ordinary native-text glyphs as vector paths as
        # well. Those duplicate outlines must not bridge nearby prose into a
        # raster figure region.
        and not any(
            _boxes_touch(box, text_box, margin=0.0)
            for text_box in protected
        )
    ]
    changed = True
    while changed:
        changed = False
        remaining: list[tuple[float, float, float, float]] = []
        for candidate in pending:
            matches = [
                index
                for index, region in enumerate(regions)
                if _boxes_touch(candidate, region, margin=8.0)
            ]
            if not matches:
                remaining.append(candidate)
                continue
            merged = candidate
            for index in reversed(matches):
                merged = _union_box(merged, regions.pop(index))
            regions.append(merged)
            changed = True
        pending = remaining
    return regions


def _extract_page_lines(
    page: Any,
    reviewed_regions: Iterable[tuple[float, float, float, float]] = (),
) -> list[_PdfLine]:
    """Return native PDF text lines in pdfplumber's reading order."""

    # Some document-to-PDF converters simulate bold text by painting the
    # identical glyph several times with sub-point offsets.  Apply the same
    # conservative geometric deduplication used by the main PDF decoder so
    # those drawing instructions do not become repeated authored characters.
    # Font name and size are part of the equality key, which preserves nearby
    # or deliberately overstruck distinct text.
    dedupe_chars = getattr(page, "dedupe_chars", None)
    text_page = (
        dedupe_chars(
            tolerance=0.5,
            extra_attrs=("fontname", "size"),
        )
        if callable(dedupe_chars)
        else page
    )
    raw_lines = text_page.extract_text_lines(
        layout=False,
        strip=True,
        return_chars=False,
    )
    protected_text_regions = [
        (
            float(raw_line.get("x0", 0.0)),
            float(raw_line.get("top", 0.0)),
            float(raw_line.get("x1", raw_line.get("x0", 0.0))),
            float(raw_line.get("bottom", raw_line.get("top", 0.0))),
        )
        for raw_line in raw_lines
        if _normalize_space(str(raw_line.get("text", "")))
    ]
    graphic_regions = _figure_graphic_regions(
        text_page, reviewed_regions, protected_text_regions
    )
    lines: list[_PdfLine] = []
    for raw_line in raw_lines:
        text = _normalize_space(str(raw_line.get("text", "")))
        if not text:
            continue
        x0 = float(raw_line.get("x0", 0.0))
        x1 = float(raw_line.get("x1", x0))
        top = float(raw_line.get("top", 0.0))
        bottom = float(raw_line.get("bottom", top))
        line_box = (x0, top, x1, bottom)
        lines.append(
            _PdfLine(
                text=text,
                markdown=_escape_literal_pdf_markup(text),
                line_number=len(lines) + 1,
                x0=x0,
                x1=x1,
                top=top,
                bottom=bottom,
                overlaps_figure_graphic=any(
                    _boxes_touch(line_box, region, margin=0.0)
                    for region in graphic_regions
                ),
            )
        )
    return lines


def _supplement_pdf_text_config(
    raw_config: Mapping[str, Any] | None,
    source: SourceFile,
) -> Mapping[str, Any] | None:
    if not raw_config:
        return None
    raw_supplements = raw_config.get("supplements", [])
    if raw_supplements is None:
        return None
    if not isinstance(raw_supplements, list):
        raise ValueError("pdf_text.supplements must be a list")
    matches: list[Mapping[str, Any]] = []
    for index, item in enumerate(raw_supplements, 1):
        if not isinstance(item, Mapping):
            raise ValueError(f"pdf_text.supplements item {index} must be a mapping")
        source_path = str(item.get("source_path", "")).replace("\\", "/")
        if source_path == source.relative_path:
            matches.append(item)
    if len(matches) > 1:
        raise ValueError(
            f"pdf_text.supplements has duplicate entries for {source.relative_path}"
        )
    if not matches:
        return None
    match = matches[0]
    if str(match.get("source_sha256", "")).casefold() != source.sha256.casefold():
        raise ValueError(
            f"pdf_text supplement hash does not match {source.relative_path}"
        )
    return match


def _supplement_duplicate_file_only_exclusion(
    config: Mapping[str, Any] | None,
    source: SourceFile,
    supplement_id: str,
    all_sources: Iterable[SourceFile],
) -> dict[str, Any] | None:
    """Validate a reviewed, hash-pinned whole-file duplicate page map.

    Some publisher downloads concatenate the byte-identical article and its
    supporting PDF into a second convenience file.  Preserve that original
    download, but do not duplicate its text, figures, or tables in canonical
    content when every page has been explicitly mapped to already inventoried
    sources.  The current source is hash-gated by
    :func:`_supplement_pdf_text_config`; every referenced source is gated here.
    """

    if config is None or "reviewed_content_disposition" not in config:
        return None
    disposition = str(config.get("reviewed_content_disposition", "")).strip()
    if disposition != "duplicate_file_only":
        raise ValueError(
            "supplement reviewed_content_disposition must be "
            "'duplicate_file_only'"
        )
    reason = str(config.get("reason", "")).strip()
    evidence = str(config.get("evidence", "")).strip()
    raw_map = config.get("duplicate_of")
    if not reason or not evidence or not isinstance(raw_map, list) or not raw_map:
        raise ValueError("supplement duplicate_file_only review is incomplete")
    if not isinstance(source.page_count, int) or source.page_count < 1:
        raise ValueError("supplement duplicate_file_only requires a known page count")

    source_by_path = {
        item.relative_path.replace("\\", "/"): item for item in all_sources
    }
    covered_pages: set[int] = set()
    locator_parts: list[str] = []
    for index, item in enumerate(raw_map, 1):
        if not isinstance(item, Mapping):
            raise ValueError(f"supplement duplicate_of item {index} must be a mapping")
        source_path = str(item.get("source_path", "")).replace("\\", "/")
        source_sha256 = str(item.get("source_sha256", "")).casefold()
        source_pages = item.get("source_pages")
        duplicate_pages = item.get("duplicate_pages")
        referenced = source_by_path.get(source_path)
        ranges = (source_pages, duplicate_pages)
        if (
            referenced is None
            or referenced is source
            or source_sha256 != referenced.sha256.casefold()
            or not isinstance(referenced.page_count, int)
            or any(
                not isinstance(value, list)
                or len(value) != 2
                or any(isinstance(page, bool) or not isinstance(page, int) for page in value)
                for value in ranges
            )
        ):
            raise ValueError(f"supplement duplicate_of item {index} is incomplete")
        assert isinstance(source_pages, list) and isinstance(duplicate_pages, list)
        source_first, source_last = source_pages
        duplicate_first, duplicate_last = duplicate_pages
        if (
            source_first < 1
            or source_last < source_first
            or source_last > referenced.page_count
            or duplicate_first < 1
            or duplicate_last < duplicate_first
            or duplicate_last > source.page_count
            or source_last - source_first != duplicate_last - duplicate_first
        ):
            raise ValueError(f"supplement duplicate_of item {index} page map is invalid")
        mapped_pages = set(range(duplicate_first, duplicate_last + 1))
        if covered_pages & mapped_pages:
            raise ValueError("supplement duplicate_of page maps overlap")
        covered_pages.update(mapped_pages)
        locator_parts.append(
            f"pages={duplicate_first}-{duplicate_last}->"
            f"{source_path}#pages={source_first}-{source_last}"
        )
    if covered_pages != set(range(1, source.page_count + 1)):
        raise ValueError("supplement duplicate_of map must cover every duplicate page")

    return {
        "schema_version": "1.0",
        "coverage_id": f"{supplement_id}-reviewed-duplicate-file-only",
        "content_kind": "supplement_source_content",
        "source_path": source.relative_path,
        "source_locator": ";".join(locator_parts),
        "status": "duplicate",
        "reason": reason,
        "evidence": evidence,
        "output_ids": [supplement_id],
    }


def _supplement_native_text_engine(config: Mapping[str, Any] | None) -> str:
    value = str((config or {}).get("native_text_engine", "pdfplumber")).strip().casefold()
    if value not in {"pdfplumber", "pypdf"}:
        raise ValueError("supplement native_text_engine must be 'pdfplumber' or 'pypdf'")
    return value


def _supplement_needs_decoded_text(config: Mapping[str, Any] | None) -> bool:
    """Select the reviewed supplement's native-text stream.

    Legacy hash-pinned supplement reviews were authored while any matching
    ``pdf_text`` entry selected the rich decoded stream, including entries that
    only supplied table or caption semantics. Preserve that contract so a
    current rebuild does not silently lose superscripts, italics, or bold
    scientific labels. A reviewed source with an incompatible embedded font
    must opt into the literal pdfplumber stream explicitly.
    """

    if config is None:
        return False
    text_stream = str(config.get("text_stream", "auto")).strip().casefold()
    if text_stream not in {"auto", "pdfplumber", "decoded"}:
        raise ValueError(
            "supplement text_stream must be 'auto', 'pdfplumber', or 'decoded'"
        )
    if text_stream == "pdfplumber":
        incompatible = {"glyph_overrides"} & set(config)
        if incompatible:
            raise ValueError(
                "supplement pdfplumber text_stream cannot use custom glyph overrides"
            )
        return False
    if text_stream == "decoded":
        return True
    return True


def _supplement_text_replacements(
    config: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Validate exact, hash-pinned native-text replacements for one PDF."""

    raw = (config or {}).get("text_replacements", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("supplement text_replacements must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise ValueError(f"supplement text replacement {index} must be a mapping")
        old = str(item.get("old", ""))
        new = str(item.get("new", ""))
        expected_hits = item.get("expected_hits")
        reason = str(item.get("reason", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if (
            not old
            or old == new
            or old in seen
            or "�" in new
            or isinstance(expected_hits, bool)
            or not isinstance(expected_hits, int)
            or expected_hits < 1
            or not reason
            or not evidence
        ):
            raise ValueError(f"supplement text replacement {index} is incomplete")
        seen.add(old)
        result.append(
            {
                "old": old,
                "new": new,
                "expected_hits": expected_hits,
                "hits": 0,
                "reason": reason,
                "evidence": evidence,
            }
        )
    if any(
        left["old"] in right["old"] or right["old"] in left["old"]
        for left_index, left in enumerate(result)
        for right in result[left_index + 1 :]
    ):
        raise ValueError("supplement text replacement keys must not overlap")
    return result


def _apply_supplement_text_replacements(
    value: str,
    replacements: list[dict[str, Any]],
    *,
    count_hits: bool,
) -> str:
    for spec in replacements:
        hits = value.count(spec["old"])
        if count_hits:
            spec["hits"] += hits
        value = value.replace(spec["old"], spec["new"])
    return value


def _decoded_markdown_projection(value: str) -> tuple[str, list[tuple[int, int]]]:
    """Return visible text plus source spans for decoder-produced Markdown.

    ``pdf_text_extractor`` emits a deliberately small rich-text dialect: bold
    and italic Markdown markers, explicit ``strong``/``em``/``sup``/``sub``
    tags, backslash escapes, and the three basic HTML entities.  Keeping a
    source span for every visible character lets reviewed literal repairs cross
    a style boundary without silently repairing only ``plain_text``.
    """

    visible: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    tag_pattern = re.compile(r"</?(?:strong|em|sup|sub)>", re.IGNORECASE)
    entity_pattern = re.compile(r"&(amp|lt|gt);")
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            visible.append(value[index + 1])
            spans.append((index, index + 2))
            index += 2
            continue
        tag = tag_pattern.match(value, index)
        if tag:
            index = tag.end()
            continue
        if value.startswith("**", index):
            index += 2
            continue
        if value[index] == "*":
            index += 1
            continue
        entity = entity_pattern.match(value, index)
        if entity:
            visible.append(html_stdlib.unescape(entity.group(0)))
            spans.append((index, entity.end()))
            index = entity.end()
            continue
        visible.append(value[index])
        spans.append((index, index + 1))
        index += 1
    return "".join(visible), spans


def _escape_decoded_markdown_text(value: str) -> str:
    """Escape literal replacement text for the decoder's rich-text dialect."""

    return (
        value.replace("\\", "\\\\")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("*", "\\*")
        .replace("_", "\\_")
    )


def _remove_empty_decoded_markdown_markup(value: str) -> str:
    """Remove decoder markup whose complete visible payload was deleted.

    Exact reviewed text repairs may remove a molecular-drawing label that the
    PDF decoder happened to style as one bold token.  Editing the projected
    characters then correctly removes the label but can leave an empty ``****``
    or ``<strong></strong>`` pair.  Such empty markup has no source-visible
    content and must not reach the safe-HTML renderer.
    """

    previous = None
    while previous != value:
        previous = value
        value = re.sub(
            r"<(strong|em|sup|sub)>(\s*)</\1>",
            r"\2",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(r"\*\*(\s*)\*\*", r"\1", value)
    return value


def _apply_decoded_line_text_replacements(
    plain_text: str,
    markdown: str,
    replacements: list[dict[str, Any]],
    *,
    count_hits: bool,
) -> tuple[str, str]:
    """Apply exact reviewed repairs to both views of one decoded PDF line.

    A literal can be contiguous in visible text while a style delimiter splits
    it in Markdown (for example ``**Figure SI 2a**)achieves``).  A direct
    ``str.replace`` therefore used to record a successful plain-text repair but
    leave the rich view unchanged; downstream block construction then restored
    the defective text.  This helper edits visible Markdown tokens and fails
    closed if its projection ever diverges from the paired plain text.
    """

    for spec in replacements:
        old = spec["old"]
        new = spec["new"]
        hits = plain_text.count(old)
        if count_hits:
            spec["hits"] += hits
        starts: list[int] = []
        cursor = 0
        while True:
            start = plain_text.find(old, cursor)
            if start < 0:
                break
            starts.append(start)
            cursor = start + len(old)

        prefix = 0
        while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
            prefix += 1
        suffix = 0
        while (
            suffix < len(old) - prefix
            and suffix < len(new) - prefix
            and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]
        ):
            suffix += 1
        old_middle_end = len(old) - suffix
        new_middle_end = len(new) - suffix
        replacement_middle = _escape_decoded_markdown_text(
            new[prefix:new_middle_end]
        )

        for start in reversed(starts):
            projected, spans = _decoded_markdown_projection(markdown)
            if projected != plain_text:
                raise ValueError(
                    "decoded supplement Markdown does not project to its paired plain text"
                )
            edit_start = start + prefix
            edit_end = start + old_middle_end
            if edit_start == edit_end:
                if edit_start == 0:
                    source_index = spans[0][0] if spans else 0
                else:
                    source_index = spans[edit_start - 1][1]
                markdown = (
                    markdown[:source_index]
                    + replacement_middle
                    + markdown[source_index:]
                )
            else:
                token_spans = spans[edit_start:edit_end]
                for token_index in range(len(token_spans) - 1, -1, -1):
                    source_start, source_end = token_spans[token_index]
                    token_replacement = replacement_middle if token_index == 0 else ""
                    markdown = (
                        markdown[:source_start]
                        + token_replacement
                        + markdown[source_end:]
                    )
            markdown = _remove_empty_decoded_markdown_markup(markdown)
            plain_text = plain_text[:start] + new + plain_text[start + len(old) :]
            projected, _ = _decoded_markdown_projection(markdown)
            if projected != plain_text:
                raise ValueError(
                    "decoded supplement text replacement could not preserve rich-text parity"
                )
    return plain_text, markdown


def _verify_supplement_text_replacements(
    replacements: list[dict[str, Any]],
) -> None:
    for index, spec in enumerate(replacements, 1):
        if spec["hits"] != spec["expected_hits"]:
            raise ValueError(
                f"supplement text replacement {index} matched {spec['hits']} times; "
                f"expected {spec['expected_hits']}"
            )


def _supplement_block_text_replacements(
    config: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Validate exact replacements applied after PDF lines become blocks.

    Some layout-only spaces do not exist in any single decoded line; they are
    introduced when adjacent native lines are joined into one paragraph.  This
    separate, hash-pinned stage repairs only those reviewed block-level joins
    and requires identical literal hits in plain and rich text.
    """

    raw = (config or {}).get("block_text_replacements", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("supplement block_text_replacements must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"supplement block text replacement {index} must be a mapping"
            )
        old = str(item.get("old", ""))
        new = str(item.get("new", ""))
        reason = str(item.get("reason", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        try:
            expected_hits = int(item.get("expected_hits", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"supplement block text replacement {index} expected_hits must be an integer"
            ) from exc
        if not old or old == new:
            raise ValueError(
                f"supplement block text replacement {index} requires distinct old/new text"
            )
        if old in seen:
            raise ValueError(
                f"duplicate supplement block text replacement key: {old!r}"
            )
        if expected_hits < 1 or not reason or not evidence:
            raise ValueError(
                f"supplement block text replacement {index} requires positive expected_hits, reason, and evidence"
            )
        seen.add(old)
        result.append(
            {
                "old": old,
                "new": new,
                "expected_hits": expected_hits,
                "reason": reason,
                "evidence": evidence,
                "hits": 0,
            }
        )
    return result


def _apply_supplement_block_text_replacements(
    blocks: list[ContentBlock],
    replacements: list[dict[str, Any]],
) -> list[ContentBlock]:
    """Apply reviewed paragraph-join repairs across optional rich-text spans.

    A print line-wrap can split one visible word at a native style boundary
    (for example ``carbox-amido`` where ``amido`` is subscripted).  The decoded
    rich view therefore need not contain the plain-text literal contiguously.
    Reuse the projection-aware line repair so the reviewed visible-text edit
    remains identical in both representations without discarding the markup.
    """

    result: list[ContentBlock] = []
    for block in blocks:
        plain_text = block.plain_text
        markdown = block.markdown
        for index, spec in enumerate(replacements, 1):
            plain_hits = plain_text.count(spec["old"])
            if plain_hits:
                plain_text, markdown = _apply_decoded_line_text_replacements(
                    plain_text,
                    markdown,
                    [spec],
                    count_hits=False,
                )
                spec["hits"] += plain_hits
        result.append(replace(block, plain_text=plain_text, markdown=markdown))
    for index, spec in enumerate(replacements, 1):
        if spec["hits"] != spec["expected_hits"]:
            raise ValueError(
                f"supplement block text replacement {index} matched "
                f"{spec['hits']} times; expected {spec['expected_hits']}"
            )
    return result


def _supplement_text_repair_rows(
    config: Mapping[str, Any] | None,
    source: SourceFile,
    supplement_id: str,
) -> list[dict[str, Any]]:
    """Bind successful exact PDF text replacements to private diagnostics.

    ``_pdf_blocks`` has already applied and hit-count verified these specs.  A
    second validation pass here is intentionally side-effect free and keeps
    the diagnostic representation coupled to the same strict parser rather
    than trusting arbitrary override mappings.
    """

    replacements = _supplement_text_replacements(config)
    rows = [
        {
            "schema_version": "1.0",
            "repair_id": f"{supplement_id}-text-replacement-{index:03d}",
            "mode": "exact_literal",
            "pattern": spec["old"],
            "replacement": spec["new"],
            "occurrences": spec["expected_hits"],
            "reason": spec["reason"],
            "evidence": spec["evidence"],
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "source_locator": "native-text;exact-literal-replacement",
        }
        for index, spec in enumerate(replacements, 1)
    ]
    block_replacements = _supplement_block_text_replacements(config)
    rows.extend(
        {
            "schema_version": "1.0",
            "repair_id": f"{supplement_id}-block-text-replacement-{index:03d}",
            "mode": "exact_literal_post_block",
            "pattern": spec["old"],
            "replacement": spec["new"],
            "occurrences": spec["expected_hits"],
            "reason": spec["reason"],
            "evidence": spec["evidence"],
            "source_path": source.relative_path,
            "source_sha256": source.sha256,
            "source_locator": "native-text;joined-block;exact-literal-replacement",
        }
        for index, spec in enumerate(block_replacements, 1)
    )
    return rows


def _literal_pdf_line_markup(value: str) -> str:
    value = html_stdlib.escape(value, quote=False)
    return value.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")


def _pypdf_native_blocks(
    source: SourceFile,
    supplement_id: str,
    path: Path,
    replacements: list[dict[str, Any]],
    reviewed_figure_regions: Mapping[
        int, list[tuple[float, float, float, float]]
    ] | None = None,
) -> tuple[list[ContentBlock], list[dict[str, Any]]]:
    """Read a malformed-but-recoverable native PDF through pypdf.

    This explicit, source-hash-gated fallback is intended for PDFs whose root
    catalog is recoverable by pypdf but rejected by pdfminer/pdfplumber.  It
    emits each native visual line separately because no trustworthy geometry
    is available; it never performs OCR.
    """

    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency checks own this
        raise RuntimeError("pypdf is required for supplement PDF fallback") from exc
    try:
        pages = PdfReader(path).pages
    except Exception as exc:
        raise ValueError(f"pypdf could not read supplement PDF: {exc}") from exc

    blocks: list[ContentBlock] = []
    warnings: list[dict[str, Any]] = []
    for page_number, page in enumerate(pages, 1):
        try:
            raw_text = page.extract_text() or ""
        except Exception as exc:
            raise ValueError(
                f"pypdf could not extract native text from page {page_number}: {exc}"
            ) from exc
        page_lines = []
        for native_line_number, raw_line in enumerate(raw_text.splitlines(), 1):
            # U+F0B7 is Word's private-use Symbol-font bullet. This is an exact
            # Unicode compatibility decoding, not an inferred textual repair.
            value = unicodedata.normalize("NFC", raw_line).replace("\uf0b7", "•").strip()
            value = _apply_supplement_text_replacements(
                value, replacements, count_hits=True
            )
            if value:
                page_lines.append((native_line_number, value))
        if not page_lines and not (reviewed_figure_regions or {}).get(page_number):
            warnings.append(
                _warning(
                    "supplement_pdf_page_native_text_empty",
                    f"PDF page {page_number} has no native text; no OCR was performed",
                    source,
                    supplement_id,
                    page=page_number,
                )
            )
            continue
        for output_line_number, (native_line_number, value) in enumerate(
            page_lines, 1
        ):
            blocks.append(
                ContentBlock(
                    block_id=(
                        f"{supplement_id}-page-{page_number:03d}-"
                        f"block-{output_line_number:03d}"
                    ),
                    kind="text",
                    markdown=_literal_pdf_line_markup(value),
                    plain_text=value,
                    source_path=source.relative_path,
                    source_locator=(
                        f"page={page_number};pypdf-native-line={native_line_number}"
                    ),
                )
            )
    return blocks, warnings


def _supplement_glyph_overrides(
    config: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    raw = config.get("glyph_overrides", [])
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ValueError("supplement glyph_overrides must be a list")
    result: dict[str, dict[str, str]] = {}
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise ValueError(f"supplement glyph override {index} must be a mapping")
        font = str(item.get("font", "")).strip()
        value = str(item.get("value", ""))
        raw_code = item.get("code")
        if isinstance(raw_code, bool):
            raw_code = None
        if isinstance(raw_code, int) and raw_code >= 0:
            code = f"RAW:{raw_code}"
        elif isinstance(raw_code, str) and re.fullmatch(
            r"(?:(?:RAW:|C)?\d+|U\+[0-9A-F]{4,6})",
            raw_code.strip(),
            flags=re.IGNORECASE,
        ):
            normalized = raw_code.strip().upper()
            code = (
                normalized
                if ":" in normalized
                or normalized.startswith(("C", "U+"))
                else f"C{normalized}"
            )
        else:
            code = ""
        if not font or not code or not value or "�" in value:
            raise ValueError(f"supplement glyph override {index} is incomplete")
        bucket = result.setdefault(font, {})
        if code in bucket:
            raise ValueError(
                f"duplicate supplement glyph override for {font} {code}"
            )
        bucket[code] = value
    return result


def _supplement_line_exclusions(
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Validate hash-pinned native-line furniture exclusions.

    Supporting-information manuscripts sometimes print editable line numbers
    in a narrow gutter.  The exclusion remains deliberately source-specific:
    it must name pages, an exact geometry box, a full-line regular expression,
    and reviewed reason/evidence.  Each configured exclusion must match at
    least one native line during extraction or the supplement fails closed.
    """

    raw = config.get("line_exclusions", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("supplement line_exclusions must be a list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise ValueError(f"supplement line exclusion {index} must be a mapping")
        raw_pages = item.get("pages")
        if raw_pages == "all":
            pages: str | set[int] = "all"
        elif (
            isinstance(raw_pages, list)
            and raw_pages
            and all(
                isinstance(page, int) and not isinstance(page, bool) and page >= 1
                for page in raw_pages
            )
        ):
            pages = set(raw_pages)
            if len(pages) != len(raw_pages):
                raise ValueError(
                    f"supplement line exclusion {index} has duplicate pages"
                )
        else:
            raise ValueError(
                f"supplement line exclusion {index} pages must be 'all' or positive integers"
            )
        raw_box = item.get("box")
        if (
            not isinstance(raw_box, list)
            or len(raw_box) != 4
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw_box)
        ):
            raise ValueError(f"supplement line exclusion {index} box is invalid")
        box = tuple(float(value) for value in raw_box)
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError(f"supplement line exclusion {index} box is empty")
        pattern = str(item.get("pattern", ""))
        reason = str(item.get("reason", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if not pattern or not reason or not evidence:
            raise ValueError(f"supplement line exclusion {index} is incomplete")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ValueError(
                f"supplement line exclusion {index} pattern is invalid"
            ) from exc
        result.append(
            {
                "pages": pages,
                "box": box,
                "pattern": compiled,
                "reason": reason,
                "evidence": evidence,
                "hits": 0,
            }
        )
    return result


def _apply_supplement_line_exclusions(
    lines: list[_PdfLine],
    page_number: int,
    exclusions: list[dict[str, Any]],
) -> list[_PdfLine]:
    retained: list[_PdfLine] = []
    for line in lines:
        excluded = False
        for spec in exclusions:
            pages = spec["pages"]
            if pages != "all" and page_number not in pages:
                continue
            left, top, right, bottom = spec["box"]
            if not (
                line.x0 >= left
                and line.x1 <= right
                and line.top >= top
                and line.bottom <= bottom
            ):
                continue
            if spec["pattern"].fullmatch(line.text.strip()) is None:
                continue
            spec["hits"] += 1
            excluded = True
            break
        if not excluded:
            retained.append(line)
    return retained


def _supplement_page_continuations(
    config: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Validate exact reviewed paragraph continuations across PDF pages."""

    raw = config.get("page_continuations", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("supplement page_continuations must be a list")
    locator_pattern = re.compile(r"page=\d+;(?:native-lines=\d+-\d+|reviewed-block=[A-Za-z0-9._-]+)")
    result: list[dict[str, str]] = []
    seen_from: set[str] = set()
    seen_to: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"supplement page continuation {index} must be a mapping"
            )
        from_locator = str(item.get("from_locator", "")).strip()
        to_locator = str(item.get("to_locator", "")).strip()
        reason = str(item.get("reason", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if (
            locator_pattern.fullmatch(from_locator) is None
            or locator_pattern.fullmatch(to_locator) is None
            or not reason
            or not evidence
            or from_locator in seen_from
            or to_locator in seen_to
        ):
            raise ValueError(
                f"supplement page continuation {index} is incomplete or duplicated"
            )
        from_page = int(from_locator.split(";", 1)[0].split("=", 1)[1])
        to_page = int(to_locator.split(";", 1)[0].split("=", 1)[1])
        if to_page != from_page + 1:
            raise ValueError(
                f"supplement page continuation {index} does not cross adjacent pages"
            )
        seen_from.add(from_locator)
        seen_to.add(to_locator)
        result.append(
            {
                "from_locator": from_locator,
                "to_locator": to_locator,
                "reason": reason,
                "evidence": evidence,
            }
        )
    return result


def _apply_supplement_page_continuations(
    blocks: list[ContentBlock],
    continuations: list[dict[str, str]],
) -> list[ContentBlock]:
    """Merge only exact, adjacent text blocks reviewed as one paragraph."""

    if not continuations:
        return blocks
    locator_indices: dict[str, list[int]] = {}
    for index, block in enumerate(blocks):
        locator_indices.setdefault(block.source_locator, []).append(index)
    pairs: set[tuple[int, int]] = set()
    for spec_index, spec in enumerate(continuations, 1):
        from_indices = locator_indices.get(spec["from_locator"], [])
        to_indices = locator_indices.get(spec["to_locator"], [])
        if len(from_indices) != 1 or len(to_indices) != 1:
            raise ValueError(
                f"supplement page continuation {spec_index} locator is absent or ambiguous"
            )
        before_index = from_indices[0]
        after_index = to_indices[0]
        if after_index != before_index + 1:
            raise ValueError(
                f"supplement page continuation {spec_index} blocks are not adjacent"
            )
        before = blocks[before_index]
        after = blocks[after_index]
        if (
            before.kind != "text"
            or after.kind != "text"
            or before.source_path != after.source_path
        ):
            raise ValueError(
                f"supplement page continuation {spec_index} does not join matching text blocks"
            )
        pairs.add((before_index, after_index))

    retained: list[ContentBlock] = []
    index = 0
    while index < len(blocks):
        group = [blocks[index]]
        while (index, index + 1) in pairs:
            index += 1
            group.append(blocks[index])
        first = group[0]
        if len(group) == 1:
            retained.append(first)
            index += 1
            continue
        plain = group[0].plain_text
        markdown = group[0].markdown
        for following in group[1:]:
            separator = "" if plain.endswith(("-", "‐", "‑")) else " "
            plain = _normalize_space(plain + separator + following.plain_text)
            markdown = _normalize_space(markdown + separator + following.markdown)
        retained.append(
            ContentBlock(
                block_id=first.block_id,
                kind=first.kind,
                markdown=markdown,
                plain_text=plain,
                source_path=first.source_path,
                source_locator=(
                    "|".join(block.source_locator for block in group)
                    + ";reviewed-page-continuation"
                ),
                source_geometry=[
                    geometry
                    for block in group
                    for geometry in block.source_geometry
                ],
            )
        )
        index += 1
    return retained


def _supplement_caption_overrides(
    config: Mapping[str, Any],
) -> dict[tuple[str, str], dict[str, str]]:
    """Validate source-reviewed replacements for split PDF captions.

    Older supplementary PDFs can divide one authored caption across a page
    boundary.  The native extractor intentionally does not guess that a line
    on the next page belongs to the previous caption, so a hash-pinned source
    configuration may provide the reviewed complete text instead.
    """

    raw = config.get("caption_overrides", [])
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ValueError("supplement caption_overrides must be a list")
    result: dict[tuple[str, str], dict[str, str]] = {}
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise ValueError(f"supplement caption override {index} must be a mapping")
        kind = str(item.get("kind", "")).strip().casefold()
        number = str(item.get("number", "")).strip()
        plain = _normalize_space(str(item.get("plain_text", "")))
        markdown = _normalize_space(str(item.get("markdown", plain)))
        locator = str(item.get("source_locator", "")).strip()
        if (
            kind not in {"figure", "scheme"}
            or not number
            or not plain
            or not markdown
            or not locator
        ):
            raise ValueError(f"supplement caption override {index} is incomplete")
        key = (kind, number.casefold())
        if key in result:
            raise ValueError(
                f"duplicate supplement caption override for {kind} {number}"
            )
        result[key] = {
            "plain_text": plain,
            "markdown": markdown,
            "source_locator": locator,
        }
    return result


def _supplement_equation_overrides(
    config: Mapping[str, Any],
) -> dict[int, list[dict[str, Any]]]:
    """Validate hash-pinned display equations reviewed against a PDF page."""

    raw = config.get("equation_overrides", [])
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ValueError("supplement equation_overrides must be a list")
    result: dict[int, list[dict[str, Any]]] = {}
    seen_locators: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise ValueError(f"supplement equation override {index} must be a mapping")
        page = item.get("page")
        after_line = item.get("after_line")
        plain = _normalize_space(str(item.get("plain_text", "")))
        markdown = _normalize_space(str(item.get("markdown", plain)))
        locator = str(item.get("source_locator", "")).strip()
        reason = str(item.get("reason", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        raw_replace = item.get("replace_lines")
        replace_lines: tuple[int, int] | None = None
        if raw_replace is not None:
            if (
                not isinstance(raw_replace, list)
                or len(raw_replace) != 2
                or any(isinstance(value, bool) for value in raw_replace)
                or any(not isinstance(value, int) for value in raw_replace)
                or raw_replace[0] < 1
                or raw_replace[0] > raw_replace[1]
            ):
                raise ValueError(
                    f"supplement equation override {index} replace_lines is invalid"
                )
            replace_lines = (raw_replace[0], raw_replace[1])
        raw_box = item.get("box")
        box: list[float] | None = None
        if raw_box is not None:
            if (
                not isinstance(raw_box, list)
                or len(raw_box) != 4
                or any(isinstance(value, bool) for value in raw_box)
            ):
                raise ValueError(
                    f"supplement equation override {index} box is invalid"
                )
            try:
                box = [float(value) for value in raw_box]
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"supplement equation override {index} box is invalid"
                ) from exc
            if box[0] >= box[2] or box[1] >= box[3]:
                raise ValueError(
                    f"supplement equation override {index} box is empty"
                )
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or isinstance(after_line, bool)
            or not isinstance(after_line, int)
            or after_line < 0
            or not plain
            or not markdown
            or not locator
            or not reason
            or not evidence
            or locator in seen_locators
        ):
            raise ValueError(f"supplement equation override {index} is incomplete")
        seen_locators.add(locator)
        result.setdefault(page, []).append(
            {
                "after_line": after_line,
                "plain_text": plain,
                "markdown": markdown,
                "source_locator": locator,
                "replace_lines": replace_lines,
                "box": box,
            }
        )
    for page, items in result.items():
        items.sort(key=lambda value: int(value["after_line"]))
        replaced: set[int] = set()
        for item in items:
            replace_lines = item["replace_lines"]
            if replace_lines is None:
                continue
            current = set(range(replace_lines[0], replace_lines[1] + 1))
            if replaced & current:
                raise ValueError(
                    f"supplement equation overrides overlap on page {page}"
                )
            replaced.update(current)
    return result


def _supplement_paragraph_overrides(
    config: Mapping[str, Any],
) -> dict[tuple[int, int, int], dict[str, Any]]:
    """Validate exact native-line replacements for source-reviewed PDF prose."""

    raw = config.get("paragraph_overrides", [])
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ValueError("supplement paragraph_overrides must be a list")
    result: dict[tuple[int, int, int], dict[str, str]] = {}
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise ValueError(f"supplement paragraph override {index} must be a mapping")
        page = item.get("page")
        first_line = item.get("first_line")
        last_line = item.get("last_line")
        mode = str(item.get("mode", "reviewed")).strip().casefold()
        block_kind = str(item.get("block_kind", "")).strip().casefold()
        plain = _normalize_space(str(item.get("plain_text", "")))
        markdown = _normalize_space(str(item.get("markdown", plain)))
        locator = str(item.get("source_locator", "")).strip()
        reason = str(item.get("reason", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        excluded_lines_raw = item.get("excluded_lines", [])
        excluded_lines = (
            list(excluded_lines_raw)
            if isinstance(excluded_lines_raw, (list, tuple))
            else []
        )
        if (
            any(isinstance(value, bool) for value in (page, first_line, last_line))
            or any(not isinstance(value, int) for value in (page, first_line, last_line))
            or page < 1
            or first_line < 1
            or first_line > last_line
            or mode not in {"reviewed", "source"}
            or block_kind not in {"", "subsection_heading", "text", "list"}
            or (mode == "reviewed" and (not plain or not markdown))
            or (mode == "source" and (plain or markdown))
            or not locator
            or not reason
            or not evidence
            or not isinstance(excluded_lines_raw, (list, tuple))
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in excluded_lines
            )
            or len(set(excluded_lines)) != len(excluded_lines)
            or any(
                value <= first_line or value >= last_line
                for value in excluded_lines
            )
        ):
            raise ValueError(f"supplement paragraph override {index} is incomplete")
        key = (page, first_line, last_line)
        if key in result:
            raise ValueError(
                f"duplicate supplement paragraph override for page {page} "
                f"lines {first_line}-{last_line}"
            )
        result[key] = {
            "plain_text": plain,
            "markdown": markdown,
            "source_locator": locator,
            "use_source_text": mode == "source",
            "block_kind": block_kind,
            "excluded_lines": sorted(excluded_lines),
        }
    return result


def _should_warn_supplement_page_native_text_empty(
    *,
    had_native_content_lines: bool,
    retained_lines: Iterable[_PdfLine],
    has_graphic_content: bool = True,
) -> bool:
    """Distinguish an empty text layer from fully reviewed exclusions.

    Hash-pinned line and table exclusions can intentionally consume every
    content line on a page after that content has been preserved in structured
    semantics. Such a page did have native text and must not be reported as
    image-only or OCR-dependent.
    """

    return (
        not had_native_content_lines
        and not any(retained_lines)
        and has_graphic_content
    )


def _supplement_reviewed_page_blocks(
    config: Mapping[str, Any] | None,
    source: SourceFile,
    supplement_id: str,
) -> dict[int, list[ContentBlock]]:
    """Build source-reviewed text blocks for PDF pages without usable text.

    This is a hash-gated transcription escape hatch for document-text pages
    such as flattened publisher forms.  It is deliberately separate from
    figure crops and OCR: the reviewed override supplies the final text, an
    exact page locator, and private reason/evidence.  Callers replace all
    unreliable native blocks on each declared page with these blocks.
    """

    if config is None:
        return {}
    raw = config.get("reviewed_page_blocks", [])
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ValueError("supplement reviewed_page_blocks must be a list")
    result: dict[int, list[ContentBlock]] = {}
    seen_ids: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"supplement reviewed page block {index} must be a mapping"
            )
        page = item.get("page")
        kind = str(item.get("block_kind", "text")).strip().casefold()
        plain = _normalize_space(str(item.get("plain_text", "")))
        markdown = _normalize_space(str(item.get("markdown", plain)))
        locator = str(item.get("source_locator", "")).strip()
        reason = str(item.get("reason", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        raw_block_id = str(item.get("block_id", "")).strip()
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or (source.page_count is not None and page > source.page_count)
            or kind not in {"text", "subsection_heading", "list", "equation", "figure_caption", "scheme_caption"}
            or not plain
            or not markdown
            or not locator
            or re.search(rf"(?:^|;)page={page}(?:;|$)", locator) is None
            or not reason
            or not evidence
        ):
            raise ValueError(
                f"supplement reviewed page block {index} is incomplete"
            )
        page_position = len(result.get(page, [])) + 1
        block_id = raw_block_id or (
            f"{supplement_id}-page-{page:03d}-reviewed-{page_position:03d}"
        )
        if block_id in seen_ids:
            raise ValueError(
                f"duplicate supplement reviewed page block id {block_id!r}"
            )
        seen_ids.add(block_id)
        result.setdefault(page, []).append(
            ContentBlock(
                block_id=block_id,
                kind=kind,
                markdown=markdown,
                plain_text=plain,
                source_path=source.relative_path,
                source_locator=locator,
            )
        )
    return result


def _block_source_page(block: ContentBlock) -> int | None:
    match = re.search(r"(?:^|;)page=(\d+)(?:;|$)", block.source_locator)
    return int(match.group(1)) if match else None


def _apply_supplement_reviewed_page_blocks(
    blocks: list[ContentBlock],
    reviewed: Mapping[int, list[ContentBlock]],
) -> list[ContentBlock]:
    """Replace complete reviewed pages while retaining other page order."""

    if not reviewed:
        return blocks
    retained = [
        block for block in blocks if _block_source_page(block) not in reviewed
    ]
    combined = [*retained, *(block for page in reviewed.values() for block in page)]
    original_order = {id(block): index for index, block in enumerate(combined)}
    return sorted(
        combined,
        key=lambda block: (
            _block_source_page(block)
            if _block_source_page(block) is not None
            else 10**9,
            original_order[id(block)],
        ),
    )


def _resolved_reviewed_page_text_warnings(
    warnings: Iterable[dict[str, Any]],
    reviewed_pages: Iterable[int],
    *,
    page_count: int | None,
) -> list[dict[str, Any]]:
    handled = set(reviewed_pages)
    complete = page_count is not None and handled == set(range(1, page_count + 1))
    result: list[dict[str, Any]] = []
    for warning in warnings:
        code = warning.get("code")
        page = warning.get("page")
        if code in {
            "image_only_page",
            "supplement_pdf_page_native_text_empty",
        } and page in handled:
            continue
        if complete and code in {
            "supplement_pdf_native_text_empty",
            "supplement_pdf_native_text_extraction_failed",
        }:
            continue
        result.append(warning)
    return result


def _supplement_table_overrides(
    config: Mapping[str, Any] | None,
    source: SourceFile,
    supplement_id: str,
) -> tuple[list[TableItem], set[int]]:
    """Build hash-pinned semantic tables reviewed against supplement PDFs."""

    if config is None:
        return [], set()
    raw = config.get("table_overrides", [])
    if raw is None:
        return [], set()
    if not isinstance(raw, list):
        raise ValueError("supplement table_overrides must be a list")

    tables: list[TableItem] = []
    pages: set[int] = set()
    seen_ids: set[str] = set()
    for table_index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"supplement table override {table_index} must be a mapping"
            )
        number = str(item.get("number", "")).strip()
        label = _normalize_space(str(item.get("label", f"Table {number}")))
        page = item.get("page")
        title_plain = _normalize_space(str(item.get("title_plain", "")))
        title_markdown = _normalize_space(
            str(item.get("title_markdown", title_plain))
        )
        source_locator = str(item.get("source_locator", "")).strip()
        source_kind = str(item.get("source_kind", "")).strip().casefold()
        reason = str(item.get("reason", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if (
            not number
            or not label
            or isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or not title_plain
            or not title_markdown
            or not source_locator
            or source_kind not in {"document", "image", "pdf"}
            or not reason
            or not evidence
        ):
            raise ValueError(
                f"supplement table override {table_index} is incomplete"
            )

        table_id = f"{supplement_id}_table_{_safe_identifier(number)}"
        if table_id in seen_ids:
            raise ValueError(f"duplicate supplement table override {table_id}")
        seen_ids.add(table_id)
        pages.add(page)

        raw_parts = item.get("parts")
        if not isinstance(raw_parts, list) or not raw_parts:
            raise ValueError(
                f"supplement table override {table_index} needs nonempty parts"
            )
        parts: list[TablePart] = []
        for part_index, raw_part in enumerate(raw_parts, 1):
            if not isinstance(raw_part, Mapping):
                raise ValueError(
                    f"supplement table override {table_index} part {part_index} "
                    "must be a mapping"
                )
            raw_rows = raw_part.get("rows")
            if not isinstance(raw_rows, list) or not raw_rows:
                raise ValueError(
                    f"supplement table override {table_index} part {part_index} "
                    "needs nonempty rows"
                )
            rows: list[list[TableCell]] = []
            for row_index, raw_row in enumerate(raw_rows, 1):
                if not isinstance(raw_row, list) or not raw_row:
                    raise ValueError(
                        f"supplement table override {table_index} part {part_index} "
                        f"row {row_index} must be a nonempty list"
                    )
                row: list[TableCell] = []
                for cell_index, raw_cell in enumerate(raw_row, 1):
                    if not isinstance(raw_cell, Mapping):
                        raise ValueError(
                            f"supplement table override {table_index} part {part_index} "
                            f"row {row_index} cell {cell_index} must be a mapping"
                        )
                    text = _normalize_space(str(raw_cell.get("text", "")))
                    markdown = (
                        _normalize_space(str(raw_cell.get("markdown", "")))
                        if "markdown" in raw_cell
                        else _escape_literal_pdf_markup(text)
                    )
                    header = raw_cell.get("header", False)
                    rowspan = raw_cell.get("rowspan", 1)
                    colspan = raw_cell.get("colspan", 1)
                    if (
                        not isinstance(header, bool)
                        or isinstance(rowspan, bool)
                        or not isinstance(rowspan, int)
                        or rowspan < 1
                        or isinstance(colspan, bool)
                        or not isinstance(colspan, int)
                        or colspan < 1
                    ):
                        raise ValueError(
                            f"supplement table override {table_index} part {part_index} "
                            f"row {row_index} cell {cell_index} has invalid cell metadata"
                        )
                    row.append(
                        TableCell(
                            text=text,
                            markdown=markdown,
                            header=header,
                            rowspan=rowspan,
                            colspan=colspan,
                        )
                    )
                rows.append(row)
            part_id = str(raw_part.get("part_id", "")).strip()
            parts.append(
                TablePart(
                    part_id=part_id or f"{table_id}_part_{part_index:03d}",
                    rows=rows,
                )
            )

        raw_footnotes = item.get("footnotes", [])
        if not isinstance(raw_footnotes, list):
            raise ValueError(
                f"supplement table override {table_index} footnotes must be a list"
            )
        footnotes_plain: list[str] = []
        footnotes_markdown: list[str] = []
        for footnote_index, raw_footnote in enumerate(raw_footnotes, 1):
            if not isinstance(raw_footnote, Mapping):
                raise ValueError(
                    f"supplement table override {table_index} footnote "
                    f"{footnote_index} must be a mapping"
                )
            plain = _normalize_space(str(raw_footnote.get("plain_text", "")))
            markdown = _normalize_space(
                str(raw_footnote.get("markdown", plain))
            )
            if not plain or not markdown:
                raise ValueError(
                    f"supplement table override {table_index} footnote "
                    f"{footnote_index} is incomplete"
                )
            footnotes_plain.append(plain)
            footnotes_markdown.append(markdown)

        tables.append(
            TableItem(
                table_id=table_id,
                source_id=label,
                label=label,
                title_markdown=title_markdown,
                title_plain=title_plain,
                parts=parts,
                footnotes_markdown=footnotes_markdown,
                footnotes_plain=footnotes_plain,
                source_path=source.relative_path,
                source_locator=source_locator,
                source_kind=source_kind,
            )
        )
    return tables, pages


def _supplement_table_text_regions(
    config: Mapping[str, Any] | None,
) -> dict[int, list[tuple[float, float, float, float]]]:
    """Return exact reviewed PDF table boxes that structured data replaces.

    A source-locator bbox is itself hash-pinned reviewed evidence.  Native text
    whose line center falls inside that box belongs to the structured table and
    must not be repeated as linear supplement prose.  Locators without a bbox
    retain the longstanding conservative behavior.
    """

    if config is None:
        return {}
    raw_tables = config.get("table_overrides", [])
    if raw_tables is None:
        return {}
    if not isinstance(raw_tables, list):
        raise ValueError("supplement table_overrides must be a list")
    regions: dict[int, list[tuple[float, float, float, float]]] = {}
    for table_index, item in enumerate(raw_tables, 1):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"supplement table override {table_index} must be a mapping"
            )
        locator = str(item.get("source_locator", "")).strip()
        bbox_match = re.search(
            r"(?:^|;)(?:table-)?bbox=([^;]+)", locator, flags=re.IGNORECASE
        )
        if bbox_match is None:
            continue
        page = item.get("page")
        locator_page_match = re.search(
            r"(?:^|;)page=(\d+)(?:;|$)", locator, flags=re.IGNORECASE
        )
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or locator_page_match is None
            or int(locator_page_match.group(1)) != page
        ):
            raise ValueError(
                f"supplement table override {table_index} bbox page is inconsistent"
            )
        raw_bbox = bbox_match.group(1).strip()
        if raw_bbox.startswith("[") and raw_bbox.endswith("]"):
            raw_bbox = raw_bbox[1:-1]
        raw_values = [value.strip() for value in raw_bbox.split(",")]
        try:
            bbox = tuple(float(value) for value in raw_values)
        except ValueError as exc:
            raise ValueError(
                f"supplement table override {table_index} bbox is invalid"
            ) from exc
        if (
            len(bbox) != 4
            or not all(math.isfinite(value) for value in bbox)
            or bbox[0] >= bbox[2]
            or bbox[1] >= bbox[3]
        ):
            raise ValueError(
                f"supplement table override {table_index} bbox is invalid"
            )
        regions.setdefault(page, []).append(bbox)
        continuations = item.get("continuation_regions", [])
        if not isinstance(continuations, list):
            raise ValueError("supplement table continuation_regions must be a list")
        for continuation in continuations:
            if not isinstance(continuation, Mapping):
                raise ValueError("supplement table continuation region must be a mapping")
            continuation_page = continuation.get("page")
            continuation_box = continuation.get("box")
            if (isinstance(continuation_page, bool) or not isinstance(continuation_page, int)
                    or continuation_page < 1 or not isinstance(continuation_box, list)
                    or len(continuation_box) != 4
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) for value in continuation_box)
                    or continuation_box[0] >= continuation_box[2]
                    or continuation_box[1] >= continuation_box[3]):
                raise ValueError("supplement table continuation region is invalid")
            regions.setdefault(continuation_page, []).append(tuple(continuation_box))
    return regions


def _supplement_unboxed_table_pages(
    config: Mapping[str, Any] | None,
    boxed_regions: Mapping[int, list[tuple[float, float, float, float]]],
) -> set[int]:
    """Return reviewed table pages lacking a precise line-exclusion box."""

    if config is None:
        return set()
    raw_tables = config.get("table_overrides", [])
    if raw_tables is None:
        return set()
    if not isinstance(raw_tables, list):
        raise ValueError("supplement table_overrides must be a list")
    pages: set[int] = set()
    for table_index, item in enumerate(raw_tables, 1):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"supplement table override {table_index} must be a mapping"
            )
        page = item.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError(
                f"supplement table override {table_index} page is invalid"
            )
        pages.add(page)
    return pages - set(boxed_regions)


def _exclude_reviewed_pdf_table_lines(
    lines: Iterable[_PdfLine],
    regions: Iterable[tuple[float, float, float, float]],
) -> list[_PdfLine]:
    """Remove native table text already represented by a reviewed table."""

    reviewed = list(regions)
    if not reviewed:
        return list(lines)
    retained: list[_PdfLine] = []
    for line in lines:
        center_x = (line.x0 + line.x1) / 2.0
        center_y = (line.top + line.bottom) / 2.0
        if any(
            left <= center_x <= right and top <= center_y <= bottom
            for left, top, right, bottom in reviewed
        ):
            continue
        retained.append(line)
    return retained


def _decoded_page_lines(
    page: Any,
    decoded_page: Any,
    reviewed_regions: Iterable[tuple[float, float, float, float]] = (),
    text_replacements: list[dict[str, Any]] | None = None,
) -> list[_PdfLine]:
    decoded_lines = list(decoded_page.lines)
    graphic_regions = _figure_graphic_regions(
        page,
        reviewed_regions,
        [tuple(float(value) for value in decoded.bbox) for decoded in decoded_lines],
    )
    lines: list[_PdfLine] = []
    for decoded in decoded_lines:
        x0, top, x1, bottom = (float(value) for value in decoded.bbox)
        line_box = (x0, top, x1, bottom)
        repaired_text, repaired_markdown = _apply_decoded_line_text_replacements(
            decoded.plain_text,
            decoded.markdown,
            text_replacements or [],
            count_hits=True,
        )
        lines.append(
            _PdfLine(
                text=repaired_text,
                markdown=repaired_markdown,
                line_number=len(lines) + 1,
                x0=x0,
                x1=x1,
                top=top,
                bottom=bottom,
                overlaps_figure_graphic=any(
                    # The 8 pt proximity used to assemble vector/raster
                    # graphics must not also consume adjacent authored prose.
                    # Text is graphic-only only when its own box intersects.
                    _boxes_touch(line_box, region, margin=0.0)
                    for region in graphic_regions
                ),
            )
        )
    return lines


def _is_supplement_page_number(line: _PdfLine, page_height: float) -> bool:
    return bool(
        re.fullmatch(r"(?:S(?:\s*[-–]\s*|\s*))?\d+", line.text.strip(), flags=re.IGNORECASE)
        and line.bottom >= page_height * 0.90
    )


def _pdf_caption_match(text: str) -> re.Match[str] | None:
    """Return a PDF caption match without promoting lowercase prose references.

    A wrapped experimental sentence can begin with an in-text reference such
    as ``figure S1 A,B) and MS(...)``.  The shared permissive caption pattern
    intentionally supports several publisher dialects, but bare PDF captions
    themselves begin with an uppercase label.  Prefixed labels such as
    ``supplementary figure 2`` remain case-insensitive.
    """

    match = CAPTION_PATTERN.match(text)
    if (
        match is not None
        and text[:1].islower()
        and re.match(
            r"^(?:supplemental|supplementary|supporting)\s+",
            text,
            flags=re.IGNORECASE,
        )
        is None
    ):
        return None
    return match


def _begins_semantic_block(text: str) -> bool:
    return bool(
        _pdf_caption_match(text)
        or FOOTNOTE_PATTERN.match(text)
        or re.match(r"\d+[.)]\s+\S", text)
        or re.match(
            r"(?:Reviewers?' comments|Reviewer\s*#?\s*\d+\b[^:]*|"
            r"Major concerns?|Minor|Other issues)\s*:",
            text,
            flags=re.IGNORECASE,
        )
    )


def _is_standalone_bold_line(markdown: str) -> bool:
    """Return whether every visible character in a PDF line is bold.

    Rich PDF text may express one bold heading as adjacent Markdown and HTML
    fragments (for example a bold title containing an italic variable).  This
    deliberately narrow recognizer only accepts complete bold spans separated
    by whitespace, so ordinary prose containing a bold phrase is unaffected.
    """

    remainder = markdown.strip()
    if not remainder:
        return False
    previous = None
    while previous != remainder:
        previous = remainder
        remainder = re.sub(r"\*\*.+?\*\*", "", remainder)
        remainder = re.sub(
            r"<strong(?:\s[^>]*)?>.*?</strong>",
            "",
            remainder,
            flags=re.IGNORECASE,
        )
    return not remainder.strip()


def _begins_bold_lead_paragraph(markdown: str) -> bool:
    """Recognize an authored paragraph introduced by a bold lead phrase."""

    value = markdown.strip()
    match = re.match(r"\*\*(?P<lead>[^*]+)\*\*[.:]?\s+\S", value)
    if match is None:
        match = re.match(
            r"<strong(?:\s[^>]*)?>(?P<lead>.+?)</strong>[.:]?\s+\S",
            value,
            flags=re.IGNORECASE,
        )
    if match is None:
        return False
    lead = html_stdlib.unescape(re.sub(r"<[^>]+>", "", match.group("lead")))
    # Compound names frequently begin with a digit or lowercase stereochemical
    # prefix.  A source-authored colon immediately after the complete bold
    # lead is an unambiguous paragraph boundary even when that lead is not
    # title-cased (for example ``**2,2,2-trichloro...:** Under nitrogen...``).
    if lead.rstrip().endswith(":") or re.match(
        r"\s*(?:\*\*[^*]+\*\*|<strong(?:\s[^>]*)?>.+?</strong>):",
        value,
        re.IGNORECASE,
    ):
        return True
    first_alpha = next((character for character in lead if character.isalpha()), "")
    return bool(first_alpha and first_alpha.isupper())


def _is_standalone_italic_line(markdown: str) -> bool:
    """Return whether every visible character in a PDF line is italic."""

    remainder = markdown.strip()
    if not remainder:
        return False
    previous = None
    while previous != remainder:
        previous = remainder
        remainder = re.sub(r"(?<!\*)\*[^*]+\*(?!\*)", "", remainder)
        remainder = re.sub(
            r"<em(?:\s[^>]*)?>.*?</em>",
            "",
            remainder,
            flags=re.IGNORECASE,
        )
    remainder = re.sub(r"</?(?:sub|sup)(?:\s[^>]*)?>", "", remainder)
    return not remainder.strip()


def _is_standalone_uppercase_heading(text: str) -> bool:
    """Recognize short multiword all-caps PDF headings without style data."""

    value = _normalize_space(text)
    if re.match(
        r"^(?:(?:tel|telephone|phone|fax|e-?mail)\b|"
        r"(?:url|subject|from|date|sent|to)\s*[:：])",
        value,
        flags=re.IGNORECASE,
    ):
        return False
    alphabetic = [character for character in value if character.isalpha()]
    word_count = len(re.findall(r"[^\W\d_]+", value, flags=re.UNICODE))
    return bool(
        len(value) <= 100
        and word_count >= 2
        and alphabetic
        and all(not character.islower() for character in alphabetic)
        and not value.endswith((".", ":", ";"))
    )


def _is_standalone_italic_heading_line(markdown: str) -> bool:
    """Limit italic-heading promotion to short lines without author markers."""

    if not _is_standalone_italic_line(markdown):
        return False
    visible = html_stdlib.unescape(re.sub(r"<[^>]+>", "", markdown))
    visible = re.sub(r"[*_]+", "", visible).strip()
    word_count = len(re.findall(r"[^\W\d_]+", visible, flags=re.UNICODE))
    return bool(
        len(visible) <= 120
        and word_count >= 2
        and re.search(r"<sup(?:\s|>)|\^\{", markdown, flags=re.IGNORECASE)
        is None
    )


def _heading_markup(markdown: str) -> str:
    """Remove the redundant outer bold notation from a detected heading."""

    result = markdown
    if _is_standalone_italic_line(result):
        result = re.sub(
            r"</?em(?:\s[^>]*)?>", "", result, flags=re.IGNORECASE
        )
        result = re.sub(r"(?<!\*)\*(?!\*)", "", result)
    return _normalize_space(
        re.sub(r"</?strong(?:\s[^>]*)?>", "", result, flags=re.IGNORECASE)
        .replace("**", "")
    )


def _is_standalone_heading_line(markdown: str) -> bool:
    """Recognize fully styled headings with an optional plain numeric prefix."""

    value = markdown.strip()
    if _is_standalone_bold_line(value) or _is_standalone_italic_heading_line(value):
        return True
    match = re.fullmatch(r"\d+(?:\.\d+)*\.?\s+(.+)", value)
    return bool(match and _is_standalone_bold_line(match.group(1)))


def _is_email_correspondence_page(lines: Iterable[_PdfLine]) -> bool:
    """Return whether a PDF page is visibly an email-message rendering.

    Email clients commonly bold sender/recipient fields, signatures, telephone
    lines, and security banners. Those styles are not document headings. Two
    distinct mail-header fields provide conservative page-level evidence that
    automatic heading recognition should be disabled while ordinary paragraph
    and rich-text extraction continues unchanged.
    """

    fields: set[str] = set()
    patterns = {
        "subject": r"(?:subject|件名)",
        "from": r"(?:from|差出人|送信者)",
        "date": r"(?:date|sent|送信日時|送信日)",
        "to": r"(?:to|宛先)",
        "cc": r"(?:cc|bcc|ＣＣ)",
    }
    for line in lines:
        value = _normalize_space(line.text)
        for field, label in patterns.items():
            if re.match(rf"^(?:{label})\s*[:：]", value, flags=re.IGNORECASE):
                fields.add(field)
    return len(fields) >= 2


def _is_pdf_heading(markdown: str, text: str) -> bool:
    """Recognize a styled PDF heading without consuming caption formatting."""

    return bool(
        _pdf_caption_match(text) is None
        and FOOTNOTE_PATTERN.match(text) is None
        and (
            _is_standalone_heading_line(markdown)
            or _is_standalone_uppercase_heading(text)
        )
    )


def _is_unreviewed_graphic_only_paragraph(
    paragraph: _PdfParagraph,
    *,
    is_caption: bool,
    is_footnote: bool,
    has_reviewed_override: bool,
) -> bool:
    """Return whether heuristic figure filtering may discard a paragraph.

    Exact, source-hash-pinned paragraph overrides are an explicit review
    decision and therefore take precedence over the conservative proximity
    heuristic used to remove text embedded in figure graphics.
    """

    return bool(
        not is_caption
        and not is_footnote
        and not has_reviewed_override
        and all(line.overlaps_figure_graphic for line in paragraph.lines)
    )


def _pdf_lines_share_alignment(
    first: _PdfLine, second: _PdfLine, *, tolerance: float = 3.0
) -> bool:
    """Return whether wrapped source lines share a left or centered alignment."""

    first_center = (first.x0 + first.x1) / 2.0
    second_center = (second.x0 + second.x1) / 2.0
    return bool(
        abs(first.x0 - second.x0) <= tolerance
        or abs(first_center - second_center) <= tolerance
    )


def _pdf_lines_share_visual_row(
    first: _PdfLine,
    second: _PdfLine,
    *,
    vertical_overlap_ratio: float = 0.70,
    maximum_horizontal_gap: float = 60.0,
) -> bool:
    """Recognize adjacent rich-text fragments on one rendered PDF row.

    Some PDFs position separately styled spans as independent native lines.
    Treating each span as a paragraph can split a centered compound name from
    its number or split its bold lead from the first prose word.  The geometry
    guard deliberately requires substantial vertical overlap, forward reading
    order, and a bounded horizontal gap so ordinary columns remain separate.
    """

    overlap = min(first.bottom, second.bottom) - max(first.top, second.top)
    shorter_height = min(first.bottom - first.top, second.bottom - second.top)
    horizontal_gap = second.x0 - first.x1
    return bool(
        shorter_height > 0
        and overlap >= shorter_height * vertical_overlap_ratio
        and horizontal_gap >= -3.0
        and horizontal_gap <= maximum_horizontal_gap
    )


def _supplement_affiliation_line_numbers(lines: list[_PdfLine]) -> set[int]:
    """Keep marked italic SI affiliations and their wrapped address lines."""

    if not any(re.fullmatch(r"(?:supporting|supplementary)\s+information",
                            _normalize_space(line.text), flags=re.IGNORECASE)
               for line in lines[:10]):
        return set()
    result: set[int] = set()
    previous: _PdfLine | None = None
    for line in lines:
        italic = _is_standalone_italic_line(line.markdown)
        marked_address = bool(
            italic and re.search(r"<sup(?:\s|>)", line.markdown, flags=re.IGNORECASE)
            and re.search(r"\b(?:school|department|university|institute|college|"
                          r"laboratory|division|centre|center|hospital)\b",
                          line.text, flags=re.IGNORECASE)
        )
        continuation = bool(
            italic and previous is not None and previous.line_number in result
            and 0 <= line.top - previous.bottom <= max(6.0, (previous.bottom - previous.top) * 0.8)
            and re.search(r"[.!?]\s*$", previous.text) is None
        )
        if marked_address or continuation:
            result.add(line.line_number)
        previous = line
    return result


def _paragraphs(
    lines: list[_PdfLine], *, recognize_headings: bool = True
) -> list[_PdfParagraph]:
    if not lines:
        return []
    affiliation_lines = _supplement_affiliation_line_numbers(lines)
    is_supplement_front_matter = any(
        re.fullmatch(
            r"(?:supporting|supplementary)\s+information",
            _normalize_space(line.text),
            flags=re.IGNORECASE,
        )
        for line in lines[:10]
    )
    ordinary_positive_gaps = [
        following.top - preceding.bottom
        for preceding, following in zip(lines, lines[1:])
        if (
            following.top > preceding.bottom
            and not preceding.overlaps_figure_graphic
            and not following.overlaps_figure_graphic
        )
    ]
    # Native line leading varies substantially across publishers, and a page
    # can mix tightly set table rows with double-spaced prose.  Cluster gaps
    # within one point and use the most frequently repeated leading.  When a
    # short page provides no repeated leading, its smallest gap is the only
    # conservative evidence of an ordinary wrapped line.  This avoids letting
    # a compact table fragment the surrounding authored prose while retaining
    # paragraph boundaries on short, single-spaced pages.
    gap_clusters: list[list[float]] = []
    for gap_value in sorted(ordinary_positive_gaps):
        if gap_clusters and abs(
            gap_value - (sum(gap_clusters[-1]) / len(gap_clusters[-1]))
        ) <= 1.0:
            gap_clusters[-1].append(gap_value)
        else:
            gap_clusters.append([gap_value])
    repeated_gap_clusters = [cluster for cluster in gap_clusters if len(cluster) >= 2]
    if repeated_gap_clusters:
        baseline_cluster = max(
            repeated_gap_clusters,
            key=lambda cluster: (len(cluster), sum(cluster) / len(cluster)),
        )
        baseline_gap = sum(baseline_cluster) / len(baseline_cluster)
    elif len(ordinary_positive_gaps) >= 2:
        baseline_gap = min(ordinary_positive_gaps)
    else:
        baseline_gap = 0.0
    result: list[_PdfParagraph] = []
    current = _PdfParagraph(lines=[lines[0]])
    for line in lines[1:]:
        previous = current.lines[-1]
        previous_height = max(1.0, previous.bottom - previous.top)
        gap = line.top - previous.bottom
        standalone_italic_after_caption = bool(
            _pdf_caption_match(current.text)
            and re.fullmatch(r"\*[^*]+\*", line.markdown)
        )
        current_is_caption = bool(_pdf_caption_match(current.text))
        current_is_heading = bool(
            recognize_headings
            and not any(member.line_number in affiliation_lines for member in current.lines)
            and
            (
                _is_standalone_heading_line(current.markdown)
                or _is_standalone_uppercase_heading(current.text)
            )
            and not current_is_caption
        )
        line_is_heading_candidate = bool(
            recognize_headings
            and line.line_number not in affiliation_lines
            and
            (
                _is_standalone_heading_line(line.markdown)
                or _is_standalone_uppercase_heading(line.text)
            )
        )
        line_is_numbered_heading = bool(
            line_is_heading_candidate
            and re.match(r"\d+(?:\.\d+)*\.?\s+\S", line.text)
        )
        line_is_heading = bool(line_is_heading_candidate and not current_is_caption)
        same_visual_row_fragment = _pdf_lines_share_visual_row(previous, line)
        current_visual_row = [
            member
            for member in current.lines
            if min(member.bottom, previous.bottom)
            - max(member.top, previous.top)
            >= min(
                member.bottom - member.top,
                previous.bottom - previous.top,
            )
            * 0.70
        ]
        current_row_x0 = min(member.x0 for member in current_visual_row)
        current_row_x1 = max(member.x1 for member in current_visual_row)
        current_row_center = (current_row_x0 + current_row_x1) / 2.0
        line_center = (line.x0 + line.x1) / 2.0
        wrapped_line_nearby = bool(
            gap
            <= max(
                8.0,
                previous_height * 1.25,
                baseline_gap + 1.0,
            )
        )
        wrapped_line_aligned = bool(
            abs(current_row_x0 - line.x0) <= 15.0
            or abs(current_row_center - line_center) <= 15.0
        )
        hyphen_wrapped_continuation = bool(
            previous.text.endswith(("-", "‐", "‑"))
            and wrapped_line_nearby
            and wrapped_line_aligned
        )
        wrapped_heading_continuation = bool(
            current_is_heading
            and not re.search(r"[.!?][\"'’)]?$", previous.text)
            and wrapped_line_nearby
            and wrapped_line_aligned
            and (
                previous.text.endswith(("-", "‐", "‑"))
                or (
                    line.text[:1].islower()
                    and re.match(r"^[a-z]\s", line.text) is None
                )
                or re.match(r"^\(\d+[A-Za-z]?\)\s*:", line.text)
            )
        )
        caption_to_complete_heading = bool(
            current_is_caption
            and line_is_heading_candidate
            and re.search(r"[.!?][\"'’)]?$", current.text)
        )
        continuation_caption_label = _pdf_caption_match(line.text)
        caption_reference_continuation = bool(
            current_is_caption
            and re.search(r"[.!?][\"'’)]?$", current.text) is None
            and continuation_caption_label is not None
            and not line.text[continuation_caption_label.end() :].strip()
            and re.search(
                r"\b(?:shown in|described in|related to|refer(?:red)? to)\s*$",
                current.text,
                flags=re.IGNORECASE,
            )
        )
        graphic_boundary = bool(
            line.overlaps_figure_graphic != previous.overlaps_figure_graphic
            and (
                not current_is_caption
                # A caption immediately above a graphic stops when the crop
                # begins. Conversely, a crop can conservatively include the
                # first caption line below a graphic; retain its continuation.
                or (
                    not previous.overlaps_figure_graphic
                    and line.overlaps_figure_graphic
                    and re.search(r"[.!?][\"'’)]?$", current.text) is not None
                )
            )
        )
        heading_boundary = bool(
            not (
                same_visual_row_fragment
                or hyphen_wrapped_continuation
                or wrapped_heading_continuation
            )
            and (
                current_is_heading != line_is_heading
                or (current_is_caption and line_is_numbered_heading)
                or caption_to_complete_heading
                or (
                    current_is_heading
                    and line_is_heading
                    and not previous.text.endswith(("-", "‐", "‑"))
                    and not (
                        line.text[:1].islower()
                        and _pdf_lines_share_alignment(previous, line)
                    )
                )
            )
        )
        force_boundary = bool(
            not (
                same_visual_row_fragment
                or hyphen_wrapped_continuation
                or wrapped_heading_continuation
            )
            and (
                (
                    _begins_semantic_block(line.text)
                    and not caption_reference_continuation
                )
                or (
                    _begins_bold_lead_paragraph(line.markdown)
                    and (
                        not current_is_caption
                        # A complete bold lead ending in a colon starts the
                        # following experimental paragraph even when it follows
                        # a figure/scheme caption. This is common for compound
                        # names beginning with digits or lowercase prefixes.
                        or bool(
                            re.match(
                                r"\s*(?:"
                                r"\*\*[^*]+:\*\*|\*\*[^*]+\*\*:|"
                                r"<strong(?:\s[^>]*)?>.+?:</strong>|"
                                r"<strong(?:\s[^>]*)?>.+?</strong>:)",
                                line.markdown,
                                flags=re.IGNORECASE,
                            )
                        )
                    )
                )
                or standalone_italic_after_caption
                or heading_boundary
                or graphic_boundary
            )
        )
        clear_wrapped_continuation = bool(
            same_visual_row_fragment
            or hyphen_wrapped_continuation
            or wrapped_heading_continuation
            or (
                line.text[:1].islower()
                and re.match(r"^[a-z]\s", line.text) is None
                and not re.search(r"[.!?][\"'’)]?$", previous.text)
                and _pdf_lines_share_alignment(previous, line)
            )
        )
        front_matter_boundary = bool(
            is_supplement_front_matter
            and gap > max(16.0, previous_height * 1.40 + 1.0)
        )
        vertical_boundary = bool(
            (
                not current_is_caption
                # A complete caption can be followed directly by ordinary
                # prose with no bold lead. Preserve wrapped caption lines,
                # but honor a source-visible paragraph gap once the caption
                # has terminal punctuation.
                or re.search(r"[.!?][\"'’)]?$", current.text) is not None
            )
            and not clear_wrapped_continuation
            and (
                front_matter_boundary
                or gap > baseline_gap + max(6.0, previous_height * 0.60) + 1.0
            )
        )
        if force_boundary or vertical_boundary:
            result.append(current)
            current = _PdfParagraph(lines=[line])
        else:
            current.lines.append(line)
    result.append(current)
    return result


def _caption_details(text: str) -> tuple[str, str, str] | None:
    match = _pdf_caption_match(text)
    if match is None:
        return None
    raw_kind = normalize_caption_kind(match.group("kind"))
    kind = "figure" if raw_kind.startswith("fig") else raw_kind.rstrip("s")
    number = normalize_caption_number(match.group("number"))
    label = f"{kind.title()} {number}"
    return kind, number, label


def _safe_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"[^A-Za-z0-9]+", "_", ascii_value).strip("_").casefold() or "item"


def _reviewed_pdf_crop_figures(
    source: SourceFile,
    crop_specs: Iterable[Mapping[str, Any]],
) -> list[FigureItem]:
    """Create semantic figures from exact, captioned supplementary PDF crops.

    Image-only and full-page-raster supplementary PDFs cannot supply reliable
    native caption structure. A crop becomes semantic only when its
    record-specific specification provides a complete reviewed caption and
    locator; ordinary crops remain assets only.
    """

    figures: list[FigureItem] = []
    seen_ids: set[str] = set()
    for index, spec in enumerate(crop_specs, 1):
        crop_source = str(
            spec.get("source_path", spec.get("source", ""))
        ).replace("\\", "/")
        if (
            crop_source != source.relative_path
            or str(spec.get("category", "")).strip().casefold()
            != "supplement_figure"
        ):
            continue
        caption_fields = (
            "caption_plain",
            "caption_markdown",
            "caption_source_locator",
        )
        # A longstanding supplement crop may only materialize an asset while
        # native PDF parsing supplies its semantic figure and caption.  Promote
        # the crop into a reviewed semantic figure only when the specification
        # explicitly opts into caption evidence.  Partial opt-ins remain an
        # error so a reviewed caption cannot be accepted without its locator.
        if not any(field in spec for field in caption_fields):
            continue
        figure_id = str(spec.get("asset_id", "")).strip()
        label = _normalize_space(str(spec.get("label", "")))
        caption_plain = _normalize_space(str(spec.get("caption_plain", "")))
        caption_markdown = _normalize_space(
            str(spec.get("caption_markdown", caption_plain))
        )
        source_locator = str(spec.get("caption_source_locator", "")).strip()
        label_match = re.fullmatch(
            r"(?:(?:Supplemental|Supplementary|Supporting)\s+)?"
            r"(?P<kind>Figures?|Figs?\.?|Schemes?)\s+"
            r"(?P<number>[A-Za-z]*\s*\d+(?:[A-Za-z]|[.-]\d+)*)",
            label,
            flags=re.IGNORECASE,
        )
        explicit_kind = str(spec.get("semantic_kind", "")).strip().casefold()
        if explicit_kind and explicit_kind not in {"figure", "scheme"}:
            raise ValueError(
                f"captioned supplement PDF crop {index} has an invalid semantic_kind"
            )
        if (
            not figure_id
            or figure_id in seen_ids
            or (label_match is None and explicit_kind not in {"figure", "scheme"})
            or not caption_plain
            or not caption_markdown
            or not source_locator
        ):
            raise ValueError(
                f"captioned supplement PDF crop {index} is incomplete or duplicated"
            )
        seen_ids.add(figure_id)
        if label_match is not None:
            raw_kind = label_match.group("kind").casefold()
            kind = "figure" if raw_kind.startswith("fig") else "scheme"
        else:
            kind = explicit_kind
        figures.append(
            FigureItem(
                figure_id=figure_id,
                source_id=label,
                label=label,
                kind=kind,
                caption_markdown=caption_markdown,
                caption_plain=caption_plain,
                source_path=source.relative_path,
                source_locator=source_locator,
            )
        )
    return figures


def _combine_reviewed_pdf_figures(
    automatic: Iterable[FigureItem],
    reviewed: Iterable[FigureItem],
    *,
    policy: str = "merge",
) -> list[FigureItem]:
    """Apply an explicit reviewed-figure policy to automatic PDF semantics.

    ``merge`` retains the long-standing partial-override behavior.  A
    hash-pinned supplement configuration may instead request ``replace`` when
    its reviewed crop map is complete; that prevents graphical labels or
    caption fragments from becoming additional bogus figures.
    """

    automatic_items = list(automatic)
    reviewed_items = list(reviewed)
    normalized_policy = policy.strip().casefold()
    if normalized_policy not in {"merge", "replace"}:
        raise ValueError(
            "supplement reviewed_figure_policy must be 'merge' or 'replace'"
        )
    if normalized_policy == "replace":
        if not reviewed_items:
            raise ValueError(
                "supplement reviewed_figure_policy 'replace' requires reviewed figures"
            )
        return reviewed_items
    if not reviewed_items:
        return automatic_items
    reviewed_by_id = {figure.figure_id: figure for figure in reviewed_items}
    combined: list[FigureItem] = []
    for figure in automatic_items:
        combined.append(reviewed_by_id.pop(figure.figure_id, figure))
    combined.extend(
        figure
        for figure in reviewed_items
        if figure.figure_id in reviewed_by_id
    )
    return combined


def _resolved_reviewed_pdf_warnings(
    warnings: Iterable[dict[str, Any]],
    reviewed_pages: Iterable[int],
    *,
    policy: str,
    reviewed_visual_pages: Iterable[int] = (),
) -> list[dict[str, Any]]:
    """Drop native-text warnings resolved by explicit reviewed visuals.

    A complete replacement figure map continues to resolve both legacy PDF
    warnings on its handled pages.  Separately, a hash-pinned page render or
    labeled supplementary figure image can resolve only the narrower
    ``native_text_empty`` warning: those pages remain faithfully accessible as
    pixels without pretending that OCR or native text exists.
    """

    handled = (
        set(reviewed_pages)
        if policy.strip().casefold() == "replace"
        else set()
    )
    visual_handled = set(reviewed_visual_pages)
    resolved_codes = {
        "image_only_page",
        "supplement_pdf_page_native_text_empty",
    }
    return [
        warning
        for warning in warnings
        if not (
            (
                warning.get("code") in resolved_codes
                and warning.get("page") in handled
            )
            or (
                warning.get("code") == "supplement_pdf_page_native_text_empty"
                and warning.get("page") in visual_handled
            )
        )
    ]


def _resolved_fully_reviewed_pdf_native_text_warning(
    warnings: Iterable[dict[str, Any]],
    *,
    page_count: int | None,
    reviewed_semantic_locators: Iterable[str],
    reviewed_visual_pages: Iterable[int],
    reviewed_text_pages: Iterable[int] = (),
) -> list[dict[str, Any]]:
    """Resolve empty-text/image-only warnings only after complete review.

    Every page needs reviewed semantics and either retained visual coverage or
    a complete reviewed text-page replacement. Requiring both maps keeps
    partial or asset-only review fail-closed for mixed text/visual supplements.
    """

    warning_items = list(warnings)
    if page_count is None or page_count < 1:
        return warning_items
    expected_pages = set(range(1, page_count + 1))
    semantic_pages: set[int] = set()
    for locator in reviewed_semantic_locators:
        semantic_pages.update(_source_locator_pages(locator))
    if (
        semantic_pages != expected_pages
        or (set(reviewed_visual_pages) | set(reviewed_text_pages)) != expected_pages
    ):
        return warning_items
    resolved_codes = {
        "image_only_page",
        "supplement_pdf_page_native_text_empty",
        "supplement_pdf_native_text_empty",
    }
    return [
        warning
        for warning in warning_items
        if warning.get("code") not in resolved_codes
    ]


def _reviewed_visual_pdf_pages(
    source: SourceFile,
    crop_specs: Iterable[Mapping[str, Any]],
) -> set[int]:
    """Return PDF pages whose otherwise visual content is explicitly covered."""

    pages: set[int] = set()
    for spec in crop_specs:
        crop_source = str(
            spec.get("source_path", spec.get("source", ""))
        ).replace("\\", "/")
        if crop_source != source.relative_path:
            continue
        category = str(spec.get("category", "")).strip().casefold()
        label = _normalize_space(str(spec.get("label", "")))
        recognized = bool(category == "supplement_page_render" and label)
        if category == "supplement_table" and re.match(
            r"^(?:(?:Supplemental|Supplementary|Supporting)\s+)?Tables?\s+",
            label,
            flags=re.IGNORECASE,
        ):
            recognized = True
        if category in {"supplement_figure", "supplement_image"} and re.match(
            r"^(?:(?:Supplemental|Supplementary|Supporting)\s+)?"
            r"(?:Figures?|Figs?\.?|Schemes?)\s+",
            label,
            flags=re.IGNORECASE,
        ):
            recognized = True
        # Reviewed PDF figures may faithfully retain an authored nonstandard
        # label (for example a chart page numbered only ``S-15``).  The same
        # explicit semantic_kind accepted by _reviewed_pdf_crop_figures is
        # sufficient evidence that the rendered pixels resolve an otherwise
        # empty native-text page; requiring the label to be rewritten as
        # "Figure ..." would impose extractor terminology on the source.
        if (
            category in {"supplement_figure", "supplement_image"}
            and str(spec.get("semantic_kind", "")).strip().casefold()
            in {"figure", "scheme"}
            and label
        ):
            recognized = True
        if not recognized:
            continue
        raw_parts = spec.get("parts")
        if isinstance(raw_parts, (list, tuple)):
            for part in raw_parts:
                if not isinstance(part, Mapping):
                    continue
                page = part.get("page")
                if isinstance(page, int) and not isinstance(page, bool) and page >= 1:
                    pages.add(page)
            continue
        page = spec.get("page")
        if isinstance(page, int) and not isinstance(page, bool) and page >= 1:
            pages.add(page)
    return pages


def _reviewed_pdf_crop_regions(
    crop: Mapping[str, Any],
) -> list[tuple[int, tuple[float, float, float, float]]]:
    """Validate one reviewed crop's single- or multi-page source regions."""

    raw_parts = crop.get("parts")
    if raw_parts is None:
        raw_regions: list[Mapping[str, Any]] = [crop]
    else:
        if "page" in crop or "box" in crop:
            raise ValueError("supplement figure crop cannot combine page/box with parts")
        if not isinstance(raw_parts, (list, tuple)) or len(raw_parts) < 2:
            raise ValueError(
                "supplement figure crop parts must contain at least two mappings"
            )
        if not all(isinstance(part, Mapping) for part in raw_parts):
            raise ValueError("supplement figure crop part must be a mapping")
        raw_regions = list(raw_parts)

    regions: list[tuple[int, tuple[float, float, float, float]]] = []
    for raw_region in raw_regions:
        raw_page = raw_region.get("page")
        raw_box = raw_region.get("box")
        if (
            isinstance(raw_page, bool)
            or not isinstance(raw_page, int)
            or raw_page < 1
            or not isinstance(raw_box, (list, tuple))
            or len(raw_box) != 4
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in raw_box
            )
        ):
            raise ValueError("supplement figure crop page/box is invalid")
        box = tuple(float(value) for value in raw_box)
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("supplement figure crop box is empty")
        regions.append((raw_page, box))
    return regions


def _reviewed_pdf_graphic_regions(
    source: SourceFile,
    crop_specs: Iterable[Mapping[str, Any]],
) -> dict[int, list[tuple[float, float, float, float]]]:
    """Return reviewed figure/image pixels that native prose must not absorb."""

    regions: dict[int, list[tuple[float, float, float, float]]] = {}
    for crop in crop_specs:
        crop_source = str(
            crop.get("source_path", crop.get("source", ""))
        ).replace("\\", "/")
        category = str(crop.get("category", "")).strip().casefold()
        if crop_source != source.relative_path or category not in {
            "supplement_figure",
            "supplement_image",
        }:
            continue
        for raw_page, box in _reviewed_pdf_crop_regions(crop):
            regions.setdefault(raw_page, []).append(box)
    return regions


def _unresolved_glyph_warning_is_covered(
    warning: Mapping[str, Any],
    *reviewed_regions: Mapping[
        int, list[tuple[float, float, float, float]]
    ],
) -> bool:
    """Return true when a decoded glyph belongs wholly to reviewed visuals.

    A source-reviewed table transcription or figure crop supersedes native
    text inside that exact region.  Decoder glyph failures there must not
    survive as prose warnings: table values have been transcribed against the
    visual, while scientific figure pixels are intentionally never OCRed.
    """

    if warning.get("code") != "unresolved_glyph":
        return False
    page = warning.get("page")
    bbox = warning.get("bbox")
    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or page < 1
        or not isinstance(bbox, (list, tuple))
        or len(bbox) != 4
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in bbox
        )
    ):
        return False
    glyph_box = tuple(float(value) for value in bbox)
    for region_map in reviewed_regions:
        for region in region_map.get(page, []):
            if (
                glyph_box[0] >= region[0]
                and glyph_box[1] >= region[1]
                and glyph_box[2] <= region[2]
                and glyph_box[3] <= region[3]
            ):
                return True
    return False


def _pdf_page_events(
    lines: list[_PdfLine],
    equation_specs: list[dict[str, Any]],
    reviewed_paragraph_ranges: Iterable[
        tuple[int, int] | tuple[int, int, tuple[int, ...]]
    ] = (),
    *,
    recognize_headings: bool = True,
) -> list[tuple[str, _PdfParagraph | dict[str, Any]]]:
    """Return page paragraphs and reviewed display equations in source order.

    A hash-pinned paragraph override may deliberately span boundaries produced
    by the conservative automatic paragraph detector (for example, a source
    manuscript whose body is double-spaced).  Exact reviewed line ranges are
    therefore grouped before automatic paragraph detection.  Missing or
    overlapping ranges fail closed rather than silently leaving fragmented
    prose in the canonical record.
    """

    paragraph_ranges: list[tuple[int, int, tuple[int, ...]]] = []
    for item in reviewed_paragraph_ranges:
        if len(item) == 2:
            first_line, last_line = item
            excluded_lines: tuple[int, ...] = ()
        else:
            first_line, last_line, excluded_lines = item
        paragraph_ranges.append(
            (int(first_line), int(last_line), tuple(sorted(set(excluded_lines))))
        )
    paragraph_ranges = sorted(set(paragraph_ranges))
    if not equation_specs and not paragraph_ranges:
        return [
            ("paragraph", paragraph)
            for paragraph in _paragraphs(
                lines, recognize_headings=recognize_headings
            )
        ]
    available_lines = {line.line_number for line in lines}
    retained_lines = list(lines)
    reserved_lines: set[int] = set()
    reviewed_paragraphs: list[_PdfParagraph] = []
    for first_line, last_line, excluded_lines in paragraph_ranges:
        if first_line < 1 or first_line > last_line:
            raise ValueError(
                f"supplement paragraph replacement lines {first_line}-{last_line} "
                "are invalid"
            )
        reviewed_exclusions = set(excluded_lines)
        if reviewed_exclusions & available_lines:
            line_list = ", ".join(str(value) for value in sorted(reviewed_exclusions))
            raise ValueError(
                f"supplement paragraph replacement lines {first_line}-{last_line} "
                f"expected reviewed exclusions still present: {line_list}"
            )
        expected = set(range(first_line, last_line + 1)) - reviewed_exclusions
        if not expected.issubset(available_lines):
            missing = ", ".join(
                str(value) for value in sorted(expected - available_lines)
            )
            raise ValueError(
                f"supplement paragraph replacement lines {first_line}-{last_line} "
                f"are absent (missing retained lines: {missing})"
            )
        if reserved_lines & expected:
            raise ValueError(
                f"supplement paragraph replacement lines {first_line}-{last_line} "
                "overlap another reviewed replacement"
            )
        selected = [
            line for line in lines if first_line <= line.line_number <= last_line
        ]
        reviewed_paragraphs.append(_PdfParagraph(lines=selected))
        reserved_lines.update(expected)

    for spec in equation_specs:
        after_line = int(spec["after_line"])
        if after_line and after_line not in available_lines:
            raise ValueError(
                f"supplement equation anchor line {after_line} is absent"
            )
        replace_lines = spec.get("replace_lines")
        if replace_lines is None:
            continue
        first_line, last_line = replace_lines
        expected = set(range(first_line, last_line + 1))
        if not expected.issubset(available_lines):
            raise ValueError(
                f"supplement equation replacement lines {first_line}-{last_line} "
                "are absent"
            )
        if reserved_lines & expected:
            raise ValueError(
                f"supplement equation replacement lines {first_line}-{last_line} "
                "overlap another reviewed replacement"
            )
        reviewed_box = spec.get("box")
        if reviewed_box is not None:
            selected_lines = [
                line for line in lines if line.line_number in expected
            ]
            outside_box = [
                line.line_number
                for line in selected_lines
                if not (
                    min(line.x1, float(reviewed_box[2]))
                    > max(line.x0, float(reviewed_box[0]))
                    and min(line.bottom, float(reviewed_box[3]))
                    > max(line.top, float(reviewed_box[1]))
                )
            ]
            if outside_box:
                line_list = ", ".join(str(value) for value in outside_box)
                raise ValueError(
                    f"supplement equation replacement lines {first_line}-{last_line} "
                    "include native lines outside the reviewed equation box: "
                    f"{line_list}"
                )
        reserved_lines.update(expected)

    retained_lines = [
        line for line in retained_lines if line.line_number not in reserved_lines
    ]

    events: list[tuple[int, int, str, _PdfParagraph | dict[str, Any]]] = []
    for paragraph in _paragraphs(
        retained_lines, recognize_headings=recognize_headings
    ):
        events.append((paragraph.last_line, 0, "paragraph", paragraph))
    for paragraph in reviewed_paragraphs:
        events.append((paragraph.last_line, 0, "paragraph", paragraph))
    for spec_index, spec in enumerate(equation_specs, 1):
        events.append(
            (int(spec["after_line"]), spec_index, "equation", spec)
        )
    events.sort(key=lambda item: (item[0], item[1]))
    return [(kind, value) for _, _, kind, value in events]


def _pdf_blocks(
    source: SourceFile,
    supplement_id: str,
    *,
    pdf_path: Path | None = None,
    pdf_text_config: Mapping[str, Any] | None = None,
    reviewed_figure_regions: Mapping[
        int, list[tuple[float, float, float, float]]
    ] | None = None,
) -> tuple[list[ContentBlock], list[FigureItem], list[dict[str, Any]]]:
    try:
        import pdfplumber
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency check owns this
        raise RuntimeError("pdfplumber is required for supplement PDF extraction") from exc

    blocks: list[ContentBlock] = []
    figures: list[FigureItem] = []
    warnings: list[dict[str, Any]] = []
    used_figure_ids: dict[str, int] = {}
    try:
        source_config = _supplement_pdf_text_config(pdf_text_config, source)
        text_replacements = _supplement_text_replacements(source_config)
        block_text_replacements = _supplement_block_text_replacements(source_config)
        reviewed_table_regions = _supplement_table_text_regions(source_config)
        unboxed_table_pages = _supplement_unboxed_table_pages(
            source_config, reviewed_table_regions
        )
        native_text_engine = _supplement_native_text_engine(source_config)
        if native_text_engine == "pypdf":
            fallback_blocks, fallback_warnings = _pypdf_native_blocks(
                source,
                supplement_id,
                pdf_path or source.path,
                text_replacements,
                reviewed_figure_regions,
            )
            _verify_supplement_text_replacements(text_replacements)
            fallback_blocks = _apply_supplement_block_text_replacements(
                fallback_blocks, block_text_replacements
            )
            if not fallback_blocks:
                fallback_warnings.append(
                    _warning(
                        "supplement_pdf_native_text_empty",
                        (
                            "PDF has no native text; the original was preserved "
                            "and no OCR was performed"
                        ),
                        source,
                        supplement_id,
                    )
                )
            return fallback_blocks, [], fallback_warnings
        decoded_document: PdfTextDocument | None = None
        caption_overrides: dict[tuple[str, str], dict[str, str]] = {}
        equation_overrides: dict[int, list[dict[str, Any]]] = {}
        paragraph_overrides: dict[tuple[int, int, int], dict[str, str]] = {}
        used_paragraph_overrides: set[tuple[int, int, int]] = set()
        line_exclusions: list[dict[str, Any]] = []
        page_continuations: list[dict[str, str]] = []
        if source_config is not None:
            caption_overrides = _supplement_caption_overrides(source_config)
            equation_overrides = _supplement_equation_overrides(source_config)
            paragraph_overrides = _supplement_paragraph_overrides(source_config)
            line_exclusions = _supplement_line_exclusions(source_config)
            page_continuations = _supplement_page_continuations(source_config)
            if _supplement_needs_decoded_text(source_config):
                decoded_document = extract_pdf_text(
                    pdf_path or source.path,
                    source.relative_path,
                    glyph_overrides=_supplement_glyph_overrides(source_config),
                    reading_regions=(source_config or {}).get(
                        "native_reading_regions"
                    ),
                )
                for raw_warning in decoded_document.warnings:
                    if (
                        text_replacements
                        and raw_warning.get("code") == "unresolved_glyph"
                    ):
                        continue
                    if _unresolved_glyph_warning_is_covered(
                        raw_warning,
                        reviewed_table_regions,
                        reviewed_figure_regions or {},
                    ):
                        continue
                    warning = dict(raw_warning)
                    warning.setdefault("schema_version", "1.0")
                    warning["supplement_id"] = supplement_id
                    warnings.append(warning)
        with pdfplumber.open(pdf_path or source.path) as document:
            for page_number, page in enumerate(document.pages, 1):
                page_regions = (reviewed_figure_regions or {}).get(page_number, [])
                lines = (
                    _decoded_page_lines(
                        page,
                        decoded_document.pages[page_number - 1],
                        page_regions,
                        text_replacements,
                    )
                    if decoded_document is not None
                    else _extract_page_lines(page, page_regions)
                )
                if decoded_document is None and text_replacements:
                    repaired_lines: list[_PdfLine] = []
                    for line in lines:
                        repaired_text = _apply_supplement_text_replacements(
                            line.text,
                            text_replacements,
                            count_hits=True,
                        )
                        repaired_lines.append(
                            replace(
                                line,
                                text=repaired_text,
                                # pdfplumber has no trustworthy style stream;
                                # rebuild its escaped rich counterpart from the
                                # repaired literal text so both forms agree.
                                markdown=_escape_literal_pdf_markup(repaired_text),
                            )
                        )
                    lines = repaired_lines
                lines = [
                    line
                    for line in lines
                    if not _is_supplement_page_number(line, float(page.height))
                ]
                had_native_content_lines = bool(lines)
                lines = _apply_supplement_line_exclusions(
                    lines, page_number, line_exclusions
                )
                # Apply exact reviewed source-line exclusions before the
                # structural table-region filter.  Legacy reviewed inputs may
                # deliberately count table-cell matches that the newer bbox
                # filter subsequently removes; reversing the order makes the
                # evidence appear unused and fails the complete PDF closed.
                lines = _exclude_reviewed_pdf_table_lines(
                    lines, reviewed_table_regions.get(page_number, [])
                )
                if not lines:
                    if _should_warn_supplement_page_native_text_empty(
                        had_native_content_lines=had_native_content_lines,
                        retained_lines=lines,
                        # A publisher-inserted blank page can contain only its
                        # running page number, which is removed above.  Do not
                        # misreport that authored blank as inaccessible
                        # scientific content.  Any image, vector, or other
                        # non-character PDF object keeps the review warning
                        # fail-closed for image-only figures and spectra.
                        has_graphic_content=any(
                            values
                            for object_kind, values in page.objects.items()
                            if object_kind != "char"
                        ),
                    ):
                        warnings.append(
                            _warning(
                                "supplement_pdf_page_native_text_empty",
                                (
                                    f"PDF page {page_number} has no native text; "
                                    "no OCR was performed"
                                ),
                                source,
                                supplement_id,
                                page=page_number,
                            )
                        )
                    continue

                paragraph_number = 0
                equation_number = 0
                page_paragraph_ranges = [
                    (
                        first_line,
                        last_line,
                        tuple(
                            int(value)
                            for value in paragraph_overrides[
                                (configured_page, first_line, last_line)
                            ].get("excluded_lines", [])
                        ),
                    )
                    for configured_page, first_line, last_line in paragraph_overrides
                    if configured_page == page_number
                ]
                recognize_page_headings = bool(
                    page_number not in unboxed_table_pages
                    and not _is_email_correspondence_page(lines)
                )
                affiliation_lines = _supplement_affiliation_line_numbers(lines)
                for event_kind, event in _pdf_page_events(
                    lines,
                    equation_overrides.get(page_number, []),
                    page_paragraph_ranges,
                    recognize_headings=recognize_page_headings,
                ):
                    if event_kind == "equation":
                        equation_number += 1
                        equation = event
                        assert isinstance(equation, dict)
                        geometry = []
                        if equation.get("box") is not None:
                            geometry = [
                                {
                                    "page": page_number,
                                    "bbox": equation["box"],
                                }
                            ]
                        blocks.append(
                            ContentBlock(
                                block_id=(
                                    f"{supplement_id}-page-{page_number:03d}-"
                                    f"equation-{equation_number:03d}"
                                ),
                                kind="equation",
                                markdown=str(equation["markdown"]),
                                plain_text=str(equation["plain_text"]),
                                source_path=source.relative_path,
                                source_locator=str(equation["source_locator"]),
                                source_geometry=geometry,
                            )
                        )
                        continue
                    paragraph_number += 1
                    paragraph = event
                    assert isinstance(paragraph, _PdfParagraph)
                    text = paragraph.text
                    if not text:
                        continue
                    caption = _caption_details(text)
                    is_footnote = bool(FOOTNOTE_PATTERN.match(text))
                    is_heading = bool(
                        recognize_page_headings
                        and not any(line.line_number in affiliation_lines for line in paragraph.lines)
                        and _is_pdf_heading(paragraph.markdown, text)
                    )
                    reviewed_paragraph = paragraph_overrides.get(
                        (page_number, paragraph.first_line, paragraph.last_line)
                    )
                    if _is_unreviewed_graphic_only_paragraph(
                        paragraph,
                        is_caption=caption is not None,
                        is_footnote=is_footnote,
                        has_reviewed_override=reviewed_paragraph is not None,
                    ):
                        # Text embedded in or overlaid on a figure belongs with
                        # the separately extracted figure asset, not prose. This
                        # is geometry-based filtering of native text, not OCR.
                        continue
                    if caption is not None:
                        kind, number, label = caption
                        block_kind = f"{kind}_caption"
                    elif is_footnote:
                        kind = ""
                        number = ""
                        label = ""
                        block_kind = "footnote"
                    elif is_heading:
                        kind = ""
                        number = ""
                        label = ""
                        block_kind = "subsection_heading"
                    else:
                        kind = ""
                        number = ""
                        label = ""
                        block_kind = "text"

                    locator = (
                        f"page={page_number};native-lines="
                        f"{paragraph.first_line}-{paragraph.last_line}"
                    )
                    block_text = text
                    block_markdown = (
                        _heading_markup(paragraph.markdown)
                        if is_heading
                        else paragraph.markdown
                    )
                    if reviewed_paragraph is not None:
                        used_paragraph_overrides.add(
                            (page_number, paragraph.first_line, paragraph.last_line)
                        )
                        if not reviewed_paragraph["use_source_text"]:
                            block_text = reviewed_paragraph["plain_text"]
                            block_markdown = reviewed_paragraph["markdown"]
                        if reviewed_paragraph["block_kind"]:
                            block_kind = reviewed_paragraph["block_kind"]
                        locator = reviewed_paragraph["source_locator"]
                    if caption is not None and block_kind in {
                        "figure_caption",
                        "scheme_caption",
                    }:
                        reviewed_caption = caption_overrides.get(
                            (kind, number.casefold())
                        )
                        if reviewed_caption is not None:
                            block_text = reviewed_caption["plain_text"]
                            block_markdown = reviewed_caption["markdown"]
                            locator = reviewed_caption["source_locator"]
                    block_id = (
                        f"{supplement_id}-page-{page_number:03d}-"
                        f"block-{paragraph_number:03d}"
                    )
                    blocks.append(
                        ContentBlock(
                            block_id=block_id,
                            kind=block_kind,
                            markdown=block_markdown,
                            plain_text=block_text,
                            source_path=source.relative_path,
                            source_locator=locator,
                        )
                    )

                    # A source-reviewed paragraph override can explicitly
                    # reclassify caption-looking prose (for example, a peer
                    # review sentence beginning "Supplementary Figure 2c") as
                    # ordinary text.  Preserve the paragraph, but only emit a
                    # semantic figure when its final block kind remains a
                    # figure/scheme caption.
                    if block_kind not in {"figure_caption", "scheme_caption"}:
                        continue
                    base_id = f"{supplement_id}_{kind}_{_safe_identifier(number)}"
                    occurrence = used_figure_ids.get(base_id, 0) + 1
                    used_figure_ids[base_id] = occurrence
                    figure_id = base_id if occurrence == 1 else f"{base_id}_{occurrence:02d}"
                    figures.append(
                        FigureItem(
                            figure_id=figure_id,
                            source_id=label,
                            label=label,
                            kind=kind,
                            caption_markdown=block_markdown,
                            caption_plain=block_text,
                            source_path=source.relative_path,
                            source_locator=locator,
                        )
                    )
        for exclusion_index, exclusion in enumerate(line_exclusions, 1):
            if exclusion["hits"] == 0:
                raise ValueError(
                    f"supplement line exclusion {exclusion_index} matched no native lines"
                )
        _verify_supplement_text_replacements(text_replacements)
        unused_paragraph_overrides = set(paragraph_overrides) - used_paragraph_overrides
        if unused_paragraph_overrides:
            page, first_line, last_line = sorted(unused_paragraph_overrides)[0]
            raise ValueError(
                "supplement paragraph override was not applied: "
                f"page {page} lines {first_line}-{last_line}"
            )
        blocks = _apply_supplement_page_continuations(blocks, page_continuations)
        blocks = _apply_supplement_block_text_replacements(
            blocks, block_text_replacements
        )
    except Exception as exc:
        warnings.append(
            _warning(
                "supplement_pdf_native_text_extraction_failed",
                (
                    "Native PDF text extraction failed; the original was preserved, "
                    f"no OCR was performed ({type(exc).__name__}: {exc})"
                ),
                source,
                supplement_id,
            )
        )
        return [], [], warnings

    if not blocks:
        warnings.append(
            _warning(
                "supplement_pdf_native_text_empty",
                "PDF has no native text; the original was preserved and no OCR was performed",
                source,
                supplement_id,
            )
        )
    return blocks, figures, warnings


def extract_supplements(
    sources: Iterable[SourceFile],
    extraction_root: Path,
    exclusion_specs: Iterable[Mapping[str, Any]] = (),
    *,
    presentation_renderer: SlideRenderer | None = render_powerpoint_slides,
    docx_figure_renderer: DocxFigureRenderer | None = render_docx_figure_crops,
    postscript_renderer: PostscriptRenderer | None = render_postscript_png,
    pdf_text_config: Mapping[str, Any] | None = None,
    pdf_crop_specs: Iterable[Mapping[str, Any]] = (),
    standalone_image_specs: Iterable[Mapping[str, Any]] = (),
    standalone_image_table_specs: Iterable[Mapping[str, Any]] = (),
    docx_figure_crop_specs: Iterable[Mapping[str, Any]] = (),
) -> list[SupplementExtraction]:
    """Copy and extract discovered supplements in deterministic source order.

    ``extraction_root`` must be the root of an isolated candidate extraction.
    Existing byte-identical destinations are accepted, while different files
    are never overwritten. Unsupported formats are still copied and returned
    with an explicit warning.
    """

    if is_reparse_point(extraction_root):
        raise ValueError(f"extraction root cannot be a reparse point: {extraction_root}")
    extraction_root.mkdir(parents=True, exist_ok=True)
    if is_reparse_point(extraction_root):
        raise ValueError(f"extraction root cannot be a reparse point: {extraction_root}")
    extraction_root = extraction_root.resolve(strict=True)

    all_sources = list(sources)
    reviewed_exclusions = list(exclusion_specs)
    reviewed_crops = list(pdf_crop_specs)
    supplement_sources = sorted(
        (source for source in all_sources if source.role == "supplement"),
        key=lambda source: (
            _natural_path_key(source.relative_path),
            source.relative_path.casefold(),
            source.relative_path,
        ),
    )
    relative_paths = [source.relative_path.casefold() for source in supplement_sources]
    if len(relative_paths) != len(set(relative_paths)):
        raise ValueError("duplicate supplementary source paths were discovered")
    reviewed_standalone_images = _reviewed_standalone_image_specs(
        standalone_image_specs, supplement_sources
    )
    reviewed_standalone_image_tables = _reviewed_standalone_image_table_specs(
        standalone_image_table_specs, supplement_sources
    )
    reviewed_docx_figure_crops = _reviewed_docx_figure_crop_specs(
        docx_figure_crop_specs, supplement_sources
    )

    results: list[SupplementExtraction] = []
    for index, source in enumerate(supplement_sources, 1):
        supplement_id = f"supplement_{index:03d}"
        copied_relative = (
            Path("supplementary") / supplement_id / source.path.name
        ).as_posix()
        destination = extraction_root / Path(copied_relative)
        _copy_verified(source, destination, extraction_root)

        tables = []
        repairs: list[dict[str, Any]] = []
        assets: list[dict[str, Any]] = []
        asset_ids: list[str] = []
        whole_file_exclusions: list[dict[str, Any]] = []
        if source.detected_format == "application/pdf":
            source_config = _supplement_pdf_text_config(pdf_text_config, source)
            duplicate_exclusion = _supplement_duplicate_file_only_exclusion(
                source_config, source, supplement_id, all_sources
            )
            if duplicate_exclusion is not None:
                blocks = []
                figures = []
                warnings = []
                whole_file_exclusions.append(duplicate_exclusion)
            else:
                figure_regions = _reviewed_pdf_graphic_regions(
                    source, reviewed_crops
                )
                # Parse the verified candidate copy so extracted text and the file
                # delivered to downstream consumers are guaranteed to be the same
                # byte sequence even if an original changes after discovery.
                blocks, figures, warnings = _pdf_blocks(
                    source,
                    supplement_id,
                    pdf_path=destination,
                    pdf_text_config=pdf_text_config,
                    reviewed_figure_regions=figure_regions,
                )
                reviewed_page_blocks = _supplement_reviewed_page_blocks(
                    source_config, source, supplement_id
                )
                blocks = _apply_supplement_reviewed_page_blocks(
                    blocks, reviewed_page_blocks
                )
                blocks = _apply_supplement_page_continuations(
                    blocks,
                    _supplement_page_continuations({
                        "page_continuations": (source_config or {}).get(
                            "reviewed_page_continuations", []
                        )
                    }),
                )
                warnings = _resolved_reviewed_page_text_warnings(
                    warnings,
                    reviewed_page_blocks,
                    page_count=source.page_count,
                )
                if blocks and not any(
                    warning.get("code") == "supplement_pdf_native_text_extraction_failed"
                    for warning in warnings
                ):
                    repairs = _supplement_text_repair_rows(
                        source_config, source, supplement_id
                    )
                reviewed_figures = _reviewed_pdf_crop_figures(source, reviewed_crops)
                reviewed_policy = str(
                    (source_config or {}).get("reviewed_figure_policy", "merge")
                )
                figures = _combine_reviewed_pdf_figures(
                    figures,
                    reviewed_figures,
                    policy=reviewed_policy,
                )
                reviewed_visual_pages = _reviewed_visual_pdf_pages(
                    source, reviewed_crops
                )
                warnings = _resolved_reviewed_pdf_warnings(
                    warnings,
                    figure_regions,
                    policy=reviewed_policy,
                    reviewed_visual_pages=reviewed_visual_pages,
                )
                blocks = _consolidate_supplement_figure_captions(blocks, figures)
                tables, table_pages = _supplement_table_overrides(
                    source_config, source, supplement_id
                )
                warnings = _resolved_fully_reviewed_pdf_native_text_warning(
                    warnings,
                    page_count=source.page_count,
                    reviewed_semantic_locators=[
                        *(
                            block.source_locator
                            for page_blocks in reviewed_page_blocks.values()
                            for block in page_blocks
                        ),
                        *(figure.source_locator for figure in reviewed_figures),
                        *(table.source_locator for table in tables),
                    ],
                    reviewed_visual_pages=reviewed_visual_pages,
                    reviewed_text_pages=reviewed_page_blocks,
                )
                # Boxed overrides have already excluded only their native table
                # lines.  Page-wide removal here would also discard unrelated
                # numbered references/notes below a table on the same page.
                table_pages = _supplement_unboxed_table_pages(
                    source_config, _supplement_table_text_regions(source_config)
                )
                if table_pages:
                    blocks = [
                        block
                        for block in blocks
                        if not (
                            block.kind in {"table_caption", "footnote"}
                            and any(
                                block.source_locator.startswith(f"page={page};")
                                for page in table_pages
                            )
                        )
                    ]
        elif source.detected_format in XML_MEDIA_TYPES:
            blocks, warnings = extract_xml_fields(
                source, supplement_id, destination
            )
            figures = []
        elif source.detected_format == PPTX_MEDIA_TYPE:
            image_specs = reviewed_standalone_images.get(source.relative_path, [])
            blocks, tables, assets, warnings = extract_pptx_supplement(
                source,
                supplement_id,
                pptx_path=destination,
                extraction_root=extraction_root,
                slide_renderer=presentation_renderer,
                required_render_slides=(
                    int(spec["frame"]) for spec in image_specs
                ),
            )
            blocks, figures = _presentation_caption_semantics(
                blocks, assets, source, supplement_id
            )
            if image_specs:
                reviewed_figures = _promote_reviewed_presentation_slide_figures(
                    source, supplement_id, image_specs, assets
                )
                existing_by_id = {figure.figure_id: figure for figure in figures}
                reviewed_ids = {figure.figure_id for figure in reviewed_figures}
                for reviewed_figure in reviewed_figures:
                    existing = existing_by_id.get(reviewed_figure.figure_id)
                    if (
                        existing is not None
                        and existing.output_path != reviewed_figure.output_path
                    ):
                        raise ValueError(
                            "reviewed presentation figure conflicts with a native figure"
                        )
                # A presentation's native caption semantics may already have
                # promoted the exact complete-slide render. The reviewed mapping
                # remains authoritative for its label and externally supplied
                # caption, so replace that semantic entry without duplicating the
                # underlying asset.
                figures = [
                    figure for figure in figures if figure.figure_id not in reviewed_ids
                ]
                figures.extend(reviewed_figures)
            asset_ids = [str(asset["asset_id"]) for asset in assets]
        elif source.detected_format in {DOCX_MEDIA_TYPE, LEGACY_DOC_MEDIA_TYPE}:
            parser_source = source
            parser_path = destination
            conversion_prefix = ""
            temporary_conversion: tempfile.TemporaryDirectory[str] | None = None
            if source.detected_format == LEGACY_DOC_MEDIA_TYPE:
                scratch_parent = extraction_root.parent
                if ".building-" in extraction_root.parent.name:
                    scratch_parent = extraction_root.parent.parent
                temporary_conversion = tempfile.TemporaryDirectory(
                    prefix=".legacy-doc-convert-", dir=scratch_parent
                )
                conversion_root = Path(temporary_conversion.name).resolve(strict=True)
                converted_root = conversion_root / "converted"
                profile_root = conversion_root / "profile"
                converted_root.mkdir()
                profile_root.mkdir()
                parser_path, renderer_name, renderer_version = convert_legacy_doc_to_docx(
                    destination,
                    conversion_root,
                    converted_root,
                    profile_root,
                )
                parser_source = replace(source, detected_format=DOCX_MEDIA_TYPE)
                conversion_prefix = (
                    "legacy-doc-conversion="
                    f"{renderer_name};version={renderer_version};"
                )
            try:
                blocks, figures, tables, assets, warnings = extract_docx_supplement(
                    parser_source,
                    supplement_id,
                    docx_path=parser_path,
                    extraction_root=extraction_root,
                )
                if conversion_prefix:
                    for block in blocks:
                        block.source_locator = conversion_prefix + block.source_locator
                    for figure in figures:
                        figure.source_locator = conversion_prefix + figure.source_locator
                    for table in tables:
                        table.source_locator = conversion_prefix + table.source_locator
                    for asset in assets:
                        locator = str(asset.get("source_locator", ""))
                        if locator:
                            asset["source_locator"] = conversion_prefix + locator
                _apply_reviewed_docx_figure_crops(
                    source=source,
                    supplement_id=supplement_id,
                    # Legacy DOC-to-DOCX conversion can lose native WMF/PICT
                    # rendering when Word reopens the converted package.
                    # Parse the OOXML, but compose the archived original bytes.
                    copied_source=destination,
                    extraction_root=extraction_root,
                    figures=figures,
                    assets=assets,
                    warnings=warnings,
                    specs=reviewed_docx_figure_crops.get(source.relative_path, []),
                    renderer=docx_figure_renderer,
                )
                asset_ids = [str(asset["asset_id"]) for asset in assets]
            finally:
                if temporary_conversion is not None:
                    temporary_conversion.cleanup()
        elif is_xls_supplement(source):
            blocks, tables, warnings = extract_xls_supplement(
                source, supplement_id, xls_path=destination
            )
            figures = []
        elif source.detected_format == XLSX_MEDIA_TYPE:
            blocks, tables, warnings = extract_xlsx_supplement(
                source,
                supplement_id,
                xlsx_path=destination,
            )
            figures = []
        elif is_cif_supplement(source):
            blocks, tables, warnings = extract_cif_supplement(
                source, supplement_id, cif_path=destination
            )
            figures = []
        elif is_csv_supplement(source):
            blocks, tables, warnings = extract_csv_supplement(
                source,
                supplement_id,
                csv_path=destination,
            )
            figures = []
        elif source.detected_format == "text/html":
            html_table = _legacy_standalone_html_table(
                source,
                supplement_id,
                destination,
            )
            html_blocks = (
                _highwire_standalone_html_blocks(source, supplement_id, destination)
                if html_table is None else None
            )
            blocks = html_blocks or []
            figures = []
            tables = [html_table] if html_table is not None else []
            warnings = (
                []
                if html_table is not None or html_blocks is not None
                else [
                    _warning(
                        "unsupported_supplement_text_extraction",
                        "The HTML supplement did not match a supported standalone structure; the original was preserved",
                        source,
                        supplement_id,
                    )
                ]
            )
        elif source.detected_format == POSTSCRIPT_MEDIA_TYPE:
            blocks = []
            figures = []
            warnings = []
            source_config = _supplement_pdf_text_config(pdf_text_config, source)
            tables, _ = _supplement_table_overrides(
                source_config, source, supplement_id
            )
            image_specs = reviewed_standalone_images.get(source.relative_path, [])
            if image_specs and tables:
                raise ValueError(
                    "one PostScript supplement cannot be both a reviewed figure "
                    "and a reviewed table"
                )
            if image_specs:
                figures, assets = _materialize_reviewed_standalone_images(
                    source,
                    supplement_id,
                    destination,
                    extraction_root,
                    image_specs,
                    postscript_renderer,
                )
            elif tables:
                if len(tables) != 1:
                    raise ValueError(
                        "one PostScript supplement must map to exactly one reviewed table"
                    )
                assets = [
                    _materialize_reviewed_postscript_table(
                        source,
                        supplement_id,
                        destination,
                        extraction_root,
                        tables[0],
                        postscript_renderer,
                    )
                ]
            else:
                warnings = [
                    _warning(
                        "unsupported_supplement_text_extraction",
                        (
                            "The PostScript source was preserved, but no hash-pinned "
                            "reviewed figure or table semantics were supplied"
                        ),
                        source,
                        supplement_id,
                    )
                ]
            asset_ids = [str(asset["asset_id"]) for asset in assets]
        elif source.detected_format.startswith(("image/", "video/", "audio/")):
            # A standalone media supplement is already represented by its
            # byte-identical local file. Per extraction policy, figures and
            # videos are not OCR targets; absence of a text stream is not an
            # extraction failure. A record-specific reviewed supporting block
            # may describe an otherwise opaque filename when source evidence
            # establishes its role.
            blocks = []
            warnings = []
            image_specs = reviewed_standalone_images.get(source.relative_path, [])
            image_table_specs = reviewed_standalone_image_tables.get(
                source.relative_path, []
            )
            if image_specs and image_table_specs:
                raise ValueError(
                    "one standalone image supplement cannot be both a reviewed "
                    "figure and a reviewed table"
                )
            if image_specs:
                figures, assets = _materialize_reviewed_standalone_images(
                    source,
                    supplement_id,
                    destination,
                    extraction_root,
                    image_specs,
                    postscript_renderer,
                )
                asset_ids = [str(asset["asset_id"]) for asset in assets]
            elif image_table_specs:
                figures = []
                assets = _materialize_reviewed_standalone_image_tables(
                    source,
                    supplement_id,
                    destination,
                    extraction_root,
                    image_table_specs,
                )
                asset_ids = [str(asset["asset_id"]) for asset in assets]
            else:
                figures = []
        else:
            blocks = []
            figures = []
            warnings = [
                _warning(
                    "unsupported_supplement_text_extraction",
                    (
                        f"No text extractor is implemented for {source.detected_format}; "
                        "the original was preserved"
                    ),
                    source,
                    supplement_id,
                )
            ]

        exclusions: list[dict[str, Any]] = list(whole_file_exclusions)
        retained_blocks: list[ContentBlock] = []
        for block in blocks:
            matched: Mapping[str, Any] | None = None
            matched_index = 0
            for spec_index, spec in enumerate(reviewed_exclusions, 1):
                pattern = spec.get("pattern")
                selected_source = spec.get("source_path")
                if not isinstance(pattern, str) or not pattern:
                    raise ValueError("supplement exclusion pattern must be non-empty")
                if selected_source and selected_source != source.relative_path:
                    continue
                if re.search(pattern, block.plain_text):
                    matched = spec
                    matched_index = spec_index
                    break
            if matched is None:
                retained_blocks.append(block)
                continue
            exclusions.append(
                {
                    "schema_version": "1.0",
                    "coverage_id": (
                        f"{supplement_id}-reviewed-exclusion-{matched_index:03d}-"
                        f"{block.block_id}"
                    ),
                    "content_kind": block.kind,
                    "source_path": block.source_path,
                    "source_locator": block.source_locator,
                    "status": "intentionally_excluded",
                    "reason": str(matched.get("reason", "reviewed publisher furniture")),
                    "evidence": str(matched.get("evidence", "")),
                    "text_sha256": hashlib.sha256(
                        block.plain_text.encode("utf-8")
                    ).hexdigest(),
                }
            )
        blocks = retained_blocks

        retained_figures: list[FigureItem] = []
        for figure in figures:
            matched = None
            matched_index = 0
            for spec_index, spec in enumerate(reviewed_exclusions, 1):
                pattern = spec.get("pattern")
                selected_source = spec.get("source_path")
                if not isinstance(pattern, str) or not pattern:
                    raise ValueError("supplement exclusion pattern must be non-empty")
                if selected_source and selected_source != source.relative_path:
                    continue
                if re.search(pattern, figure.caption_plain):
                    matched = spec
                    matched_index = spec_index
                    break
            if matched is None:
                retained_figures.append(figure)
                continue
            exclusions.append(
                {
                    "schema_version": "1.0",
                    "coverage_id": (
                        f"{supplement_id}-reviewed-exclusion-{matched_index:03d}-"
                        f"{figure.figure_id}"
                    ),
                    "content_kind": "figure_caption",
                    "source_path": figure.source_path,
                    "source_locator": figure.source_locator,
                    "status": "intentionally_excluded",
                    "reason": str(matched.get("reason", "reviewed publisher furniture")),
                    "evidence": str(matched.get("evidence", "")),
                    "text_sha256": hashlib.sha256(
                        figure.caption_plain.encode("utf-8")
                    ).hexdigest(),
                }
            )
        figures = retained_figures

        results.append(
            SupplementExtraction(
                supplement_id=supplement_id,
                source=source,
                copied_path=copied_relative,
                blocks=blocks,
                figures=figures,
                warnings=warnings,
                repairs=repairs,
                exclusions=exclusions,
                tables=tables,
                assets=assets,
                asset_ids=asset_ids,
            )
        )
    _consolidate_cross_file_caption_figures(results, reviewed_crops)
    return results


__all__ = ["extract_supplements"]
