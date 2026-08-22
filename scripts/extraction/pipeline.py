"""Orchestrate one offline, staged record extraction."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - CLI dependency check
    yaml = None

from .discovery import discover_sources, source_fingerprint
from .html_extractor import extract_html
from .metadata import RecordMetadata, load_record_metadata
from .models import ContentBlock, EmbeddedAsset, SourceFile
from .ocr import (
    OcrDependencyError,
    OcrModelFile,
    RapidOcrEngine,
    RapidOcrModelSet,
    ocr_pdf_from_config,
    parse_pdf_ocr_config,
)
from .pdf_article_extractor import extract_pdf_article
from .pdf_ocr_text import (
    ocr_run_to_pdf_text_document,
    ocr_text_extraction_details,
)
from .paths import (
    WINDOWS_DEVICE_NAMES,
    UnsafePathError,
    atomic_write_bytes,
    ensure_within,
    is_reparse_point,
    reject_reparse_chain,
    sha256_file,
    validate_record_id,
    validate_run_id,
)
from .pdf_extractor import inspect_pdf_native_text, render_pdf_crops
from .record_json import write_record_json
from .reporting import write_diagnostics, write_validation_result
from .supplements import extract_supplements
from .validation import validate_candidate


class ExtractionError(RuntimeError):
    """A controlled extraction failure."""


@dataclass(frozen=True)
class ExtractionResult:
    record_id: str
    run_id: str
    run_root: Path
    extraction_root: Path
    diagnostic_root: Path
    source_fingerprint: str
    status: str
    finding_count: int


def _bundled_rapidocr_models() -> RapidOcrModelSet:
    """Locate the pinned wheel's three local PP-OCRv6 model files.

    Runtime model URLs are never followed.  OCR refuses to start unless every
    expected wheel-bundled file is already present and readable; the adapter
    records their hashes in the private diagnostics.
    """

    spec = find_spec("rapidocr")
    locations = tuple(spec.submodule_search_locations or ()) if spec else ()
    if len(locations) != 1:
        raise OcrDependencyError(
            "RapidOCR 3.9.2 is not installed as one local package; install "
            "requirements.txt"
        )
    model_root = Path(locations[0]).resolve() / "models"
    return RapidOcrModelSet(
        detector=OcrModelFile(
            model_root / "PP-OCRv6_det_small.onnx",
            "PP-OCRv6-det-small",
        ),
        classifier=OcrModelFile(
            model_root / "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
            "ch-ppocr-mobile-v2.0-cls-mobile",
        ),
        recognizer=OcrModelFile(
            model_root / "PP-OCRv6_rec_small.onnx",
            "PP-OCRv6-rec-small",
        ),
    )


def _run_pdf_ocr(
    source: Path,
    source_path: str,
    config: dict[str, Any],
    crop_specs: list[dict[str, Any]],
) -> tuple[Any, dict[str, Any]]:
    plan = parse_pdf_ocr_config(config, crop_specs=crop_specs)
    if plan.requested_engine.casefold() != "rapidocr":
        raise ExtractionError(
            f"unsupported local OCR engine: {plan.requested_engine!r}"
        )
    engine = RapidOcrEngine(
        _bundled_rapidocr_models(),
        use_cls=plan.use_cls,
        text_score=plan.text_score,
        box_thresh=plan.box_thresh,
        unclip_ratio=plan.unclip_ratio,
    )
    run = ocr_pdf_from_config(
        source,
        source_path,
        config,
        engine,
        crop_specs=crop_specs,
    )
    document = ocr_run_to_pdf_text_document(source, source_path, run, config)
    return document, ocr_text_extraction_details(run)


def _load_override(path: Path | None, record_id: str) -> tuple[dict[str, Any], str | None, str | None]:
    if path is None or not path.exists():
        return {}, None, None
    if yaml is None:
        raise ExtractionError("PyYAML is required to load extraction overrides")
    text = path.read_text(encoding="utf-8", errors="strict")
    value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ExtractionError(f"override must be a mapping: {path}")
    if str(value.get("record_id", "")) != record_id:
        raise ExtractionError(f"override record_id does not match {record_id}: {path}")
    if str(value.get("schema_version", "")) != "1.0":
        raise ExtractionError(f"unsupported override schema_version: {path}")
    for key in (
        "text_repairs",
        "pdf_crops",
        "supplement_exclusions",
        "front_matter",
        "supporting_information_additions",
        "source_anomalies",
    ):
        if key in value and not isinstance(value[key], list):
            raise ExtractionError(f"override {key} must be a list: {path}")
    for key in ("pdf_text", "pdf_ocr", "expected_counts"):
        if key in value and not isinstance(value[key], dict):
            raise ExtractionError(f"override {key} must be a mapping: {path}")
    return value, text, sha256_file(path)


def _apply_front_matter_overrides(
    article: Any,
    specs: list[dict[str, Any]],
    sources: list[SourceFile],
) -> None:
    """Add reviewed source details omitted from the publisher article HTML."""

    allowed_sources = {source.relative_path for source in sources}
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise ExtractionError(f"front_matter item {index} must be a mapping")
        label = str(spec.get("label", "")).strip()
        value = str(spec.get("value", "")).strip()
        source_path = str(spec.get("source_path", "")).replace("\\", "/")
        source_locator = str(spec.get("source_locator", "")).strip()
        if not label or not value or "\n" in label or "\n" in value:
            raise ExtractionError(
                f"front_matter item {index} requires single-line label and value"
            )
        if source_path not in allowed_sources or not source_locator:
            raise ExtractionError(
                f"front_matter item {index} requires discovered source evidence"
            )
        article.front_matter.append(
            ContentBlock(
                block_id=f"front-matter-{index:03d}",
                kind="front_matter",
                markdown=f"**{label}:** {value}",
                plain_text=f"{label}: {value}",
                source_path=source_path,
                source_locator=source_locator,
            )
        )


def _apply_supporting_information_additions(
    article: Any,
    specs: list[dict[str, Any]],
    sources: list[SourceFile],
) -> None:
    allowed_sources = {source.relative_path for source in sources}
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise ExtractionError(
                f"supporting_information_additions item {index} must be a mapping"
            )
        value = str(spec.get("value", "")).strip()
        source_path = str(spec.get("source_path", "")).replace("\\", "/")
        source_locator = str(spec.get("source_locator", "")).strip()
        if (
            not value
            or "\n" in value
            or source_path not in allowed_sources
            or not source_locator
        ):
            raise ExtractionError(
                f"invalid supporting_information_additions item {index}"
            )
        article.supporting_information.append(
            ContentBlock(
                block_id=f"supporting-addition-{index:03d}",
                kind="supporting_information",
                markdown=value,
                plain_text=value,
                source_path=source_path,
                source_locator=source_locator,
            )
        )


def _validated_source_anomalies(
    specs: list[dict[str, Any]], sources: list[SourceFile]
) -> list[dict[str, Any]]:
    allowed_sources = {source.relative_path for source in sources}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise ExtractionError(f"source_anomalies item {index} must be a mapping")
        anomaly_id = str(spec.get("anomaly_id", "")).strip()
        source_path = str(spec.get("source_path", "")).replace("\\", "/")
        source_locator = str(spec.get("source_locator", "")).strip()
        observed = str(spec.get("observed", "")).strip()
        assessment = str(spec.get("assessment", "")).strip()
        disposition = str(spec.get("disposition", "")).strip()
        if (
            not anomaly_id
            or anomaly_id in seen
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", anomaly_id)
            or source_path not in allowed_sources
            or not all((source_locator, observed, assessment, disposition))
        ):
            raise ExtractionError(f"invalid source_anomalies item {index}")
        seen.add(anomaly_id)
        rows.append(
            {
                "schema_version": "1.0",
                "anomaly_id": anomaly_id,
                "source_path": source_path,
                "source_locator": source_locator,
                "observed": observed,
                "assessment": assessment,
                "disposition": disposition,
            }
        )
    return rows


def _source(sources: list[SourceFile], role: str) -> SourceFile:
    matches = [source for source in sources if source.role == role]
    if len(matches) != 1:
        raise ExtractionError(f"expected exactly one {role} source; found {len(matches)}")
    return matches[0]


def inventory_record(repository_root: Path, record_id: str) -> dict[str, Any]:
    sources = discover_sources(repository_root, record_id)
    return {
        "schema_version": "1.0",
        "record_id": validate_record_id(record_id),
        "source_fingerprint": source_fingerprint(sources),
        "sources": [source.as_dict() for source in sources],
    }


def _enrich_assets(
    rendered_assets: list[dict[str, Any]], crop_specs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    specs = {str(spec.get("asset_id")): spec for spec in crop_specs}
    enriched: list[dict[str, Any]] = []
    for asset in rendered_assets:
        spec = specs[asset["asset_id"]]
        merged = dict(asset)
        for key in (
            "category",
            "label",
            "parent_table_id",
            "compound_id",
            "source_role",
            "caption_page",
            "caption_box",
        ):
            if key in spec:
                merged[key] = spec[key]
        if "category" not in merged:
            merged["category"] = merged.get("kind", "figure")
        enriched.append(merged)
    return enriched


def _materialize_embedded_assets(
    pending_assets: list[EmbeddedAsset],
    extraction_root: Path,
    sources: list[SourceFile],
    *,
    reserved_assets: Iterable[Mapping[str, Any]] = (),
    reserved_paths: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Write source-embedded bytes only inside the isolated candidate root."""

    try:
        reject_reparse_chain(extraction_root, extraction_root)
        extraction_root = ensure_within(extraction_root, extraction_root)
    except (FileNotFoundError, OSError, UnsafePathError) as exc:
        raise ExtractionError(f"embedded asset output root is unsafe: {exc}") from exc
    source_hashes = {source.relative_path: source.sha256 for source in sources}
    seen_ids, seen_paths = _asset_identity_keys(
        reserved_assets, reserved_paths=reserved_paths
    )
    assets: list[dict[str, Any]] = []
    for pending in pending_assets:
        asset_id = _normalized_asset_id(pending.asset_id)
        if asset_id in seen_ids:
            raise ExtractionError(
                f"embedded asset has a duplicate asset_id: {asset_id!r}"
            )
        relative, parts = _normalized_asset_output_path(pending.output_path)
        path_key = relative.casefold()
        if path_key in seen_paths:
            raise ExtractionError(
                f"embedded asset has a duplicate output path: {relative!r}"
            )
        if not isinstance(pending.data, bytes):
            raise ExtractionError(
                f"embedded asset {asset_id!r} data must be immutable bytes"
            )
        if not isinstance(pending.ocr_performed, bool):
            raise ExtractionError(
                f"embedded asset {asset_id!r} ocr_performed must be boolean"
            )
        if pending.source_path not in source_hashes:
            raise ExtractionError(
                f"embedded asset refers to an undiscovered source: {pending.source_path!r}"
            )
        try:
            destination = ensure_within(
                extraction_root.joinpath(*parts), extraction_root, require_exists=False
            )
            reject_reparse_chain(destination, extraction_root)
            if os.path.lexists(destination):
                raise ExtractionError(
                    f"embedded asset output already exists: {relative!r}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            reject_reparse_chain(destination, extraction_root)
            atomic_write_bytes(destination, pending.data)
            reject_reparse_chain(destination, extraction_root)
            ensure_within(destination, extraction_root, require_exists=True)
            if is_reparse_point(destination) or not destination.is_file():
                raise ExtractionError(
                    f"embedded asset output is not a regular file: {relative!r}"
                )
        except ExtractionError:
            raise
        except (FileNotFoundError, OSError, UnsafePathError) as exc:
            raise ExtractionError(
                f"could not safely materialize embedded asset {asset_id!r}: {exc}"
            ) from exc
        assets.append(
            {
                "schema_version": "1.0",
                "asset_id": asset_id,
                "category": pending.category,
                "label": pending.label,
                "source_path": pending.source_path,
                "source_locator": pending.source_locator,
                "source_sha256": source_hashes[pending.source_path],
                "output_path": relative,
                "media_type": pending.media_type,
                "sha256": sha256_file(destination),
                "bytes": destination.stat().st_size,
                "ocr_performed": pending.ocr_performed,
            }
        )
        seen_ids.add(asset_id)
        seen_paths.add(path_key)
    return assets


def _normalized_asset_id(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ExtractionError(
            f"embedded asset has an empty or non-canonical asset_id: {value!r}"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ExtractionError(
            f"embedded asset_id contains a control character: {value!r}"
        )
    return value


def _normalized_asset_output_path(value: Any) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ExtractionError(
            f"embedded asset has an empty or non-canonical output path: {value!r}"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ExtractionError(
            f"embedded asset output path contains a control character: {value!r}"
        )
    normalized = value.replace("\\", "/")
    if normalized.startswith("/"):
        raise ExtractionError(f"embedded asset output path must be relative: {value!r}")
    parts = tuple(normalized.split("/"))
    if any(not part or part in {".", ".."} for part in parts):
        raise ExtractionError(
            f"embedded asset output path contains an unsafe segment: {value!r}"
        )
    for part in parts:
        if (
            part != part.strip()
            or part.endswith(".")
            or any(character in '<>:"|?*' for character in part)
            or part.split(".", 1)[0].upper() in WINDOWS_DEVICE_NAMES
        ):
            raise ExtractionError(
                f"embedded asset output path is not portable: {value!r}"
            )
    return "/".join(parts), parts


def _asset_identity_keys(
    assets: Iterable[Mapping[str, Any]], *, reserved_paths: Iterable[str] = ()
) -> tuple[set[str], set[str]]:
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for index, asset in enumerate(assets, start=1):
        if not isinstance(asset, Mapping):
            raise ExtractionError(f"asset registry entry {index} is not an object")
        asset_id = _normalized_asset_id(asset.get("asset_id"))
        relative, _ = _normalized_asset_output_path(asset.get("output_path"))
        path_key = relative.casefold()
        if asset_id in seen_ids:
            raise ExtractionError(f"duplicate asset_id across extraction sources: {asset_id!r}")
        if path_key in seen_paths:
            raise ExtractionError(
                f"duplicate output path across extraction sources: {relative!r}"
            )
        seen_ids.add(asset_id)
        seen_paths.add(path_key)
    for value in reserved_paths:
        relative, _ = _normalized_asset_output_path(value)
        path_key = relative.casefold()
        if path_key in seen_paths:
            raise ExtractionError(
                f"asset output conflicts with a preserved supplementary file: {relative!r}"
            )
        seen_paths.add(path_key)
    return seen_ids, seen_paths


def _reject_existing_asset_outputs(
    assets: Iterable[Mapping[str, Any]], extraction_root: Path
) -> None:
    for asset in assets:
        asset_id = _normalized_asset_id(asset.get("asset_id"))
        relative, parts = _normalized_asset_output_path(asset.get("output_path"))
        try:
            destination = ensure_within(
                extraction_root.joinpath(*parts), extraction_root, require_exists=False
            )
            reject_reparse_chain(destination, extraction_root)
        except (FileNotFoundError, OSError, UnsafePathError) as exc:
            raise ExtractionError(
                f"asset output for {asset_id!r} is unsafe: {relative!r}"
            ) from exc
        if os.path.lexists(destination):
            raise ExtractionError(
                f"asset output for {asset_id!r} would overwrite an existing file: {relative!r}"
            )


def _prepare_supplement_assets(
    supplements: Iterable[Any], extraction_root: Path
) -> list[dict[str, Any]]:
    """Normalize parser assets and bind them to the files actually written."""

    prepared: list[dict[str, Any]] = []
    for supplement in supplements:
        normalized_for_supplement: list[dict[str, Any]] = []
        for index, raw in enumerate(supplement.assets, start=1):
            if not isinstance(raw, Mapping):
                raise ExtractionError(
                    f"supplement {supplement.supplement_id!r} asset {index} is not an object"
                )
            value = dict(raw)
            asset_id = _normalized_asset_id(value.get("asset_id"))
            relative, parts = _normalized_asset_output_path(
                value.get("output_path", value.get("path"))
            )
            try:
                destination = ensure_within(
                    extraction_root.joinpath(*parts), extraction_root
                )
                reject_reparse_chain(destination, extraction_root)
            except (FileNotFoundError, OSError, UnsafePathError) as exc:
                raise ExtractionError(
                    f"supplement asset {asset_id!r} is missing or unsafe: {relative!r}"
                ) from exc
            if is_reparse_point(destination) or not destination.is_file():
                raise ExtractionError(
                    f"supplement asset {asset_id!r} is not a regular file: {relative!r}"
                )
            actual_sha256 = sha256_file(destination)
            declared_sha256 = value.get("sha256")
            if declared_sha256 is not None and declared_sha256 != actual_sha256:
                raise ExtractionError(
                    f"supplement asset {asset_id!r} hash does not match its materialized file"
                )
            ocr_performed = value.get("ocr_performed", False)
            if not isinstance(ocr_performed, bool):
                raise ExtractionError(
                    f"supplement asset {asset_id!r} ocr_performed must be boolean"
                )
            value.pop("path", None)
            value.update(
                {
                    "schema_version": "1.0",
                    "asset_id": asset_id,
                    "category": str(value.get("category") or value.get("kind") or "asset"),
                    "output_path": relative,
                    "supplement_id": supplement.supplement_id,
                    "source_path": supplement.source.relative_path,
                    "source_sha256": supplement.source.sha256,
                    "sha256": actual_sha256,
                    "bytes": destination.stat().st_size,
                    "ocr_performed": ocr_performed,
                }
            )
            normalized_for_supplement.append(value)
            prepared.append(value)
        supplement.assets = normalized_for_supplement
    return prepared


def _attach_assets(
    article: Any, supplements: list[Any], assets: list[dict[str, Any]]
) -> None:
    output_by_id = {asset["asset_id"]: asset["output_path"] for asset in assets}
    for figure in article.figures:
        figure.output_path = output_by_id.get(figure.figure_id)
    for table in article.tables:
        table.image_path = (
            output_by_id.get(table.table_id)
            if table.requires_source_image
            else None
        )
        table.structure_assets = {
            str(asset["compound_id"]): str(asset["output_path"])
            for asset in assets
            if asset.get("category") == "table_cell"
            and asset.get("parent_table_id") == table.table_id
            and asset.get("compound_id")
        }
    for supplement in supplements:
        for figure in supplement.figures:
            figure.output_path = output_by_id.get(figure.figure_id)


def _record_missing_asset_warnings(article: Any, supplements: list[Any]) -> None:
    for figure in article.figures:
        if figure.kind in {"figure", "scheme"} and not figure.output_path:
            article.warnings.append(
                {
                    "schema_version": "1.0",
                    "code": "main_figure_asset_missing",
                    "severity": "scientific",
                    "message": f"No local asset is attached for {figure.label}.",
                    "source_path": figure.source_path,
                    "source_locator": figure.source_locator,
                }
            )
    for table in article.tables:
        if table.requires_source_image and not table.image_path:
            article.warnings.append(
                {
                    "schema_version": "1.0",
                    "code": "main_table_source_image_missing",
                    "severity": "scientific",
                    "message": f"No source rendering is attached for {table.label}.",
                    "source_path": table.source_path,
                    "source_locator": table.source_locator,
                }
            )
    for supplement in supplements:
        for figure in supplement.figures:
            if not figure.output_path:
                article.warnings.append(
                    {
                        "schema_version": "1.0",
                        "code": "supplement_figure_asset_missing",
                        "severity": "scientific",
                        "message": f"No local asset is attached for {figure.label}.",
                        "source_path": figure.source_path,
                        "source_locator": figure.source_locator,
                    }
                )


def _verify_pdf_text(repository_root: Path, sources: list[SourceFile]) -> list[dict[str, Any]]:
    inspections: list[dict[str, Any]] = []
    for source in sources:
        if source.detected_format != "application/pdf":
            continue
        inspection = inspect_pdf_native_text(repository_root, source.relative_path)
        inspections.append(
            {
                "source_path": source.relative_path,
                "page_count": inspection["page_count"],
                "pages_with_native_text": sum(
                    1 for page in inspection["pages"] if page["has_native_text"]
                ),
            }
        )
    return inspections


def _validate_crop_sources(
    crop_specs: list[dict[str, Any]], sources: list[SourceFile]
) -> None:
    allowed = {
        source.relative_path
        for source in sources
        if source.role in {"main_pdf", "supplement"}
        and source.detected_format == "application/pdf"
    }
    for index, spec in enumerate(crop_specs, start=1):
        raw = spec.get("source_path", spec.get("source", ""))
        normalized = str(raw).replace("\\", "/")
        if normalized not in allowed:
            raise ExtractionError(
                f"PDF crop {index} source is not a discovered PDF for this record: {raw!r}"
            )


def _effective_crop_specs(article: Any, crop_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply table-output policy before any crop directories or files exist.

    A full-table rendering is useful only when pixels are the table's
    authoritative representation.  Explicit ``table_cell`` crops remain
    independent: they preserve meaningful graphical cells in an otherwise
    HTML-native table and create their subdirectory only when such crops exist.
    """

    tables = {table.table_id: table for table in article.tables}
    selected: list[dict[str, Any]] = []
    for index, spec in enumerate(crop_specs, start=1):
        category = str(spec.get("category", "")).strip().casefold()
        asset_id = str(spec.get("asset_id") or "")
        if category == "table" or (
            category != "table_cell" and asset_id in tables
        ):
            table_id = str(spec.get("parent_table_id") or asset_id)
            table = tables.get(table_id)
            if table is None:
                raise ExtractionError(
                    f"PDF crop {index} identifies unknown main table {table_id!r}"
                )
            if not table.requires_source_image:
                continue
        elif category == "table_cell":
            table_id = str(spec.get("parent_table_id") or "")
            if table_id not in tables:
                raise ExtractionError(
                    f"PDF crop {index} identifies unknown parent table {table_id!r}"
                )
        selected.append(spec)
    return selected


def _verify_source_snapshots(
    repository_root: Path,
    sources: list[SourceFile],
    override_path: Path | None,
    override_hash: str | None,
) -> None:
    """Fail closed if any authoritative input changed during extraction."""

    for source in sources:
        ensure_within(source.path, repository_root)
        reject_reparse_chain(source.path, repository_root)
        if not source.path.is_file():
            raise ExtractionError(f"source disappeared during extraction: {source.relative_path}")
        if source.path.stat().st_size != source.size or sha256_file(source.path) != source.sha256:
            raise ExtractionError(f"source changed during extraction: {source.relative_path}")
    if override_path is not None and override_hash is not None:
        ensure_within(override_path, repository_root)
        reject_reparse_chain(override_path, repository_root)
        if not override_path.is_file() or sha256_file(override_path) != override_hash:
            raise ExtractionError("private extraction override changed during extraction")


def extract_record(
    repository_root: Path,
    record_id: str,
    *,
    run_id: str,
    override_path: Path | None = None,
    dpi: int = 300,
) -> ExtractionResult:
    """Create, validate, and atomically publish one candidate inside staging.

    This never promotes into a record's live ``extraction/`` directory.
    """

    record_id = validate_record_id(record_id)
    run_id = validate_run_id(run_id)
    repository_root = repository_root.resolve(strict=True)
    private_root = ensure_within(repository_root / "papers (private)", repository_root)
    record_root = ensure_within(private_root / record_id, private_root)
    reject_reparse_chain(record_root, private_root)
    staging_root = ensure_within(private_root / "staging", private_root)
    reject_reparse_chain(staging_root, private_root)
    record_staging = staging_root / record_id
    record_staging.mkdir(parents=True, exist_ok=True)
    reject_reparse_chain(record_staging, staging_root)

    run_root = record_staging / run_id
    if run_root.exists():
        raise ExtractionError(f"staged run already exists and will not be overwritten: {run_root}")
    if override_path is None:
        candidate_override = record_root / "extraction_overrides.yaml"
        override_path = candidate_override if candidate_override.exists() else None
    elif not override_path.is_absolute():
        override_path = repository_root / override_path
    if override_path is not None:
        ensure_within(override_path, record_root)
        reject_reparse_chain(override_path, record_root)

    sources = discover_sources(repository_root, record_id)
    override, override_text, override_hash = _load_override(override_path, record_id)
    fingerprint = source_fingerprint(sources, override_hash)
    metadata_source = _source(sources, "public_metadata")
    html_sources = [source for source in sources if source.role == "main_html"]
    pdf_sources = [source for source in sources if source.role == "main_pdf"]
    if len(html_sources) > 1 or len(pdf_sources) != 1:
        raise ExtractionError(
            "expected exactly one main PDF and at most one main HTML source; "
            f"found {len(pdf_sources)} PDF and {len(html_sources)} HTML"
        )
    html_source = html_sources[0] if html_sources else None
    pdf_source = pdf_sources[0]
    metadata: RecordMetadata = load_record_metadata(metadata_source.path, record_id)
    requested_crop_specs = list(override.get("pdf_crops", []))
    _validate_crop_sources(requested_crop_specs, sources)
    if html_source is not None:
        article = extract_html(
            html_source.path,
            html_source.relative_path,
            override.get("text_repairs", []),
        )
    else:
        ocr_config = override.get("pdf_ocr")
        text_document = None
        text_extraction = None
        if ocr_config is not None:
            text_document, text_extraction = _run_pdf_ocr(
                pdf_source.path,
                pdf_source.relative_path,
                ocr_config,
                requested_crop_specs,
            )
        article = extract_pdf_article(
            pdf_source.path,
            pdf_source.relative_path,
            metadata,
            override.get("pdf_text", {}),
            crop_specs=requested_crop_specs,
            text_repairs=override.get("text_repairs", []),
            text_document=text_document,
            text_extraction=text_extraction,
        )
    _apply_front_matter_overrides(
        article, list(override.get("front_matter", [])), sources
    )
    _apply_supporting_information_additions(
        article,
        list(override.get("supporting_information_additions", [])),
        sources,
    )
    source_anomalies = _validated_source_anomalies(
        list(override.get("source_anomalies", [])), sources
    )
    if article.title != metadata.title:
        raise ExtractionError(
            f"article title does not match public metadata: {article.title!r} != {metadata.title!r}"
        )
    if html_source is not None:
        pdf_inspections = _verify_pdf_text(repository_root, sources)
        if any(
            item["pages_with_native_text"] != item["page_count"]
            for item in pdf_inspections
        ):
            raise ExtractionError(
                "the HTML-primary record contains a PDF page without native text; "
                "HTML/PDF reconciliation for that case is not enabled"
            )
        article.warnings.append(
            {
                "schema_version": "1.0",
                "code": "automated_pdf_html_alignment_not_implemented",
                "severity": "structural",
                "message": (
                    "PDF native-text availability is checked and reviewed overrides are applied, "
                    "but automated element-by-element PDF/HTML alignment is not implemented yet."
                ),
                "source_path": html_source.relative_path,
            }
        )

    build_root = Path(tempfile.mkdtemp(prefix=f".{run_id}.building-", dir=record_staging))
    extraction_root = build_root / "extraction"
    diagnostic_root = build_root / "extraction_diagnostic"
    extraction_root.mkdir()
    try:
        supplements = extract_supplements(
            sources, extraction_root, override.get("supplement_exclusions", [])
        )
        supplement_assets = _prepare_supplement_assets(
            supplements, extraction_root
        )
        supplement_paths = [supplement.copied_path for supplement in supplements]
        reserved_output_paths = [*supplement_paths, "record.json"]
        embedded_assets = _materialize_embedded_assets(
            list(article.embedded_assets),
            extraction_root,
            sources,
            reserved_assets=supplement_assets,
            reserved_paths=reserved_output_paths,
        )
        crop_specs = _effective_crop_specs(article, requested_crop_specs)
        _asset_identity_keys(
            [*supplement_assets, *embedded_assets, *crop_specs],
            reserved_paths=reserved_output_paths,
        )
        _reject_existing_asset_outputs(crop_specs, extraction_root)
        rendered_assets = render_pdf_crops(
            repository_root, extraction_root, crop_specs, dpi=dpi
        )
        assets = [
            *embedded_assets,
            *supplement_assets,
            *_enrich_assets(rendered_assets, crop_specs),
        ]
        _asset_identity_keys(assets, reserved_paths=reserved_output_paths)
        _attach_assets(article, supplements, assets)
        _record_missing_asset_warnings(article, supplements)
        # record.json is the sole canonical content document for new runs.
        # Tables live inside it; only binary/large assets remain as sidecars.
        rendered = write_record_json(
            metadata, article, supplements, assets, extraction_root
        )
        write_diagnostics(
            diagnostic_root,
            extraction_root,
            record_id=record_id,
            run_id=run_id,
            fingerprint=fingerprint,
            sources=sources,
            article=article,
            supplements=supplements,
            assets=assets,
            coverage=rendered.coverage,
            override_text=override_text,
            source_anomalies=source_anomalies,
        )
        expected_counts = override.get("expected_counts")
        validation = validate_candidate(
            extraction_root,
            diagnostic_root,
            expected_title=metadata.title,
            expected_counts=expected_counts,
            expected_metadata=metadata.as_dict(),
        )
        status = write_validation_result(
            diagnostic_root, validation.findings, validation.counts
        )
        _verify_source_snapshots(
            repository_root, sources, override_path, override_hash
        )
        os.replace(build_root, run_root)
    except BaseException:
        # The directory was created by this invocation, is beneath the fixed
        # private staging root, and has never been exposed as a completed run.
        try:
            ensure_within(build_root, record_staging)
            reject_reparse_chain(build_root, record_staging)
            shutil.rmtree(build_root)
        except (OSError, UnsafePathError):
            pass
        raise

    final_extraction = run_root / "extraction"
    final_diagnostic = run_root / "extraction_diagnostic"
    return ExtractionResult(
        record_id=record_id,
        run_id=run_id,
        run_root=run_root,
        extraction_root=final_extraction,
        diagnostic_root=final_diagnostic,
        source_fingerprint=fingerprint,
        status=status,
        finding_count=len(validation.findings),
    )
