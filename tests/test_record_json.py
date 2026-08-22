from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from scripts.extraction.metadata import RecordMetadata
from scripts.extraction.models import (
    ArticleExtraction,
    ContentBlock,
    FigureItem,
    Section,
    SourceFile,
    SupplementExtraction,
    TableCell,
    TableItem,
    TablePart,
)
from scripts.extraction.record_json import (
    RecordJsonError,
    build_record_json,
    validate_record_payload,
    write_record_json,
)
from scripts.extraction.record_schema import (
    RECORD_SCHEMA_ID,
    RECORD_SCHEMA_VERSION,
    RecordSchemaError,
    load_record_schema,
    validate_record_schema,
)
from scripts.extraction.rich_text import rich_text_matches_plain


def _block(
    block_id: str,
    kind: str,
    markdown: str,
    plain_text: str,
    *,
    source_path: str = "papers (private)/00001/html/main.html",
) -> ContentBlock:
    return ContentBlock(
        block_id=block_id,
        kind=kind,
        markdown=markdown,
        plain_text=plain_text,
        source_path=source_path,
        source_locator=f"//*[@id='{block_id}']",
        source_geometry=[{"page": 1, "box": [1, 2, 3, 4]}],
    )


def _fixture() -> tuple[
    RecordMetadata,
    ArticleExtraction,
    list[SupplementExtraction],
    list[dict[str, object]],
]:
    metadata = RecordMetadata(
        record_id="00001",
        title="A scientific record",
        authors=("Ada Author", "Ben Writer"),
        journal="Example Journal",
        publication_year=2026,
        doi="10.1000/example",
        document_type="research-article",
    )
    table = TableItem(
        table_id="table_001",
        source_id="tbl1",
        label="Table 1",
        title_markdown="Values of <em>K</em><sub>a</sub>",
        title_plain="Values of K_{a}",
        parts=[
            TablePart(
                part_id="tbl1-part-01",
                rows=[
                    [
                        TableCell(
                            text="Analyte",
                            markdown="<strong>Analyte</strong>",
                            header=True,
                        ),
                        TableCell(
                            text="Value",
                            markdown="Value",
                            header=True,
                        ),
                    ],
                    [
                        TableCell(
                            text="H_{2}O",
                            markdown="H<sub>2</sub>O",
                            header=False,
                        ),
                        TableCell(
                            text="5",
                            markdown="5",
                            header=False,
                        ),
                    ],
                ],
            )
        ],
        footnotes_markdown=["[a] **As printed.**"],
        footnotes_plain=["[a] As printed."],
        structure_assets={"H2O": "tables/main/table_001/cell_h2o.png"},
        source_path="papers (private)/00001/html/main.html",
        source_locator="//*[@id='tbl1']",
        source_kind="html",
    )
    article = ArticleExtraction(
        title=metadata.title,
        bibliographic={"volume": "7", "issue": "2", "pages": "10-20"},
        front_matter=[
            _block(
                "front-keywords",
                "front_matter",
                "**Keywords:** K<sub>a</sub> &lt; 5",
                "Keywords: K_{a} < 5",
            )
        ],
        sections=[
            Section(
                section_id="section-abstract",
                heading="Abstract",
                blocks=[
                    _block(
                        "abstract-001",
                        "paragraph",
                        "H<sub>2</sub>O is **important**.",
                        "H_{2}O is important.",
                    )
                ],
                source_path="papers (private)/00001/html/main.html",
                source_locator="//h2[1]",
            )
        ],
        figures=[
            FigureItem(
                figure_id="figure_001",
                source_id="fig1",
                label="Figure 1",
                kind="figure",
                caption_markdown="Fig. 1 H<sub>2</sub>O.",
                caption_plain="Fig. 1 H_{2}O.",
                source_path="papers (private)/00001/html/main.html",
                source_locator="//*[@id='fig1']",
                output_path="figures/main/figure_001.png",
            )
        ],
        tables=[table],
        references=[
            _block("reference-001", "reference", "1. A &amp; B.", "1. A & B.")
        ],
        supporting_information=[],
        repairs=[],
        warnings=[{"code": "must-stay-private", "confidence": 0.7}],
    )
    supplement_source = SourceFile(
        role="supplement",
        path=Path("unused.xlsx"),
        relative_path="papers (private)/00001/supplementary/data.xlsx",
        size=123,
        sha256="a" * 64,
        detected_format=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    supplements = [
        SupplementExtraction(
            supplement_id="supplement_001",
            source=supplement_source,
            copied_path="supplementary/supplement_001/data.xlsx",
            blocks=[
                _block(
                    "supplement-001-paragraph-001",
                    "paragraph",
                    "Supplement text.",
                    "Supplement text.",
                    source_path=supplement_source.relative_path,
                )
            ],
            figures=[],
            warnings=[],
        )
    ]
    assets: list[dict[str, object]] = [
        {
            "asset_id": "figure_001",
            "category": "figure",
            "label": "Figure 1",
            "output_path": "figures/main/figure_001.png",
            "media_type": "image/png",
            "source_path": "papers (private)/00001/pdf/main.pdf",
            "page": 1,
            "box": [10, 20, 100, 200],
            "sha256": "b" * 64,
            "confidence": 0.9,
        },
        {
            "asset_id": "table_001_cell_h2o",
            "category": "table_cell",
            "label": "Table 1 H2O structure",
            "output_path": "tables/main/table_001/cell_h2o.png",
            "media_type": "image/png",
            "parent_table_id": "table_001",
            "compound_id": "H2O",
            "source_path": "papers (private)/00001/pdf/main.pdf",
        },
        {
            "asset_id": "supplement_001_dataset",
            "category": "dataset",
            "label": "Raw measurements",
            "output_path": "supplementary/supplement_001/raw.xlsx",
            "media_type": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            "supplement_id": "supplement_001",
            "source_path": supplement_source.relative_path,
        },
    ]
    return metadata, article, supplements, assets


class RecordJsonTests(unittest.TestCase):
    def test_builds_content_only_lossless_record_with_inline_tables(self) -> None:
        metadata, article, supplements, assets = _fixture()
        result = build_record_json(metadata, article, supplements, assets)
        payload = result.payload

        self.assertEqual(payload["schema_version"], "1.1")
        self.assertEqual(payload["asset_path_base"], "extraction_root")
        self.assertEqual(payload["content_format"], "safe-html")
        self.assertEqual(payload["record"]["authors"], ["Ada Author", "Ben Writer"])
        content = payload["front_matter"][0]["content"]
        self.assertEqual(content["plain_text"], "Keywords: K_{a} < 5")
        self.assertEqual(
            content["html"],
            "<strong>Keywords:</strong> K<sub>a</sub> &lt; 5",
        )
        self.assertNotIn("**", content["html"])
        self.assertEqual(
            payload["sections"][0]["blocks"][0]["content"]["html"],
            "H<sub>2</sub>O is <strong>important</strong>.",
        )
        self.assertEqual(
            payload["sections"][0]["heading"],
            {"plain_text": "Abstract", "html": "Abstract"},
        )

        table = payload["tables"][0]
        self.assertEqual(table["parts"][0]["rows"][1][0]["text"], "H_{2}O")
        self.assertEqual(
            table["parts"][0]["rows"][1][0]["html"], "H<sub>2</sub>O"
        )
        self.assertEqual(
            table["structure_assets"]["H2O"],
            "tables/main/table_001/cell_h2o.png",
        )
        self.assertNotIn("source_path", table)

        supplement = payload["supplements"][0]
        self.assertEqual(supplement["tables"], [])
        self.assertEqual(supplement["asset_ids"], ["supplement_001_dataset"])
        self.assertEqual(payload["figures"][0]["asset_id"], "figure_001")
        self.assertEqual(
            [asset["asset_id"] for asset in payload["assets"]],
            ["figure_001", "supplement_001_dataset", "table_001_cell_h2o"],
        )

        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            "source_path",
            "source_locator",
            "source_geometry",
            "confidence",
            "sha256",
            "must-stay-private",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("source_path", json.dumps(result.coverage))
        self.assertTrue(
            all(row["output_path"] == "record.json" for row in result.coverage)
        )
        self.assertTrue(
            all(
                row["output_locator"]["json_pointer"].startswith("/")
                for row in result.coverage
            )
        )

    def test_writes_identical_canonical_bytes_for_permuted_assets(self) -> None:
        metadata, article, supplements, assets = _fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            write_record_json(metadata, article, supplements, assets, first)
            write_record_json(metadata, article, supplements, list(reversed(assets)), second)
            self.assertEqual(
                (first / "record.json").read_bytes(),
                (second / "record.json").read_bytes(),
            )
            raw = (first / "record.json").read_text(encoding="utf-8")
            decoded = json.loads(raw)
            self.assertEqual(decoded["record"]["title"], metadata.title)
            self.assertLess(raw.index('"title"'), raw.index('"assets"'))

    def test_preserves_specific_parent_over_supplement_membership(self) -> None:
        metadata, article, supplements, assets = _fixture()
        assets[2]["parent_id"] = "supplement_001_slide_001_chart_001"

        payload = build_record_json(metadata, article, supplements, assets).payload

        dataset = next(
            asset
            for asset in payload["assets"]
            if asset["asset_id"] == "supplement_001_dataset"
        )
        self.assertEqual(
            dataset["parent_id"], "supplement_001_slide_001_chart_001"
        )

    def test_ordered_list_preserves_nested_number_and_spaced_ellipsis(self) -> None:
        metadata, article, supplements, assets = _fixture()
        article.sections[0].blocks = [
            ContentBlock(
                block_id="main-list-0001",
                kind="list",
                markdown="1. 1. Introduction\n1. These ...",
                plain_text="1. 1. Introduction\n1. These ...",
                source_path="html/main.html",
                source_locator="//ol[1]",
            )
        ]

        payload = build_record_json(metadata, article, supplements, assets).payload

        content = payload["sections"][0]["blocks"][0]["content"]
        self.assertEqual(
            content["html"],
            "<ol><li>1. Introduction</li><li>These ...</li></ol>",
        )
        self.assertEqual(
            content["plain_text"], "1. 1. Introduction\n1. These ..."
        )

    def test_nonlist_leading_markers_are_semantic_content(self) -> None:
        for plain, safe_html in (
            ("1. Introduction", "Introduction"),
            ("- Important", "Important"),
            ("2) Result", "Result"),
        ):
            with self.subTest(plain=plain):
                self.assertFalse(rich_text_matches_plain(plain, safe_html))
        self.assertTrue(
            rich_text_matches_plain(
                "1. 1. Introduction\n1. These ...",
                "<ol><li>1. Introduction</li><li>These ...</li></ol>",
            )
        )

    def test_rejects_traversal_absolute_urls_and_data_uris(self) -> None:
        for bad_path in (
            "../secret.png",
            "/absolute.png",
            "figures\\bad.png",
            "https://example.test/a.png",
            "data:image/png;base64,AAAA",
        ):
            with self.subTest(path=bad_path):
                metadata, article, supplements, assets = _fixture()
                assets[0]["output_path"] = bad_path
                article.figures[0].output_path = bad_path
                with self.assertRaises(RecordJsonError):
                    build_record_json(metadata, article, supplements, assets)

    def test_preserves_literal_percent_and_hash_in_publisher_filename(self) -> None:
        metadata, article, supplements, assets = _fixture()
        literal = "figures/main/publisher%2Efigure#1.png"
        assets[0]["output_path"] = literal
        article.figures[0].output_path = literal
        payload = build_record_json(metadata, article, supplements, assets).payload
        self.assertEqual(payload["assets"][0]["path"], literal)

    def test_rejects_unsafe_html_in_extraction_markup(self) -> None:
        metadata, article, supplements, assets = _fixture()
        article.sections[0].blocks[0].markdown = '<script src="x">bad</script>'
        with self.assertRaisesRegex(RecordJsonError, "forbidden HTML tag"):
            build_record_json(metadata, article, supplements, assets)

    def test_restores_escaped_literal_markdown_characters(self) -> None:
        metadata, article, supplements, assets = _fixture()
        block = article.sections[0].blocks[0]
        block.plain_text = "HF/6-31G** energy minimization"
        block.markdown = r"HF/6-31G\*\* energy minimization"

        payload = build_record_json(metadata, article, supplements, assets).payload

        content = payload["sections"][0]["blocks"][0]["content"]
        self.assertEqual(content["html"], "HF/6-31G** energy minimization")

    def test_section_heading_uses_safe_rich_text(self) -> None:
        metadata, article, supplements, assets = _fixture()
        article.sections[0].heading = "MPE·Fe<sup>II</sup> Footprinting"

        payload = build_record_json(metadata, article, supplements, assets).payload

        self.assertEqual(
            payload["sections"][0]["heading"],
            {
                "plain_text": "MPE·Fe^{II} Footprinting",
                "html": "MPE·Fe<sup>II</sup> Footprinting",
            },
        )

    def test_canonicalizes_publisher_reference_brackets(self) -> None:
        metadata, article, supplements, assets = _fixture()
        block = article.sections[0].blocks[0]
        block.plain_text = "Prior work.1, 2"
        block.markdown = "Prior work.[1], [2]"

        payload = build_record_json(metadata, article, supplements, assets).payload

        content = payload["sections"][0]["blocks"][0]["content"]
        self.assertEqual(content["plain_text"], "Prior work.[1], [2]")
        self.assertEqual(content["html"], "Prior work.[1], [2]")

    def test_rejects_contradictory_plain_and_rich_scientific_text(self) -> None:
        metadata, article, supplements, assets = _fixture()
        article.sections[0].blocks[0].plain_text = "A contradictory result."

        with self.assertRaisesRegex(RecordJsonError, "plain and rich text disagree"):
            build_record_json(metadata, article, supplements, assets)

    def test_allows_rich_roman_oxidation_state_with_compact_plain_text(self) -> None:
        metadata, article, supplements, assets = _fixture()
        block = article.sections[0].blocks[0]
        block.plain_text = "MPE·FeII footprinting."
        block.markdown = "MPE·Fe<sup>II</sup> footprinting."

        payload = build_record_json(metadata, article, supplements, assets).payload

        content = payload["sections"][0]["blocks"][0]["content"]
        self.assertEqual(content["plain_text"], "MPE·Fe^{II} footprinting.")
        self.assertEqual(content["html"], "MPE·Fe<sup>II</sup> footprinting.")

    def test_enriches_flat_plain_script_from_rich_source(self) -> None:
        metadata, article, supplements, assets = _fixture()
        block = article.sections[0].blocks[0]
        block.plain_text = "H2O"
        block.markdown = "H<sup>2</sup>O"

        payload = build_record_json(metadata, article, supplements, assets).payload

        content = payload["sections"][0]["blocks"][0]["content"]
        self.assertEqual(content["plain_text"], "H^{2}O")

    def test_does_not_overwrite_explicit_plain_script_semantics(self) -> None:
        metadata, article, supplements, assets = _fixture()
        block = article.sections[0].blocks[0]
        block.plain_text = "H_{2}O"
        block.markdown = "H<sup>2</sup>O"

        with self.assertRaisesRegex(RecordJsonError, "plain and rich text disagree"):
            build_record_json(metadata, article, supplements, assets)

    def test_semantic_validation_rejects_duplicate_ids_and_unknown_assets(self) -> None:
        metadata, article, supplements, assets = _fixture()
        payload = build_record_json(metadata, article, supplements, assets).payload
        duplicate = deepcopy(payload)
        duplicate["references"][0]["block_id"] = "abstract-001"
        with self.assertRaisesRegex(RecordJsonError, "duplicate block_id"):
            validate_record_payload(duplicate)
        unknown = deepcopy(payload)
        unknown["figures"][0]["asset_id"] = "missing"
        with self.assertRaisesRegex(RecordJsonError, "unknown asset"):
            validate_record_payload(unknown)
        missing_table_asset = deepcopy(payload)
        missing_table_asset["tables"][0]["structure_assets"]["H2O"] = (
            "tables/main/missing.png"
        )
        with self.assertRaisesRegex(RecordJsonError, "absent from the asset registry"):
            validate_record_payload(missing_table_asset)

        # Domain records may faithfully use names that would be diagnostic at
        # the container level; this unconstrained author-data subtree is exempt.
        domain_fields = deepcopy(payload)
        domain_fields["tables"][0]["machine_records"] = [
            {"confidence": "95%", "bytes": 8, "sha256": "author-supplied value"}
        ]
        validate_record_payload(domain_fields)

    def test_schema_errors_are_deterministic_and_schema_is_isolated(self) -> None:
        metadata, article, supplements, assets = _fixture()
        payload = build_record_json(metadata, article, supplements, assets).payload
        invalid = deepcopy(payload)
        invalid["schema_version"] = "2.0"
        with self.assertRaises(RecordSchemaError) as first:
            validate_record_payload(invalid, source_path="candidate/record.json")
        with self.assertRaises(RecordSchemaError) as second:
            validate_record_payload(invalid, source_path="candidate/record.json")
        self.assertEqual(first.exception.violations, second.exception.violations)
        self.assertIn("candidate/record.json", str(first.exception))

        schema = load_record_schema()
        schema["$id"] = "changed"
        self.assertNotEqual(load_record_schema()["$id"], "changed")

    def test_versioned_schemas_accept_legacy_and_latest_records(self) -> None:
        metadata, article, supplements, assets = _fixture()
        latest = build_record_json(metadata, article, supplements, assets).payload
        self.assertEqual(latest["schema_version"], RECORD_SCHEMA_VERSION)
        validate_record_schema(latest)

        legacy = deepcopy(latest)
        legacy["schema_version"] = "1.0"
        validate_record_schema(legacy)

        self.assertEqual(load_record_schema()["$id"], RECORD_SCHEMA_ID)
        self.assertEqual(
            load_record_schema("1.0")["$id"],
            "https://pip-litdb.org/schema/record-1.0.schema.json",
        )

    def test_presentation_tables_are_structured_without_source_images(self) -> None:
        metadata, article, supplements, assets = _fixture()
        article.tables[0] = replace(article.tables[0], source_kind="presentation")
        self.assertFalse(article.tables[0].requires_source_image)
        payload = build_record_json(metadata, article, supplements, assets).payload
        self.assertNotIn("source_image", payload["tables"][0])
        validate_record_schema(payload)

        legacy = deepcopy(payload)
        legacy["schema_version"] = "1.0"
        with self.assertRaises(RecordSchemaError):
            validate_record_schema(legacy)

        with_source_image = deepcopy(payload)
        with_source_image["tables"][0]["source_image"] = "tables/table_001.png"
        with self.assertRaises(RecordSchemaError):
            validate_record_schema(with_source_image)


if __name__ == "__main__":
    unittest.main()
