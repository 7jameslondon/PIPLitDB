from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.extraction.validation import (
    _validate_pdf_ocr_diagnostics,
    validate_candidate,
)


PDF_PATH = "pdf/main.pdf"
PDF_SHA256 = "a" * 64
MODEL_HASHES = {
    "detector": "b" * 64,
    "classifier": "c" * 64,
    "recognizer": "d" * 64,
}


def _engine(*, diagnostic: bool) -> dict[str, object]:
    models: list[dict[str, object]] = []
    names = {
        "detector": "det.onnx",
        "classifier": "cls.onnx",
        "recognizer": "rec.onnx",
    }
    for index, role in enumerate(("detector", "classifier", "recognizer"), start=1):
        model: dict[str, object] = {
            "role": role,
            "version": f"model-{index}",
            "bytes": index * 100,
            "sha256": MODEL_HASHES[role],
        }
        if diagnostic:
            model["path"] = f"C:/private/models/{names[role]}"
        else:
            model["model_name"] = names[role]
        models.append(model)
    return {
        "name": "rapidocr",
        "version": "3.9.2",
        "backend": "onnxruntime",
        "backend_version": "1.28.0",
        "execution_provider": "CPUExecutionProvider",
        "network_access": "disabled",
        "settings": {
            "box_thresh": 0.5,
            "text_score": 0.5,
            "unclip_ratio": 1.6,
            "use_cls": False,
        },
        "models": models,
    }


def _observation(
    pdf_box: tuple[float, float, float, float],
    *,
    confidence: float = 0.98,
) -> dict[str, object]:
    x0, y0, x1, y1 = pdf_box
    polygon = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    return {
        "text": "Synthetic OCR text",
        "confidence": confidence,
        "polygon_pixels": polygon,
        "polygon_pdf_points": polygon,
    }


def _region(
    region_id: str,
    page: int,
    *,
    masked: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "kind": "ocr_region",
        "region_id": region_id,
        "page": page,
        "role": "document_text",
        "box": [0.0, 0.0, 100.0, 100.0],
        "coordinate_system": "pdf-points-top-left",
        "dpi": 300,
        "dimensions_pixels": {"width": 100, "height": 100},
        "rendered_input_sha256": "e" * 64,
        "masked_exclusions": masked or [],
        "observations": [_observation((10.0, 10.0, 20.0, 20.0))],
        "ocr_performed": True,
    }


def _loaded() -> dict[str, object]:
    mask = {
        "exclusion_id": "crop-figure_001",
        "role": "figure",
        "box": [40.0, 40.0, 60.0, 60.0],
        "intersection_box": [40.0, 40.0, 60.0, 60.0],
        "masked_pixel_box": [40, 40, 60, 60],
    }
    diagnostic_engine = _engine(diagnostic=True)
    manifest_engine = _engine(diagnostic=False)
    return {
        "manifest.json": {
            "ocr_performed": True,
            "pipeline": {
                "rapidocr": "3.9.2",
                "onnxruntime": "1.28.0",
            },
            "text_extraction": {
                "source_role": "main_pdf",
                "source_path": PDF_PATH,
                "method": "deterministic-local-region-ocr",
                "ocr_performed": True,
                "ocr_dpi": 300,
                "ocr_region_count": 2,
                "ocr_engine": manifest_engine,
            },
            "assets": [
                {
                    "asset_id": "figure_001",
                    "category": "figure",
                    "page": 1,
                    "box": [40.0, 40.0, 60.0, 60.0],
                    "coordinate_system": "pdf-points-top-left",
                    "ocr_performed": False,
                    "source_path": PDF_PATH,
                    "source_sha256": PDF_SHA256,
                }
            ],
        },
        "sources.json": {
            "sources": [
                {
                    "role": "main_pdf",
                    "path": PDF_PATH,
                    "sha256": PDF_SHA256,
                    "page_count": 2,
                }
            ]
        },
        "page_analysis.jsonl": [
            {
                "schema_version": "1.0",
                "kind": "ocr_run",
                "method": "deterministic-local-region-ocr",
                "source_path": PDF_PATH,
                "source_sha256": PDF_SHA256,
                "requested_engine": "rapidocr",
                "dpi": 300,
                "engine": diagnostic_engine,
                "region_count": 2,
                "ocr_performed": True,
            },
            _region("page-001-text", 1, masked=[mask]),
            _region("page-002-text", 2),
            {
                "kind": "page_summary",
                "page": 1,
                "source_path": PDF_PATH,
                "classification": "image_only",
                "ocr_performed": True,
            },
            {
                "kind": "page_summary",
                "page": 2,
                "source_path": PDF_PATH,
                "classification": "image_only",
                "ocr_performed": True,
            },
        ],
    }


