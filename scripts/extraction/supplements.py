"""Preserve and extract supplementary sources without modifying originals.

The public entry point, :func:`extract_supplements`, accepts the ``SourceFile``
objects returned by discovery and an existing extraction root. Every supplement
is copied byte-for-byte to a deterministic ``supplement_NNN`` directory before
any format-specific extraction is attempted. PDF extraction uses only the
document's native text layer; this module never invokes OCR.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .caption_patterns import CAPTION_PATTERN
from .models import ContentBlock, FigureItem, SourceFile, SupplementExtraction
from .paths import (
    ensure_within,
    is_reparse_point,
    reject_reparse_chain,
    sha256_file,
)
from .pptx_supplement import extract_pptx_supplement
from .xml_supplement import extract_xml_fields


FOOTNOTE_PATTERN = re.compile(r"^\[[A-Za-z0-9]+\](?:\s|$)")
SPACE_PATTERN = re.compile(r"\s+")
XML_MEDIA_TYPES = frozenset({"application/xml", "text/xml"})
PPTX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "presentationml.presentation"
)


@dataclass(frozen=True)
class _PdfLine:
    text: str
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
        return _normalize_space(" ".join(line.text for line in self.lines))

    @property
    def first_line(self) -> int:
        return self.lines[0].line_number

    @property
    def last_line(self) -> int:
        return self.lines[-1].line_number


def _normalize_space(value: str) -> str:
    return SPACE_PATTERN.sub(" ", unicodedata.normalize("NFC", value)).strip()


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
    """Merge PPT caption paragraphs and link a single-slide preview figure."""

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

    captions = [block for block in merged if block.kind == "figure_caption"]
    previews = [
        asset
        for asset in assets
        if str(asset.get("category") or "").casefold()
        == "supplement_slide_preview"
    ]
    if len(captions) != 1 or len(previews) != 1:
        return merged, []

    caption = captions[0]
    match = CAPTION_PATTERN.match(caption.plain_text)
    if match is None or not match.group("kind").casefold().startswith("fig"):
        return merged, []
    preview = previews[0]
    try:
        slide_count = int(preview.get("presentation_slide_count") or 0)
    except (TypeError, ValueError):
        slide_count = 0
    caption_slide = re.search(
        r"(?:^|;)slide=(\d+)(?:;|$)", caption.source_locator
    )
    if (
        slide_count != 1
        or caption_slide is None
        or int(caption_slide.group(1)) != 1
    ):
        return merged, []
    figure_id = str(preview.get("asset_id") or "").strip()
    output_path = str(preview.get("output_path") or "").strip()
    if not figure_id or not output_path:
        return merged, []
    label = f"Figure {match.group('number').upper()}"
    figure = FigureItem(
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
    preview["category"] = "figure"
    # The slide's component images belong to the semantic supplementary
    # figure. Chart workbooks retain their stronger table parent relation.
    for asset in assets:
        if (
            str(asset.get("category") or "").casefold() == "supplement_image"
            and asset.get("parent_id") == supplement_id
        ):
            asset["parent_id"] = figure_id
    return [block for block in merged if block is not caption], [figure]


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


def _figure_graphic_regions(page: Any) -> list[tuple[float, float, float, float]]:
    """Find graphics connected to raster images without treating tables as figures.

    A raster image can be accompanied by vector bonds, labels, or panel marks.
    Those nearby vector objects are folded into the image region. Vector-only
    ruled tables are intentionally not used as seeds because their native cell
    text belongs in the textual extraction.
    """

    regions = [box for raw in page.images if (box := _box(raw)) is not None]
    pending = [
        box
        for raw in (*page.lines, *page.curves, *page.rects)
        if (box := _box(raw)) is not None
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


def _extract_page_lines(page: Any) -> list[_PdfLine]:
    """Return native PDF text lines in pdfplumber's reading order."""

    raw_lines = page.extract_text_lines(
        layout=False,
        strip=True,
        return_chars=False,
    )
    graphic_regions = _figure_graphic_regions(page)
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
                line_number=len(lines) + 1,
                x0=x0,
                x1=x1,
                top=top,
                bottom=bottom,
                overlaps_figure_graphic=any(
                    _boxes_touch(line_box, region, margin=8.0)
                    for region in graphic_regions
                ),
            )
        )
    return lines


def _begins_semantic_block(text: str) -> bool:
    return bool(CAPTION_PATTERN.match(text) or FOOTNOTE_PATTERN.match(text))


def _paragraphs(lines: list[_PdfLine]) -> list[_PdfParagraph]:
    if not lines:
        return []
    result: list[_PdfParagraph] = []
    current = _PdfParagraph(lines=[lines[0]])
    for line in lines[1:]:
        previous = current.lines[-1]
        previous_height = max(1.0, previous.bottom - previous.top)
        gap = line.top - previous.bottom
        force_boundary = _begins_semantic_block(line.text)
        # Native lines belonging to one paragraph normally overlap slightly or
        # have only a few points of leading. A larger vertical gap is treated as
        # a deliberate paragraph/section boundary.
        vertical_boundary = gap > max(6.0, previous_height * 0.60)
        if force_boundary or vertical_boundary:
            result.append(current)
            current = _PdfParagraph(lines=[line])
        else:
            current.lines.append(line)
    result.append(current)
    return result


