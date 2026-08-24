"""Write private diagnostics without adding provenance noise to record content."""

from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path
from typing import Any, Iterable

from .models import ArticleExtraction, SourceFile, SupplementExtraction, ValidationFinding
from .paths import atomic_write_json, atomic_write_jsonl, atomic_write_text, sha256_file
from .record_schema import RECORD_SCHEMA_VERSION


PIPELINE_VERSION = "0.2.0-record-json"


def _dependency_versions() -> dict[str, str]:
    versions: dict[str, str] = {"pipeline": PIPELINE_VERSION}
    for package in (
        "lxml",
        "jsonschema",
        "onnxruntime",
        "Pillow",
        "pdfplumber",
        "pypdf",
        "pypdfium2",
        "PyYAML",
        "rapidocr",
    ):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


def _pipeline_code_sha256() -> str:
    """Fingerprint the checked-out extraction implementation and schemas."""

    root = Path(__file__).resolve().parent
    files = [
        *root.glob("*.py"),
        *root.glob("*.ps1"),
        *root.joinpath("schemas").glob("*.json"),
    ]
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def inventory_files(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(
            (candidate for candidate in root.rglob("*") if candidate.is_file()),
            key=lambda item: item.relative_to(root).as_posix(),
        )
    ]


def _nonempty_string(value: Any, default: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _portable_ocr_engine_details(value: dict[str, Any]) -> dict[str, Any]:
    """Remove machine-local model paths from the portable manifest copy.

    Full installed paths remain in private ``page_analysis.jsonl``.  Manifest
    model entries retain stable filenames, versions, sizes, and hashes without
    being mistaken for extraction-output hash assertions.
    """

    result = dict(value)
    models = value.get("models")
    if isinstance(models, list):
        portable_models: list[dict[str, Any]] = []
        for model in models:
            if not isinstance(model, dict):
                continue
            portable = {key: item for key, item in model.items() if key != "path"}
            raw_path = model.get("path")
            if isinstance(raw_path, str) and raw_path.strip():
                portable["model_name"] = Path(raw_path).name
            portable_models.append(portable)
        result["models"] = portable_models
    return result


def _text_extraction_details(article: ArticleExtraction) -> dict[str, Any]:
    """Return normalized, backward-compatible article text metadata."""

    raw = article.text_extraction if isinstance(article.text_extraction, dict) else {}
    source_role = _nonempty_string(raw.get("source_role"), "main_html")
    source_path = _nonempty_string(raw.get("source_path"), source_role)
    default_method = (
        "deterministic-pdf-glyph-layout-extraction"
        if source_role == "main_pdf"
        else "publisher-html-semantic-extraction"
    )
    method = _nonempty_string(raw.get("method"), default_method)
    ocr_performed = raw.get("ocr_performed") is True or any(
        isinstance(row, dict) and row.get("ocr_performed") is True
        for row in article.page_diagnostics
    )
    if "supporting_source" in raw:
        supporting_source = raw.get("supporting_source")
        if not isinstance(supporting_source, str) or not supporting_source.strip():
            supporting_source = None
        else:
            supporting_source = supporting_source.strip()
    else:
        # Preserve the original HTML-first report for existing callers.  PDF
        # extraction does not claim that an HTML source exists unless the
        # extractor explicitly records one.
        supporting_source = "main_pdf" if source_role == "main_html" else None
    result: dict[str, Any] = {
        "source_role": source_role,
        "source_path": source_path,
        "method": method,
        "ocr_performed": ocr_performed,
        "supporting_source": supporting_source,
    }
    if ocr_performed:
        raw_dpi = raw.get("ocr_dpi")
        raw_count = raw.get("ocr_region_count")
        raw_engine = raw.get("ocr_engine")
        if isinstance(raw_dpi, int) and not isinstance(raw_dpi, bool) and raw_dpi > 0:
            result["ocr_dpi"] = raw_dpi
        if (
            isinstance(raw_count, int)
            and not isinstance(raw_count, bool)
            and raw_count > 0
        ):
            result["ocr_region_count"] = raw_count
        if isinstance(raw_engine, dict):
            result["ocr_engine"] = _portable_ocr_engine_details(raw_engine)
    return result


def _source_label(source_role: str) -> str:
    return {
        "main_html": "publisher HTML",
        "main_pdf": "main PDF",
    }.get(source_role, source_role.replace("_", " "))


def _page_analysis_rows(article: ArticleExtraction) -> list[dict[str, Any]]:
    return [
        {"schema_version": "1.0", **row}
        for row in article.page_diagnostics
    ]


def _block_rows(
    article: ArticleExtraction, supplements: Iterable[SupplementExtraction]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    blocks = [block for section in article.sections for block in section.blocks]
    blocks.extend(article.front_matter)
    blocks.extend(article.supporting_information)
    blocks.extend(article.references)
    for supplement in supplements:
        blocks.extend(supplement.blocks)
    for block in blocks:
        row = {
            "schema_version": "1.0",
            "block_id": block.block_id,
            "kind": block.kind,
            "source_path": block.source_path,
            "source_locator": block.source_locator,
            "text_sha256": hashlib.sha256(
                block.plain_text.encode("utf-8")
            ).hexdigest(),
            "characters": len(block.plain_text),
        }
        if block.source_geometry:
            row["source_geometry"] = block.source_geometry
        rows.append(row)
    return rows


def _reconciliation_rows(
    article: ArticleExtraction,
    supplements: list[SupplementExtraction],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    text_extraction = _text_extraction_details(article)
    source_role = text_extraction["source_role"]
    source_label = _source_label(source_role)
    if source_role == "main_pdf":
        body_content = "main article prose, structure, and references"
        if article.tables:
            body_content += ", including PDF-derived table values"
        if text_extraction["ocr_performed"]:
            body_reason = (
                "local region-based OCR of reviewed document-text regions is primary; "
                "figure and scheme regions were excluded, and OCR use is recorded "
                "page by page"
            )
        else:
            body_reason = "deterministic PDF glyph/layout extraction is primary"
    elif source_role == "main_html":
        body_content = "main article prose, structure, and references"
        if article.tables:
            body_content += ", including HTML-native table values"
        body_reason = (
            "publisher HTML is primary; reviewed PDF-backed overrides supply "
            "identified corrections and visual assets"
        )
    else:
        body_content = "main article prose, structure, references, and table values"
        body_reason = f"{source_label} is the primary semantic article source"

    if text_extraction["ocr_performed"]:
        ocr_reason = (
            "OCR was performed for article text; page-level use is recorded "
            "when page analysis is available"
        )
    elif source_role == "main_pdf":
        ocr_reason = (
            "deterministic PDF glyph/layout extraction was used; no OCR was performed"
        )
    else:
        ocr_reason = f"native text from {source_label} was used; no OCR was performed"

    rows: list[dict[str, Any]] = [
        {
            "schema_version": "1.0",
            "decision_id": "main-body-authority",
            "content": body_content,
            "selected_source": text_extraction["source_path"],
            "supporting_source": text_extraction["supporting_source"],
            "reason": body_reason,
        },
        {
            "schema_version": "1.0",
            "decision_id": "ocr-policy",
            "content": "main article text",
            "selected_source": text_extraction["method"],
            "supporting_source": None,
            "reason": ocr_reason,
        },
    ]
    if any(figure.kind == "graphical_abstract" and not figure.output_path for figure in article.figures):
        rows.append(
            {
                "schema_version": "1.0",
                "decision_id": "graphical-abstract-image",
                "content": "graphical abstract image",
                "selected_source": None,
                "supporting_source": "supplied article sources",
                "reason": "no graphical-abstract pixels are available in the supplied article sources",
                "status": "unresolved",
            }
        )
    for table in article.tables:
        rows.append(
            {
                "schema_version": "1.0",
                "decision_id": f"table-source-{table.table_id}",
                "content": table.label,
                "selected_source": table.source_path,
                "supporting_source": table.image_path,
                "reason": (
                    "the table is image/PDF-sourced, so its JSON is accompanied by the authoritative source image"
                    if table.requires_source_image
                    else "the table is HTML-native, so structured JSON is sufficient and no redundant full-table image is emitted"
                ),
                "status": "included",
            }
        )
    for asset in assets:
        category = str(asset.get("category", ""))
        supporting_structure = None
        if source_role in {"main_html", "main_pdf"} and category != "supplement_figure":
            source_prefix = "HTML" if source_role == "main_html" else "PDF"
            structure_kind = "table" if category == "table" else "caption"
            supporting_structure = f"{source_prefix} {structure_kind} structure"
        rows.append(
            {
                "schema_version": "1.0",
                "decision_id": f"asset-{asset['asset_id']}",
                "content": asset.get("label") or asset["asset_id"],
                "selected_source": asset.get("source_path"),
                "supporting_source": supporting_structure,
                "reason": "source pixels rendered without OCR",
                "status": "included",
            }
        )
    for supplement in supplements:
        rows.append(
            {
                "schema_version": "1.0",
                "decision_id": f"source-{supplement.supplement_id}",
                "content": supplement.source.relative_path,
                "selected_source": supplement.source.relative_path,
                "supporting_source": None,
                "reason": "original bytes copied and native text extracted into record.json",
                "status": "included",
            }
        )
    return rows


def _source_coverage(
    sources: list[SourceFile],
    article: ArticleExtraction,
    supplements: list[SupplementExtraction],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    text_extraction = _text_extraction_details(article)
    primary_role = text_extraction["source_role"]
    article_blocks = [block for section in article.sections for block in section.blocks]
    article_blocks.extend(article.front_matter)
    article_blocks.extend(article.supporting_information)
    article_blocks.extend(article.references)
    rows: list[dict[str, Any]] = []
    for index, source in enumerate(sources, 1):
        if source.role == "main_pdf":
            disposition = "included"
            if primary_role == "main_pdf":
                if text_extraction["ocr_performed"]:
                    reason = (
                        "primary semantic source for article content; local OCR "
                        "represents only reviewed document-text regions while "
                        "figure and scheme pixels remain excluded"
                    )
                else:
                    reason = (
                        "primary semantic source for article content; deterministic "
                        "PDF glyph/layout extraction represents the main text"
                    )
            else:
                reason = (
                    "visual assets represented and native-text availability "
                    "inspected; full automated alignment remains pending"
                )
        elif source.role == "main_html":
            disposition = "included"
            reason = (
                "primary semantic source for article content"
                if primary_role == "main_html"
                else "supporting semantic source for article reconciliation"
            )
        elif source.role == "supplement":
            disposition = "included"
            reason = "original copied and supported text/assets extracted"
        else:
            disposition = "included"
            reason = "record identity and citation metadata rendered"
        output_ids = [
            block.block_id
            for block in article_blocks
            if block.source_path == source.relative_path
        ]
        output_ids.extend(
            figure.figure_id
            for figure in article.figures
            if figure.source_path == source.relative_path
        )
        output_ids.extend(
            table.table_id
            for table in article.tables
            if table.source_path == source.relative_path
        )
        output_ids.extend(
            str(asset["asset_id"])
            for asset in assets
            if asset.get("source_path") == source.relative_path
        )
        for supplement in supplements:
            if supplement.source.relative_path != source.relative_path:
                continue
            output_ids.append(supplement.supplement_id)
            output_ids.extend(block.block_id for block in supplement.blocks)
            output_ids.extend(figure.figure_id for figure in supplement.figures)
        if source.role == "public_metadata":
            output_ids.extend(("record-title", "record-authors", "record-citation"))
        elif source.role == "main_html" and primary_role == "main_html":
            output_ids.append("record-citation")
        if (
            source.role == "main_pdf"
            and primary_role != "main_pdf"
            and not output_ids
        ):
            disposition = "duplicate"
            reason = (
                "secondary verification source for completeness, notation, and "
                "visual agreement with the primary HTML; no distinct canonical "
                "output is required"
            )
        rows.append(
            {
                "schema_version": "1.0",
                "coverage_id": f"source-{index:03d}",
                "content_kind": "source_file",
                "source_path": source.relative_path,
                "source_locator": "entire file",
                "status": disposition,
                "reason": reason,
                "output_ids": sorted(set(output_ids)),
            }
        )
    return rows


def _supplement_exclusions(
    supplements: Iterable[SupplementExtraction],
) -> list[dict[str, Any]]:
    return [
        exclusion
        for supplement in supplements
        for exclusion in supplement.exclusions
    ]


def _pdf_exclusion_coverage(article: ArticleExtraction) -> list[dict[str, Any]]:
    """Account for decoded PDF text deliberately routed away from body prose."""

    reasons = {
        "title_or_authors": (
            "duplicate",
            "PDF title/author lines are represented by canonical public metadata",
            ["record-title", "record-authors"],
        ),
        "article_history": (
            "duplicate",
            "article-history text is represented in normalized front matter",
            [block.block_id for block in article.front_matter],
        ),
        "caption": (
            "duplicate",
            "caption text is represented in the consolidated caption section",
            [figure.figure_id for figure in article.figures],
        ),
        "visual_asset": (
            "duplicate",
            "visual-region text remains represented by the linked figure or scheme asset",
            [figure.figure_id for figure in article.figures],
        ),
        "configured_furniture": (
            "intentionally_excluded",
            "recurring journal header, printed page number, or other configured publisher furniture",
            [],
        ),
        "rotated_furniture": (
            "intentionally_excluded",
            "rotated publisher download/terms strip outside the article content",
            [],
        ),
    }
    rows: list[dict[str, Any]] = []
    source_path = _text_extraction_details(article)["source_path"]
    for summary in article.page_diagnostics:
        if not isinstance(summary, dict):
            continue
        if summary.get("kind") == "configured_exclusion":
            page = summary.get("page")
            box = summary.get("box")
            index = summary.get("exclusion_index")
            reason = summary.get("reason")
            if (
                not isinstance(page, int)
                or not isinstance(index, int)
                or not isinstance(box, list)
                or len(box) != 4
                or not isinstance(reason, str)
                or not reason.strip()
            ):
                continue
            rows.append(
                {
                    "schema_version": "1.0",
                    "coverage_id": (
                        f"pdf-configured-exclusion-p{page:03d}-{index:03d}"
                    ),
                    "content_kind": "pdf_page_region",
                    "source_path": source_path,
                    "source_locator": (
                        f"PDF page {page}, box "
                        f"[{', '.join(f'{float(value):.2f}' for value in box)}]"
                    ),
                    "status": "intentionally_excluded",
                    "reason": reason,
                }
            )
            continue
        if summary.get("kind") != "page_summary":
            continue
        page = summary.get("page")
        excluded = summary.get("excluded_lines")
        output_ids_by_category = summary.get("excluded_output_ids", {})
        if not isinstance(page, int) or not isinstance(excluded, dict):
            continue
        for category, raw_count in sorted(excluded.items()):
            if category not in reasons or not isinstance(raw_count, int) or raw_count <= 0:
                continue
            status, reason, fallback_output_ids = reasons[category]
            exact_output_ids = (
                output_ids_by_category.get(category)
                if isinstance(output_ids_by_category, dict)
                else None
            )
            output_ids = (
                exact_output_ids
                if isinstance(exact_output_ids, list)
                and all(isinstance(value, str) and value for value in exact_output_ids)
                else fallback_output_ids
            )
            row: dict[str, Any] = {
                "schema_version": "1.0",
                "coverage_id": f"pdf-exclusion-p{page:03d}-{category}",
                "content_kind": "pdf_body_routing",
                "source_path": source_path,
                "source_locator": f"PDF page {page}; {raw_count} decoded line(s)",
                "status": status,
                "reason": reason,
            }
            if output_ids:
                row["output_ids"] = sorted(set(output_ids))
            rows.append(row)
    return rows


def _confidence_document(
    article: ArticleExtraction,
    supplements: list[SupplementExtraction],
    assets: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    text_extraction = _text_extraction_details(article)
    source_role = text_extraction["source_role"]
    source_label = _source_label(source_role)
    warning_codes = {str(item.get("code", "")) for item in warnings}
    graphical_missing = "graphical_abstract_image_unavailable" in warning_codes
    supplement_warnings = any(supplement.warnings for supplement in supplements)
    main_text_pending = (
        "automated_pdf_html_alignment_not_implemented" in warning_codes
        or text_extraction["ocr_performed"]
        or source_role == "main_pdf"
    )
    if source_role == "main_pdf":
        identity_basis = (
            "public metadata identity agrees with the main PDF; visible citation "
            "details are included"
        )
        if text_extraction["ocr_performed"]:
            main_text_basis = (
                "local region-based OCR is primary; its probabilistic output, "
                "page/region geometry, engine and model hashes, excluded visual "
                "regions, and source-reviewed repairs are recorded in diagnostics"
            )
        else:
            main_text_basis = (
                "deterministic PDF glyph/layout extraction is primary; no OCR was "
                "used for article text"
            )
    elif source_role == "main_html":
        identity_basis = (
            "public metadata identity agrees with the publisher HTML; visible "
            "citation details are included"
        )
        main_text_basis = (
            "publisher HTML is primary and reviewed PDF-backed repairs are "
            "recorded; automated element alignment is pending when noted"
        )
    else:
        identity_basis = (
            f"public metadata identity agrees with {source_label}; visible citation "
            "details are included"
        )
        main_text_basis = f"{source_label} is the primary semantic article source"
    return {
        "schema_version": "1.0",
        "probabilistic": bool(text_extraction["ocr_performed"]),
        "scale": {
            "high": "independently identified sources agree and deterministic checks pass",
            "medium": "usable candidate with a material check still pending",
            "unresolved": "required information is unavailable or not yet reconciled",
            "not_applicable": "the record contains no content in this category",
        },
        "categories": {
            "identity_and_citation": {
                "level": "high",
                "basis": identity_basis,
            },
            "main_text": {
                "level": "medium" if main_text_pending else "high",
                "basis": main_text_basis,
            },
            "scientific_tables": {
                "level": "medium" if article.tables else "not_applicable",
                "basis": (
                    "machine-ready JSON is available for every table; source renderings are retained only for image/PDF-sourced tables, with graphical cell images retained when needed"
                    if article.tables
                    else "the article contains no scientific tables"
                ),
            },
            "figures_and_schemes": {
                "level": (
                    "medium"
                    if assets
                    else ("unresolved" if article.figures else "not_applicable")
                ),
                "basis": (
                    "source regions were copied or rendered without applying OCR "
                    "to figure content and remain subject to visual review"
                    if assets
                    else (
                        "figure or scheme records exist but no local assets are attached"
                        if article.figures
                        else "the article contains no figures or schemes"
                    )
                ),
            },
            "supplementary_material": {
                "level": (
                    ("medium" if supplement_warnings else "high")
                    if supplements
                    else "not_applicable"
                ),
                "basis": (
                    "original bytes are preserved and supported native text/assets are extracted"
                    if supplements
                    else "the record contains no supplementary files"
                ),
            },
            "graphical_abstract_image": {
                "level": (
                    "unresolved"
                    if graphical_missing
                    else (
                        "high"
                        if any(figure.kind == "graphical_abstract" for figure in article.figures)
                        else "not_applicable"
                    )
                ),
                "basis": (
                    "no recoverable graphical-abstract pixels exist in the supplied sources"
                    if graphical_missing
                    else (
                        "source image is represented"
                        if any(figure.kind == "graphical_abstract" for figure in article.figures)
                        else "the supplied article does not declare a graphical abstract"
                    )
                ),
            },
        },
    }


def write_diagnostics(
    diagnostic_root: Path,
    extraction_root: Path,
    *,
    record_id: str,
    run_id: str,
    fingerprint: str,
    sources: list[SourceFile],
    article: ArticleExtraction,
    supplements: list[SupplementExtraction],
    assets: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    override_text: str | None,
    source_anomalies: list[dict[str, Any]],
) -> None:
    diagnostic_root.mkdir(parents=True, exist_ok=False)
    warnings = list(article.warnings)
    text_extraction = _text_extraction_details(article)
    for supplement in supplements:
        warnings.extend(supplement.warnings)
    if any(figure.kind == "graphical_abstract" and not figure.output_path for figure in article.figures):
        warnings.append(
            {
                "schema_version": "1.0",
                "code": "graphical_abstract_image_unavailable",
                "severity": "structural",
                "message": "No graphical-abstract pixels exist in the supplied article sources.",
                "source_path": next(
                    (
                        source.relative_path
                        for source in sources
                        if source.role == text_extraction["source_role"]
                    ),
                    None,
                ),
            }
        )
        coverage = [
            *coverage,
            {
                "schema_version": "1.0",
                "coverage_id": "unresolved-graphical-abstract-image",
                "content_kind": "graphical_abstract_image",
                "source_path": next(
                    (
                        source.relative_path
                        for source in sources
                        if source.role == text_extraction["source_role"]
                    ),
                    "",
                ),
                "source_locator": "graphical abstract figure container",
                "status": "unresolved",
                "reason": "no recoverable image pixels in supplied sources",
            },
        ]

    override_snapshot = (
        {
            "override_snapshot_sha256": hashlib.sha256(
                override_text.encode("utf-8")
            ).hexdigest(),
            "override_snapshot_bytes": len(override_text.encode("utf-8")),
        }
        if override_text is not None
        else {}
    )
    atomic_write_json(
        diagnostic_root / "sources.json",
        {
            "schema_version": "1.0",
            "record_id": record_id,
            "source_fingerprint": fingerprint,
            "sources": [source.as_dict() for source in sources],
            **override_snapshot,
        },
    )
    atomic_write_jsonl(diagnostic_root / "blocks.jsonl", _block_rows(article, supplements))
    atomic_write_jsonl(
        diagnostic_root / "coverage.jsonl",
        [
            *_source_coverage(sources, article, supplements, assets),
            *_pdf_exclusion_coverage(article),
            *coverage,
            *_supplement_exclusions(supplements),
        ],
    )
    atomic_write_jsonl(
        diagnostic_root / "reconciliation.jsonl",
        _reconciliation_rows(article, supplements, assets),
    )
    atomic_write_jsonl(
        diagnostic_root / "repairs.jsonl",
        [repair.as_dict() for repair in article.repairs],
    )
    atomic_write_jsonl(diagnostic_root / "warnings.jsonl", warnings)
    atomic_write_jsonl(
        diagnostic_root / "source_anomalies.jsonl", source_anomalies
    )
    if article.page_diagnostics:
        atomic_write_jsonl(
            diagnostic_root / "page_analysis.jsonl",
            _page_analysis_rows(article),
        )
    if override_text is not None:
        atomic_write_text(diagnostic_root / "overrides.yaml", override_text)
    atomic_write_json(
        diagnostic_root / "confidence.json",
        _confidence_document(article, supplements, assets, warnings),
    )
    atomic_write_json(
        diagnostic_root / "quality.json",
        {
            "schema_version": "1.0",
            "record_id": record_id,
            "status": "needs_review" if warnings else "draft",
            "review": "not_performed",
            "unresolved_coverage": sum(
                1 for entry in coverage if entry.get("status") == "unresolved"
            ),
            "warnings": len(warnings),
            "validation": "not_run",
            "confidence": "confidence.json",
        },
    )
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "record_id": record_id,
        "run_id": run_id,
        "source_fingerprint": fingerprint,
        "pipeline": _dependency_versions(),
        "pipeline_code_sha256": _pipeline_code_sha256(),
        **override_snapshot,
        "ocr_performed": text_extraction["ocr_performed"],
        "files": inventory_files(extraction_root),
        "assets": assets,
    }
    record_json = extraction_root / "record.json"
    if record_json.is_file():
        manifest["record_document"] = {
            "path": "record.json",
            "media_type": "application/vnd.pip-litdb.record+json",
            "schema_version": RECORD_SCHEMA_VERSION,
        }
    if article.text_extraction:
        manifest["text_extraction"] = {
            "source_role": text_extraction["source_role"],
            "source_path": text_extraction["source_path"],
            "method": text_extraction["method"],
            "ocr_performed": text_extraction["ocr_performed"],
            "supporting_source": text_extraction["supporting_source"],
            **(
                {"ocr_dpi": text_extraction["ocr_dpi"]}
                if "ocr_dpi" in text_extraction
                else {}
            ),
            **(
                {"ocr_region_count": text_extraction["ocr_region_count"]}
                if "ocr_region_count" in text_extraction
                else {}
            ),
            **(
                {"ocr_engine": text_extraction["ocr_engine"]}
                if "ocr_engine" in text_extraction
                else {}
            ),
        }
    if article.page_diagnostics:
        manifest["page_analysis"] = "page_analysis.jsonl"
    atomic_write_json(diagnostic_root / "manifest.json", manifest)


def write_validation_result(
    diagnostic_root: Path,
    findings: Iterable[ValidationFinding],
    counts: dict[str, int],
) -> str:
    findings_list = [finding.as_dict() for finding in findings]
    blocking = [
        finding
        for finding in findings_list
        if finding["severity"] in {"critical", "scientific", "structural"}
    ]
    status = "needs_review"
    atomic_write_json(
        diagnostic_root / "validation.json",
        {
            "schema_version": "1.0",
            "status": "failed" if blocking else "passed",
            "counts": counts,
            "findings": findings_list,
        },
    )
    quality_path = diagnostic_root / "quality.json"
    import json

    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["status"] = status
    quality["validation"] = "failed" if blocking else "passed"
    quality["validation_findings"] = len(findings_list)
    atomic_write_json(quality_path, quality)
    return status
