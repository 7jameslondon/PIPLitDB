"""Deterministic, OCR-free PDF rendering helpers.

Crop coordinates use PDF points (1/72 inch) with a top-left origin.  Source
and output paths in crop specifications must be relative to their respective
roots; absolute paths, parent traversal, and reparse points are rejected.
"""

from __future__ import annotations

import io
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pypdfium2
from PIL import Image

from .paths import (
    UnsafePathError,
    atomic_write_bytes,
    ensure_within,
    is_reparse_point,
    reject_reparse_chain,
    sha256_file,
)


DEFAULT_DPI = 300
MAX_DPI = 1_200
MAX_OUTPUT_PIXELS = 100_000_000
_ASSET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PdfExtractionError(ValueError):
    """Raised when a PDF crop request is invalid or cannot be rendered."""


def render_pdf_crops(
    source_root: Path,
    extraction_root: Path,
    crop_specs: Iterable[Mapping[str, Any]],
    *,
    dpi: int | float = DEFAULT_DPI,
    max_output_pixels: int = MAX_OUTPUT_PIXELS,
) -> list[dict[str, Any]]:
    """Render PDF crop specifications to deterministic PNG files.

    Each specification must contain ``source_path`` (``source`` is accepted as
    an alias), ``asset_id``, and ``output_path`` relative to
    *extraction_root*. A normal crop declares a one-based ``page`` and ``box``
    as ``[left, top, right, bottom]`` in PDF points. A composite crop instead
    declares ``parts`` containing two or more such page/box mappings; the
    rendered parts are centered and stacked vertically into one image, unless
    ``preserve_source_layout`` retains same-page parts at their original relative
    positions (leaving excluded caption/prose areas blank). A normal
    crop may declare ``padding_points`` to add a deterministic white border
    after rendering; this is useful when complete authored artwork ends exactly
    at a crop boundary and the neighboring page content cannot be included. No
    OCR or text recognition is performed. A one-based ``embedded_image_index``
    selects an image object in PDFium page traversal order at its native
    resolution, without overlaid page text. Its ``box`` must match the object's
    page bounds within one point; this avoids silently selecting another image.

    The returned dictionaries are suitable for an extraction manifest.  The
    function preserves input order and rejects duplicate asset IDs or outputs.
    """

    render_dpi = _validate_dpi(dpi)
    if isinstance(max_output_pixels, bool) or not isinstance(max_output_pixels, int):
        raise PdfExtractionError("max_output_pixels must be an integer")
    if max_output_pixels <= 0:
        raise PdfExtractionError("max_output_pixels must be positive")

    resolved_source_root = _validate_existing_root(source_root, "source_root")
    resolved_extraction_root = _prepare_output_root(extraction_root)
    normalized_specs = [
        _normalize_crop_spec(spec, index=index)
        for index, spec in enumerate(crop_specs, start=1)
    ]
    _reject_duplicate_specs(normalized_specs)

    # Opening a source once also makes rendering a batch from a long PDF much
    # faster while retaining the caller's deterministic input order.
    documents: dict[str, pypdfium2.PdfDocument] = {}
    source_paths: dict[str, Path] = {}
    results: list[dict[str, Any]] = []
    try:
        for spec in normalized_specs:
            source_name = spec["source_path"]
            if source_name not in documents:
                source_path = _resolve_source_path(
                    resolved_source_root, source_name
                )
                source_paths[source_name] = source_path
                try:
                    documents[source_name] = pypdfium2.PdfDocument(str(source_path))
                except Exception as exc:  # PDFium exposes version-specific errors.
                    raise PdfExtractionError(
                        f"could not open PDF source {source_name!r}: {exc}"
                    ) from exc

            result = _render_one_crop(
                documents[source_name],
                source_paths[source_name],
                resolved_extraction_root,
                spec,
                dpi=render_dpi,
                max_output_pixels=max_output_pixels,
            )
            results.append(result)
    finally:
        for document in documents.values():
            document.close()

    return results


