"""Adapt ordered, local OCR results to the PDF article text model.

The OCR engine deliberately knows nothing about article structure.  This
adapter keeps the reviewed region order, maps each observation back to exact
PDF geometry, and stores engine/model details only in private diagnostics.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pypdfium2

from .ocr import OcrRunResult
from .paths import sha256_file
from .pdf_text_extractor import PdfTextDocument, PdfTextLine, PdfTextPage


class PdfOcrTextError(ValueError):
    """An OCR run cannot be represented safely as ordered PDF text."""


def _clean(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def _bbox(points: tuple[tuple[float, float], ...]) -> tuple[float, float, float, float]:
    if len(points) < 4:
        raise PdfOcrTextError("an OCR observation polygon has fewer than four points")
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    if not all(math.isfinite(value) for value in (*xs, *ys)):
        raise PdfOcrTextError("an OCR observation polygon is not finite")
    result = (min(xs), min(ys), max(xs), max(ys))
    if result[0] >= result[2] or result[1] >= result[3]:
        raise PdfOcrTextError("an OCR observation polygon is empty")
    return result


def _source_locator(page: int, box: tuple[float, float, float, float]) -> str:
    return (
        f"PDF page {page}, box "
        f"[{box[0]:.2f}, {box[1]:.2f}, {box[2]:.2f}, {box[3]:.2f}]"
    )


def _escape_ocr_markdown(value: str) -> str:
    """Escape literal OCR text before it enters the rich-text projection.

    OCR observations have no source styling, but they can contain characters
    that the downstream limited-Markdown renderer treats as markup or HTML
    entities. Keep the plain observation unchanged and escape only its
    Markdown twin, matching the native-PDF text path.
    """

    return (
        value.replace("\\", "\\\\")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("*", "\\*")
        .replace("_", "\\_")
    )


def _vertical_overlap_ratio(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    overlap = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    return overlap / max(min(first[3] - first[1], second[3] - second[1]), 0.0001)


def _join_same_line_text(current: str, following: str) -> str:
    """Join detector fragments without inferring duplicated characters."""

    current = _clean(current)
    following = _clean(following)
    if not current:
        return following
    if not following:
        return current
    if following[0] in ",.;:!?)]}" or current[-1] in "([{/-":
        return current + following
    return current + " " + following


def _coalesced_region_lines(region: Any) -> list[dict[str, Any]]:
    """Merge same-baseline detector fragments without changing line order."""

    items = [
        {
            "text": _clean(observation.text),
            "confidence": float(observation.confidence),
            "bbox": _bbox(observation.polygon_pdf_points),
        }
        for observation in region.observations
        if _clean(observation.text)
    ]
    items.sort(
        key=lambda item: (
            (item["bbox"][1] + item["bbox"][3]) / 2,
            item["bbox"][0],
        )
    )
    clusters: list[list[dict[str, Any]]] = []
    for item in items:
        center = (item["bbox"][1] + item["bbox"][3]) / 2
        matching: list[dict[str, Any]] | None = None
        for cluster in reversed(clusters[-3:]):
            cluster_top = min(part["bbox"][1] for part in cluster)
            cluster_bottom = max(part["bbox"][3] for part in cluster)
            cluster_box = (0.0, cluster_top, 1.0, cluster_bottom)
            cluster_center = (cluster_top + cluster_bottom) / 2
            height = max(item["bbox"][3] - item["bbox"][1], 0.0001)
            if (
                _vertical_overlap_ratio(cluster_box, item["bbox"]) >= 0.55
                or abs(center - cluster_center) <= height * 0.35
            ):
                matching = cluster
                break
        if matching is None:
            clusters.append([item])
        else:
            matching.append(item)

    result: list[dict[str, Any]] = []
    for cluster in clusters:
        cluster.sort(key=lambda item: item["bbox"][0])
        text = ""
        seen: set[tuple[str, tuple[float, float, float, float]]] = set()
        for item in cluster:
            # Only a repeated whole observation at identical geometry proves
            # duplication. Adjacent words can share letters and overlap boxes.
            identity = (item["text"], item["bbox"])
            if identity in seen:
                continue
            seen.add(identity)
            text = _join_same_line_text(text, item["text"])
        box = (
            min(item["bbox"][0] for item in cluster),
            min(item["bbox"][1] for item in cluster),
            max(item["bbox"][2] for item in cluster),
            max(item["bbox"][3] for item in cluster),
        )
        result.append(
            {
                "text": text,
                "confidence": min(item["confidence"] for item in cluster),
                "bbox": box,
                "constituent_geometry": tuple(
                    (region.page, item["bbox"]) for item in cluster
                ),
            }
        )
    return result


def _reviewed_regions(config: Mapping[str, Any]) -> set[str]:
    raw = config.get("reviewed_regions", [])
    if raw is None:
        return set()
    if not isinstance(raw, list) or not all(
        isinstance(value, str) and value.strip() for value in raw
    ):
        raise PdfOcrTextError("pdf_ocr.reviewed_regions must be a list of region IDs")
    result = {value.strip() for value in raw}
    if len(result) != len(raw):
        raise PdfOcrTextError("pdf_ocr.reviewed_regions contains a duplicate")
    return result


def _review_threshold(config: Mapping[str, Any]) -> float:
    raw = config.get("review_threshold", 0.90)
    if isinstance(raw, bool):
        raise PdfOcrTextError("pdf_ocr.review_threshold must be a number")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise PdfOcrTextError("pdf_ocr.review_threshold must be a number") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise PdfOcrTextError("pdf_ocr.review_threshold must be between 0 and 1")
    return value


def ocr_run_to_pdf_text_document(
    source: Path,
    source_path: str,
    run: OcrRunResult,
    config: Mapping[str, Any] | None = None,
) -> PdfTextDocument:
    """Return OCR observations as ordered ``PdfTextLine`` objects.

    Page and region order comes exclusively from the reviewed OCR plan.  Lines
    are never globally re-sorted here, which is essential for multi-column
    scans.  Figure and scheme pixels have already been masked by ``ocr.py``.
    """

    config = config or {}
    if not isinstance(config, Mapping):
        raise PdfOcrTextError("pdf_ocr must be a mapping")
    source = source.resolve(strict=True)
    normalized_source_path = source_path.replace("\\", "/")
    if run.source_path != normalized_source_path:
        raise PdfOcrTextError("OCR result source path does not match the main PDF")
    if run.source_sha256 != sha256_file(source):
        raise PdfOcrTextError("OCR result source hash does not match the main PDF")

    try:
        pdf = pypdfium2.PdfDocument(str(source))
    except Exception as exc:
        raise PdfOcrTextError(f"could not reopen OCR PDF source: {exc}") from exc
    page_sizes: dict[int, tuple[float, float]] = {}
    try:
        for page_number in range(1, len(pdf) + 1):
            page = pdf[page_number - 1]
            try:
                width, height = (float(value) for value in page.get_size())
            finally:
                page.close()
            page_sizes[page_number] = (width, height)
    finally:
        pdf.close()

    reviewed = _reviewed_regions(config)
    threshold = _review_threshold(config)
    declared_region_ids = {region.region_id for region in run.regions}
    unknown_reviewed = sorted(reviewed - declared_region_ids)
    if unknown_reviewed:
        raise PdfOcrTextError(
            "pdf_ocr.reviewed_regions names unknown region(s): "
            + ", ".join(unknown_reviewed)
        )

    page_lines: dict[int, list[PdfTextLine]] = defaultdict(list)
    diagnostic_rows: list[dict[str, Any]] = [
        {
            "schema_version": "1.0",
            "kind": "ocr_run",
            "method": "deterministic-local-region-ocr",
            "source_path": run.source_path,
            "source_sha256": run.source_sha256,
            "requested_engine": run.requested_engine,
            "dpi": run.dpi,
            "engine": run.engine.as_dict(),
            "region_count": len(run.regions),
            "ocr_performed": bool(run.regions),
        }
    ]
    warnings: list[dict[str, Any]] = []
    seen_regions: set[str] = set()
    for region in run.regions:
        if region.region_id in seen_regions:
            raise PdfOcrTextError(f"duplicate OCR region ID: {region.region_id}")
        seen_regions.add(region.region_id)
        if region.page not in page_sizes:
            raise PdfOcrTextError(
                f"OCR region {region.region_id!r} refers to nonexistent page {region.page}"
            )
        region_row = region.as_dict()
        region_row["source_review_status"] = (
            "reviewed" if region.region_id in reviewed else "not_reviewed"
        )
        region_row["review_threshold"] = threshold
        diagnostic_rows.append(region_row)
        for line in _coalesced_region_lines(region):
            text = str(line["text"])
            box = line["bbox"]
            locator = _source_locator(region.page, box)
            page_lines[region.page].append(
                PdfTextLine(
                    page=region.page,
                    bbox=box,
                    plain_text=text,
                    markdown=_escape_ocr_markdown(text),
                    source_locator=locator,
                    source_region_id=region.region_id,
                    ocr_confidence=float(line["confidence"]),
                    constituent_geometry=line["constituent_geometry"],
                )
            )
        for observation_index, observation in enumerate(region.observations, start=1):
            if observation.confidence < threshold and region.region_id not in reviewed:
                box = _bbox(observation.polygon_pdf_points)
                locator = _source_locator(region.page, box)
                warnings.append(
                    {
                        "schema_version": "1.0",
                        "code": "ocr_observation_requires_review",
                        "severity": "scientific",
                        "message": (
                            f"OCR observation {observation_index} in region "
                            f"{region.region_id!r} has confidence "
                            f"{observation.confidence:.4f}, below the reviewed "
                            f"threshold {threshold:.4f}."
                        ),
                        "source_path": run.source_path,
                        "source_locator": locator,
                        "region_id": region.region_id,
                        "observation_index": observation_index,
                    }
                )

    pages = [
        PdfTextPage(
            page=page_number,
            width=page_sizes[page_number][0],
            height=page_sizes[page_number][1],
            classification="image_only",
            lines=page_lines.get(page_number, []),
        )
        for page_number in sorted(page_sizes)
    ]
    return PdfTextDocument(
        relative_path=run.source_path,
        pages=pages,
        diagnostic_rows=diagnostic_rows,
        warnings=warnings,
        all_pages_classified=len(pages) > 0,
    )


def ocr_text_extraction_details(run: OcrRunResult) -> dict[str, Any]:
    """Return manifest-safe details for one deterministic local OCR run."""

    return {
        "source_role": "main_pdf",
        "source_path": run.source_path,
        "method": "deterministic-local-region-ocr",
        "ocr_performed": bool(run.regions),
        "ocr_dpi": run.dpi,
        "ocr_region_count": len(run.regions),
        "ocr_engine": run.engine.as_dict(),
    }


__all__ = [
    "PdfOcrTextError",
    "ocr_run_to_pdf_text_document",
    "ocr_text_extraction_details",
]
