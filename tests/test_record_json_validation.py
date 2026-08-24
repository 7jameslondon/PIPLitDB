from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
import unittest

from PIL import Image, ImageDraw

from scripts.extraction.validation import _record_json_counts, validate_candidate


TITLE = "Synthetic JSON article"
RECORD_ID = "00001"
RECORD_DOCUMENT = {
    "path": "record.json",
    "media_type": "application/vnd.pip-litdb.record+json",
    "schema_version": "1.0",
}
EXPECTED_COUNTS = {
    "pages": 0,
    "references": 1,
    "equations": 0,
    "main_figures": 1,
    "main_schemes": 1,
    "main_figures_and_schemes": 2,
    "tables": 1,
    "supplementary_figures": 1,
    "supplementary_files": 1,
    "presentation_embedded_files": 0,
}
ZERO_COUNTS = {key: 0 for key in EXPECTED_COUNTS}


def _rich(text: str) -> dict[str, str]:
    return {"plain_text": text, "html": text}


def _block(block_id: str, text: str) -> dict[str, object]:
    return {
        "block_id": block_id,
        "kind": "paragraph",
        "content": _rich(text),
    }


def _payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "asset_path_base": "extraction_root",
        "content_format": "safe-html",
        "record": {
            "record_id": RECORD_ID,
            "title": TITLE,
            "authors": ["Alex Example"],
            "journal": "Synthetic Journal",
            "publication_year": 2026,
            "doi": "10.0000/synthetic-json",
            "document_type": "research article",
            "bibliographic": {},
        },
        "front_matter": [],
        "sections": [
            {
                "section_id": "section-results",
                "heading": "Results",
                "level": 2,
                "blocks": [_block("body-001", "Synthetic result.")],
            }
        ],
        "figures": [
            {
                "figure_id": "figure_001",
                "label": "Figure 1",
                "kind": "figure",
                "caption": _rich("Figure 1. Synthetic figure."),
                "asset_id": "figure_001",
            },
            {
                "figure_id": "scheme_001",
                "label": "Scheme 1",
                "kind": "scheme",
                "caption": _rich("Scheme 1. Synthetic scheme."),
                "asset_id": "scheme_001",
            },
        ],
        "tables": [
            {
                "table_id": "table_001",
                "label": "Table 1",
                "title": _rich("Synthetic table"),
                "parts": [
                    {
                        "part_id": "part-001",
                        "rows": [
                            [
                                {
                                    "text": "Value",
                                    "html": "Value",
                                    "header": True,
                                    "rowspan": 1,
                                    "colspan": 1,
                                }
                            ]
                        ],
                    }
                ],
                "footnotes": [],
                "footnotes_typed": [],
                "structure_assets": {},
                "machine_records": [{"Value": "Synthetic"}],
                "source_kind": "html",
            }
        ],
        "supporting_information": [],
        "supplements": [
            {
                "supplement_id": "supplement_001",
                "file": {
                    "path": "supplementary/source.txt",
                    "media_type": "text/plain",
                },
                "blocks": [],
                "figures": [
                    {
                        "figure_id": "supplement_figure_001",
                        "label": "Figure S1",
                        "kind": "figure",
                        "caption": _rich("Figure S1. Synthetic supplement figure."),
                        "asset_id": "supplement_figure_001",
                    }
                ],
                "tables": [],
                "asset_ids": [],
            }
        ],
        "references": [_block("reference-001", "[1] Synthetic reference.")],
        "assets": [
            {
                "asset_id": "figure_001",
                "kind": "figure",
                "label": "Figure 1",
                "path": "figures/figure_001.png",
                "media_type": "image/png",
            },
            {
                "asset_id": "scheme_001",
                "kind": "scheme",
                "label": "Scheme 1",
                "path": "schemes/scheme_001.png",
                "media_type": "image/png",
            },
            {
                "asset_id": "supplement_figure_001",
                "kind": "figure",
                "label": "Figure S1",
                "path": "supplementary/figure_s1.png",
                "media_type": "image/png",
                "parent_id": "supplement_001",
            },
        ],
    }


