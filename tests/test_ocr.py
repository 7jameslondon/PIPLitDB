from __future__ import annotations

import hashlib
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from pypdf import PdfWriter

from scripts.extraction.ocr import (
    ONNXRUNTIME_VERSION,
    RAPIDOCR_VERSION,
    OcrDependencyError,
    OcrDetection,
    OcrEngineDiagnostics,
    OcrModelFile,
    OcrNetworkDisabledError,
    OcrPolicyError,
    PdfOcrRegion,
    PdfVisualExclusion,
    RapidOcrEngine,
    RapidOcrModelSet,
    ocr_pdf_regions,
    ocr_rendered_region,
    parse_pdf_ocr_config,
)


class CapturingEngine:
    def __init__(self, detections: tuple[OcrDetection, ...] = ()) -> None:
        self.detections = detections
        self.images: list[Image.Image] = []

    def recognize(self, image: Image.Image) -> tuple[OcrDetection, ...]:
        self.images.append(image.copy())
        return self.detections

    def diagnostics(self) -> OcrEngineDiagnostics:
        return OcrEngineDiagnostics(
            name="synthetic",
            version="1.0",
            backend="synthetic",
            backend_version="1.0",
            network_access="disabled",
            execution_provider="test",
        )

    def close(self) -> None:
        for image in self.images:
            image.close()


def _box(x1: float, y1: float, x2: float, y2: float):
    return ((x1, y1), (x2, y1), (x2, y2), (x1, y2))


class RenderedRegionTests(unittest.TestCase):
    def test_masks_visual_pixels_and_sorts_only_within_region(self) -> None:
        image = Image.new("RGB", (100, 100), (0, 0, 0))
        engine = CapturingEngine(
            (
                OcrDetection("lower", 0.8, _box(5, 70, 40, 80)),
                OcrDetection("upper", 0.9, _box(5, 5, 40, 15)),
            )
        )
        region = PdfOcrRegion("page-001-left", 1, (0, 0, 100, 100))
        exclusion = PdfVisualExclusion(
            "figure-001", 1, (20, 20, 80, 80), "figure"
        )
        self.addCleanup(image.close)
        self.addCleanup(engine.close)

        first = ocr_rendered_region(
            image, region, engine, visual_exclusions=(exclusion,), dpi=300
        )
        second = ocr_rendered_region(
            image, region, engine, visual_exclusions=(exclusion,), dpi=300
        )

        captured = engine.images[0]
        self.assertEqual(captured.getpixel((10, 10)), (0, 0, 0))
        self.assertEqual(captured.getpixel((50, 50)), (255, 255, 255))
        self.assertEqual(first.text, "upper\nlower")
        self.assertEqual(first.image_sha256, second.image_sha256)
        self.assertEqual(
            first.masked_exclusions[0]["masked_pixel_box"], [20, 20, 80, 80]
        )
        self.assertEqual(
            first.observations[0].polygon_pdf_points[0], (5.0, 5.0)
        )

    def test_rejects_region_wholly_inside_visual(self) -> None:
        image = Image.new("RGB", (20, 20), "black")
        engine = CapturingEngine()
        self.addCleanup(image.close)
        self.addCleanup(engine.close)

        with self.assertRaises(OcrPolicyError):
            ocr_rendered_region(
                image,
                PdfOcrRegion("unsafe", 2, (10, 10, 30, 30)),
                engine,
                visual_exclusions=(
                    PdfVisualExclusion(
                        "scheme-1", 2, (0, 0, 40, 40), "scheme"
                    ),
                ),
            )
        self.assertEqual(engine.images, [])

    def test_rejects_figure_as_ocr_target(self) -> None:
        image = Image.new("RGB", (20, 20), "white")
        engine = CapturingEngine()
        self.addCleanup(image.close)
        self.addCleanup(engine.close)
        with self.assertRaises(OcrPolicyError):
            ocr_rendered_region(
                image,
                PdfOcrRegion("figure-target", 1, (0, 0, 20, 20), role="figure"),
                engine,
            )


