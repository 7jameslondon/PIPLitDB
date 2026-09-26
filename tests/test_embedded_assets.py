from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.extraction.models import EmbeddedAsset, SourceFile, SupplementExtraction
from scripts.extraction.pipeline import (
    ExtractionError,
    _asset_identity_keys,
    _embedded_assets_after_pdf_overrides,
    _enrich_assets,
    _materialize_embedded_assets,
    _prepare_supplement_assets,
)


class EmbeddedAssetTests(unittest.TestCase):
    def test_pdf_crop_enrichment_retains_reviewed_supplement_parent(self) -> None:
        rendered = [
            {
                "asset_id": "supplement_001_pa1_structure",
                "output_path": "supplementary/supplement_001/pa1.png",
            }
        ]
        specs = [
            {
                "asset_id": "supplement_001_pa1_structure",
                "parent_id": "supplement_001",
                "category": "supplement_image",
                "label": "PA1",
            }
        ]

        [asset] = _enrich_assets(rendered, specs)

        self.assertEqual(asset["parent_id"], "supplement_001")
        self.assertEqual(asset["category"], "supplement_image")
    def setUp(self) -> None:
        self.source = SourceFile(
            role="main_html",
            path=Path("source.html"),
            relative_path="papers (private)/00001/html/main.html",
            size=4,
            sha256=hashlib.sha256(b"html").hexdigest(),
            detected_format="text/html",
        )

    def _pending(self, **changes: object) -> EmbeddedAsset:
        values: dict[str, object] = {
            "asset_id": "figure_001",
            "category": "figure",
            "label": "Figure 1",
            "media_type": "image/png",
            "output_path": "figures/main/figure_001.png",
            "data": b"png",
            "source_path": self.source.relative_path,
            "source_locator": "figure#f0001",
        }
        values.update(changes)
        return EmbeddedAsset(**values)  # type: ignore[arg-type]

    def test_materializes_exact_bytes_and_manifest_hash(self) -> None:
        data = b"\x89PNG\r\n\x1a\nsource-bytes"
        pending = EmbeddedAsset(
            asset_id="figure_001",
            category="figure",
            label="Figure 1",
            media_type="image/png",
            output_path="figures/main/figure_001.png",
            data=data,
            source_path=self.source.relative_path,
            source_locator="figure#f0001 img[src]",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = _materialize_embedded_assets([pending], root, [self.source])
            self.assertEqual(
                (root / "figures/main/figure_001.png").read_bytes(), data
            )
            self.assertEqual(assets[0]["sha256"], hashlib.sha256(data).hexdigest())
            self.assertFalse(assets[0]["ocr_performed"])

    def test_materializes_table_cell_relationship_metadata(self) -> None:
        pending = self._pending(
            asset_id="table_001_part_01_cell_r002_c003",
            category="table_cell",
            label="Table 1 graphical cell row 2 column 3",
            output_path="tables/main/table_001_cells/part_01_r002_c003.png",
            parent_table_id="table_001",
            compound_id="part_01_row_2_column_3",
        )
        with tempfile.TemporaryDirectory() as temporary:
            [asset] = _materialize_embedded_assets(
                [pending], Path(temporary), [self.source]
            )

        self.assertEqual(asset["parent_table_id"], "table_001")
        self.assertEqual(asset["compound_id"], "part_01_row_2_column_3")

    def test_reviewed_pdf_crop_replaces_same_id_embedded_asset(self) -> None:
        first = self._pending()
        second = self._pending(
            asset_id="figure_002",
            label="Figure 2",
            output_path="figures/main/figure_002.png",
        )
        remaining = _embedded_assets_after_pdf_overrides(
            [first, second],
            [{"asset_id": "figure_001"}],
        )

        self.assertEqual(remaining, [second])

    def test_rejects_traversal(self) -> None:
        pending = self._pending(output_path="../escape.png")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ExtractionError):
                _materialize_embedded_assets(
                    [pending], Path(temporary), [self.source]
                )

    def test_normalizes_separators_but_rejects_noncanonical_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = _materialize_embedded_assets(
                [self._pending(output_path=r"figures\main\figure_001.png")],
                root,
                [self.source],
            )
            self.assertEqual(assets[0]["output_path"], "figures/main/figure_001.png")

        for value in (
            "figures//figure.png",
            "figures/./figure.png",
            " figures/figure.png",
            "figures/figure.png ",
            "figures/figure.",
            "figures/CON.png",
            "figures/line\nbreak.png",
        ):
            with self.subTest(path=value), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(ExtractionError):
                    _materialize_embedded_assets(
                        [self._pending(output_path=value)],
                        Path(temporary),
                        [self.source],
                    )

    def test_rejects_case_insensitive_duplicates_and_reserved_assets(self) -> None:
        first = self._pending()
        duplicate_path = replace(
            first,
            asset_id="figure_002",
            output_path="FIGURES/main/FIGURE_001.PNG",
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ExtractionError, "duplicate output path"):
                _materialize_embedded_assets(
                    [first, duplicate_path], Path(temporary), [self.source]
                )

        reserved = {
            "asset_id": "figure_001",
            "output_path": "supplementary/supplement_001/media.png",
        }
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ExtractionError, "duplicate asset_id"):
                _materialize_embedded_assets(
                    [first],
                    Path(temporary),
                    [self.source],
                    reserved_assets=[reserved],
                )

    def test_global_registry_rejects_cross_source_id_and_path_collisions(self) -> None:
        supplement = {
            "asset_id": "supplement_001_media_001",
            "output_path": "supplementary/supplement_001/media/image.png",
        }
        with self.assertRaisesRegex(ExtractionError, "duplicate asset_id"):
            _asset_identity_keys(
                [
                    supplement,
                    {
                        "asset_id": "supplement_001_media_001",
                        "output_path": "figures/pdf/figure_001.png",
                    },
                ]
            )
        with self.assertRaisesRegex(ExtractionError, "duplicate output path"):
            _asset_identity_keys(
                [
                    supplement,
                    {
                        "asset_id": "pdf_figure_001",
                        "output_path": "SUPPLEMENTARY/supplement_001/MEDIA/IMAGE.PNG",
                    },
                ]
            )

    def test_refuses_to_overwrite_an_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "figures/main/figure_001.png"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"preserve me")

            with self.assertRaisesRegex(ExtractionError, "already exists"):
                _materialize_embedded_assets(
                    [self._pending(data=b"replacement")], root, [self.source]
                )

            self.assertEqual(destination.read_bytes(), b"preserve me")

    def test_rejects_reparse_parent_without_writing_outside(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "extraction"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "figures").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            with self.assertRaises(ExtractionError):
                _materialize_embedded_assets(
                    [self._pending(output_path="figures/escaped.png")],
                    root,
                    [self.source],
                )

            self.assertFalse((outside / "escaped.png").exists())

    def test_normalizes_materialized_supplement_assets_for_the_pipeline(self) -> None:
        data = b"presentation media"
        source = replace(
            self.source,
            role="supplement",
            relative_path="papers (private)/00001/supplementary/slides.pptx",
        )
        supplement = SupplementExtraction(
            supplement_id="supplement_001",
            source=source,
            copied_path="supplementary/supplement_001/slides.pptx",
            blocks=[],
            figures=[],
            warnings=[],
            assets=[
                {
                    "asset_id": "supplement_001_media_001",
                    "kind": "supplement_image",
                    "label": "Slide image",
                    "path": "supplementary/supplement_001/media/image.tiff",
                    "media_type": "image/tiff",
                }
            ],
            asset_ids=["supplement_001_media_001"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "supplementary/supplement_001/media/image.tiff"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(data)

            assets = _prepare_supplement_assets([supplement], root)

        self.assertEqual(assets[0]["output_path"], "supplementary/supplement_001/media/image.tiff")
        self.assertEqual(assets[0]["category"], "supplement_image")
        self.assertEqual(assets[0]["supplement_id"], "supplement_001")
        self.assertEqual(assets[0]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(assets[0]["bytes"], len(data))
        self.assertFalse(assets[0]["ocr_performed"])
        self.assertNotIn("path", assets[0])
        self.assertEqual(supplement.assets, assets)


if __name__ == "__main__":
    unittest.main()
