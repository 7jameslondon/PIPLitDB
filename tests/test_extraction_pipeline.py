from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

try:
    from lxml import html as lxml_html
except ModuleNotFoundError:
    lxml_html = None

from scripts.extraction.paths import (
    UnsafePathError,
    canonical_json,
    ensure_within,
    sha256_file,
    validate_record_id,
)
from scripts.extraction.models import TableCell, TableItem, TablePart
from scripts.extraction.metadata import load_record_metadata
from scripts.extraction.pipeline import _attach_assets, _record_missing_asset_warnings
from scripts.extraction.renderer import write_table_derivatives
from scripts.extraction.table_schema import TableSchemaError, validate_table_payload
from scripts.extraction.validation import (
    _required_asset_findings,
    _table_derivative_findings,
    validate_candidate,
)

if lxml_html is not None:
    from scripts.extraction.html_extractor import extract_html
else:  # pragma: no cover - documents the optional dependency boundary
    extract_html = None


class ExtractionMetadataTests(unittest.TestCase):
    def test_canonical_author_name_is_preferred_for_rendering(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "00001.yaml"
            path.write_text(
                "title: Synthetic article\n"
                "authors:\n"
                '  - name: "Thomas\u2005G. Example"\n'
                '    canonical_name: "Thomas G. Example"\n'
                "publication_year: 2000\n"
                "journal: Synthetic Journal\n"
                "doi: 10.0000/example\n"
                "document_type: research_article\n",
                encoding="utf-8",
            )

            metadata = load_record_metadata(path, "00001")

        self.assertEqual(metadata.authors, ("Thomas G. Example",))

@unittest.skipUnless(lxml_html is not None, "lxml is required for extraction tests")
class HtmlExtractionTests(unittest.TestCase):
    def extract(self, source: str):
        with TemporaryDirectory() as directory:
            source_path = Path(directory) / "article.html"
            source_path.write_text(source, encoding="utf-8")
            return extract_html(source_path, "html/article.html")

    def test_utf8_inline_notation_citations_and_terminal_linkouts(self) -> None:
        result = self.extract(
            """<!doctype html>
<html><body><article>
  <div><a>Volume 12, Issue 3</a><span> pp. 101-109</span></div>
  <div><span>First published: </span><span>07 June 2020</span></div>
  <h1>β-Lactam formation in H₂O</h1>
  <section>
    <h2>Results</h2>
    <p>Rate <i>k</i><sub>obs</sub> was 10<sup>−3</sup> s<sup>−1</sup>;
       see <a href="#bib1">1</a>.</p>
    <p>A complete sentence.<a href="#fig1">1</a></p>
    <p>Previously described.<span><a href="#bib1">1</a></span><a href="#fig1">1</a>, <a href="#fig2">2</a></p>
  </section>
  <figure id="fig1"><figcaption><p><strong>Figure 1.</strong> Synthetic caption.</p></figcaption></figure>
  <section id="article-references">
    <h2>References</h2>
    <ol><li id="bib1"><span>1.</span> Example, α study.
      <div><span>10.1234/example</span></div></li></ol>
  </section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "β-Lactam formation in H₂O")
        self.assertEqual(len(result.sections), 1)
        paragraphs = result.sections[0].blocks
        self.assertIn("<em>k</em><sub>obs</sub>", paragraphs[0].markdown)
        self.assertIn("10<sup>−3</sup> s<sup>−1</sup>", paragraphs[0].markdown)
        self.assertIn("see [1].", paragraphs[0].markdown)
        self.assertEqual(paragraphs[1].markdown, "A complete sentence.")
        self.assertEqual(paragraphs[2].markdown, "Previously described.[1]")
        self.assertEqual(len(result.references), 1)
        self.assertIn("α study", result.references[0].plain_text)
        self.assertIn(
            "https://doi.org/10.1234/example",
            result.references[0].markdown,
        )
        self.assertEqual(
            result.bibliographic,
            {
                "volume": "12",
                "issue": "3",
                "pages": "101-109",
                "first_published": "07 June 2020",
            },
        )

    def test_unequal_diagram_rows_preserve_intentional_blank_cell(self) -> None:
        result = self.extract(
            """<!doctype html>
<html><body><article>
  <h1>Synthetic table study</h1>
  <section><h2>Data</h2><p>Results are tabulated.</p></section>
  <div id="tbl7">
    <header><strong>Table 7.</strong> Synthetic measurements</header>
    <table>
      <thead><tr><th>Entry</th><th>Species</th><th>Selectivity</th></tr></thead>
      <tbody>
        <tr><td><a href="#for001">-1</a></td><td>X<sub>2</sub></td><td></td></tr>
        <tr><td>7</td><td>product β</td></tr>
      </tbody>
    </table>
  </div>
</article></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].source_kind, "html")
        rows = result.tables[0].parts[0].rows
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [cell.text for cell in rows[1]], ["7", "X_{2} product β", ""]
        )
        self.assertEqual(rows[1][1].markdown, "X<sub>2</sub><br>product β")
        self.assertEqual(rows[1][2].markdown, "")
        self.assertEqual(rows[1][2].as_dict()["html"], "")

    def test_standalone_bold_paragraph_becomes_subsection_heading(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Synthetic hierarchy</h1>
<section><h2>Methods</h2><p><b>Preparation details</b></p>
<p><b>Compound A</b>: Synthetic procedure.</p></section>
</article></body></html>"""
        )

        blocks = result.sections[0].blocks
        self.assertEqual(blocks[0].kind, "subsection_heading")
        self.assertEqual(blocks[0].markdown, "### Preparation details")
        self.assertEqual(blocks[1].kind, "paragraph")


class PathSafetyAndDeterminismTests(unittest.TestCase):
    def test_record_ids_and_parent_traversal_are_rejected(self) -> None:
        self.assertEqual(validate_record_id("01234"), "01234")
        for unsafe_id in ("1234", "123456", "../12", "12/34", "abcde"):
            with self.subTest(record_id=unsafe_id):
                with self.assertRaises(ValueError):
                    validate_record_id(unsafe_id)

        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            root = temporary / "approved"
            root.mkdir()
            inside = root / "inside.txt"
            outside = temporary / "outside.txt"
            inside.write_text("inside", encoding="utf-8")
            outside.write_text("outside", encoding="utf-8")

            self.assertEqual(ensure_within(inside, root), inside.resolve())
            with self.assertRaises(UnsafePathError):
                ensure_within(root / ".." / "outside.txt", root)

    def test_canonical_json_is_stable_unicode_and_key_sorted(self) -> None:
        first = {"z": [3, 2, 1], "a": "β"}
        second = {"a": "β", "z": [3, 2, 1]}

        rendered = canonical_json(first)

        self.assertEqual(rendered, canonical_json(second))
        self.assertEqual(rendered, '{\n  "a": "β",\n  "z": [\n    3,\n    2,\n    1\n  ]\n}\n')

    def test_table_derivatives_include_typed_scientific_records_without_csv(self) -> None:
        table = TableItem(
            table_id="table_001",
            source_id="tbl1",
            label="Table 1",
            title_markdown="K<sub>a</sub>",
            title_plain="K_{a} [M^{−1}] measurements",
            parts=[
                TablePart(
                    part_id="part-01",
                    rows=[
                        [
                            TableCell("Polyamide on pTEST", "Polyamide on pTEST", True),
                            TableCell("5′-aTGGACAt-3′", "5′-aTGGACAt-3′", True),
                        ],
                        [
                            TableCell("2 b", "2 b", False),
                            TableCell(
                                "≤1×10^{8} [≥190]^{[e]}",
                                "<strong>≤1×10<sup>8</sup></strong>",
                                False,
                            ),
                        ],
                    ],
                )
            ],
            footnotes_markdown=["[a] Synthetic scope. [e] Alternate match."],
            footnotes_plain=["[a] Synthetic scope. [e] Alternate match."],
            source_path="html/main.html",
            source_locator="//table[1]",
            source_kind="html",
            structure_assets={"2b": "tables/main/table_001_cells/structure_2b.png"},
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            structure_image = (
                root
                / "tables"
                / "main"
                / "table_001_cells"
                / "structure_2b.png"
            )
            structure_image.parent.mkdir(parents=True)
            structure_image.write_bytes(b"synthetic table-cell image")
            write_table_derivatives([table], root)
            payload = json.loads((root / table.json_path).read_text(encoding="utf-8"))
            record = payload["machine_records"][0]

            self.assertEqual(record["compound_id"], "2b")
            self.assertEqual(
                record["structure_asset"],
                "tables/main/table_001_cells/structure_2b.png",
            )
            self.assertEqual(record["association_constant"]["relation"], "<=")
            self.assertEqual(record["association_constant"]["exponent"], 8)
            self.assertEqual(record["specificity"], {"relation": ">=", "value": 190.0})
            self.assertTrue(record["match_site"])
            self.assertEqual(record["footnotes"], ["e"])
            self.assertEqual(
                payload["footnotes_typed"],
                [
                    {"label": "a", "scope": "table", "text": "Synthetic scope."},
                    {"label": "e", "scope": "table", "text": "Alternate match."},
                ],
            )
            self.assertEqual(payload["source_kind"], "html")
            self.assertNotIn("source", payload)
            self.assertEqual(list(root.rglob("*.csv")), [])
            self.assertEqual(list(root.rglob("table_001.png")), [])
            self.assertTrue(structure_image.is_file())

    def test_table_schema_marker_is_the_only_change_to_faithful_json_content(self) -> None:
        table = TableItem(
            table_id="table_009",
            source_id="tbl9",
            label="Table 9",
            title_markdown="β scope and selectivity",
            title_plain="β scope and selectivity",
            parts=[
                TablePart(
                    part_id="tbl9-part-01",
                    rows=[
                        [
                            TableCell(
                                "Condition",
                                "<strong>Condition</strong>",
                                True,
                                rowspan=2,
                            ),
                            TableCell("Results", "Results", True, colspan=2),
                        ],
                        [
                            TableCell("Yield (%)", "Yield (%)", True),
                            TableCell("ee (%)", "<em>ee</em> (%)", True),
                        ],
                        [
                            TableCell("A", "A", False),
                            TableCell(
                                "≤1×10^{−3}",
                                "≤1×10<sup>−3</sup>",
                                False,
                            ),
                        ],
                        [TableCell("Not reported", "<span>Not reported</span>", False)],
                    ],
                ),
                TablePart(
                    part_id="tbl9-part-02",
                    rows=[
                        [TableCell("Continued", "<u>Continued</u>", True)],
                        [TableCell("β-form", "β-form", False)],
                    ],
                ),
            ],
            footnotes_markdown=[
                "[a] Values are means; *n* = 3.",
                "[e] As reported by the authors—unchanged.",
            ],
            footnotes_plain=[
                "[a] Values are means; n = 3.",
                "[e] As reported by the authors—unchanged.",
            ],
            source_path="html/main.html",
            source_locator="//table[9]",
            source_kind="html",
            structure_assets={
                "β-form": "tables/main/table_009_cells/structure_beta.png"
            },
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_table_derivatives([table], root)
            payload = json.loads((root / str(table.json_path)).read_text("utf-8"))

        # This is the complete pre-schema table payload. Removing the new
        # path-base declaration must leave it byte-for-data identical: the
        # schema describes this representation and does not normalize it.
        expected_pre_schema_payload = {
            "schema_version": "1.0",
            "table_id": "table_009",
            "label": "Table 9",
            "title": "β scope and selectivity",
            "parts": [
                {
                    "part_id": "tbl9-part-01",
                    "rows": [
                        [
                            {
                                "text": "Condition",
                                "html": "<strong>Condition</strong>",
                                "header": True,
                                "rowspan": 2,
                                "colspan": 1,
                            },
                            {
                                "text": "Results",
                                "html": "Results",
                                "header": True,
                                "rowspan": 1,
                                "colspan": 2,
                            },
                        ],
                        [
                            {
                                "text": "Yield (%)",
                                "html": "Yield (%)",
                                "header": True,
                                "rowspan": 1,
                                "colspan": 1,
                            },
                            {
                                "text": "ee (%)",
                                "html": "<em>ee</em> (%)",
                                "header": True,
                                "rowspan": 1,
                                "colspan": 1,
                            },
                        ],
                        [
                            {
                                "text": "A",
                                "html": "A",
                                "header": False,
                                "rowspan": 1,
                                "colspan": 1,
                            },
                            {
                                "text": "≤1×10^{−3}",
                                "html": "≤1×10<sup>−3</sup>",
                                "header": False,
                                "rowspan": 1,
                                "colspan": 1,
                            },
                        ],
                        [
                            {
                                "text": "Not reported",
                                "html": "<span>Not reported</span>",
                                "header": False,
                                "rowspan": 1,
                                "colspan": 1,
                            }
                        ],
                    ],
                },
                {
                    "part_id": "tbl9-part-02",
                    "rows": [
                        [
                            {
                                "text": "Continued",
                                "html": "<u>Continued</u>",
                                "header": True,
                                "rowspan": 1,
                                "colspan": 1,
                            }
                        ],
                        [
                            {
                                "text": "β-form",
                                "html": "β-form",
                                "header": False,
                                "rowspan": 1,
                                "colspan": 1,
                            }
                        ],
                    ],
                },
            ],
            "footnotes": [
                "[a] Values are means; n = 3.",
                "[e] As reported by the authors—unchanged.",
            ],
            "footnotes_typed": [
                {
                    "label": "a",
                    "scope": "table",
                    "text": "Values are means; n = 3.",
                },
                {
                    "label": "e",
                    "scope": "table",
                    "text": "As reported by the authors—unchanged.",
                },
            ],
            "structure_assets": {
                "β-form": "tables/main/table_009_cells/structure_beta.png"
            },
            "machine_records": [],
            "source_kind": "html",
        }
        self.assertEqual(payload["asset_path_base"], "extraction_root")
        self.assertEqual(
            {key: value for key, value in payload.items() if key != "asset_path_base"},
            expected_pre_schema_payload,
        )
        self.assertEqual(
            [len(row) for row in payload["parts"][0]["rows"]], [2, 2, 2, 1]
        )
        self.assertEqual(len(payload["parts"]), 2)
        validate_table_payload(payload)

    def test_every_table_emits_json_even_when_rows_are_not_rectangular(self) -> None:
        tables = [
            TableItem(
                table_id="table_001",
                source_id="tbl1",
                label="Table 1",
                title_markdown="Complete table",
                title_plain="Complete table",
                parts=[
                    TablePart(
                        part_id="tbl1-part-01",
                        rows=[
                            [
                                TableCell("Name", "Name", True),
                                TableCell("Value", "Value", True),
                            ],
                            [
                                TableCell("alpha", "alpha", False),
                                TableCell("1", "1", False),
                            ],
                        ],
                    )
                ],
                footnotes_markdown=[],
                footnotes_plain=[],
                source_path="html/main.html",
                source_locator="//table[1]",
                source_kind="html",
            ),
            TableItem(
                table_id="table_002",
                source_id="tbl2",
                label="Table 2",
                title_markdown="Ragged table",
                title_plain="Ragged table",
                parts=[
                    TablePart(
                        part_id="tbl2-part-01",
                        rows=[
                            [
                                TableCell("Name", "Name", True),
                                TableCell("Value", "Value", True),
                            ],
                            [TableCell("beta", "beta", False)],
                        ],
                    )
                ],
                footnotes_markdown=[],
                footnotes_plain=[],
                source_path="html/main.html",
                source_locator="//table[2]",
                source_kind="html",
            ),
        ]

        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_table_derivatives(tables, root)

            for table in tables:
                with self.subTest(table_id=table.table_id):
                    json_path = root / "tables" / "main" / f"{table.table_id}.json"
                    self.assertTrue(json_path.is_file())
                    payload = json.loads(json_path.read_text(encoding="utf-8"))
                    self.assertEqual(payload["table_id"], table.table_id)
                    self.assertEqual(payload["source_kind"], "html")
                    self.assertNotIn("source", payload)
            self.assertEqual(list(root.rglob("*.csv")), [])
            self.assertFalse(
                any(
                    path.is_dir() and path.name.endswith("_cells")
                    for path in root.rglob("*")
                )
            )

    def test_table_image_requirements_apply_only_to_pdf_and_image_sources(self) -> None:
        def make_table(number: int, source_kind: str) -> TableItem:
            return TableItem(
                table_id=f"table_{number:03d}",
                source_id=f"tbl{number}",
                label=f"Table {number}",
                title_markdown=f"Table {number}",
                title_plain=f"Table {number}",
                parts=[],
                footnotes_markdown=[],
                footnotes_plain=[],
                source_path=f"{source_kind}/table-{number}",
                source_locator=f"table {number}",
                source_kind=source_kind,
            )

        html_table = make_table(1, "html")
        pdf_table = make_table(2, "pdf")
        image_table = make_table(3, "image")
        article = SimpleNamespace(
            figures=[],
            tables=[html_table, pdf_table, image_table],
            warnings=[],
        )

        _attach_assets(article, [], [])
        _record_missing_asset_warnings(article, [])

        missing_table_warnings = [
            warning
            for warning in article.warnings
            if warning["code"] == "main_table_source_image_missing"
        ]
        self.assertEqual(
            [warning["source_path"] for warning in missing_table_warnings],
            [pdf_table.source_path, image_table.source_path],
        )
        self.assertNotIn(
            html_table.source_path,
            {warning["source_path"] for warning in missing_table_warnings},
        )

        article.warnings.clear()
        _attach_assets(
            article,
            [],
            [
                {
                    "asset_id": pdf_table.table_id,
                    "output_path": "tables/main/table_002.png",
                },
                {
                    "asset_id": image_table.table_id,
                    "output_path": "tables/main/table_003.png",
                },
            ],
        )
        _record_missing_asset_warnings(article, [])

        self.assertIsNone(html_table.image_path)
        self.assertEqual(pdf_table.image_path, "tables/main/table_002.png")
        self.assertEqual(image_table.image_path, "tables/main/table_003.png")
        self.assertEqual(article.warnings, [])

        with TemporaryDirectory() as directory:
            root = Path(directory)
            for table in (pdf_table, image_table):
                image_path = root / str(table.image_path)
                image_path.parent.mkdir(parents=True, exist_ok=True)
                image_path.write_bytes(b"synthetic full-table image")

            write_table_derivatives(article.tables, root)

            self.assertFalse((root / "tables/main/table_001.png").exists())
            for table in article.tables:
                payload = json.loads(
                    (root / f"tables/main/{table.table_id}.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(payload["source_kind"], table.source_kind)
                self.assertNotIn("source", payload)
                if table.requires_source_image:
                    self.assertTrue((root / str(table.image_path)).is_file())
            self.assertEqual(list(root.rglob("*.csv")), [])


class TableSchemaContractTests(unittest.TestCase):
    @staticmethod
    def valid_payload(source_kind: str = "html") -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "1.0",
            "asset_path_base": "extraction_root",
            "table_id": "table_001",
            "label": "Table 1",
            "title": "Faithful synthetic table",
            "parts": [
                {
                    "part_id": "part-01",
                    "rows": [
                        [
                            {
                                "text": "α",
                                "html": "<em>α</em><sup>2</sup>",
                                "header": True,
                                "rowspan": 2,
                                "colspan": 3,
                            }
                        ],
                        [],
                    ],
                },
                {"part_id": "part-02", "rows": []},
            ],
            "footnotes": ["[e] Authors' wording; retained verbatim."],
            "footnotes_typed": [
                {
                    "label": "e",
                    "scope": "table",
                    "text": "Authors' wording; retained verbatim.",
                }
            ],
            "structure_assets": {},
            "machine_records": [],
            "source_kind": source_kind,
        }
        if source_kind in {"pdf", "image"}:
            payload["source_image"] = "tables/main/table_001.png"
        return payload

    def test_schema_accepts_faithful_multipart_spans_html_and_ragged_rows(self) -> None:
        payload = self.valid_payload()
        before = deepcopy(payload)

        validate_table_payload(payload)

        self.assertEqual(payload, before)
        self.assertEqual(payload["parts"][0]["rows"][1], [])  # type: ignore[index]
        self.assertEqual(
            payload["parts"][0]["rows"][0][0]["html"],  # type: ignore[index]
            "<em>α</em><sup>2</sup>",
        )
        self.assertEqual(
            payload["footnotes"], ["[e] Authors' wording; retained verbatim."]
        )

    def test_schema_rejects_invalid_container_without_rewriting_it(self) -> None:
        cases: dict[str, object] = {}

        missing_title = self.valid_payload()
        del missing_title["title"]
        cases["missing required field"] = missing_title

        wrong_version = self.valid_payload()
        wrong_version["schema_version"] = "2.0"
        cases["unsupported schema version"] = wrong_version

        wrong_path_base = self.valid_payload()
        wrong_path_base["asset_path_base"] = "table_directory"
        cases["ambiguous asset path base"] = wrong_path_base

        invalid_span = self.valid_payload()
        invalid_span["parts"][0]["rows"][0][0]["rowspan"] = 0  # type: ignore[index]
        cases["invalid cell span"] = invalid_span

        unexpected_field = self.valid_payload()
        unexpected_field["publisher_layout"] = "normalized"
        cases["undefined container field"] = unexpected_field

        cases["non-object root"] = []

        for description, payload in cases.items():
            with self.subTest(description=description):
                before = deepcopy(payload)
                with self.assertRaises(TableSchemaError) as raised:
                    validate_table_payload(payload)
                self.assertTrue(raised.exception.violations)
                self.assertEqual(payload, before)

    def test_source_image_is_required_only_for_pdf_or_image_sources(self) -> None:
        validate_table_payload(self.valid_payload("html"))

        html_with_image = self.valid_payload("html")
        html_with_image["source_image"] = "tables/main/table_001.png"
        with self.assertRaises(TableSchemaError):
            validate_table_payload(html_with_image)

        for source_kind in ("pdf", "image"):
            with self.subTest(source_kind=source_kind):
                payload = self.valid_payload(source_kind)
                validate_table_payload(payload)
                del payload["source_image"]
                with self.assertRaises(TableSchemaError):
                    validate_table_payload(payload)


class IndependentValidatorTests(unittest.TestCase):
    def write_json(self, path: Path, value: object) -> None:
        path.write_text(canonical_json(value), encoding="utf-8")

    def test_html_table_requires_structured_json_link_but_not_source_image(self) -> None:
        record_text = """# Synthetic article

## Tables

### Table 1

Assets: [structured data](tables/main/table_001.json)

<table><tr><th>Value</th></tr><tr><td>1</td></tr></table>
"""

        self.assertEqual(_required_asset_findings(record_text), [])

        findings_without_json = _required_asset_findings(
            record_text.replace(
                "Assets: [structured data](tables/main/table_001.json)\n\n", ""
            )
        )
        self.assertEqual(
            [finding.code for finding in findings_without_json],
            ["missing_consolidated_asset"],
        )
        self.assertIn("structured JSON", findings_without_json[0].message)

    def test_table_csv_is_an_unexpected_derivative(self) -> None:
        with TemporaryDirectory() as directory:
            extraction = Path(directory)
            table_root = extraction / "tables" / "main"
            table_root.mkdir(parents=True)
            self.write_json(
                table_root / "table_001.json",
                TableSchemaContractTests.valid_payload(),
            )
            csv_path = table_root / "table_001.csv"
            csv_path.write_text("name,value\nalpha,1\n", encoding="utf-8")

            findings = _table_derivative_findings(
                extraction,
                {Path("tables/main/table_001.json")},
            )

            csv_findings = [
                finding for finding in findings if finding.code == "unexpected_table_csv"
            ]
            self.assertEqual(len(csv_findings), 1)
            self.assertEqual(csv_findings[0].path, "tables/main/table_001.csv")

    def test_malformed_and_schema_invalid_table_json_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            extraction = Path(directory)
            table_root = extraction / "tables" / "main"
            table_root.mkdir(parents=True)

            malformed_path = table_root / "table_001.json"
            malformed_path.write_text('{"schema_version": "1.0",', encoding="utf-8")

            schema_invalid_path = table_root / "table_002.json"
            schema_invalid = TableSchemaContractTests.valid_payload()
            schema_invalid["table_id"] = "table_002"
            del schema_invalid["asset_path_base"]
            self.write_json(schema_invalid_path, schema_invalid)

            findings = _table_derivative_findings(
                extraction,
                {
                    Path("tables/main/table_001.json"),
                    Path("tables/main/table_002.json"),
                },
            )

            rejected = [
                (finding.code, finding.path, finding.severity)
                for finding in findings
                if finding.code in {"invalid_table_json", "invalid_table_schema"}
            ]
            self.assertEqual(
                rejected,
                [
                    (
                        "invalid_table_json",
                        "tables/main/table_001.json",
                        "critical",
                    ),
                    (
                        "invalid_table_schema",
                        "tables/main/table_002.json",
                        "structural",
                    ),
                ],
            )

    def test_reports_unsafe_links_placeholders_orphans_hashes_and_counts(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            extraction = root / "extraction"
            diagnostic = root / "extraction_diagnostic"
            assets = extraction / "assets"
            assets.mkdir(parents=True)
            diagnostic.mkdir()

            (extraction / "record.md").write_text(
                """# Synthetic validation article

[Unsafe](../outside.txt)
[Unsafe scheme](javascript:alert)
[Missing](assets/missing.bin)
[Linked](assets/linked.bin)

Unresolved publisher token: equation/tex2gif-sup-42.gif

## Figures

Figure 1. A wholly synthetic result.
""",
                encoding="utf-8",
            )
            linked = assets / "linked.bin"
            linked.write_bytes(b"linked synthetic asset")
            (extraction / "orphan.bin").write_bytes(b"unlinked synthetic asset")

            self.write_json(
                diagnostic / "manifest.json",
                {
                    "schema_version": "1.0",
                    "files": [
                        {
                            "output_path": "assets/linked.bin",
                            "sha256": "0" * 64,
                        },
                        {
                            "output_path": "../escaped.bin",
                            "sha256": "1" * 64,
                        },
                    ],
                },
            )
            self.write_json(diagnostic / "sources.json", {"sources": []})
            (diagnostic / "coverage.jsonl").write_text("", encoding="utf-8")
            self.write_json(diagnostic / "quality.json", {"schema_version": "1.0"})
            self.write_json(
                diagnostic / "confidence.json",
                {
                    "schema_version": "1.0",
                    "categories": {
                        "synthetic": {"level": "high", "basis": "synthetic fixture"}
                    },
                },
            )
            (diagnostic / "warnings.jsonl").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "code": "main_table_source_image_missing",
                        "severity": "scientific",
                        "message": "A PDF-sourced table has no full-table image.",
                        "source_path": "pdf/main.pdf",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            expected_counts = {"main_figures": 2}
            first = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic validation article",
                expected_counts=expected_counts,
            )
            second = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic validation article",
                expected_counts=expected_counts,
            )

            codes = {finding.code for finding in first.findings}
            self.assertTrue(
                {
                    "unsafe_local_link",
                    "unsafe_external_scheme",
                    "missing_local_link",
                    "publisher_equation_placeholder",
                    "orphan_output_file",
                    "output_hash_mismatch",
                    "invalid_manifest_path",
                    "content_count_mismatch",
                    "diagnostic_warning_main_table_source_image_missing",
                }.issubset(codes)
            )
            self.assertEqual(first.status, "fail")
            self.assertEqual(first.counts["main_figures"], 0)
            self.assertEqual(first.as_dict(), second.as_dict())

    def test_valid_candidate_with_matching_hash_and_count_passes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            extraction = root / "extraction"
            diagnostic = root / "extraction_diagnostic"
            assets = extraction / "figures"
            assets.mkdir(parents=True)
            diagnostic.mkdir()

            (extraction / "record.md").write_text(
                """# Synthetic passing article

## Figure and Scheme Captions

### Figure 1

Asset: [Figure 1](figures/figure_001.png)

Synthetic caption.
""",
                encoding="utf-8",
            )
            image = assets / "figure_001.png"
            image.write_bytes(b"synthetic image bytes")

            self.write_json(
                diagnostic / "manifest.json",
                {
                    "files": [
                        {
                            "path": "figures/figure_001.png",
                            "bytes": image.stat().st_size,
                            "sha256": sha256_file(image),
                        },
                        {
                            "path": "record.md",
                            "bytes": (extraction / "record.md").stat().st_size,
                            "sha256": sha256_file(extraction / "record.md"),
                        },
                    ]
                },
            )
            self.write_json(diagnostic / "sources.json", {"sources": []})
            (diagnostic / "coverage.jsonl").write_text(
                json.dumps(
                    {
                        "coverage_id": "synthetic-record",
                        "content_kind": "article",
                        "status": "included",
                        "source_path": "generated",
                        "source_locator": "synthetic validation fixture",
                        "output_path": "record.md",
                        "output_locator": {"start_line": 1, "end_line": 9},
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            self.write_json(diagnostic / "quality.json", {"schema_version": "1.0"})
            self.write_json(
                diagnostic / "confidence.json",
                {
                    "schema_version": "1.0",
                    "categories": {
                        "synthetic": {"level": "high", "basis": "synthetic fixture"}
                    },
                },
            )

            report = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic passing article",
                expected_counts={"main_figures": 1},
            )

            self.assertEqual(report.findings, ())
            self.assertTrue(report.passed)

            (extraction / "record.md").write_text(
                """# Synthetic passing article

## Local Assets

- [Figure 1](figures/figure_001.png)
""",
                encoding="utf-8",
            )
            truncated = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic passing article",
                expected_counts={"main_figures": 1},
            )
            self.assertIn(
                "content_count_mismatch",
                {finding.code for finding in truncated.findings},
            )


if __name__ == "__main__":
    unittest.main()