def _codes(loaded: dict[str, object]) -> set[str]:
    return {
        finding.code for finding in _validate_pdf_ocr_diagnostics(loaded)
    }


class PdfOcrDiagnosticValidationTests(unittest.TestCase):
    def test_complete_ocr_ledger_passes(self) -> None:
        self.assertEqual(_validate_pdf_ocr_diagnostics(_loaded()), [])

    def test_html_and_native_pdf_candidates_are_unchanged(self) -> None:
        html = deepcopy(_loaded())
        html["manifest.json"]["text_extraction"]["source_role"] = "main_html"  # type: ignore[index]
        native = deepcopy(_loaded())
        native["manifest.json"]["ocr_performed"] = False  # type: ignore[index]
        native["manifest.json"]["text_extraction"]["ocr_performed"] = False  # type: ignore[index]
        native["page_analysis.jsonl"] = [
            {
                "kind": "page_summary",
                "page": 1,
                "ocr_performed": False,
            }
        ]

        self.assertEqual(_validate_pdf_ocr_diagnostics(html), [])
        self.assertEqual(_validate_pdf_ocr_diagnostics(native), [])

    def test_requires_exactly_one_ocr_run(self) -> None:
        missing = deepcopy(_loaded())
        missing["page_analysis.jsonl"] = [
            row
            for row in missing["page_analysis.jsonl"]  # type: ignore[union-attr]
            if row.get("kind") != "ocr_run"
        ]
        duplicate = deepcopy(_loaded())
        duplicate["page_analysis.jsonl"].insert(  # type: ignore[union-attr]
            1,
            deepcopy(duplicate["page_analysis.jsonl"][0]),  # type: ignore[index]
        )

        self.assertIn("ocr_run_count_invalid", _codes(missing))
        self.assertIn("ocr_run_count_invalid", _codes(duplicate))

    def test_checks_source_engine_models_dpi_counts_and_versions(self) -> None:
        loaded = deepcopy(_loaded())
        manifest = loaded["manifest.json"]  # type: ignore[assignment]
        run = loaded["page_analysis.jsonl"][0]  # type: ignore[index]
        manifest["text_extraction"]["ocr_dpi"] = 200
        manifest["text_extraction"]["ocr_region_count"] = 9
        manifest["text_extraction"]["ocr_engine"]["version"] = "wrong"
        manifest["text_extraction"]["ocr_engine"]["models"][0]["sha256"] = "f" * 64
        manifest["pipeline"]["rapidocr"] = "wrong"
        run["source_sha256"] = "0" * 64

        codes = _codes(loaded)
        self.assertTrue(
            {
                "ocr_dpi_mismatch",
                "ocr_engine_mismatch",
                "ocr_model_mismatch",
                "ocr_pipeline_version_mismatch",
                "ocr_region_count_mismatch",
                "ocr_source_hash_mismatch",
            }.issubset(codes)
        )

    def test_requires_unique_regions_for_every_ocr_page(self) -> None:
        missing = deepcopy(_loaded())
        missing["page_analysis.jsonl"] = [
            row
            for row in missing["page_analysis.jsonl"]  # type: ignore[union-attr]
            if row.get("region_id") != "page-002-text"
        ]
        duplicate = deepcopy(_loaded())
        extra = deepcopy(duplicate["page_analysis.jsonl"][1])  # type: ignore[index]
        duplicate["page_analysis.jsonl"].insert(3, extra)  # type: ignore[union-attr]
        duplicate["manifest.json"]["text_extraction"]["ocr_region_count"] = 3  # type: ignore[index]
        duplicate["page_analysis.jsonl"][0]["region_count"] = 3  # type: ignore[index]

        self.assertTrue(
            {"ocr_page_region_missing", "ocr_region_count_mismatch"}.issubset(
                _codes(missing)
            )
        )
        self.assertIn("ocr_region_duplicate", _codes(duplicate))

    def test_rejects_invalid_observation_geometry_confidence_and_hash(self) -> None:
        invalid_observation = deepcopy(_loaded())
        region = invalid_observation["page_analysis.jsonl"][1]  # type: ignore[index]
        observation = region["observations"][0]
        observation["confidence"] = 1.5
        observation["polygon_pdf_points"] = [
            [95, 95],
            [110, 95],
            [110, 110],
            [95, 110],
        ]
        invalid_hash = deepcopy(_loaded())
        invalid_hash["page_analysis.jsonl"][1][  # type: ignore[index]
            "rendered_input_sha256"
        ] = "bad"

        self.assertIn("ocr_observation_invalid", _codes(invalid_observation))
        self.assertIn("ocr_region_malformed", _codes(invalid_hash))

    def test_requires_visual_masks_and_forbids_visual_ocr_overlap(self) -> None:
        loaded = deepcopy(_loaded())
        region = loaded["page_analysis.jsonl"][1]  # type: ignore[index]
        region["masked_exclusions"] = []
        region["observations"] = [_observation((45.0, 45.0, 55.0, 55.0))]
        asset = loaded["manifest.json"]["assets"][0]  # type: ignore[index]
        asset["ocr_performed"] = True
        asset["source_sha256"] = "0" * 64

        codes = _codes(loaded)
        self.assertTrue(
            {
                "ocr_observation_overlaps_visual",
                "ocr_visual_asset_ocr_forbidden",
                "ocr_visual_asset_source_mismatch",
                "ocr_visual_exclusion_missing",
            }.issubset(codes)
        )

    def test_absolute_model_paths_are_diagnostics_only(self) -> None:
        loaded = deepcopy(_loaded())
        manifest = loaded["manifest.json"]
        text_extraction = manifest["text_extraction"]  # type: ignore[index]
        engine = text_extraction["ocr_engine"]
        manifest_model = engine["models"][0]
        manifest_model["path"] = "C:/private/models/det.onnx"

        self.assertIn("ocr_model_path_exposed", _codes(loaded))

    def test_validate_candidate_invokes_ocr_audit(self) -> None:
        loaded = deepcopy(_loaded())
        loaded["page_analysis.jsonl"] = [
            row
            for row in loaded["page_analysis.jsonl"]  # type: ignore[union-attr]
            if row.get("kind") != "ocr_run"
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            extraction = root / "extraction"
            diagnostic = root / "extraction_diagnostic"
            extraction.mkdir()
            diagnostic.mkdir()
            (extraction / "record.md").write_text(
                "# Synthetic OCR article\n",
                encoding="utf-8",
            )
            for name in ("manifest.json", "sources.json"):
                (diagnostic / name).write_text(
                    json.dumps(loaded[name]) + "\n",
                    encoding="utf-8",
                )
            (diagnostic / "page_analysis.jsonl").write_text(
                "\n".join(
                    json.dumps(row)
                    for row in loaded["page_analysis.jsonl"]  # type: ignore[union-attr]
                )
                + "\n",
                encoding="utf-8",
            )
            (diagnostic / "coverage.jsonl").write_text("", encoding="utf-8")
            (diagnostic / "quality.json").write_text("{}\n", encoding="utf-8")
            (diagnostic / "confidence.json").write_text(
                json.dumps(
                    {
                        "categories": {
                            "main_text": {
                                "level": "high",
                                "basis": "synthetic OCR fixture",
                            }
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic OCR article",
            )

        self.assertIn(
            "ocr_run_count_invalid",
            {finding.code for finding in report.findings},
        )


if __name__ == "__main__":
    unittest.main()