def _caption_details(text: str) -> tuple[str, str, str] | None:
    match = CAPTION_PATTERN.match(text)
    if match is None:
        return None
    raw_kind = match.group("kind").casefold()
    kind = "figure" if raw_kind.startswith("fig") else raw_kind.rstrip("s")
    number = match.group("number")
    label = f"{kind.title()} {number}"
    return kind, number, label


def _safe_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"[^A-Za-z0-9]+", "_", ascii_value).strip("_").casefold() or "item"


def _pdf_blocks(
    source: SourceFile,
    supplement_id: str,
    *,
    pdf_path: Path | None = None,
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
        with pdfplumber.open(pdf_path or source.path) as document:
            for page_number, page in enumerate(document.pages, 1):
                lines = _extract_page_lines(page)
                if not lines:
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

                for paragraph_number, paragraph in enumerate(_paragraphs(lines), 1):
                    text = paragraph.text
                    if not text:
                        continue
                    caption = _caption_details(text)
                    is_footnote = bool(FOOTNOTE_PATTERN.match(text))
                    if (
                        caption is None
                        and not is_footnote
                        and all(line.overlaps_figure_graphic for line in paragraph.lines)
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
                    else:
                        kind = ""
                        number = ""
                        label = ""
                        block_kind = "text"

                    locator = (
                        f"page={page_number};native-lines="
                        f"{paragraph.first_line}-{paragraph.last_line}"
                    )
                    block_id = (
                        f"{supplement_id}-page-{page_number:03d}-"
                        f"block-{paragraph_number:03d}"
                    )
                    blocks.append(
                        ContentBlock(
                            block_id=block_id,
                            kind=block_kind,
                            markdown=text,
                            plain_text=text,
                            source_path=source.relative_path,
                            source_locator=locator,
                        )
                    )

                    if kind not in {"figure", "scheme"}:
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
                            caption_markdown=text,
                            caption_plain=text,
                            source_path=source.relative_path,
                            source_locator=locator,
                        )
                    )
    except Exception as exc:
        warnings.append(
            _warning(
                "supplement_pdf_native_text_extraction_failed",
                (
                    "Native PDF text extraction failed; the original was preserved, "
                    f"no OCR was performed ({type(exc).__name__})"
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

    reviewed_exclusions = list(exclusion_specs)
    supplement_sources = sorted(
        (source for source in sources if source.role == "supplement"),
        key=lambda source: (
            _natural_path_key(source.relative_path),
            source.relative_path.casefold(),
            source.relative_path,
        ),
    )
    relative_paths = [source.relative_path.casefold() for source in supplement_sources]
    if len(relative_paths) != len(set(relative_paths)):
        raise ValueError("duplicate supplementary source paths were discovered")

    results: list[SupplementExtraction] = []
    for index, source in enumerate(supplement_sources, 1):
        supplement_id = f"supplement_{index:03d}"
        copied_relative = (
            Path("supplementary") / supplement_id / source.path.name
        ).as_posix()
        destination = extraction_root / Path(copied_relative)
        _copy_verified(source, destination, extraction_root)

        tables = []
        assets: list[dict[str, Any]] = []
        asset_ids: list[str] = []
        if source.detected_format == "application/pdf":
            # Parse the verified candidate copy so extracted text and the file
            # delivered to downstream consumers are guaranteed to be the same
            # byte sequence even if an original changes after discovery.
            blocks, figures, warnings = _pdf_blocks(
                source, supplement_id, pdf_path=destination
            )
        elif source.detected_format in XML_MEDIA_TYPES:
            blocks, warnings = extract_xml_fields(
                source, supplement_id, destination
            )
            figures = []
        elif source.detected_format == PPTX_MEDIA_TYPE:
            blocks, tables, assets, warnings = extract_pptx_supplement(
                source,
                supplement_id,
                pptx_path=destination,
                extraction_root=extraction_root,
            )
            blocks, figures = _presentation_caption_semantics(
                blocks, assets, source, supplement_id
            )
            asset_ids = [str(asset["asset_id"]) for asset in assets]
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

        exclusions: list[dict[str, Any]] = []
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

        results.append(
            SupplementExtraction(
                supplement_id=supplement_id,
                source=source,
                copied_path=copied_relative,
                blocks=blocks,
                figures=figures,
                warnings=warnings,
                exclusions=exclusions,
                tables=tables,
                assets=assets,
                asset_ids=asset_ids,
            )
        )
    return results


__all__ = ["extract_supplements"]
