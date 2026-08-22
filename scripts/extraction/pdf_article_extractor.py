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
    r"^(?:\[(?P<bracket_number>\d+)\]|(?P<period_number>\d+)\.)\s*"
)
_RECEIVED = re.compile(r"^Received\b", flags=re.IGNORECASE)


def _clean(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def _reference_number(match: re.Match[str] | None) -> int | None:
    if match is None:
        return None
    value = match.group("bracket_number") or match.group("period_number")
    return int(value) if value is not None else None


def _strip_source_emphasis(value: str) -> str:
    """Discard publisher font emphasis while preserving scientific markup.

    PDF font runs often split one chemical name or caption into dozens of
    adjacent bold/italic fragments.  Those typographic runs are not reliable
    semantic structure.  Superscript/subscript HTML and escaped literal
    asterisks remain intact.
    """

    return re.sub(r"(?<!\\)\*", "", value)


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
        elif result.endswith(("-", "‐", "‑")):
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
            match = re.fullmatch(r"C?(\d+)", code_raw.strip(), flags=re.IGNORECASE)
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
                "line_keys": {
                    (line.page, line.source_locator) for line in evidence
                },
            }
        )
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
        if (
            not isinstance(page, int)
            or isinstance(page, bool)
            or page < 1
            or not field
            or not label
            or not value
            or "\n" in label
            or "\n" in value
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
                kind="front_matter",
                markdown=f"**{label}:** {value}",
                plain_text=f"{label}: {value}",
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
            _strip_source_emphasis(line.markdown) for line in lines
        )
        reviewed_caption = spec.get("caption_reviewed_value")
        if reviewed_caption is not None:
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
        page = spec.get("page")
        if not isinstance(page, int):
            continue
        result.append(
            {
                "page": page,
                "box": _box(spec.get("box"), field="PDF crop box"),
                "output_id": str(spec.get("asset_id", "")).strip(),
            }
        )
    return result


def _split_heading(line: PdfTextLine) -> tuple[str, str, str] | None:
    plain_match = _NUMBERED_HEADING.match(line.plain_text)
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
            position = unstyled_markdown.find(rest_plain)
            rest_markdown = (
                _clean(unstyled_markdown[position:])
                if position >= 0
                else rest_plain
            )
        return heading, rest_markdown, rest_plain

    markdown_match = _STRONG_PREFIX.match(line.markdown)
    if markdown_match:
        heading = _clean(markdown_match.group("strong")).rstrip(".")
        rest_markdown = _clean(
            _strip_source_emphasis(markdown_match.group("rest"))
        )
        if heading.casefold() in {"experimental part", "references"}:
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
        "experimental",
        "experimental part",
        "acknowledgment",
        "acknowledgments",
        "acknowledgement",
        "acknowledgements",
        "references",
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
    previous_height = max(previous.bbox[3] - previous.bbox[1], 1.0)
    gap = current.bbox[1] - previous.bbox[3]
    if gap > previous_height * 0.9:
        return True
    # In this journal, first lines are indented while continuations return to
    # the left margin.  Require a material indent so glyph jitter cannot split.
    return current.bbox[0] - previous.bbox[0] > 6.0


def _make_block(
    block_id: str,
    kind: str,
    lines: list[PdfTextLine],
    source_path: str,
    *,
    markdown_prefix: str = "",
    plain_prefix: str = "",
) -> ContentBlock:
    if not lines and not (markdown_prefix or plain_prefix):
        raise PdfArticleExtractionError("cannot make an empty PDF content block")
    markdown = _join_fragments(
        [
            markdown_prefix,
            *(_strip_source_emphasis(line.markdown) for line in lines),
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
        )
    else:
        document = text_document
        if document.relative_path.replace("\\", "/") != source_path.replace("\\", "/"):
            raise PdfArticleExtractionError(
                "provided PDF text document does not match the main PDF"
            )
    if not document.all_pages_classified:
        raise PdfArticleExtractionError("not every PDF page received a text classification")

    configured_headings = _configured_section_headings(config, document)
    abstract_region_ids, reviewed_abstract_lines = _configured_abstract_lines(
        config, document
    )
    heading_region_ids = {
        str(item["key"])[len("region:") :]
        for item in configured_headings
        if str(item["key"]).startswith("region:")
    }
    if heading_region_ids & abstract_region_ids:
        raise PdfArticleExtractionError(
            "an OCR region cannot be both an abstract and a section heading"
        )

    crop_specs = list(crop_specs)
    figures, caption_keys = _figures(document, source_path, crop_specs)
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
    if abstract_region_ids or reviewed_abstract_lines:
        for line in body_lines:
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
                                _make_block(
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
                            _make_block(
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
                    _make_block(
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
                _make_block(
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
    abstract = Section(
        section_id="section-abstract",
        heading="Abstract",
        source_path=source_path if abstract_lines else "",
        source_locator=(
            _source_locator(abstract_lines[0].page, abstract_lines)
            if abstract_lines
            else ""
        ),
        source_geometry=_source_geometry(abstract_lines),
    )
    if abstract_lines:
        abstract.blocks.append(
            _make_block("main-abstract-0001", "abstract", abstract_lines, source_path)
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
            _make_block(
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
        if folded in {"experimental", "experimental part"}:
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
            if heading_title.casefold() == "references":
                references_mode = True
                continue
            start_section(
                heading_title,
                heading_evidence,
                explicit_level=heading_level,
            )
            continue
        if references_mode:
            if _REFERENCE_START.match(text) and reference_lines:
                number = _reference_number(
                    _REFERENCE_START.match(reference_lines[0].plain_text)
                )
                if number is None:
                    raise PdfArticleExtractionError(
                        "reference text before the first numbered entry: "
                        f"{reference_lines[0].plain_text!r}"
                    )
                references.append(
                    _make_block(
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
        if heading and heading[0].casefold() == "references":
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
        if current_lines and _new_paragraph(current_lines[-1], line):
            flush_paragraph()
        current_lines.append(line)

    flush_paragraph()
    if reference_lines:
        match = _REFERENCE_START.match(reference_lines[0].plain_text)
        reference_number = _reference_number(match)
        if reference_number is None:
            raise PdfArticleExtractionError("reference continuation has no numbered start")
        references.append(
            _make_block(
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
        raise PdfArticleExtractionError(
            "PDF reference numbers must start at 1 and remain contiguous"
        )

    page_diagnostics = list(document.diagnostic_rows)
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
                "ocr_performed": any(
                    line.source_region_id is not None for line in page.lines
                ),
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