def render_pdf_crop(
    source_root: Path,
    extraction_root: Path,
    crop_spec: Mapping[str, Any],
    *,
    dpi: int | float = DEFAULT_DPI,
    max_output_pixels: int = MAX_OUTPUT_PIXELS,
) -> dict[str, Any]:
    """Render one crop; see :func:`render_pdf_crops` for the specification."""

    return render_pdf_crops(
        source_root,
        extraction_root,
        [crop_spec],
        dpi=dpi,
        max_output_pixels=max_output_pixels,
    )[0]


def inspect_pdf_native_text(
    source_root: Path,
    source_path: str,
    *,
    pages: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Return PDFium's native text for selected pages without performing OCR.

    ``pages`` contains one-based page numbers.  When it is omitted, every page
    is inspected.  This helper is diagnostic: native PDF text can have an
    incorrect reading order even when ``has_native_text`` is true.
    """

    resolved_source_root = _validate_existing_root(source_root, "source_root")
    normalized_source, _ = _normalize_relative_path(source_path, "source_path")
    resolved_source = _resolve_source_path(resolved_source_root, normalized_source)

    try:
        document = pypdfium2.PdfDocument(str(resolved_source))
    except Exception as exc:
        raise PdfExtractionError(
            f"could not open PDF source {normalized_source!r}: {exc}"
        ) from exc

    try:
        page_count = len(document)
        selected_pages = _normalize_pages(pages, page_count)
        inspected: list[dict[str, Any]] = []
        for page_number in selected_pages:
            page = document[page_number - 1]
            try:
                width_points, height_points = page.get_size()
                text_page = page.get_textpage()
                try:
                    text = text_page.get_text_range()
                finally:
                    text_page.close()
            finally:
                page.close()

            inspected.append(
                {
                    "page": page_number,
                    "width_points": _clean_number(width_points),
                    "height_points": _clean_number(height_points),
                    "character_count": len(text),
                    "non_whitespace_character_count": sum(
                        not character.isspace() for character in text
                    ),
                    "has_native_text": bool(text.strip()),
                    "text": text,
                }
            )
    finally:
        document.close()

    return {
        "schema_version": "1.0",
        "method": "pdfium-native-text-no-ocr",
        "source_path": normalized_source,
        "page_count": page_count,
        "pages": inspected,
    }


def _render_one_crop(
    document: pypdfium2.PdfDocument,
    source_path: Path,
    extraction_root: Path,
    spec: dict[str, Any],
    *,
    dpi: float,
    max_output_pixels: int,
) -> dict[str, Any]:
    if "parts" in spec:
        return _render_composite_crop(
            document,
            source_path,
            extraction_root,
            spec,
            dpi=dpi,
            max_output_pixels=max_output_pixels,
        )

    render_image = (_render_embedded_image if "embedded_image_index" in spec
                    else _render_crop_image)
    extra = ({"image_index": spec["embedded_image_index"]}
             if "embedded_image_index" in spec else {})
    image, geometry = render_image(
        document,
        page_number=spec["page"],
        box=spec["box"],
        asset_id=spec["asset_id"],
        dpi=dpi,
        max_output_pixels=max_output_pixels,
        **extra,
    )
    try:
        rotation = int(spec.get("rotate_clockwise", 0))
        if rotation:
            rotated = image.rotate(-rotation, expand=True, fillcolor="white")
            image.close()
            image = rotated
            geometry["dimensions_pixels"] = {
                "width": image.width,
                "height": image.height,
            }
            geometry["render_rotation_degrees_clockwise"] = rotation
        padding_points = float(spec.get("padding_points", 0))
        if padding_points:
            padding_pixels = max(1, math.ceil(padding_points * dpi / 72.0))
            padded_width = image.width + (2 * padding_pixels)
            padded_height = image.height + (2 * padding_pixels)
            if padded_width * padded_height > max_output_pixels:
                raise PdfExtractionError(
                    f"padded crop for {spec['asset_id']!r} would exceed "
                    f"{max_output_pixels:,} pixels"
                )
            padded = Image.new("RGB", (padded_width, padded_height), "white")
            padded.paste(image, (padding_pixels, padding_pixels))
            image.close()
            image = padded
            geometry["dimensions_pixels"] = {
                "width": image.width,
                "height": image.height,
            }
            geometry["padding_points"] = _clean_number(padding_points)
            geometry["padding_pixels"] = padding_pixels
        png_bytes = _encode_png(image)
        width_pixels, height_pixels = image.size
    finally:
        image.close()

    output_relative = spec["output_path"]
    output_path = _write_crop_output(
        extraction_root,
        output_relative,
        png_bytes,
    )
    result = _crop_result(
        spec,
        source_path,
        output_path,
        dpi=dpi,
        width_pixels=width_pixels,
        height_pixels=height_pixels,
    )
    result.update(geometry)
    return result


def _render_composite_crop(
    document: pypdfium2.PdfDocument,
    source_path: Path,
    extraction_root: Path,
    spec: dict[str, Any],
    *,
    dpi: float,
    max_output_pixels: int,
) -> dict[str, Any]:
    images: list[Image.Image] = []
    parts: list[dict[str, Any]] = []
    try:
        for part_index, part in enumerate(spec["parts"], start=1):
            image, geometry = _render_crop_image(
                document,
                page_number=part["page"],
                box=part["box"],
                asset_id=f"{spec['asset_id']} part {part_index}",
                dpi=dpi,
                max_output_pixels=max_output_pixels,
            )
            images.append(image)
            parts.append(geometry)

        source_layout = spec.get("preserve_source_layout", False)
        if source_layout:
            origin_x = min(part["box"][0] for part in spec["parts"])
            origin_y = min(part["box"][1] for part in spec["parts"])
            placements = [(round((part["box"][0] - origin_x) * dpi / 72),
                           round((part["box"][1] - origin_y) * dpi / 72))
                          for part in spec["parts"]]
            width_pixels = max(left + im.width for im, (left, top) in zip(images, placements))
            height_pixels = max(top + im.height for im, (left, top) in zip(images, placements))
        else:
            width_pixels = max(image.width for image in images)
            height_pixels = sum(image.height for image in images)
        if width_pixels * height_pixels > max_output_pixels:
            raise PdfExtractionError(
                f"composite crop for {spec['asset_id']!r} would exceed "
                f"{max_output_pixels:,} pixels"
            )
        composite = Image.new("RGB", (width_pixels, height_pixels), "white")
        try:
            top = 0
            for index, (image, part) in enumerate(zip(images, parts, strict=True)):
                if source_layout:
                    left, top = placements[index]
                else:
                    left = (width_pixels - image.width) // 2
                composite.paste(image, (left, top))
                part["placement_pixels"] = {
                    "left": left,
                    "top": top,
                    "width": image.width,
                    "height": image.height,
                }
                top += image.height
            png_bytes = _encode_png(composite)
        finally:
            composite.close()
    finally:
        for image in images:
            image.close()

    output_relative = spec["output_path"]
    output_path = _write_crop_output(
        extraction_root,
        output_relative,
        png_bytes,
    )
    result = _crop_result(
        spec,
        source_path,
        output_path,
        dpi=dpi,
        width_pixels=width_pixels,
        height_pixels=height_pixels,
    )
    result.update(
        {
            "parts": parts,
            "composition": {
                "layout": "source",
                "alignment": "source_coordinates",
            } if source_layout else {
                "layout": "vertical",
                "alignment": "center",
                "gap_pixels": 0,
            },
        }
    )
    return result


def _render_embedded_image(document, *, page_number, box, asset_id, dpi,
                           max_output_pixels, image_index):
    if page_number > len(document):
        raise PdfExtractionError(f"page {page_number} is outside the PDF")
    page = document[page_number - 1]
    try:
        width, height = page.get_size()
        _validate_box(box, width, height, asset_id=asset_id)
        objects = list(page.get_objects(filter=[pypdfium2.raw.FPDF_PAGEOBJ_IMAGE]))
        if image_index > len(objects):
            raise PdfExtractionError(f"embedded image {image_index} is outside PDF page {page_number}")
        obj = objects[image_index - 1]
        left, bottom, right, top = obj.get_bounds()
        actual_box = [left, height - top, right, height - bottom]
        if any(abs(a - b) > 1 for a, b in zip(box, actual_box)):
            raise PdfExtractionError("embedded image bounds differ from reviewed box")
        metadata = obj.get_metadata()
        if metadata.width * metadata.height > max_output_pixels:
            raise PdfExtractionError("embedded image exceeds output pixel limit")
        bitmap = obj.get_bitmap(render=True, scale_to_original=True)
        try:
            shared = bitmap.to_pil()
            try:
                normalized = _to_rgb_on_white(shared)
                try:
                    image = normalized.copy()
                finally:
                    if normalized is not shared:
                        normalized.close()
            finally:
                shared.close()
        finally:
            bitmap.close()
        if image.width * image.height > max_output_pixels:
            image.close()
            raise PdfExtractionError("embedded image exceeds output pixel limit")
        return image, {"page": page_number, "box": actual_box,
                       "coordinate_system": "pdf-points-top-left",
                       "page_dimensions_points": {"width": width, "height": height},
                       "dimensions_pixels": {"width": image.width, "height": image.height},
                       "method": "pdfium-embedded-image-native-resolution",
                       "embedded_image_index": image_index}
    finally:
        page.close()


def _render_crop_image(
    document: pypdfium2.PdfDocument,
    *,
    page_number: int,
    box: Sequence[float],
    asset_id: str,
    dpi: float,
    max_output_pixels: int,
) -> tuple[Image.Image, dict[str, Any]]:
    page_count = len(document)
    if page_number > page_count:
        raise PdfExtractionError(
            f"page {page_number} is outside the PDF ({page_count} pages)"
        )

    page = document[page_number - 1]
    try:
        page_width, page_height = page.get_size()
        left, top, right, bottom = _validate_box(
            box, page_width, page_height, asset_id=asset_id
        )
        scale = dpi / 72.0
        expected_width = max(1, math.ceil((right - left) * scale))
        expected_height = max(1, math.ceil((bottom - top) * scale))
        if expected_width * expected_height > max_output_pixels:
            raise PdfExtractionError(
                f"crop for {asset_id!r} would exceed "
                f"{max_output_pixels:,} pixels"
            )

        # PDFium's crop tuple is the amount removed from left, bottom, right,
        # and top.  Convert from the public top-left bounding box here.
        pdfium_crop = (
            left,
            page_height - bottom,
            page_width - right,
            top,
        )
        try:
            bitmap = page.render(
                scale=scale,
                crop=pdfium_crop,
                may_draw_forms=False,
                fill_color=(255, 255, 255, 255),
                draw_annots=True,
                optimize_mode="print",
                rev_byteorder=True,
            )
            try:
                shared_image = bitmap.to_pil()
                try:
                    # ``to_pil()`` may share PDFium's bitmap buffer.  Copy it
                    # before closing the bitmap so later PNG encoding never
                    # reads released native memory.
                    image = shared_image.copy()
                finally:
                    shared_image.close()
            finally:
                bitmap.close()
        except Exception as exc:
            raise PdfExtractionError(
                f"could not render {asset_id!r} from "
                f"PDF page {page_number}: {exc}"
            ) from exc
    finally:
        page.close()

    # Flatten transparency against white and normalize every output to RGB so
    # identical source content and settings produce stable PNG bytes.
    try:
        normalized_image = _to_rgb_on_white(image)
        if normalized_image.width * normalized_image.height > max_output_pixels:
            raise PdfExtractionError(
                f"rendered crop for {asset_id!r} exceeds "
                f"{max_output_pixels:,} pixels"
            )
        width_pixels, height_pixels = normalized_image.size
        result_image = normalized_image.copy()
    finally:
        image.close()
        if "normalized_image" in locals() and normalized_image is not image:
            normalized_image.close()
    return result_image, {
        "page": page_number,
        "box": [
            _clean_number(left),
            _clean_number(top),
            _clean_number(right),
            _clean_number(bottom),
        ],
        "coordinate_system": "pdf-points-top-left",
        "page_dimensions_points": {
            "width": _clean_number(page_width),
            "height": _clean_number(page_height),
        },
        "dimensions_pixels": {
            "width": width_pixels,
            "height": height_pixels,
        },
    }


def _write_crop_output(
    extraction_root: Path,
    output_relative: str,
    png_bytes: bytes,
) -> Path:
    output_path = _resolve_output_path(extraction_root, output_relative)
    try:
        atomic_write_bytes(output_path, png_bytes)
    except OSError as exc:
        raise PdfExtractionError(
            f"could not write PDF crop output {output_relative!r}: {exc}"
        ) from exc
    # Recheck after the atomic replace so a filesystem race cannot silently
    # redirect the manifest to a location outside the extraction root.
    ensure_within(output_path, extraction_root, require_exists=True)
    return output_path


def _crop_result(
    spec: Mapping[str, Any],
    source_path: Path,
    output_path: Path,
    *,
    dpi: float,
    width_pixels: int,
    height_pixels: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "asset_id": spec["asset_id"],
        "source_path": spec["source_path"],
        "source_sha256": sha256_file(source_path),
        "dpi": _clean_number(dpi),
        "output_path": spec["output_path"],
        "media_type": "image/png",
        "sha256": sha256_file(output_path),
        "bytes": output_path.stat().st_size,
        "dimensions_pixels": {
            "width": width_pixels,
            "height": height_pixels,
        },
        "ocr_performed": False,
    }
    if "kind" in spec:
        result["kind"] = spec["kind"]
    return result


def _normalize_crop_spec(
    raw_spec: Mapping[str, Any], *, index: int
) -> dict[str, Any]:
    if not isinstance(raw_spec, Mapping):
        raise PdfExtractionError(f"crop specification {index} must be a mapping")

    source_value = raw_spec.get("source_path", raw_spec.get("source"))
    if source_value is None:
        raise PdfExtractionError(
            f"crop specification {index} is missing source_path"
        )
    source_path, _ = _normalize_relative_path(source_value, "source_path")

    asset_id = raw_spec.get("asset_id")
    if not isinstance(asset_id, str) or not _ASSET_ID_PATTERN.fullmatch(asset_id):
        raise PdfExtractionError(
            f"crop specification {index} has an invalid asset_id"
        )

    raw_parts = raw_spec.get("parts")
    if raw_parts is None:
        page, numeric_box = _normalize_crop_part(raw_spec, context=f"crop specification {index}")
        parts = None
    else:
        if "page" in raw_spec or "box" in raw_spec:
            raise PdfExtractionError(
                f"crop specification {index} cannot combine page/box with parts"
            )
        if (
            not isinstance(raw_parts, (list, tuple))
            or len(raw_parts) < 2
        ):
            raise PdfExtractionError(
                f"crop specification {index} parts must contain at least two mappings"
            )
        parts = []
        for part_index, raw_part in enumerate(raw_parts, start=1):
            if not isinstance(raw_part, Mapping):
                raise PdfExtractionError(
                    f"crop specification {index} part {part_index} must be a mapping"
                )
            part_page, part_box = _normalize_crop_part(
                raw_part,
                context=f"crop specification {index} part {part_index}",
            )
            parts.append({"page": part_page, "box": part_box})

    if "output_path" not in raw_spec:
        raise PdfExtractionError(
            f"crop specification {index} is missing output_path"
        )
    output_path, output_parts = _normalize_relative_path(
        raw_spec["output_path"], "output_path"
    )
    if not output_parts[-1].lower().endswith(".png"):
        raise PdfExtractionError("PDF crop output_path must end in .png")

    normalized: dict[str, Any] = {
        "source_path": source_path,
        "asset_id": asset_id,
        "output_path": output_path,
    }
    if parts is None:
        normalized.update({"page": page, "box": numeric_box})
    else:
        normalized["parts"] = parts
    if "embedded_image_index" in raw_spec:
        image_index = raw_spec["embedded_image_index"]
        if (parts is not None or isinstance(image_index, bool)
                or not isinstance(image_index, int) or image_index < 1):
            raise PdfExtractionError("embedded_image_index requires a positive integer and single page/box")
        normalized["embedded_image_index"] = image_index
    preserve_layout = raw_spec.get("preserve_source_layout", False)
    if not isinstance(preserve_layout, bool):
        raise PdfExtractionError("preserve_source_layout must be a boolean")
    if preserve_layout:
        if parts is None or len({part["page"] for part in parts}) != 1:
            raise PdfExtractionError("preserve_source_layout requires same-page parts")
        normalized["preserve_source_layout"] = True
    if "kind" in raw_spec:
        kind = raw_spec["kind"]
        if not isinstance(kind, str) or not kind.strip():
            raise PdfExtractionError("crop kind must be a non-empty string")
        normalized["kind"] = kind.strip()
    raw_rotation = raw_spec.get("rotate_clockwise", 0)
    if (
        isinstance(raw_rotation, bool)
        or not isinstance(raw_rotation, int)
        or raw_rotation not in {0, 90, 180, 270}
    ):
        raise PdfExtractionError(
            "crop rotate_clockwise must be one of 0, 90, 180, or 270"
        )
    if parts is not None and raw_rotation:
        raise PdfExtractionError(
            "composite PDF crops cannot use rotate_clockwise"
        )
    if raw_rotation:
        normalized["rotate_clockwise"] = raw_rotation
    raw_padding = raw_spec.get("padding_points", 0)
    padding_points = _finite_number(raw_padding, "padding_points")
    if padding_points < 0 or padding_points > 72:
        raise PdfExtractionError(
            "crop padding_points must be between 0 and 72"
        )
    if parts is not None and padding_points:
        raise PdfExtractionError(
            "composite PDF crops cannot use padding_points"
        )
    if padding_points:
        normalized["padding_points"] = padding_points
    return normalized


def _normalize_crop_part(
    raw_part: Mapping[str, Any],
    *,
    context: str,
) -> tuple[int, list[float]]:
    page = raw_part.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise PdfExtractionError(f"{context} page must be a positive integer")
    box = raw_part.get("box")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise PdfExtractionError(f"{context} box must contain four numbers")
    return page, [_finite_number(value, "box") for value in box]


def _normalize_relative_path(value: Any, field: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str) or not value:
        raise PdfExtractionError(f"{field} must be a non-empty string")
    if "\x00" in value:
        raise PdfExtractionError(f"{field} contains a NUL byte")

    # Treat both slash styles as separators on every platform.  This also
    # rejects Windows drive-relative paths such as C:folder/file.pdf.
    slash_path = value.replace("\\", "/")
    if slash_path.startswith("/") or re.match(r"^[A-Za-z]:", slash_path):
        raise PdfExtractionError(f"{field} must be relative")
    parts = tuple(slash_path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise PdfExtractionError(f"{field} contains an unsafe path component")
    if any(":" in part for part in parts):
        raise PdfExtractionError(f"{field} contains an unsafe colon")
    return "/".join(parts), parts


def _resolve_source_path(source_root: Path, relative_path: str) -> Path:
    _, parts = _normalize_relative_path(relative_path, "source_path")
    candidate = source_root.joinpath(*parts)
    try:
        reject_reparse_chain(candidate, source_root)
        resolved = ensure_within(candidate, source_root, require_exists=True)
    except (UnsafePathError, FileNotFoundError) as exc:
        raise PdfExtractionError(
            f"unsafe or missing PDF source {relative_path!r}"
        ) from exc
    if not resolved.is_file() or is_reparse_point(candidate):
        raise PdfExtractionError(f"PDF source is not a regular file: {relative_path!r}")
    with resolved.open("rb") as stream:
        signature = stream.read(1_024)
    if b"%PDF-" not in signature:
        raise PdfExtractionError(f"source is not a PDF: {relative_path!r}")
    return resolved


def _resolve_output_path(extraction_root: Path, relative_path: str) -> Path:
    _, parts = _normalize_relative_path(relative_path, "output_path")
    candidate = extraction_root.joinpath(*parts)
    try:
        reject_reparse_chain(candidate, extraction_root)
        ensure_within(candidate, extraction_root, require_exists=False)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        reject_reparse_chain(candidate, extraction_root)
        resolved = ensure_within(candidate, extraction_root, require_exists=False)
    except (UnsafePathError, FileNotFoundError, OSError) as exc:
        raise PdfExtractionError(
            f"unsafe PDF crop output {relative_path!r}"
        ) from exc
    if candidate.exists() and (is_reparse_point(candidate) or not candidate.is_file()):
        raise PdfExtractionError(
            f"PDF crop output is not a regular file: {relative_path!r}"
        )
    return resolved


def _validate_existing_root(root: Path, field: str) -> Path:
    path = Path(root)
    try:
        if is_reparse_point(path):
            raise PdfExtractionError(f"{field} may not be a reparse point")
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise PdfExtractionError(f"{field} does not exist: {path}") from exc
    if not resolved.is_dir():
        raise PdfExtractionError(f"{field} is not a directory: {path}")
    return resolved


def _prepare_output_root(root: Path) -> Path:
    path = Path(root)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PdfExtractionError(f"could not create extraction_root: {path}") from exc
    return _validate_existing_root(path, "extraction_root")


def _validate_dpi(value: int | float) -> float:
    dpi = _finite_number(value, "dpi")
    if dpi <= 0 or dpi > MAX_DPI:
        raise PdfExtractionError(f"dpi must be greater than 0 and at most {MAX_DPI}")
    return dpi


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PdfExtractionError(f"{field} must contain finite numbers")
    result = float(value)
    if not math.isfinite(result):
        raise PdfExtractionError(f"{field} must contain finite numbers")
    return result


def _validate_box(
    box: Sequence[float],
    page_width: float,
    page_height: float,
    *,
    asset_id: str,
) -> tuple[float, float, float, float]:
    left, top, right, bottom = box
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise PdfExtractionError(f"crop box is invalid for {asset_id!r}")
    # PDF page boxes commonly round a six-decimal binary-float dimension to
    # two decimals in inventory tooling (for example 841.919983 -> 841.92).
    # Accept only that sub-thousandth-point representation noise; materially
    # out-of-bounds crops still fail closed.
    tolerance = 1e-4
    if right > page_width + tolerance or bottom > page_height + tolerance:
        raise PdfExtractionError(
            f"crop box exceeds the PDF page for {asset_id!r} "
            f"({page_width:g} x {page_height:g} points)"
        )
    # Clamp values that differ from the page edge only by floating-point noise.
    right = min(right, page_width)
    bottom = min(bottom, page_height)
    return left, top, right, bottom


def _reject_duplicate_specs(specs: Sequence[Mapping[str, Any]]) -> None:
    asset_ids: set[str] = set()
    outputs: set[str] = set()
    for spec in specs:
        asset_id = str(spec["asset_id"])
        output = str(spec["output_path"]).casefold()
        if asset_id in asset_ids:
            raise PdfExtractionError(f"duplicate asset_id: {asset_id!r}")
        if output in outputs:
            raise PdfExtractionError(
                f"duplicate PDF crop output_path: {spec['output_path']!r}"
            )
        asset_ids.add(asset_id)
        outputs.add(output)


def _normalize_pages(pages: Sequence[int] | None, page_count: int) -> list[int]:
    if pages is None:
        return list(range(1, page_count + 1))
    if isinstance(pages, (str, bytes)):
        raise PdfExtractionError("pages must be a sequence of one-based integers")
    result: list[int] = []
    seen: set[int] = set()
    for value in pages:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise PdfExtractionError("pages must contain positive integers")
        if value > page_count:
            raise PdfExtractionError(
                f"page {value} is outside the PDF ({page_count} pages)"
            )
        if value in seen:
            raise PdfExtractionError(f"duplicate page requested: {value}")
        seen.add(value)
        result.append(value)
    return result


def _to_rgb_on_white(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        try:
            background.alpha_composite(rgba)
            return background.convert("RGB")
        finally:
            rgba.close()
            background.close()
    return image.convert("RGB")


def _encode_png(image: Image.Image) -> bytes:
    stream = io.BytesIO()
    image.save(
        stream,
        format="PNG",
        optimize=False,
        compress_level=9,
    )
    return stream.getvalue()


def _clean_number(value: int | float) -> int | float:
    numeric = float(value)
    if numeric.is_integer():
        return int(numeric)
    return round(numeric, 6)


__all__ = [
    "DEFAULT_DPI",
    "MAX_DPI",
    "MAX_OUTPUT_PIXELS",
    "PdfExtractionError",
    "inspect_pdf_native_text",
    "render_pdf_crop",
    "render_pdf_crops",
]
