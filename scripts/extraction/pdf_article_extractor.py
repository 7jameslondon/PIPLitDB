"""Build the source-neutral article model from a PDF-only publication.

This module deliberately separates PDF text acquisition from page semantics.
It never receives visual-asset pixels.  Text may come from deterministic glyph
decoding or an upstream region OCR run whose figure/scheme exclusions were
already enforced.  Unresolved text warnings survive to the normal validation
gate instead of being guessed here.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .metadata import RecordMetadata
from .models import (
    ArticleExtraction,
    ContentBlock,
    FigureItem,
    Repair,
    Section,
)
from .paths import sha256_file
from .pdf_text_extractor import PdfTextDocument, PdfTextLine, extract_pdf_text


class PdfArticleExtractionError(RuntimeError):
    """The PDF decoded, but could not be represented safely as an article."""


_NUMBERED_HEADING = re.compile(
    r"^(?P<number>\d+)\.\s+(?P<title>.+?\.)\s*(?:[–—]\s*)?(?P<rest>.*)$"
)
_STRONG_PREFIX = re.compile(
    r"^\*\*(?P<strong>.+?)\*\*\s*(?:[–—]\s*)?(?P<rest>.*)$"
)
_REFERENCE_START = re.compile(
    r"^(?:\[(?P<bracket_number>\d+)\]|\((?P<paren_number>\d+)[)）]|(?P<close_paren_number>\d+)[)）]|(?P<period_number>\d+)\.|"
    r"(?P<bare_number>\d+)\s+)\s*"
)
_AUTHOR_YEAR_REFERENCE_START = re.compile(
    r"^\S.*\((?:18|19|20)\d{2}[a-z]?\)(?:\s|$)"
)
_RECEIVED = re.compile(r"^Received\b", flags=re.IGNORECASE)


def _clean(value: str) -> str:
    # Expand only typographic Latin ligatures, without compatibility-folding
    # scientific superscripts, Greek letters or other semantic symbols.
    value = value.translate(str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
                                          "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}))
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def _reference_number(match: re.Match[str] | None) -> int | None:
    if match is None:
        return None
    value = (
        match.group("bracket_number")
        or match.group("paren_number")
        or match.group("close_paren_number")
        or match.group("period_number")
        or match.group("bare_number")
    )
    return int(value) if value is not None else None


def _strip_source_emphasis(value: str) -> str:
    """Discard publisher font emphasis while preserving scientific markup.

    PDF font runs often split one chemical name or caption into dozens of
    adjacent bold/italic fragments.  Those typographic runs are not reliable
    semantic structure.  Superscript/subscript HTML and escaped literal
    asterisks remain intact.
    """

    without_html_emphasis = re.sub(
        r"</?(?:strong|em)>", "", value, flags=re.IGNORECASE
    )
    return re.sub(r"(?<!\\)\*", "", without_html_emphasis)


def _box(value: Any, *, field: str) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise PdfArticleExtractionError(f"{field} must contain four coordinates")
    result = tuple(float(item) for item in value)
    if not (result[0] < result[2] and result[1] < result[3]):
        raise PdfArticleExtractionError(f"{field} is empty or inverted")
    return result  # type: ignore[return-value]


def _intersection_ratio(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    if left >= right or top >= bottom:
        return 0.0
    intersection = (right - left) * (bottom - top)
    area = max((first[2] - first[0]) * (first[3] - first[1]), 0.0001)
    return intersection / area


def _line_in_box(line: PdfTextLine, box: tuple[float, float, float, float]) -> bool:
    center_x = (line.bbox[0] + line.bbox[2]) / 2
    center_y = (line.bbox[1] + line.bbox[3]) / 2
    return (
        box[0] <= center_x <= box[2]
        and box[1] <= center_y <= box[3]
    ) or _intersection_ratio(line.bbox, box) >= 0.5


def _join_fragments(values: Iterable[str]) -> str:
    """Join printed lines while retaining every visible source hyphen.

    A terminal hyphen joins directly to the next line.  We intentionally do
    not guess whether it is a semantic hyphen or typographic word division.
    """

    result = ""
    for raw in values:
        value = _clean(raw)
        if not value:
            continue
        if not result:
            result = value
        elif re.search(r"[-‐‑](?:\*{1,2}|</(?:em|strong)>)*$", result):
            # A source emphasis run may close after the visible line-end
            # hyphen. Join the visible word just as in plain text, coalescing
            # matching emphasis delimiters to avoid accidental strong markup.
            match = re.search(r"[-‐‑](\*{1,2})$", result)
            if match and value.startswith(match.group(1)):
                marker = match.group(1)
                result = result[:-len(marker)] + value[len(marker):]
            else:
                result += value.lstrip()
        else:
            result += " " + value
    return result


def _source_locator(page: int, lines: list[PdfTextLine]) -> str:
    if not lines:
        return f"PDF page {page}"
    pages = sorted({line.page for line in lines})
    if len(pages) > 1:
        return (
            f"PDF pages {', '.join(str(value) for value in pages)}; exact "
            "per-line page/bbox geometry is stored in source_geometry"
        )
    left = min(line.bbox[0] for line in lines)
    top = min(line.bbox[1] for line in lines)
    right = max(line.bbox[2] for line in lines)
    bottom = max(line.bbox[3] for line in lines)
    return (
        f"PDF page {page}, box "
        f"[{left:.2f}, {top:.2f}, {right:.2f}, {bottom:.2f}]"
    )


def _source_geometry(lines: Iterable[PdfTextLine]) -> list[dict[str, Any]]:
    """Return ordered, exact line geometry for private source diagnostics."""

    result: list[dict[str, Any]] = []
    for line in lines:
        segments = line.constituent_geometry or ((line.page, line.bbox),)
        result.extend(
            {
                "page": page,
                "bbox": [float(value) for value in bbox],
            }
            for page, bbox in segments
        )
    return result


def _glyph_overrides(config: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = defaultdict(dict)
    raw = config.get("glyph_overrides", [])
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise PdfArticleExtractionError("pdf_text.glyph_overrides must be a list")
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise PdfArticleExtractionError(
                f"pdf_text.glyph_overrides item {index} must be a mapping"
            )
        font = str(item.get("font", "")).strip()
        code_raw = item.get("code")
        value = str(item.get("value", ""))
        if not font or code_raw is None or not value:
            raise PdfArticleExtractionError(
                f"pdf_text.glyph_overrides item {index} is incomplete"
            )
        if isinstance(code_raw, str):
            raw_code = code_raw.strip()
            unicode_match = re.fullmatch(
                r"U\+([0-9A-F]{1,6})", raw_code, flags=re.IGNORECASE
            )
            if unicode_match:
                codepoint = int(unicode_match.group(1), 16)
                if codepoint > 0x10FFFF:
                    raise PdfArticleExtractionError(
                        f"invalid glyph code in pdf_text.glyph_overrides item {index}"
                    )
                semantic_key = f"U+{codepoint:04X}"
                if semantic_key in result[font]:
                    raise PdfArticleExtractionError(
                        f"duplicate glyph override {font} {semantic_key}"
                    )
                result[font][semantic_key] = value
                continue
            raw_match = re.fullmatch(r"RAW:(\d+)", raw_code, flags=re.IGNORECASE)
            if raw_match:
                raw_value = int(raw_match.group(1))
                semantic_key = f"RAW:{raw_value}"
                if semantic_key in result[font]:
                    raise PdfArticleExtractionError(
                        f"duplicate glyph override {font} {semantic_key}"
                    )
                result[font][semantic_key] = value
                continue
            match = re.fullmatch(r"C?(\d+)", raw_code, flags=re.IGNORECASE)
            if not match:
                raise PdfArticleExtractionError(
                    f"invalid glyph code in pdf_text.glyph_overrides item {index}"
                )
            code = int(match.group(1))
        elif isinstance(code_raw, int):
            code = code_raw
        else:
            raise PdfArticleExtractionError(
                f"invalid glyph code in pdf_text.glyph_overrides item {index}"
            )
        semantic_key = f"C{code}"
        if code < 0 or code > 255 or semantic_key in result[font]:
            raise PdfArticleExtractionError(
                f"duplicate or out-of-range glyph override {font} C{code}"
            )
        result[font][semantic_key] = value
    return dict(result)


def _excluded_regions(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = config.get("exclude_regions", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PdfArticleExtractionError("pdf_text.exclude_regions must be a list")
    regions: list[dict[str, Any]] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise PdfArticleExtractionError(
                f"pdf_text.exclude_regions item {index} must be a mapping"
            )
        pages = item.get("pages", "all")
        if pages != "all":
            if not isinstance(pages, list) or not pages or not all(
                isinstance(page, int) and page > 0 for page in pages
            ):
                raise PdfArticleExtractionError(
                    f"pdf_text.exclude_regions item {index} has invalid pages"
                )
            pages = frozenset(pages)
        regions.append(
            {
                "pages": pages,
                "box": _box(item.get("box"), field=f"exclude region {index} box"),
                "reason": str(item.get("reason", "publisher page furniture")),
            }
        )
    return regions


def _region_applies(region: Mapping[str, Any], page: int) -> bool:
    return region["pages"] == "all" or page in region["pages"]


def _region_lines(document: PdfTextDocument) -> dict[str, list[PdfTextLine]]:
    result: dict[str, list[PdfTextLine]] = defaultdict(list)
    for page in document.pages:
        for line in page.lines:
            if line.source_region_id:
                result[line.source_region_id].append(line)
    return dict(result)


def _configured_section_headings(
    config: Mapping[str, Any], document: PdfTextDocument
) -> list[dict[str, Any]]:
    raw = config.get("section_headings", [])
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise PdfArticleExtractionError("pdf_text.section_headings must be a list")
    available = _region_lines(document)
    result: list[dict[str, Any]] = []
    used_keys: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise PdfArticleExtractionError(
                f"pdf_text.section_headings item {index} must be a mapping"
            )
        region_id = str(item.get("region_id", "")).strip()
        title = str(item.get("title", "")).strip()
        level = item.get("level", 2)
        if (
            not title
            or "\n" in title
            or isinstance(level, bool)
            or not isinstance(level, int)
            or not 2 <= level <= 6
        ):
            raise PdfArticleExtractionError(
                f"pdf_text.section_headings item {index} is invalid"
            )
        if region_id:
            key = f"region:{region_id}"
            evidence = available.get(region_id, [])
        else:
            page = item.get("page")
            if not isinstance(page, int) or isinstance(page, bool) or page < 1:
                raise PdfArticleExtractionError(
                    f"pdf_text.section_headings item {index} requires a page"
                )
            box = _box(
                item.get("box"),
                field=f"pdf_text.section_headings item {index} box",
            )
            key = (
                f"box:{page}:"
                + ",".join(f"{value:.6f}" for value in box)
            )
            evidence = _lines_in_region(document, page, box)
        if key in used_keys:
            raise PdfArticleExtractionError(
                f"duplicate configured section-heading target {key!r}"
            )
        used_keys.add(key)
        if not evidence:
            raise PdfArticleExtractionError(
                f"configured section-heading target {key!r} has no OCR text"
            )
        result.append(
            {
                "key": key,
                "title": title,
                "level": level,
                "evidence": evidence,
                "source_heading_text": str(
                    item.get("source_heading_text", "")
                ).strip(),
                "line_keys": {
                    (line.page, line.source_locator) for line in evidence
                },
            }
        )
        source_heading_text = result[-1]["source_heading_text"]
        if source_heading_text:
            combined_plain = _join_fragments(
                line.plain_text for line in evidence
            )
            combined_markdown = _join_fragments(
                _strip_source_emphasis(line.markdown) for line in evidence
            )
            if not combined_plain.startswith(source_heading_text):
                raise PdfArticleExtractionError(
                    f"configured section-heading target {key!r} does not begin "
                    f"with source_heading_text {source_heading_text!r}"
                )
            if not combined_markdown.startswith(source_heading_text):
                raise PdfArticleExtractionError(
                    f"configured section-heading markdown for target {key!r} "
                    "does not begin with source_heading_text"
                )
            result[-1]["body_plain"] = _clean(
                combined_plain[len(source_heading_text) :]
            )
            result[-1]["body_markdown"] = _clean(
                combined_markdown[len(source_heading_text) :]
            )
            if config.get("preserve_source_emphasis", False):
                # Removing an inline heading must not strip meaningful font
                # runs from the rest of the paragraph (compound labels,
                # enzyme names, etc.). Consume only its visible prefix.
                from lxml import etree, html as lxml_html
                from .rich_text import inline_markup_to_safe_html
                rich = inline_markup_to_safe_html(_join_fragments(line.markdown for line in evidence))
                fragment = lxml_html.fragment_fromstring(rich, create_parent="div")
                if not fragment.text_content().startswith(source_heading_text):
                    raise PdfArticleExtractionError("reviewed rich heading prefix differs from source_heading_text")
                remaining = len(source_heading_text)

                def consume(node):
                    nonlocal remaining
                    for owner, attribute in [(node, "text")]:
                        value = getattr(owner, attribute) or ""
                        taken = min(remaining, len(value))
                        setattr(owner, attribute, value[taken:])
                        remaining -= taken
                    for child in node:
                        consume(child)
                        value = child.tail or ""
                        taken = min(remaining, len(value))
                        child.tail = value[taken:]
                        remaining -= taken

                consume(fragment)
                for node in reversed(list(fragment.iterdescendants())):
                    if node.tag in {"strong", "em", "span"} and not node.text_content():
                        node.drop_tag()
                result[-1]["body_markdown"] = _clean((fragment.text or "") + "".join(
                    etree.tostring(child, encoding="unicode", method="html") for child in fragment))
    return result


def _configured_abstract_lines(
    config: Mapping[str, Any], document: PdfTextDocument
) -> tuple[set[str], list[PdfTextLine]]:
    raw = config.get("abstract_region_ids", [])
    if raw is None:
        result: set[str] = set()
    elif not isinstance(raw, list) or not all(
        isinstance(value, str) and value.strip() for value in raw
    ):
        raise PdfArticleExtractionError(
            "pdf_text.abstract_region_ids must be a list of OCR region IDs"
        )
    else:
        result = {value.strip() for value in raw}
        if len(result) != len(raw):
            raise PdfArticleExtractionError(
                "pdf_text.abstract_region_ids contains a duplicate"
            )
    available = _region_lines(document)
    missing = sorted(result - set(available))
    if missing:
        raise PdfArticleExtractionError(
            "configured abstract region(s) have no OCR text: " + ", ".join(missing)
        )
    configured_lines: list[PdfTextLine] = []
    region = config.get("abstract_region")
    regions = config.get("abstract_regions", [])
    if regions is None:
        regions = []
    if not isinstance(regions, list):
        raise PdfArticleExtractionError(
            "pdf_text.abstract_regions must be a list"
        )
    if region is not None and regions:
        raise PdfArticleExtractionError(
            "pdf_text.abstract_region and abstract_regions are mutually exclusive"
        )
    seen_line_keys: set[tuple[int, str]] = set()
    for index, item in enumerate(regions, start=1):
        if not isinstance(item, Mapping):
            raise PdfArticleExtractionError(
                f"pdf_text.abstract_regions item {index} must be a mapping"
            )
        page = item.get("page")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise PdfArticleExtractionError(
                f"pdf_text.abstract_regions item {index} requires a positive page"
            )
        box = _box(
            item.get("box"),
            field=f"pdf_text.abstract_regions item {index} box",
        )
        lines = _lines_in_region(document, page, box)
        if not lines:
            raise PdfArticleExtractionError(
                f"pdf_text.abstract_regions item {index} contains no text"
            )
        for line in lines:
            key = (line.page, line.source_locator)
            if key in seen_line_keys:
                raise PdfArticleExtractionError(
                    "pdf_text.abstract_regions overlap"
                )
            seen_line_keys.add(key)
            configured_lines.append(line)
    if region is not None:
        if not isinstance(region, Mapping):
            raise PdfArticleExtractionError("pdf_text.abstract_region must be a mapping")
        page = region.get("page")
        value = str(region.get("reviewed_value", "")).strip()
        if (
            not isinstance(page, int)
            or isinstance(page, bool)
            or page < 1
            or not value
        ):
            raise PdfArticleExtractionError(
                "pdf_text.abstract_region requires page, box, and reviewed_value"
            )
        box = _box(region.get("box"), field="pdf_text.abstract_region box")
        configured_lines.append(
            PdfTextLine(
                page=page,
                bbox=box,
                plain_text=value,
                markdown=value,
                source_locator=(
                    f"PDF page {page}, box "
                    f"[{box[0]:.2f}, {box[1]:.2f}, {box[2]:.2f}, {box[3]:.2f}]"
                ),
                source_region_id="reviewed-abstract",
            )
        )
    return result, configured_lines


def _configured_reference_start(
    config: Mapping[str, Any], document: PdfTextDocument
) -> dict[str, Any] | None:
    raw = config.get("reference_start")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PdfArticleExtractionError(
            "pdf_text.reference_start must be a mapping"
        )
    page = raw.get("page")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise PdfArticleExtractionError(
            "pdf_text.reference_start requires a positive page"
        )
    box = _box(raw.get("box"), field="pdf_text.reference_start box")
    lines = _lines_in_region(document, page, box)
    candidates = [
        line
        for line in lines
        if _reference_number(_REFERENCE_START.match(_clean(line.plain_text))) == 1
    ]
    if len(candidates) != 1:
        raise PdfArticleExtractionError(
            "pdf_text.reference_start must contain exactly one line beginning "
            "with reference number 1"
        )
    evidence = candidates[0]
    return {
        "line_key": (evidence.page, evidence.source_locator),
        "evidence": evidence,
    }


def _configured_front_matter(
    config: Mapping[str, Any], source_path: str
) -> list[ContentBlock]:
    raw = config.get("front_matter_regions", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PdfArticleExtractionError(
            "pdf_text.front_matter_regions must be a list"
        )
    blocks: list[ContentBlock] = []
    used_ids: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise PdfArticleExtractionError(
                f"pdf_text.front_matter_regions item {index} must be a mapping"
            )
        page = item.get("page")
        field = re.sub(
            r"[^a-z0-9]+", "-", str(item.get("field", "")).casefold()
        ).strip("-")
        group = str(item.get("group", "")).strip()
        label = str(item.get("label", "")).strip()
        value = str(item.get("reviewed_value", "")).strip()
        markdown_value = str(item.get("reviewed_markdown", value)).strip()
        kind = str(item.get("kind", "front_matter")).strip().casefold()
        if (
            not isinstance(page, int)
            or isinstance(page, bool)
            or page < 1
            or not field
            or not value
            or not markdown_value
            or not re.fullmatch(r"[a-z][a-z0-9_]*", kind)
            or "\n" in value
            or "\n" in markdown_value
            or "\n" in label
        ):
            raise PdfArticleExtractionError(
                f"pdf_text.front_matter_regions item {index} is invalid"
            )
        box = _box(
            item.get("box"),
            field=f"pdf_text.front_matter_regions item {index} box",
        )
        suffix = f"-{group}" if group else ""
        block_id = f"front-matter-{field}{suffix}"
        if block_id in used_ids:
            raise PdfArticleExtractionError(
                f"duplicate configured front-matter ID {block_id!r}"
            )
        used_ids.add(block_id)
        locator = (
            f"PDF page {page}, box "
            f"[{box[0]:.2f}, {box[1]:.2f}, {box[2]:.2f}, {box[3]:.2f}]"
        )
        blocks.append(
            ContentBlock(
                block_id=block_id,
                kind=kind,
                markdown=(
                    f"**{label}:** {markdown_value}" if label else markdown_value
                ),
                plain_text=f"{label}: {value}" if label else value,
                source_path=source_path,
                source_locator=locator,
                source_geometry=[{"page": page, "bbox": list(box)}],
            )
        )
    return blocks


def _lines_in_region(
    document: PdfTextDocument,
    page: int,
    box: tuple[float, float, float, float],
) -> list[PdfTextLine]:
    target = next((item for item in document.pages if item.page == page), None)
    if target is None:
        raise PdfArticleExtractionError(f"caption refers to nonexistent PDF page {page}")
    return [line for line in target.lines if _line_in_box(line, box)]


def _figures(
    document: PdfTextDocument,
    source_path: str,
    crop_specs: Iterable[Mapping[str, Any]],
    *, preserve_source_emphasis: bool = False,
) -> tuple[list[FigureItem], dict[tuple[int, str], str]]:
    figures: list[FigureItem] = []
    caption_line_outputs: dict[tuple[int, str], str] = {}
    seen: set[str] = set()
    for index, spec in enumerate(crop_specs, 1):
        category = str(spec.get("category", "")).strip().casefold()
        if category not in {"figure", "scheme", "graphical_abstract"}:
            continue
        figure_id = str(spec.get("asset_id", "")).strip()
        label = str(spec.get("label", "")).strip()
        caption_page = spec.get("caption_page", spec.get("page"))
        caption_box_raw = spec.get("caption_box")
        if (
            not figure_id
            or figure_id in seen
            or not label
            or not isinstance(caption_page, int)
            or caption_box_raw is None
        ):
            raise PdfArticleExtractionError(
                f"PDF visual crop {index} requires a unique ID, label, caption page, and caption box"
            )
        seen.add(figure_id)
        caption_box = _box(caption_box_raw, field=f"PDF visual crop {index} caption_box")
        lines = sorted(
            _lines_in_region(document, caption_page, caption_box),
            key=lambda line: (line.bbox[1], line.bbox[0]),
        )
        for line in lines:
            line_key = (line.page, line.source_locator)
            prior = caption_line_outputs.get(line_key)
            if prior is not None and prior != figure_id:
                raise PdfArticleExtractionError(
                    "one PDF caption line belongs to multiple visual assets"
                )
            caption_line_outputs[line_key] = figure_id
        caption_plain = _join_fragments(line.plain_text for line in lines)
        caption_markdown = _join_fragments(
            (line.markdown if preserve_source_emphasis else _strip_source_emphasis(line.markdown)) for line in lines
        )
        if "caption_reviewed_value" in spec:
            reviewed_caption = spec["caption_reviewed_value"]
            if not isinstance(reviewed_caption, str) or not reviewed_caption.strip():
                raise PdfArticleExtractionError(
                    f"PDF visual crop {index} caption_reviewed_value must be nonempty text"
                )
            # Scanned captions can contain primes, subscripts, or fragmented
            # detector boxes that OCR cannot reproduce reliably.  A private,
            # source-reviewed value may replace the recognized characters,
            # while the exact caption box remains the provenance/evidence.
            caption_plain = reviewed_caption.strip()
            caption_markdown = caption_plain
        figures.append(
            FigureItem(
                figure_id=figure_id,
                source_id=None,
                label=label,
                kind=category,
                caption_markdown=caption_markdown,
                caption_plain=caption_plain,
                source_path=source_path,
                source_locator=(
                    f"PDF page {caption_page}, caption box "
                    f"[{', '.join(f'{value:.2f}' for value in caption_box)}]"
                ),
            )
        )
    return figures, caption_line_outputs


def _visual_regions(crop_specs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for spec in crop_specs:
        category = str(spec.get("category", "")).strip().casefold()
        if category not in {"figure", "scheme", "graphical_abstract", "table"}:
            continue
        for part in spec.get("parts", [spec]):
            page = part.get("page")
            if not isinstance(page, int):
                continue
            result.append(
                {
                    "page": page,
                    "box": _box(part.get("box"), field="PDF crop box"),
                    "output_id": str(spec.get("asset_id", "")).strip(),
                }
            )
    return result


def _split_heading(line: PdfTextLine) -> tuple[str, str, str] | None:
    plain_match = _NUMBERED_HEADING.match(line.plain_text)
    # A procedural sentence such as ``1. Prepare a 1.14× DNA solution`` can
    # superficially match because the decimal point is mistaken for the end
    # of a heading. Reject that exact numeric continuation while retaining
    # legitimate unstyled headings such as ``1. General.`` and
    # ``1. Synthesis.`` found in older PDFs.
    if plain_match and not re.match(r"^\s*(?:\*\*|<strong\b)", line.markdown):
        title_before_period = plain_match.group("title")[:-1].rstrip()
        rest_after_period = plain_match.group("rest").lstrip()
        if (
            title_before_period[-1:].isdigit()
            and rest_after_period[:1].isdigit()
        ) or re.match(
            r"^(?:add|carefully|combine|crush|denature|digest|dissolve|for\b|"
            r"heat|precipitate|prepare|resuspend|set up|thermocycle|to\b|transfer)\b",
            title_before_period,
            flags=re.IGNORECASE,
        ):
            plain_match = None
    if plain_match:
        heading = (
            f"{plain_match.group('number')}. "
            f"{plain_match.group('title').rstrip('.')}"
        )
        rest_plain = _clean(plain_match.group("rest"))
        if not rest_plain:
            return heading, "", ""

        # A wrapped journal heading is often split into several independent
        # bold/italic font runs.  Derive the section title from the reliable
        # plain-text line, then retain scientific sub/sup markup only in the
        # body text after the printed dash separator.
        unstyled_markdown = _clean(_strip_source_emphasis(line.markdown))
        separator = re.search(r"\.\s*[–—]\s*", unstyled_markdown)
        if separator:
            rest_markdown = _clean(unstyled_markdown[separator.end() :])
        else:
            markup_match = _NUMBERED_HEADING.match(unstyled_markdown)
            position = unstyled_markdown.find(rest_plain)
            rest_markdown = (
                _clean(markup_match.group("rest"))
                if markup_match and markup_match.group("title") == plain_match.group("title")
                else _clean(unstyled_markdown[position:]) if position >= 0
                else rest_plain
            )
        return heading, rest_markdown, rest_plain

    markdown_match = _STRONG_PREFIX.match(line.markdown)
    if markdown_match:
        heading = _clean(markdown_match.group("strong")).rstrip(".")
        rest_markdown = _clean(
            _strip_source_emphasis(markdown_match.group("rest"))
        )
        if heading.casefold() in {"experimental part", "reference", "references", "references and notes", "references and footnotes"}:
            rest_plain = re.sub(
                rf"^{re.escape(heading)}\.?\s*",
                "",
                _clean(line.plain_text),
                count=1,
                flags=re.IGNORECASE,
            )
            return heading, rest_markdown, rest_plain
    folded = _clean(line.plain_text).casefold()
    if folded in {
        "introduction",
        "results and discussion",
        "materials and methods",
        "results",
        "discussion",
        "experimental",
        "experimental part",
        "experimental section",
        "acknowledgment",
        "acknowledgments",
        "acknowledgement",
        "acknowledgements",
        "reference",
        "references",
        "references and notes",
        "references and footnotes",
    }:
        return _clean(line.plain_text), "", ""
    return None


def _coalesce_wrapped_headings(lines: list[PdfTextLine]) -> list[PdfTextLine]:
    """Join a bold numbered heading that wraps before its terminal period.

    The journal sets every word in a heading as a separate bold text run.  A
    long heading can therefore end one printed line in a hyphen and finish on
    the next line before body prose begins.  Coalescing only this narrow case
    lets the ordinary heading splitter preserve the section boundary.
    """

    def looks_like_heading_continuation(line: PdfTextLine) -> bool:
        markdown = line.markdown.lstrip()
        return bool(re.match(r"^(?:<sup>)?\*{2,}", markdown))

    result: list[PdfTextLine] = []
    index = 0
    while index < len(lines):
        current = lines[index]
        plain = _clean(current.plain_text)
        if (
            re.match(r"^\d+\.\s+", plain)
            and _NUMBERED_HEADING.match(plain) is None
            and current.markdown.lstrip().startswith("**")
        ):
            merged = [current]
            cursor = index + 1
            combined_plain = plain
            combined_markdown = _clean(current.markdown)
            combined_line: PdfTextLine | None = None
            while cursor < len(lines) and cursor <= index + 2:
                following = lines[cursor]
                if (
                    following.page != current.page
                    or not looks_like_heading_continuation(following)
                ):
                    break
                merged.append(following)
                if following.markdown.lstrip().startswith("<sup>"):
                    combined_plain += _clean(following.plain_text)
                    combined_markdown += _clean(following.markdown)
                else:
                    combined_plain = _join_fragments(
                        (combined_plain, following.plain_text)
                    )
                    combined_markdown = _join_fragments(
                        (combined_markdown, following.markdown)
                    )
                cursor += 1
                if _NUMBERED_HEADING.match(combined_plain):
                    left = min(line.bbox[0] for line in merged)
                    top = min(line.bbox[1] for line in merged)
                    right = max(line.bbox[2] for line in merged)
                    bottom = max(line.bbox[3] for line in merged)
                    constituent_geometry = tuple(
                        segment
                        for merged_line in merged
                        for segment in (
                            merged_line.constituent_geometry
                            or ((merged_line.page, merged_line.bbox),)
                        )
                    )
                    combined_line = replace(
                        current,
                        bbox=(left, top, right, bottom),
                        plain_text=combined_plain,
                        markdown=combined_markdown,
                        source_locator=(
                            f"PDF page {current.page}, box "
                            f"[{left:.2f}, {top:.2f}, {right:.2f}, {bottom:.2f}]"
                        ),
                        constituent_geometry=constituent_geometry,
                    )
                    break
            if combined_line is not None:
                result.append(combined_line)
                index = cursor
                continue
        result.append(current)
        index += 1
    return result


def _new_paragraph(previous: PdfTextLine, current: PdfTextLine) -> bool:
    if previous.page != current.page:
        return previous.plain_text.rstrip().endswith((".", "?", "!"))
    if (
        previous.source_region_id
        and current.source_region_id
        and previous.source_region_id != current.source_region_id
    ):
        # OCR regions are already supplied in reviewed reading order.  A
        # column/region transition is therefore analogous to a page break,
        # not a large first-line indent.  Continue an unfinished sentence and
        # split only when the preceding region ends with terminal punctuation.
        return previous.plain_text.rstrip().endswith((".", "?", "!"))
    if (
        previous.plain_text.rstrip().endswith("-")
        and re.match(r"^[a-z]", current.plain_text.lstrip())
    ):
        # A hanging numbered/list item can indent its continuation far enough
        # to resemble a new paragraph.  An unfinished lowercase word after a
        # line-end hyphen is stronger evidence of visual line wrapping.
        return False
    if abs(current.bbox[1] - previous.bbox[1]) <= 1.0:
        # Justified manuscript text can be decoded as several horizontal runs
        # on one printed baseline. A larger x coordinate is then the next word,
        # not a first-line indent announcing a new paragraph.
        return False
    previous_height = max(previous.bbox[3] - previous.bbox[1], 1.0)
    gap = current.bbox[1] - previous.bbox[3]
    # In this journal, first lines are indented while continuations return to
    # the left margin.  Require a material indent so glyph jitter cannot split.
    if current.bbox[0] - previous.bbox[0] > 6.0:
        return True
    # Manuscript-layout PDFs are often double-spaced: their ordinary line gap
    # can be roughly twice the glyph height, exactly like the visual gap
    # between paragraphs. When first-line indentation is available, retain
    # those same-margin lines and reserve the geometry-only fallback for a
    # materially larger vertical separation.
    return gap > previous_height * 3.0


def _make_block(
    block_id: str,
    kind: str,
    lines: list[PdfTextLine],
    source_path: str,
    *,
    markdown_prefix: str = "",
    plain_prefix: str = "",
    preserve_source_emphasis: bool = False,
) -> ContentBlock:
    if not lines and not (markdown_prefix or plain_prefix):
        raise PdfArticleExtractionError("cannot make an empty PDF content block")
    markdown = _join_fragments(
        [
            markdown_prefix,
            *((line.markdown if preserve_source_emphasis else _strip_source_emphasis(line.markdown)) for line in lines),
        ]
    )
    plain = _join_fragments([plain_prefix, *(line.plain_text for line in lines)])
    page = lines[0].page if lines else 1
    return ContentBlock(
        block_id=block_id,
        kind=kind,
        markdown=markdown,
        plain_text=plain,
        source_path=source_path,
        source_locator=_source_locator(page, lines),
        source_geometry=_source_geometry(lines),
    )


def _apply_repairs(
    article: ArticleExtraction, repair_specs: Iterable[Mapping[str, Any]]
) -> None:
    text_fields: list[tuple[Any, str]] = []
    for section in article.sections:
        text_fields.append((section, "heading"))
        for block in section.blocks:
            text_fields.extend(((block, "markdown"), (block, "plain_text")))
    for block in article.references:
        text_fields.extend(((block, "markdown"), (block, "plain_text")))
    for figure in article.figures:
        text_fields.extend(((figure, "caption_markdown"), (figure, "caption_plain")))

    for index, spec in enumerate(repair_specs, 1):
        pattern = str(spec.get("pattern", ""))
        replacement = str(spec.get("replacement", ""))
        if not pattern:
            continue
        expected_matches = spec.get("expected_matches")
        if expected_matches is not None and (
            isinstance(expected_matches, bool)
            or not isinstance(expected_matches, int)
            or expected_matches < 1
        ):
            raise PdfArticleExtractionError(
                f"text repair {index} expected_matches must be a positive integer"
            )
        total = 0
        for owner, field in text_fields:
            value = getattr(owner, field)
            updated, count = re.subn(pattern, replacement, value)
            if count:
                setattr(owner, field, updated)
                total += count
        # Markdown and plain fields normally produce the same match; report
        # semantic occurrences, not storage-field writes.
        occurrences = (total + 1) // 2
        if expected_matches is not None and occurrences != expected_matches:
            raise PdfArticleExtractionError(
                f"text repair {index} matched {occurrences} times; "
                f"expected {expected_matches}"
            )
        if occurrences:
            article.repairs.append(
                Repair(
                    repair_id=f"repair-{index:03d}",
                    pattern=pattern,
                    replacement=replacement,
                    occurrences=occurrences,
                    reason=str(spec.get("reason", "private reviewed override")),
                    evidence=str(spec.get("evidence", "")),
                )
            )


def _reviewed_native_regions(document: PdfTextDocument, config: Mapping[str, Any]) -> None:
    """Replace defective hidden PDF text only in explicitly reviewed regions.

    The archived PDF hash pins the transcription; native region boundaries
    retain source coverage and every replacement carries its own geometry.
    This is for source-specific corrupt OCR layers, never automatic repair.
    """
    specs = config.get("reviewed_native_regions", [])
    if not specs:
        return
    if not isinstance(specs, list) or not re.fullmatch(r"[0-9a-f]{64}", str(config.get("source_sha256", ""))):
        raise PdfArticleExtractionError("reviewed_native_regions requires a pinned PDF hash and a list")
    regions = {item["region_id"]: item for item in config.get("native_reading_regions", [])}
    seen: set[str] = set()
    for spec in specs:
        if not isinstance(spec, Mapping):
            raise PdfArticleExtractionError("reviewed native region must be a mapping")
        region_id = spec.get("region_id")
        region = regions.get(region_id)
        if region is None or region_id in seen or not str(spec.get("reason", "")).strip() or not str(spec.get("evidence", "")).strip():
            raise PdfArticleExtractionError("reviewed native region requires unique existing region and evidence")
        seen.add(region_id)
        page = next((p for p in document.pages if p.page == region["page"]), None)
        original = [line for line in page.lines if line.source_region_id == region_id] if page else []
        raw_lines = spec.get("lines")
        if not original or not isinstance(raw_lines, list) or not raw_lines:
            raise PdfArticleExtractionError("reviewed native region must replace existing text with nonempty lines")
        replacement = []
        outer = region["box"]
        for number, raw in enumerate(raw_lines, 1):
            box = _box(raw.get("box"), field="reviewed native line box")
            plain = str(raw.get("reviewed_value", "")).strip()
            markdown = str(raw.get("reviewed_markdown", plain)).strip()
            if not plain or not markdown or "\n" in plain or box[0] < outer[0] or box[1] < outer[1] or box[2] > outer[2] or box[3] > outer[3]:
                raise PdfArticleExtractionError("reviewed native line must be nonempty and stay inside its source region")
            replacement.append(PdfTextLine(page=page.page, bbox=box, plain_text=plain, markdown=markdown,
                source_locator=f"PDF page {page.page}, reviewed text box {list(box)}",
                source_region_id=f"{region_id}-reviewed-{number}"))
        first = next(i for i, line in enumerate(page.lines) if line.source_region_id == region_id)
        page.lines[:] = page.lines[:first] + replacement + [line for line in page.lines[first:] if line.source_region_id != region_id]
        document.diagnostic_rows.append({"kind": "reviewed_native_region", "status": "reviewed", "source_path": document.relative_path,
            "page": page.page, "region_id": region_id, "box": outer, "original_line_count": len(original),
            "reviewed_line_count": len(replacement), "reason": spec["reason"], "evidence": spec["evidence"]})


def _reviewed_image_only_regions(
    document: PdfTextDocument, config: Mapping[str, Any]
) -> None:
    """Add an exact reviewed transcription for declared text on image-only pages.

    This is a fail-closed companion to ``reviewed_native_regions`` for a mixed
    PDF whose otherwise native text layer contains one or more rasterized
    pages.  Every configured reading region on an affected page must be
    transcribed explicitly.  Figure, scheme, chart and table pixels remain
    outside these regions and are preserved through reviewed visual crops.
    """

    specs = config.get("reviewed_image_only_regions", [])
    if not specs:
        return
    if not isinstance(specs, list) or not re.fullmatch(
        r"[0-9a-f]{64}", str(config.get("source_sha256", ""))
    ):
        raise PdfArticleExtractionError(
            "reviewed_image_only_regions requires a pinned PDF hash and a list"
        )
    raw_regions = config.get("native_reading_regions", [])
    if not isinstance(raw_regions, list):
        raise PdfArticleExtractionError(
            "reviewed_image_only_regions requires native_reading_regions"
        )
    regions = {item.get("region_id"): item for item in raw_regions if isinstance(item, Mapping)}
    if len(regions) != len(raw_regions):
        raise PdfArticleExtractionError(
            "reviewed image-only text requires uniquely named reading regions"
        )

    additions: dict[int, dict[str, list[PdfTextLine]]] = defaultdict(dict)
    seen: set[str] = set()
    for spec in specs:
        if not isinstance(spec, Mapping):
            raise PdfArticleExtractionError(
                "reviewed image-only region must be a mapping"
            )
        region_id = spec.get("region_id")
        region = regions.get(region_id)
        if (
            region is None
            or region_id in seen
            or not str(spec.get("reason", "")).strip()
            or not str(spec.get("evidence", "")).strip()
        ):
            raise PdfArticleExtractionError(
                "reviewed image-only region requires unique existing region and evidence"
            )
        seen.add(region_id)
        page_number = region.get("page")
        page = next((item for item in document.pages if item.page == page_number), None)
        if page is None or page.classification != "image_only" or page.lines:
            raise PdfArticleExtractionError(
                "reviewed image-only region must target an otherwise empty image-only page"
            )
        raw_lines = spec.get("lines")
        if not isinstance(raw_lines, list) or not raw_lines:
            raise PdfArticleExtractionError(
                "reviewed image-only region requires nonempty reviewed lines"
            )
        outer = _box(region.get("box"), field="reviewed image-only region box")
        replacement: list[PdfTextLine] = []
        for number, raw in enumerate(raw_lines, 1):
            if not isinstance(raw, Mapping):
                raise PdfArticleExtractionError(
                    "reviewed image-only line must be a mapping"
                )
            box = _box(raw.get("box"), field="reviewed image-only line box")
            plain = str(raw.get("reviewed_value", "")).strip()
            markdown = str(raw.get("reviewed_markdown", plain)).strip()
            if (
                not plain
                or not markdown
                or "\n" in plain
                or box[0] < outer[0]
                or box[1] < outer[1]
                or box[2] > outer[2]
                or box[3] > outer[3]
            ):
                raise PdfArticleExtractionError(
                    "reviewed image-only line must be nonempty and stay inside its source region"
                )
            replacement.append(
                PdfTextLine(
                    page=page.page,
                    bbox=box,
                    plain_text=plain,
                    markdown=markdown,
                    source_locator=(
                        f"PDF page {page.page}, reviewed image-only text box {list(box)}"
                    ),
                    source_region_id=f"{region_id}-reviewed-{number}",
                )
            )
        additions[page.page][str(region_id)] = replacement
        document.diagnostic_rows.append(
            {
                "kind": "reviewed_image_only_region",
                "status": "reviewed",
                "source_path": document.relative_path,
                "page": page.page,
                "region_id": region_id,
                "box": list(outer),
                "reviewed_line_count": len(replacement),
                "reason": spec["reason"],
                "evidence": spec["evidence"],
            }
        )

    for page_number, page_additions in additions.items():
        declared = [
            str(item["region_id"])
            for item in raw_regions
            if item.get("page") == page_number
        ]
        if not declared or set(declared) != set(page_additions):
            raise PdfArticleExtractionError(
                "every reading region on a reviewed image-only page must be transcribed"
            )
        page = next(item for item in document.pages if item.page == page_number)
        page.lines.extend(
            line
            for region_id in declared
            for line in page_additions[region_id]
        )
        document.warnings[:] = [
            warning
            for warning in document.warnings
            if not (
                warning.get("code") == "image_only_page"
                and warning.get("page") == page_number
            )
        ]
        document.diagnostic_rows.append(
            {
                "kind": "reviewed_image_only_page",
                "status": "reviewed",
                "source_path": document.relative_path,
                "page": page_number,
                "region_ids": declared,
            }
        )


def extract_pdf_article(
    source: Path,
    source_path: str,
    metadata: RecordMetadata,
    config: Mapping[str, Any] | None = None,
    *,
    crop_specs: Iterable[Mapping[str, Any]] = (),
    text_repairs: Iterable[Mapping[str, Any]] = (),
    text_document: PdfTextDocument | None = None,
    text_extraction: Mapping[str, Any] | None = None,
) -> ArticleExtraction:
    """Extract one PDF-only article without applying OCR to visual assets.

    ``text_document`` may contain deterministic native decoding or ordered OCR
    observations.  OCR itself happens upstream so this semantic layer never
    receives figure or scheme pixels.
    """

    config = config or {}
    if not isinstance(config, Mapping):
        raise PdfArticleExtractionError("pdf_text override must be a mapping")
    preserve_emphasis = config.get("preserve_source_emphasis", False)
    if not isinstance(preserve_emphasis, bool):
        raise PdfArticleExtractionError("pdf_text.preserve_source_emphasis must be boolean")
    # Opt in only after source review establishes that the native font runs
    # carry authored meaning (for example, bold mismatch bases in DNA sites).
    def make_block(*args: Any, **kwargs: Any) -> ContentBlock:
        return _make_block(*args, preserve_source_emphasis=preserve_emphasis, **kwargs)
    configured_source = str(config.get("source_path", source_path)).replace("\\", "/")
    if configured_source != source_path.replace("\\", "/"):
        raise PdfArticleExtractionError("pdf_text.source_path does not match the main PDF")
    configured_hash = str(config.get("source_sha256", "")).strip().casefold()
    if configured_hash and configured_hash != sha256_file(source):
        raise PdfArticleExtractionError("pdf_text.source_sha256 does not match the main PDF")
    title_region = config.get("title_region")
    if title_region is not None:
        if not isinstance(title_region, Mapping):
            raise PdfArticleExtractionError("pdf_text.title_region must be a mapping")
        title_page = title_region.get("page")
        reviewed_title = str(title_region.get("reviewed_value", "")).strip()
        if (
            not isinstance(title_page, int)
            or isinstance(title_page, bool)
            or title_page < 1
            or reviewed_title != metadata.title
        ):
            raise PdfArticleExtractionError(
                "pdf_text.title_region must identify the exact metadata title"
            )
        _box(title_region.get("box"), field="pdf_text.title_region box")

    if text_document is None:
        document = extract_pdf_text(
            source,
            source_path,
            glyph_overrides=_glyph_overrides(config),
            reading_regions=config.get("native_reading_regions"),
            word_gap_points=config.get('word_gap_points'),
        )
    else:
        document = text_document
        if document.relative_path.replace("\\", "/") != source_path.replace("\\", "/"):
            raise PdfArticleExtractionError(
                "provided PDF text document does not match the main PDF"
            )
    # Source-reviewed replacements need not inherit a detector confidence:
    # that score describes the discarded OCR observation, not the reviewed
    # transcription. Preserve whether OCR ran before replacing those lines.
    ocr_pages = {
        page.page for page in document.pages
        if any(line.ocr_confidence is not None for line in page.lines)
    }
    ocr_pages.update(
        row["page"] for row in document.diagnostic_rows
        if row.get("kind") == "ocr_region" and isinstance(row.get("page"), int)
    )
    _reviewed_native_regions(document, config)
    _reviewed_image_only_regions(document, config)
    if not document.all_pages_classified:
        raise PdfArticleExtractionError("not every PDF page received a text classification")

    configured_headings = _configured_section_headings(config, document)
    configured_abstract_headings = [
        item
        for item in configured_headings
        if str(item["title"]).casefold() == "abstract"
    ]
    if len(configured_abstract_headings) > 1:
        raise PdfArticleExtractionError(
            "pdf_text.section_headings must identify at most one Abstract heading"
        )
    configured_abstract_heading = (
        configured_abstract_headings[0] if configured_abstract_headings else None
    )
    configured_reference_start = _configured_reference_start(config, document)
    reference_entry_starts = config.get('reference_entry_starts', [])
    if not isinstance(reference_entry_starts, list):
        raise PdfArticleExtractionError('reference_entry_starts must be a list')
    reference_entry_keys = {}
    for number, spec in enumerate(reference_entry_starts, 1):
        if not isinstance(spec, Mapping) or not isinstance(spec.get('page'), int):
            raise PdfArticleExtractionError('reference_entry_starts requires page and exact first-line box')
        lines = _lines_in_region(document, spec['page'], _box(spec.get('box'), field='reference entry box'))
        if len(lines) != 1 or lines[0].plain_text != spec.get('text'):
            raise PdfArticleExtractionError('reference entry boundary differs from reviewed first line')
        key = (lines[0].page, lines[0].source_locator)
        if key in reference_entry_keys:
            raise PdfArticleExtractionError('duplicate reference entry boundary')
        reference_entry_keys[key] = number
    paragraph_break_regions_raw = config.get("paragraph_break_before_regions", [])
    if (
        not isinstance(paragraph_break_regions_raw, list)
        or any(
            not isinstance(region_id, str) or not region_id.strip()
            for region_id in paragraph_break_regions_raw
        )
    ):
        raise PdfArticleExtractionError(
            "pdf_text.paragraph_break_before_regions must be a list of "
            "non-empty region IDs"
        )
    paragraph_break_regions = [
        region_id.strip() for region_id in paragraph_break_regions_raw
    ]
    if len(paragraph_break_regions) != len(set(paragraph_break_regions)):
        raise PdfArticleExtractionError(
            "pdf_text.paragraph_break_before_regions must not contain duplicates"
        )
    paragraph_break_region_ids = set(paragraph_break_regions)
    for reviewed_region in config.get("reviewed_native_regions", []):
        for number, reviewed_line in enumerate(reviewed_region["lines"], 1):
            if reviewed_line.get("paragraph_break", True):
                paragraph_break_region_ids.add(f"{reviewed_region['region_id']}-reviewed-{number}")
    abstract_region_ids, reviewed_abstract_lines = _configured_abstract_lines(
        config, document
    )
    configured_abstract_line_keys = {
        (line.page, line.source_locator)
        for line in reviewed_abstract_lines
        if line.source_region_id != "reviewed-abstract"
    }
    heading_region_ids = {
        str(item["key"])[len("region:") :]
        for item in configured_headings
        if str(item["key"]).startswith("region:")
    }
    if heading_region_ids & abstract_region_ids:
        raise PdfArticleExtractionError(
            "an OCR region cannot be both an abstract and a section heading"
        )

    # Record overrides can contain crops from the main article and several
    # supplements. A page number is meaningful only within its source PDF:
    # never let a supplement's table suppress main text at the same coordinates,
    # or pair a supplement's figure with an unrelated main-article caption.
    # Unqualified crops remain supported for direct legacy callers.
    crop_specs = [
        spec for spec in crop_specs
        if str(spec.get("source_path", spec.get("source", source_path))).replace("\\", "/")
        == source_path.replace("\\", "/")
    ]
    figures, caption_keys = _figures(document, source_path, crop_specs, preserve_source_emphasis=preserve_emphasis)
    visual_regions = _visual_regions(crop_specs)
    excluded_regions = _excluded_regions(config)

    body_lines: list[PdfTextLine] = []
    excluded_counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    excluded_output_ids: dict[int, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for page in document.pages:
        for line in page.lines:
            key = (line.page, line.source_locator)
            caption_output_id = caption_keys.get(key)
            if caption_output_id is not None:
                excluded_counts[line.page]["caption"] += 1
                excluded_output_ids[line.page]["caption"].add(caption_output_id)
                continue
            if line.rotated:
                excluded_counts[line.page]["rotated_furniture"] += 1
                continue
            if any(
                _region_applies(region, line.page)
                and _line_in_box(line, region["box"])
                for region in excluded_regions
            ):
                excluded_counts[line.page]["configured_furniture"] += 1
                continue
            matching_visuals = [
                region
                for region in visual_regions
                if (
                region["page"] == line.page and _line_in_box(line, region["box"])
                )
            ]
            if matching_visuals:
                excluded_counts[line.page]["visual_asset"] += 1
                excluded_output_ids[line.page]["visual_asset"].update(
                    region["output_id"]
                    for region in matching_visuals
                    if region["output_id"]
                )
                continue
            if _clean(line.plain_text):
                body_lines.append(line)

    # Reviewed OCR plans already encode multi-column reading order.  A global
    # geometry sort would interleave their columns, so only native PDF lines
    # use the legacy page/y/x ordering.
    if not any(line.source_region_id for line in body_lines):
        body_lines.sort(key=lambda line: (line.page, line.bbox[1], line.bbox[0]))

    front_matter: list[ContentBlock] = _configured_front_matter(config, source_path)
    abstract_lines: list[PdfTextLine] = list(reviewed_abstract_lines)
    retained: list[PdfTextLine] = []
    if abstract_region_ids or reviewed_abstract_lines or config.get("abstract_region_ids") == []:
        for line in body_lines:
            key = (line.page, line.source_locator)
            if key in configured_abstract_line_keys:
                continue
            if line.source_region_id in abstract_region_ids:
                abstract_lines.append(line)
            else:
                retained.append(line)
    else:
        before_first_heading = True
        affiliation: list[PdfTextLine] = []
        for line in body_lines:
            text = _clean(line.plain_text)
            heading = _split_heading(line)
            if line.page == 1 and before_first_heading:
                if heading and (
                    heading[0].startswith("1. ")
                    or heading[0].casefold() == "introduction"
                ):
                    before_first_heading = False
                    retained.append(line)
                    continue
                folded = text.casefold()
                if (
                    (len(folded) >= 12 and folded in metadata.title.casefold())
                    or folded.startswith("by ")
                ):
                    excluded_counts[1]["title_or_authors"] += 1
                    continue
                if "division of" in folded or affiliation:
                    if folded.startswith("dedicated to"):
                        if affiliation:
                            front_matter.append(
                                make_block(
                                    "front-matter-affiliation",
                                    "front_matter",
                                    affiliation,
                                    source_path,
                                    markdown_prefix="**Affiliation:**",
                                    plain_prefix="Affiliation:",
                                )
                            )
                            affiliation = []
                        front_matter.append(
                            make_block(
                                "front-matter-dedication",
                                "front_matter",
                                [line],
                                source_path,
                                markdown_prefix="**Dedication:**",
                                plain_prefix="Dedication:",
                            )
                        )
                        continue
                    affiliation.append(line)
                    continue
                # The unlabeled, ruled paragraph between the dedication and
                # first heading is the journal abstract.
                if text:
                    abstract_lines.append(line)
                    continue
            if _RECEIVED.match(text):
                front_matter.append(
                    make_block(
                        "front-matter-article-history",
                        "front_matter",
                        [line],
                        source_path,
                        markdown_prefix="**Article history:**",
                        plain_prefix="Article history:",
                    )
                )
                excluded_counts[line.page]["article_history"] += 1
                continue
            retained.append(line)
        if affiliation:
            front_matter.append(
                make_block(
                    "front-matter-affiliation",
                    "front_matter",
                    affiliation,
                    source_path,
                    markdown_prefix="**Affiliation:**",
                    plain_prefix="Affiliation:",
                )
            )

    retained = _coalesce_wrapped_headings(retained)

    sections: list[Section] = []
    abstract_heading_evidence = (
        list(configured_abstract_heading["evidence"])
        if configured_abstract_heading is not None
        else []
    )
    abstract = Section(
        section_id="section-abstract",
        heading="Abstract",
        source_path=source_path if abstract_heading_evidence or abstract_lines else "",
        source_locator=(
            _source_locator(
                abstract_heading_evidence[0].page, abstract_heading_evidence
            )
            if abstract_heading_evidence
            else _source_locator(abstract_lines[0].page, abstract_lines)
            if abstract_lines
            else ""
        ),
        source_geometry=_source_geometry(
            abstract_heading_evidence or abstract_lines
        ),
    )
    if abstract_lines:
        abstract.blocks.append(
            make_block("main-abstract-0001", "abstract", abstract_lines, source_path)
        )
        sections.append(abstract)

    references: list[ContentBlock] = []
    current_section: Section | None = None
    current_lines: list[PdfTextLine] = []
    current_prefix_markdown = ""
    current_prefix_plain = ""
    section_counts: dict[str, int] = defaultdict(int)
    block_number = 0
    reference_lines: list[PdfTextLine] = []
    references_mode = False
    unnumbered_reference_mode = False
    unnumbered_reference_left: float | None = None
    experimental_mode = False

    def flush_paragraph() -> None:
        nonlocal current_lines, current_prefix_markdown, current_prefix_plain, block_number
        if current_section is None or not (
            current_lines or current_prefix_markdown or current_prefix_plain
        ):
            current_lines = []
            current_prefix_markdown = ""
            current_prefix_plain = ""
            return
        block_number += 1
        current_section.blocks.append(
            make_block(
                f"main-paragraph-{block_number:04d}",
                "paragraph",
                current_lines,
                source_path,
                markdown_prefix=current_prefix_markdown,
                plain_prefix=current_prefix_plain,
            )
        )
        current_lines = []
        current_prefix_markdown = ""
        current_prefix_plain = ""

    def start_section(
        heading: str,
        evidence_lines: Iterable[PdfTextLine] = (),
        *,
        explicit_level: int | None = None,
    ) -> None:
        nonlocal current_section, experimental_mode
        evidence = list(evidence_lines)
        folded = heading.casefold()
        if folded in {"experimental", "experimental part", "experimental section"}:
            level = 2
            experimental_mode = True
        elif experimental_mode and re.match(r"^\d+\.\s", heading):
            level = 3
        else:
            level = 2
        if explicit_level is not None:
            level = explicit_level
        base = re.sub(r"[^a-z0-9]+", "-", heading.casefold()).strip("-") or "section"
        section_counts[base] += 1
        suffix = "" if section_counts[base] == 1 else f"-{section_counts[base]}"
        current_section = Section(
            section_id=f"section-{base}{suffix}",
            heading=heading.rstrip("."),
            level=level,
            source_path=source_path if evidence else "",
            source_locator=(
                _source_locator(evidence[0].page, evidence) if evidence else ""
            ),
            source_geometry=_source_geometry(evidence),
        )
        sections.append(current_section)

    started_configured_headings: set[str] = set()
    started_configured_reference = False
    for line in retained:
        text = _clean(line.plain_text)
        line_key = (line.page, line.source_locator)
        matching_headings = [
            item for item in configured_headings if line_key in item["line_keys"]
        ]
        if len(matching_headings) > 1:
            raise PdfArticleExtractionError(
                f"OCR line {line.source_locator!r} belongs to multiple configured headings"
            )
        if matching_headings:
            configured_heading = matching_headings[0]
            heading_key = str(configured_heading["key"])
            if heading_key in started_configured_headings:
                continue
            started_configured_headings.add(heading_key)
            heading_title = str(configured_heading["title"])
            heading_level = int(configured_heading["level"])
            heading_evidence = list(configured_heading["evidence"])
            flush_paragraph()
            if heading_title.casefold() == "abstract" and abstract_lines:
                if configured_heading.get("body_plain") or configured_heading.get(
                    "body_markdown"
                ):
                    raise PdfArticleExtractionError(
                        "a configured Abstract heading must not contain body text"
                    )
                continue
            if heading_title.casefold() in {"reference", "references", "references and notes", "references and footnotes"}:
                references_mode = True
                continue
            start_section(
                heading_title,
                heading_evidence,
                explicit_level=heading_level,
            )
            body_plain = str(configured_heading.get("body_plain", ""))
            body_markdown = str(configured_heading.get("body_markdown", ""))
            if body_plain or body_markdown:
                current_prefix_plain = body_plain
                current_prefix_markdown = body_markdown
                # All configured heading lines are skipped by the routing loop.
                # Retain their geometry once, with blank text, so body prose
                # printed after the heading remains traceable without rendering
                # the configured title twice.
                current_lines = [
                    replace(evidence_line, markdown="", plain_text="")
                    for evidence_line in heading_evidence
                ]
            continue
        if (
            configured_reference_start is not None
            and line_key == configured_reference_start["line_key"]
        ):
            flush_paragraph()
            references_mode = True
            started_configured_reference = True
        if references_mode:
            if reference_entry_keys:
                entry_number = reference_entry_keys.get(line_key)
                if entry_number is not None:
                    if entry_number != len(references) + (2 if reference_lines else 1):
                        raise PdfArticleExtractionError('reviewed reference entries are out of order')
                    if reference_lines:
                        references.append(make_block(f'reference-{len(references)+1:03d}', 'reference', reference_lines, source_path))
                        reference_lines = []
                if not reference_lines and entry_number is None:
                    raise PdfArticleExtractionError('reference content precedes reviewed first entry')
                reference_lines.append(line)
                continue
            next_match = _REFERENCE_START.match(text)
            if unnumbered_reference_mode:
                if (
                    reference_lines
                    and unnumbered_reference_left is not None
                    and abs(line.bbox[0] - unnumbered_reference_left) <= 2.0
                    and _AUTHOR_YEAR_REFERENCE_START.match(text)
                ):
                    references.append(
                        make_block(
                            f"reference-{len(references) + 1:03d}",
                            "reference",
                            reference_lines,
                            source_path,
                        )
                    )
                    reference_lines = []
                reference_lines.append(line)
                continue
            current_number = (
                _reference_number(_REFERENCE_START.match(reference_lines[0].plain_text))
                if reference_lines
                else None
            )
            next_number = _reference_number(next_match)
            if not reference_lines and next_number is None:
                if not _AUTHOR_YEAR_REFERENCE_START.match(text):
                    raise PdfArticleExtractionError(
                        "reference text before the first numbered or author-year entry: "
                        f"{line.plain_text!r}"
                    )
                unnumbered_reference_mode = True
                unnumbered_reference_left = line.bbox[0]
                reference_lines.append(line)
                continue
            if (
                reference_lines
                and current_number is not None
                and next_number == current_number + 1
            ):
                number = current_number
                if number is None:
                    raise PdfArticleExtractionError(
                        "reference text before the first numbered entry: "
                        f"{reference_lines[0].plain_text!r}"
                    )
                references.append(
                    make_block(
                        f"reference-{number:03d}",
                        "reference",
                        reference_lines,
                        source_path,
                    )
                )
                reference_lines = []
            reference_lines.append(line)
            continue

        heading = _split_heading(line)
        if (heading and experimental_mode and current_lines
                and re.match(r"^\d+\.\s", heading[0])
                and current_lines[-1].plain_text.strip()
                and not current_lines[-1].plain_text.rstrip().endswith((".", "?", "!"))
                and not _new_paragraph(current_lines[-1], line)):
            # A wrapped experimental paragraph can begin its next line with
            # a spectrum value or compound number followed by a period.
            # Preserve that geometrically continuous unfinished sentence;
            # explicitly configured headings were already routed above.
            heading = None
        if heading and heading[0].casefold() in {"reference", "references", "references and notes", "references and footnotes"}:
            flush_paragraph()
            references_mode = True
            if heading[1] or heading[2]:
                reference_lines.append(
                    replace(
                        line,
                        markdown=heading[1],
                        plain_text=heading[2],
                    )
                )
            continue
        if heading:
            flush_paragraph()
            start_section(heading[0], [line])
            if heading[1] or heading[2]:
                current_prefix_markdown = heading[1]
                current_prefix_plain = heading[2]
                # Retain the heading line as source geometry for body text
                # printed after the heading separator, without rendering its
                # text twice. This also gives multi-page blocks an honest
                # page-range locator.
                current_lines = [replace(line, markdown="", plain_text="")]
            continue
        if text.startswith("We are grateful for financial support"):
            flush_paragraph()
            start_section("Acknowledgments", [line])
        if current_section is None:
            start_section("Article Text", [line])
        if (
            current_lines
            and line.source_region_id in paragraph_break_region_ids
            and line.source_region_id != current_lines[-1].source_region_id
        ):
            flush_paragraph()
        # A reviewed OCR plan already declares the order of distinct regions
        # (for example, a left column followed by a right column). Geometry
        # and OCR punctuation at that boundary are not reliable evidence of a
        # new paragraph. Callers that observed a real paragraph boundary use
        # ``paragraph_break_before_regions`` above; retain the default flow
        # across all other reviewed region transitions.
        same_region = (
            current_lines
            and current_lines[-1].source_region_id == line.source_region_id
        )
        native_geometry = current_lines and not (
            current_lines[-1].source_region_id or line.source_region_id
        )
        if current_lines and (same_region or native_geometry) and _new_paragraph(
            current_lines[-1], line
        ):
            flush_paragraph()
        current_lines.append(line)

    flush_paragraph()
    if reference_lines:
        match = _REFERENCE_START.match(reference_lines[0].plain_text)
        reference_number = (
            len(references) + 1
            if reference_entry_keys or unnumbered_reference_mode
            else _reference_number(match)
        )
        if reference_number is None:
            raise PdfArticleExtractionError("reference continuation has no numbered start")
        references.append(
            make_block(
                f"reference-{reference_number:03d}",
                "reference",
                reference_lines,
                source_path,
            )
        )

    missing_configured_headings = sorted(
        {str(item["key"]) for item in configured_headings}
        - started_configured_headings
    )
    if missing_configured_headings:
        raise PdfArticleExtractionError(
            "configured section-heading region(s) were not routed into the article: "
            + ", ".join(missing_configured_headings)
        )
    if configured_reference_start is not None and not started_configured_reference:
        raise PdfArticleExtractionError(
            "configured reference-start line was not routed into the article"
        )
    if reference_entry_keys and len(references) != len(reference_entry_keys):
        raise PdfArticleExtractionError('not every reviewed reference entry was extracted')
    if not sections or not any(section.blocks for section in sections):
        raise PdfArticleExtractionError("no article text blocks were reconstructed")
    reference_numbers = [
        number
        for block in references
        if (number := _reference_number(_REFERENCE_START.match(block.plain_text)))
        is not None
    ]
    if reference_numbers and reference_numbers != list(
        range(1, reference_numbers[-1] + 1)
    ):
        # Some archived publications contain an authored numbering gap. Keep
        # those labels only with exact, source-pinned reviewed boundaries and
        # an explicit sequence; never silently renumber the publication.
        reviewed_labels = config.get("reference_label_sequence")
        if not (
            reference_entry_keys
            and configured_hash
            and isinstance(reviewed_labels, list)
            and all(type(number) is int for number in reviewed_labels)
            and reviewed_labels == reference_numbers
        ):
            raise PdfArticleExtractionError(
                "PDF reference numbers must start at 1 and remain contiguous"
            )

    page_diagnostics = list(document.diagnostic_rows)
    if configured_reference_start is not None:
        evidence = configured_reference_start["evidence"]
        page_diagnostics.append(
            {
                "schema_version": "1.0",
                "kind": "configured_reference_start",
                "status": "reviewed",
                "source_path": source_path,
                "page": evidence.page,
                "source_locator": evidence.source_locator,
                "plain_text": evidence.plain_text,
            }
        )
    for exclusion_index, region in enumerate(excluded_regions, start=1):
        target_pages = (
            [page.page for page in document.pages]
            if region["pages"] == "all"
            else sorted(region["pages"])
        )
        for page_number in target_pages:
            page_diagnostics.append(
                {
                    "schema_version": "1.0",
                    "kind": "configured_exclusion",
                    "source_path": source_path,
                    "page": page_number,
                    "exclusion_index": exclusion_index,
                    "box": list(region["box"]),
                    "coordinate_system": "pdf-points-top-left",
                    "reason": region["reason"],
                    "status": "intentionally_excluded",
                }
            )
    for page in document.pages:
        summary = {
                "schema_version": "1.0",
                "kind": "page_summary",
                "source_path": source_path,
                "page": page.page,
                "classification": page.classification,
                "decoded_lines": len(page.lines),
                "excluded_lines": dict(sorted(excluded_counts[page.page].items())),
                "ocr_performed": page.page in ocr_pages,
            }
        if excluded_output_ids[page.page]:
            summary["excluded_output_ids"] = {
                category: sorted(output_ids)
                for category, output_ids in sorted(
                    excluded_output_ids[page.page].items()
                )
                if output_ids
            }
        page_diagnostics.append(summary)

    bibliographic_raw = config.get("bibliographic", {})
    if not isinstance(bibliographic_raw, Mapping):
        raise PdfArticleExtractionError("pdf_text.bibliographic must be a mapping")
    bibliographic = {
        key: str(value)
        for key, value in bibliographic_raw.items()
        if key in {"volume", "issue", "pages", "first_published"} and value is not None
    }
    if text_extraction is None:
        extraction_details: dict[str, Any] = {
            "source_role": "main_pdf",
            "source_path": source_path,
            "method": "deterministic_pdf_glyph_and_layout_decoding",
            "ocr_performed": False,
        }
    else:
        extraction_details = dict(text_extraction)
        if (
            extraction_details.get("source_role") != "main_pdf"
            or str(extraction_details.get("source_path", "")).replace("\\", "/")
            != source_path.replace("\\", "/")
            or not isinstance(extraction_details.get("ocr_performed"), bool)
        ):
            raise PdfArticleExtractionError(
                "provided text-extraction details do not match the main PDF"
            )

    article = ArticleExtraction(
        title=metadata.title,
        bibliographic=bibliographic,
        sections=sections,
        figures=figures,
        tables=[],
        references=references,
        supporting_information=[],
        repairs=[],
        warnings=list(document.warnings),
        front_matter=front_matter,
        text_extraction=extraction_details,
        page_diagnostics=page_diagnostics,
    )
    _apply_repairs(article, text_repairs)
    return article


__all__ = ["PdfArticleExtractionError", "extract_pdf_article"]