class OcrConfigTests(unittest.TestCase):
    def test_preserves_declared_order_and_derives_safe_caption_targets(self) -> None:
        plan = parse_pdf_ocr_config(
            {
                "engine": "rapidocr",
                "dpi": 400,
                "use_cls": False,
                "text_score": 0.5,
                "box_thresh": 0.5,
                "unclip_ratio": 1.6,
                "page_regions": [
                    {
                        "page": 2,
                        "reading_order": [
                            {
                                "region_id": "page-002-right-first",
                                "box": [300, 60, 550, 700],
                                "role": "document_text",
                            },
                            {
                                "region_id": "page-002-left-second",
                                "box": [40, 60, 280, 700],
                                "role": "equation",
                            },
                        ],
                        "exclusions": [
                            {
                                "exclusion_id": "manual-figure",
                                "box": [330, 200, 500, 350],
                                "role": "figure",
                            }
                        ],
                    }
                ],
            },
            crop_specs=(
                {
                    "asset_id": "scheme_001",
                    "label": "Scheme 1",
                    "page": 2,
                    "box": [50, 400, 250, 600],
                    "caption_page": 2,
                    "caption_box": [50, 605, 250, 630],
                },
            ),
        )

        self.assertEqual(plan.requested_engine, "rapidocr")
        self.assertEqual(plan.dpi, 400)
        self.assertFalse(plan.use_cls)
        self.assertEqual(plan.text_score, 0.5)
        self.assertEqual(plan.box_thresh, 0.5)
        self.assertEqual(plan.unclip_ratio, 1.6)
        self.assertEqual(
            [region.region_id for region in plan.regions],
            [
                "page-002-right-first",
                "page-002-left-second",
                "caption-scheme_001",
            ],
        )
        self.assertEqual(plan.regions[-1].role, "caption")
        self.assertEqual(plan.regions[-1].asset_id, "scheme_001")
        self.assertEqual(
            [(item.exclusion_id, item.role) for item in plan.visual_exclusions],
            [("manual-figure", "figure"), ("crop-scheme_001", "scheme")],
        )

    def test_rejects_visual_role_in_reading_order(self) -> None:
        with self.assertRaises(OcrPolicyError):
            parse_pdf_ocr_config(
                {
                    "page_regions": [
                        {
                            "page": 1,
                            "reading_order": [
                                {
                                    "region_id": "bad",
                                    "box": [0, 0, 20, 20],
                                    "role": "scheme",
                                }
                            ],
                        }
                    ]
                }
            )


class RapidOcrAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        files = {}
        for role in ("detector", "classifier", "recognizer", "dictionary"):
            path = root / f"{role}.bin"
            path.write_bytes(f"synthetic-{role}".encode("ascii"))
            files[role] = OcrModelFile(path, f"synthetic-{role}-v1")
        self.files = files
        self.models = RapidOcrModelSet(**files)
        self.versions = {
            "rapidocr": RAPIDOCR_VERSION,
            "onnxruntime": ONNXRUNTIME_VERSION,
        }

    def test_lazy_adapter_normalizes_result_and_records_model_hashes(self) -> None:
        captured: dict[str, object] = {}

        class FakeRapid:
            def __init__(self, *, params):
                captured["params"] = params

            def __call__(self, content: bytes):
                captured["content"] = content
                return SimpleNamespace(
                    boxes=[_box(10, 20, 30, 40)],
                    txts=("β-test",),
                    scores=(0.975,),
                )

        engine = RapidOcrEngine(
            self.models,
            engine_factory=FakeRapid,
            package_versions=self.versions,
        )
        image = Image.new("RGB", (50, 50), "white")
        self.addCleanup(image.close)
        detections = engine.recognize(image)

        self.assertEqual(detections[0].text, "β-test")
        self.assertTrue(captured["content"].startswith(b"\x89PNG\r\n\x1a\n"))
        params = captured["params"]
        self.assertEqual(params["EngineConfig.onnxruntime.intra_op_num_threads"], 1)
        self.assertFalse(params["EngineConfig.onnxruntime.use_cuda"])
        diagnostics = engine.diagnostics()
        self.assertEqual(diagnostics.network_access, "disabled")
        self.assertEqual(
            [item.role for item in diagnostics.models],
            ["detector", "classifier", "recognizer", "dictionary"],
        )
        detector_path = self.files["detector"].path
        self.assertEqual(
            diagnostics.models[0].sha256,
            hashlib.sha256(detector_path.read_bytes()).hexdigest(),
        )

    def test_onnx_recognizer_dictionary_is_optional(self) -> None:
        captured: dict[str, object] = {}

        class FakeRapid:
            def __init__(self, *, params):
                captured["params"] = params

            def __call__(self, _content: bytes):
                return SimpleNamespace(boxes=None, txts=None, scores=None)

        models = RapidOcrModelSet(
            detector=self.files["detector"],
            classifier=self.files["classifier"],
            recognizer=self.files["recognizer"],
        )
        engine = RapidOcrEngine(
            models,
            use_cls=False,
            engine_factory=FakeRapid,
            package_versions=self.versions,
        )

        self.assertNotIn("Rec.rec_keys_path", captured["params"])
        self.assertFalse(captured["params"]["Global.use_cls"])
        self.assertEqual(
            [item.role for item in engine.diagnostics().models],
            ["detector", "classifier", "recognizer"],
        )
        self.assertFalse(dict(engine.diagnostics().settings)["use_cls"])

    def test_lazy_import_reports_optional_dependency(self) -> None:
        with patch(
            "scripts.extraction.ocr.importlib.import_module",
            side_effect=ModuleNotFoundError("rapidocr"),
        ) as importer:
            with self.assertRaises(OcrDependencyError):
                RapidOcrEngine(self.models, package_versions=self.versions)
        importer.assert_called_once_with("rapidocr")

    def test_network_is_blocked_during_third_party_initialization(self) -> None:
        def factory(*, params):
            del params
            socket.create_connection(("127.0.0.1", 9), timeout=0.01)

        with self.assertRaises(OcrNetworkDisabledError):
            RapidOcrEngine(
                self.models,
                engine_factory=factory,
                package_versions=self.versions,
            )

    def test_rejects_changed_model_before_inference(self) -> None:
        engine = RapidOcrEngine(
            self.models,
            engine_factory=lambda **_kwargs: lambda _content: SimpleNamespace(
                boxes=None, txts=None, scores=None
            ),
            package_versions=self.versions,
        )
        self.files["detector"].path.write_bytes(b"changed-model")
        image = Image.new("RGB", (10, 10), "white")
        self.addCleanup(image.close)
        with self.assertRaisesRegex(Exception, "changed after engine initialization"):
            engine.recognize(image)


class PdfRenderingTests(unittest.TestCase):
    def test_renders_declared_pdf_region_without_intermediate_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            with source.open("wb") as stream:
                writer.write(stream)

            engine = CapturingEngine(
                (OcrDetection("blank-page", 0.9, _box(1, 1, 10, 10)),)
            )
            self.addCleanup(engine.close)
            run = ocr_pdf_regions(
                source,
                "papers (private)/00000/pdf/main.pdf",
                (PdfOcrRegion("page-001", 1, (0, 0, 36, 36)),),
                engine,
                dpi=72,
            )

            self.assertEqual(run.regions[0].dimensions_pixels, (36, 36))
            self.assertEqual(run.regions[0].text, "blank-page")
            self.assertEqual(run.engine.name, "synthetic")
            self.assertEqual(run.source_path, "papers (private)/00000/pdf/main.pdf")
            self.assertEqual(list(Path(temporary).iterdir()), [source])


if __name__ == "__main__":
    unittest.main()