def _expected_pointers(payload: dict[str, object]) -> dict[str, str]:
    expected = {
        "record-title": "/record/title",
        "record-authors": "/record/authors",
        "record-citation": "/record",
    }

    def add_blocks(values: object, base: str) -> None:
        if not isinstance(values, list):
            return
        for index, value in enumerate(values):
            if isinstance(value, dict) and isinstance(value.get("block_id"), str):
                expected[f"content-{value['block_id']}"] = f"{base}/{index}"

    def add_figures(values: object, base: str) -> None:
        if not isinstance(values, list):
            return
        for index, value in enumerate(values):
            if not isinstance(value, dict) or not isinstance(
                value.get("figure_id"), str
            ):
                continue
            figure_id = value["figure_id"]
            pointer = f"{base}/{index}"
            expected[f"figure-{figure_id}"] = pointer
            caption = value.get("caption")
            if isinstance(caption, dict) and (
                caption.get("plain_text") or caption.get("html")
            ):
                expected[f"caption-{figure_id}"] = pointer + "/caption"

    def add_tables(values: object, base: str) -> None:
        if not isinstance(values, list):
            return
        for index, value in enumerate(values):
            if not isinstance(value, dict) or not isinstance(
                value.get("table_id"), str
            ):
                continue
            table_id = value["table_id"]
            pointer = f"{base}/{index}"
            expected[f"table-{table_id}"] = pointer
            expected[f"table-title-{table_id}"] = pointer + "/title"
            expected[f"table-body-{table_id}"] = pointer + "/parts"
            for footnote_index, _ in enumerate(value.get("footnotes", []), start=1):
                expected[f"table-footnotes-{table_id}-{footnote_index:02d}"] = (
                    f"{pointer}/footnotes/{footnote_index - 1}"
                )

    add_blocks(payload["front_matter"], "/front_matter")
    add_blocks(payload["supporting_information"], "/supporting_information")
    add_blocks(payload["references"], "/references")
    for section_index, section in enumerate(payload["sections"]):  # type: ignore[arg-type]
        assert isinstance(section, dict)
        section_id = section["section_id"]
        expected[f"heading-{section_id}"] = f"/sections/{section_index}/heading"
        add_blocks(section["blocks"], f"/sections/{section_index}/blocks")
    add_figures(payload["figures"], "/figures")
    add_tables(payload["tables"], "/tables")
    for supplement_index, supplement in enumerate(payload["supplements"]):  # type: ignore[arg-type]
        assert isinstance(supplement, dict)
        supplement_id = supplement["supplement_id"]
        base = f"/supplements/{supplement_index}"
        expected[f"supplement-file-{supplement_id}"] = base + "/file/path"
        add_blocks(supplement["blocks"], base + "/blocks")
        add_figures(supplement["figures"], base + "/figures")
        add_tables(supplement["tables"], base + "/tables")
    for asset_index, asset in enumerate(payload["assets"]):  # type: ignore[arg-type]
        assert isinstance(asset, dict)
        expected[f"asset-{asset['asset_id']}"] = f"/assets/{asset_index}/path"
    return expected


def _coverage(payload: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "schema_version": "1.0",
            "status": "included",
            "coverage_id": coverage_id,
            "content_kind": "synthetic_content",
            "source_path": "synthetic/source.html",
            "source_locator": coverage_id,
            "output_path": "record.json",
            "output_locator": {"json_pointer": pointer},
            "output_id": coverage_id,
        }
        for coverage_id, pointer in _expected_pointers(payload).items()
    ]


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
        encoding="utf-8",
    )


def _manifest_files(extraction: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(
        (candidate for candidate in extraction.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.as_posix(),
    ):
        value = path.read_bytes()
        rows.append(
            {
                "path": path.relative_to(extraction).as_posix(),
                "bytes": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
            }
        )
    return rows


def _manifest_assets(payload: dict[str, object]) -> list[dict[str, str]]:
    return [
        {
            "asset_id": asset["asset_id"],
            "category": asset["kind"],
            "output_path": asset["path"],
        }
        for asset in payload["assets"]  # type: ignore[index]
        if isinstance(asset, dict)
        and isinstance(asset.get("asset_id"), str)
        and isinstance(asset.get("kind"), str)
        and isinstance(asset.get("path"), str)
    ]


def _refresh_manifest_files(extraction: Path, diagnostic: Path) -> None:
    path = diagnostic / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"] = _manifest_files(extraction)
    _write_json(path, manifest)


def _install_pdf_crop_fixture(
    extraction: Path,
    diagnostic: Path,
    *,
    touches_left: bool,
) -> None:
    output_path = "figures/figure_001.png"
    image = Image.new("RGB", (32, 24), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((4, 5, 20, 18), fill="black")
    if touches_left:
        image.putpixel((0, 12), (0, 0, 0))
    image.save(extraction / output_path, format="PNG")

    manifest_path = diagnostic / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    asset = next(
        row
        for row in manifest["assets"]
        if row.get("asset_id") == "figure_001"
    )
    asset.update(
        {
            "box": [10.0, 20.0, 42.0, 44.0],
            "coordinate_system": "pdf-points-top-left",
            "dimensions_pixels": {"width": 32, "height": 24},
            "media_type": "image/png",
            "page": 1,
            "source_path": f"papers (private)/{RECORD_ID}/pdf/main.pdf",
        }
    )
    _write_json(manifest_path, manifest)
    _refresh_manifest_files(extraction, diagnostic)


class RecordJsonValidationTests(unittest.TestCase):
    def _candidate(
        self,
        *,
        payload: dict[str, object] | None = None,
        declare_json: bool = True,
        missing_asset_paths: tuple[str, ...] = (),
    ) -> tuple[Path, Path, dict[str, object]]:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        extraction = root / "extraction"
        diagnostic = root / "extraction_diagnostic"
        extraction.mkdir()
        diagnostic.mkdir()
        value = deepcopy(payload if payload is not None else _payload())
        _write_json(extraction / "record.json", value)

        paths: set[str] = {
            asset["path"]
            for asset in value["assets"]  # type: ignore[index]
            if isinstance(asset, dict) and isinstance(asset.get("path"), str)
        }
        for supplement in value["supplements"]:  # type: ignore[index]
            if not isinstance(supplement, dict):
                continue
            source = supplement.get("file")
            if isinstance(source, dict) and isinstance(source.get("path"), str):
                paths.add(source["path"])
        for raw_path in sorted(paths):
            pure = PurePosixPath(raw_path)
            if (
                raw_path in missing_asset_paths
                or pure.is_absolute()
                or ".." in pure.parts
                or "\\" in raw_path
            ):
                continue
            target = extraction.joinpath(*pure.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"synthetic asset\n")

        manifest: dict[str, object] = {
            "record_id": RECORD_ID,
            "text_extraction": {"source_role": "main_html"},
            "files": _manifest_files(extraction),
            "assets": _manifest_assets(value),
        }
        if declare_json:
            manifest["record_document"] = deepcopy(RECORD_DOCUMENT)
        _write_json(diagnostic / "manifest.json", manifest)
        _write_json(
            diagnostic / "sources.json",
            {
                "record_id": RECORD_ID,
                "sources": [
                    {
                        "role": "supplementary_material",
                        "path": (
                            f"papers (private)/{RECORD_ID}/"
                            "supplementary/source.txt"
                        ),
                        "sha256": hashlib.sha256(
                            b"synthetic asset\n"
                        ).hexdigest(),
                    }
                ],
            },
        )
        _write_jsonl(diagnostic / "coverage.jsonl", _coverage(value))
        _write_json(diagnostic / "quality.json", {})
        _write_json(
            diagnostic / "confidence.json",
            {
                "categories": {
                    "main_text": {
                        "level": "high",
                        "basis": "Synthetic JSON validation fixture.",
                    }
                }
            },
        )
        return extraction, diagnostic, value

    def _report(self, extraction: Path, diagnostic: Path, *, title: str = TITLE):
        return validate_candidate(
            extraction,
            diagnostic,
            expected_title=title,
            expected_counts=EXPECTED_COUNTS,
        )

    @staticmethod
    def _codes(report: object) -> set[str]:
        return {finding.code for finding in report.findings}  # type: ignore[attr-defined]

    def test_valid_declared_json_candidate_passes_with_json_counts_and_title(self) -> None:
        extraction, diagnostic, _ = self._candidate()

        report = self._report(extraction, diagnostic)

        self.assertTrue(report.passed, report.as_dict())
        self.assertEqual(report.expected_title, TITLE)
        self.assertEqual(report.counts, EXPECTED_COUNTS)
        manifest = json.loads(
            (diagnostic / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertTrue(
            all(set(row) == {"path", "bytes", "sha256"} for row in manifest["files"])
        )
        self.assertEqual(
            {row["path"] for row in manifest["files"]},
            {
                path.relative_to(extraction).as_posix()
                for path in extraction.rglob("*")
                if path.is_file()
            },
        )
        self.assertEqual(manifest["assets"], _manifest_assets(_payload()))
        mismatch = self._report(extraction, diagnostic, title="Wrong title")
        self.assertIn("title_mismatch", self._codes(mismatch))

    def test_pdf_crop_with_safe_whitespace_has_no_boundary_warning(self) -> None:
        extraction, diagnostic, _ = self._candidate()
        _install_pdf_crop_fixture(extraction, diagnostic, touches_left=False)

        report = self._report(extraction, diagnostic)

        self.assertTrue(report.passed, report.as_dict())
        self.assertNotIn("pdf_crop_content_touches_boundary", self._codes(report))

    def test_pdf_crop_touching_boundary_gets_advisory_warning(self) -> None:
        extraction, diagnostic, _ = self._candidate()
        _install_pdf_crop_fixture(extraction, diagnostic, touches_left=True)

        report = self._report(extraction, diagnostic)

        matches = [
            finding
            for finding in report.findings
            if finding.code == "pdf_crop_content_touches_boundary"
        ]
        self.assertTrue(report.passed, report.as_dict())
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].severity, "cosmetic")
        self.assertEqual(matches[0].path, "figures/figure_001.png")
        self.assertIn("left (1 pixel)", matches[0].message)

    def test_presentation_figure_caption_blocks_count_as_supplementary_figures(self) -> None:
        payload = _payload()
        supplement = payload["supplements"][0]  # type: ignore[index]
        supplement["figures"] = []  # type: ignore[index]
        supplement["blocks"] = [  # type: ignore[index]
            {
                "block_id": "slide-caption-001",
                "kind": "figure_caption",
                "content": _rich("Supplementary Fig. SI1."),
            }
        ]

        self.assertEqual(_record_json_counts(payload)["supplementary_figures"], 1)

        # A PDF representation may contain both objects; it must not be counted twice.
        supplement["figures"] = _payload()["supplements"][0]["figures"]  # type: ignore[index]
        self.assertEqual(_record_json_counts(payload)["supplementary_figures"], 1)

    def test_counts_equation_blocks_and_linked_presentation_workbooks(self) -> None:
        payload = _payload()
        section = payload["sections"][0]  # type: ignore[index]
        section["blocks"] = [  # type: ignore[index]
            _block("paragraph-001", "Prose."),
            {
                "block_id": "equation-001",
                "kind": "equation",
                "content": _rich("x"),
            },
            {
                "block_id": "equation-002",
                "kind": "display_equation",
                "content": _rich("y"),
            },
            {"block_id": "equation-003", "kind": "math", "content": _rich("z")},
        ]
        supplement = payload["supplements"][0]  # type: ignore[index]
        supplement["tables"] = [  # type: ignore[index]
            {
                "table_id": "supplement_001_slide_001_chart_001",
                "source_kind": "presentation",
            }
        ]
        payload["assets"].extend(  # type: ignore[union-attr]
            [
                {
                    "asset_id": "chart-workbook",
                    "kind": "supplement_data",
                    "path": (
                        "supplementary/supplement_001/embedded/"
                        "Microsoft_Excel_Worksheet1.xlsx"
                    ),
                    "media_type": (
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                    "parent_id": "supplement_001_slide_001_chart_001",
                },
                {
                    "asset_id": "unlinked-workbook",
                    "kind": "supplement_data",
                    "path": "supplementary/supplement_001/embedded/orphan.xlsx",
                    "media_type": (
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    ),
                    "parent_id": "not-a-presentation-table",
                },
            ]
        )

        counts = _record_json_counts(payload)

        self.assertEqual(counts["equations"], 3)
        self.assertEqual(counts["presentation_embedded_files"], 1)

    def test_expected_presentation_workbook_count_detects_missing_asset(self) -> None:
        extraction, diagnostic, _ = self._candidate()
        expected = dict(EXPECTED_COUNTS)
        expected["presentation_embedded_files"] = 1

        report = validate_candidate(
            extraction,
            diagnostic,
            expected_title=TITLE,
            expected_counts=expected,
        )

        mismatches = [
            finding
            for finding in report.findings
            if finding.code == "content_count_mismatch"
        ]
        self.assertEqual(len(mismatches), 1)
        self.assertIn("presentation_embedded_files", mismatches[0].message)

    def test_latest_manifest_and_presentation_table_are_supported(self) -> None:
        payload = _payload()
        payload["schema_version"] = "1.1"
        payload["tables"][0]["source_kind"] = "presentation"  # type: ignore[index]
        extraction, diagnostic, _ = self._candidate(payload=payload)
        manifest_path = diagnostic / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["record_document"]["schema_version"] = "1.1"
        _write_json(manifest_path, manifest)

        report = self._report(extraction, diagnostic)

        codes = self._codes(report)
        self.assertNotIn("invalid_record_document_declaration", codes)
        self.assertNotIn("invalid_record_schema", codes)
        self.assertNotIn("record_schema_declaration_mismatch", codes)

    def test_manifest_schema_version_must_match_record(self) -> None:
        payload = _payload()
        payload["schema_version"] = "1.1"
        extraction, diagnostic, _ = self._candidate(payload=payload)

        report = self._report(extraction, diagnostic)

        self.assertIn("record_schema_declaration_mismatch", self._codes(report))

    def test_empty_and_incomplete_manifest_file_inventory_are_rejected(self) -> None:
        for case in ("empty", "incomplete"):
            with self.subTest(case=case):
                extraction, diagnostic, _ = self._candidate()
                manifest_path = diagnostic / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["files"] = (
                    [] if case == "empty" else manifest["files"][:-1]
                )
                _write_json(manifest_path, manifest)

                report = self._report(extraction, diagnostic)

                self.assertIn("manifest_file_unlisted", self._codes(report))

    def test_record_identity_must_match_diagnostics(self) -> None:
        payload = _payload()
        payload["record"]["record_id"] = "00002"  # type: ignore[index]
        extraction, diagnostic, _ = self._candidate(payload=payload)

        report = self._report(extraction, diagnostic)

        self.assertIn("record_identity_mismatch", self._codes(report))

    def test_plain_and_rich_scientific_text_must_agree(self) -> None:
        payload = _payload()
        content = payload["sections"][0]["blocks"][0]["content"]  # type: ignore[index]
        content["html"] = "Opposite scientific result."
        extraction, diagnostic, _ = self._candidate(payload=payload)

        report = self._report(extraction, diagnostic)

        self.assertIn("record_dual_text_mismatch", self._codes(report))

    def test_figure_asset_ids_cannot_be_swapped(self) -> None:
        payload = _payload()
        figures = payload["figures"]  # type: ignore[assignment]
        figures[0]["asset_id"] = "scheme_001"
        figures[1]["asset_id"] = "figure_001"
        extraction, diagnostic, _ = self._candidate(payload=payload)

        report = self._report(extraction, diagnostic)

        self.assertIn("figure_asset_identity_mismatch", self._codes(report))

    def test_canonical_asset_must_match_manifest_path_and_kind(self) -> None:
        extraction, diagnostic, _ = self._candidate()
        manifest_path = diagnostic / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["assets"][0]["category"] = "scheme"
        manifest["assets"][0]["output_path"] = "figures/wrong.png"
        _write_json(manifest_path, manifest)

        report = self._report(extraction, diagnostic)

        self.assertIn("record_asset_manifest_mismatch", self._codes(report))

    def test_malformed_and_schema_invalid_json_are_rejected(self) -> None:
        extraction, diagnostic, _ = self._candidate()
        (extraction / "record.json").write_text("{not JSON\n", encoding="utf-8")
        malformed = self._report(extraction, diagnostic)
        self.assertIn("invalid_record_json", self._codes(malformed))

        invalid_payload = _payload()
        invalid_payload["schema_version"] = "9.9"
        extraction, diagnostic, _ = self._candidate(payload=invalid_payload)
        invalid = self._report(extraction, diagnostic)
        self.assertIn("invalid_record_schema", self._codes(invalid))

    def test_unsafe_and_missing_assets_are_rejected(self) -> None:
        unsafe_payload = _payload()
        unsafe_payload["assets"][0]["path"] = "../escape.png"  # type: ignore[index]
        extraction, diagnostic, _ = self._candidate(payload=unsafe_payload)
        unsafe = self._report(extraction, diagnostic)
        self.assertIn("unsafe_record_asset_path", self._codes(unsafe))

        missing = "figures/figure_001.png"
        extraction, diagnostic, _ = self._candidate(
            missing_asset_paths=(missing,)
        )
        missing_report = self._report(extraction, diagnostic)
        self.assertIn("missing_record_asset", self._codes(missing_report))

    def test_json_and_markdown_together_are_rejected(self) -> None:
        extraction, diagnostic, _ = self._candidate()
        (extraction / "record.md").write_text(f"# {TITLE}\n", encoding="utf-8")
        _refresh_manifest_files(extraction, diagnostic)

        report = self._report(extraction, diagnostic)

        self.assertIn("multiple_record_documents", self._codes(report))

    def test_json_requires_manifest_record_document_declaration(self) -> None:
        extraction, diagnostic, _ = self._candidate(declare_json=False)

        report = self._report(extraction, diagnostic)

        self.assertIn("record_document_declaration_missing", self._codes(report))

    def test_bad_json_pointer_and_reverse_coverage_are_rejected(self) -> None:
        extraction, diagnostic, _ = self._candidate()
        coverage_path = diagnostic / "coverage.jsonl"
        rows = [json.loads(line) for line in coverage_path.read_text().splitlines()]
        title_row = next(row for row in rows if row["coverage_id"] == "record-title")
        title_row["output_locator"] = {"json_pointer": "/record/not-a-title"}
        _write_jsonl(coverage_path, rows)

        report = self._report(extraction, diagnostic)
        codes = self._codes(report)

        self.assertIn("invalid_record_json_coverage_pointer", codes)
        self.assertIn("record_json_reverse_coverage_missing", codes)

    def test_redundant_json_and_csv_table_sidecars_are_rejected(self) -> None:
        extraction, diagnostic, _ = self._candidate()
        tables = extraction / "tables"
        tables.mkdir()
        _write_json(tables / "table_001.json", {"duplicated": True})
        (tables / "table_001.csv").write_text("duplicated\n", encoding="utf-8")
        _refresh_manifest_files(extraction, diagnostic)

        report = self._report(extraction, diagnostic)

        self.assertEqual(
            [finding.code for finding in report.findings].count(
                "redundant_table_sidecar"
            ),
            2,
        )

    def test_legacy_markdown_candidate_remains_supported(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        extraction = root / "extraction"
        diagnostic = root / "extraction_diagnostic"
        extraction.mkdir()
        diagnostic.mkdir()
        (extraction / "record.md").write_text(f"# {TITLE}\n", encoding="utf-8")
        _write_json(
            diagnostic / "manifest.json",
            {
                "record_id": RECORD_ID,
                "text_extraction": {"source_role": "main_html"},
                "files": _manifest_files(extraction),
                "assets": [],
            },
        )
        _write_json(
            diagnostic / "sources.json",
            {"record_id": RECORD_ID, "sources": []},
        )
        _write_jsonl(
            diagnostic / "coverage.jsonl",
            [
                {
                    "status": "included",
                    "coverage_id": "legacy-record",
                    "content_kind": "article",
                    "source_path": "synthetic/source.html",
                    "source_locator": "article",
                    "output_path": "record.md",
                    "output_locator": {"start_line": 1, "end_line": 1},
                    "output_id": "legacy-record",
                }
            ],
        )
        _write_json(diagnostic / "quality.json", {})
        _write_json(
            diagnostic / "confidence.json",
            {
                "categories": {
                    "main_text": {
                        "level": "high",
                        "basis": "Synthetic legacy validation fixture.",
                    }
                }
            },
        )

        report = validate_candidate(
            extraction,
            diagnostic,
            expected_title=TITLE,
            expected_counts=ZERO_COUNTS,
        )

        self.assertTrue(report.passed, report.as_dict())


if __name__ == "__main__":
    unittest.main()
