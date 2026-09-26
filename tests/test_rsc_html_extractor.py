from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

try:
    from lxml import html as lxml_html
except ModuleNotFoundError:
    lxml_html = None

if lxml_html is not None:
    from scripts.extraction.html_extractor import extract_html
else:  # pragma: no cover - documents the optional dependency boundary
    extract_html = None


@unittest.skipUnless(lxml_html is not None, "lxml is required for extraction tests")
class RscHtmlExtractionTests(unittest.TestCase):
    def extract(self, source: str):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "article.html"
            path.write_text(source, encoding="utf-8")
            return extract_html(path, "html/main.html")

    def test_silverchair_title_esi_control_is_not_scientific_superscript(self) -> None:
        result = self.extract(
            """<body><h1 id="aria468111">A study of <sup>Br</sup>U DNA
            <sup><a reveal-id="fn1">†</a></sup>
            <span><i title="Open Access"><span>Open Access</span></i></span></h1>
            <section><h2>Body</h2><p>Text.</p></section></body>"""
        )

        self.assertEqual(result.title, "A study of ^{Br}U DNA")

        for near_miss in (
            '<sup><a reveal-id="figure1">†</a></sup>',
            '<sup><a reveal-id="fn1">authored</a></sup>',
            '<sup><a reveal-id="fn1" href="#fn1">†</a></sup>',
        ):
            candidate = self.extract(
                f"""<body><h1>A study of <sup>Br</sup>U DNA {near_miss}</h1>
                <section><h2>Body</h2><p>Text.</p></section></body>"""
            )
            self.assertNotEqual(candidate.title, "A study of ^{Br}U DNA")

    def test_authenticated_rsc_snapshot_recovers_scientific_streams(self) -> None:
        pixel = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+"
            "A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        result = self.extract(
            f"""<body>
            <header>
              <h1 id="aria468111">A study of <sup>Br</sup>U DNA
                <sup><a reveal-id="fn1">†</a></sup>
                <span><i title="Open Access"><span>Open Access</span></i></span>
              </h1>
              <a href="https://doi.org/10.1039/C5CC00000A">DOI</a>
            </header>
            <div id="ContentTab"><main>
              <div><section aria-label="Main abstract"><p>Abstract result.</p></section></div>
              <p>Main scientific text.</p>
              <div><figure id="visual-abstract">
                <h2>Visual Abstract</h2><div>
                  <div><img src="data:image/png;base64,{pixel}"></div>
                  <div><p>Graphical summary.</p></div>
                </div>
              </figure></div>
              <figure id="figure-1">
                <a id="jump-1" scrollto-destination="jump-1"></a>
                <div reveal-group-id="fig1">
                  <span></span><div>Fig. 1</div>
                  <div><img src="data:image/png;base64,{pixel}"></div>
                  <div><p>Complete Figure 1 caption.</p></div>
                </div>
              </figure>
              <div><figure id="table-1">
                <div id="tab1"><span id="label-tab1">Table 1</span>
                  <div id="caption-tab1"><p>Binding kinetics.</p></div>
                </div>
                <div><table role="table" aria-labelledby="label-tab1" aria-describedby="caption-tab1">
                  <thead><tr><th>Sample</th><th><i>K</i><sub>D</sub> (M)</th></tr></thead>
                  <tbody><tr><td>1</td><td>5.0 × 10<sup>−8</sup></td></tr></tbody>
                </table></div>
                <div><table aria-hidden="true"><tr><td>duplicate</td></tr></table></div>
                <div><a href="#table-1" aria-label="View large table 1">View</a></div>
              </figure></div>
              <h2 id="99">References</h2>
              <div><h2 id="footNotesSectionTitle" scrollto-destination="footNotesSectionTitle">Footnotes</h2>
                <div id="fn1" content-id="fn1"><span><span rel="nofollow">†</span></span>
                  <p>Electronic supplementary information is available.</p>
                </div>
              </div>
            </main></div>
            </body>"""
        )

        self.assertEqual(result.title, "A study of ^{Br}U DNA")
        self.assertEqual(
            [(section.heading, [block.plain_text for block in section.blocks])
             for section in result.sections],
            [
                ("Abstract", ["Abstract result."]),
                ("Main text", ["Main scientific text."]),
            ],
        )
        self.assertEqual(
            [(figure.label, figure.caption_plain) for figure in result.figures],
            [
                ("Graphical Abstract", "Graphical summary."),
                ("Figure 1", "Complete Figure 1 caption."),
            ],
        )
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].label, "Table 1")
        self.assertEqual(result.tables[0].title_plain, "Table 1 Binding kinetics.")
        self.assertEqual(
            [[cell.text for cell in row] for row in result.tables[0].parts[0].rows],
            [["Sample", "K_{D} (M)"], ["1", "5.0 × 10^{−8}"]],
        )
        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            ["Electronic supplementary information is available."],
        )
        self.assertEqual(result.warnings, [])

    def test_rsc_notes_and_references_with_mixed_table_wrappers(self) -> None:
        pixel = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+"
            "A8AAQUBAScY42YAAAAASUVORK5CYII="
        )

        def table(number: int, tag: str, role: str) -> str:
            identifier = f' id="table-{number}"' if tag == "figure" else ""
            return f"""<div><{tag}{identifier} content-id="tab{number} ">
              <div id="tab{number}"><span id="label-tab{number}">Table {number}</span>
                <div id="caption-tab{number}"><p>Measurements {number} with
                  Δ<em>T</em><sub>m</sub>.</p></div></div>
              <div><table role="{role}" aria-labelledby="label-tab{number}"
                aria-describedby="caption-tab{number}"><tbody><tr>
                <td>Value</td><td>{number}</td></tr></tbody></table></div>
              <div></div>
              <div><a href="#table-{number}" aria-label="View large Table {number}">View</a></div>
            </{tag}></div>"""

        references = "".join(
            f"""<div><div><span>{number}.</span><div>
              <span>Author {number}</span>, <div><em>Journal</em></div>, <div>2024</div>.
              <div><div><a href="https://example.invalid">Google Scholar</a></div>
              <div><a href="http://dx.doi.org/10.1039/example">Crossref</a></div></div>
            </div></div></div>"""
            for number in range(1, 3)
        )
        result = self.extract(
            f"""<body><header>
              <h1 id="aria468111">Variant article
                <span><i title="Open Access"><span>Open Access</span></i></span></h1>
              <a href="https://doi.org/10.1039/C5CC00000A">DOI</a>
            </header><div id="ContentTab"><main>
              <div><section aria-label="Main abstract"><p>Abstract result.</p></section></div>
              <p>Main scientific text.</p>
              <div><figure id="visual-abstract"><h2>Visual Abstract</h2><div>
                <div><img src="data:image/png;base64,{pixel}"></div>
                <div><p>Graphical summary.</p></div></div></figure></div>
              <figure id="figure-1"><a id="jump-1" scrollto-destination="jump-1"></a>
                <div reveal-group-id="fig1"><span></span><div>Fig. 1</div>
                <div><img src="data:image/png;base64,{pixel}"></div>
                <div><p>Complete Figure 1 caption.</p></div></div></figure>
              {table(1, "figure", "presentation")}
              {table(2, "div", "table")}
              {table(3, "div", "table")}
              {table(4, "div", "table")}
              <h2 id="97">Conflicts of interest</h2>
              <div><div id="98-content"><p>There are no conflicts to declare.</p></div></div>
              <h2 id="99">Data availability</h2>
              <div><div id="100-content"><p>Data are included in the ESI.</p></div></div>
              <h2 id="101">Notes and references</h2>
              <div><div id="101-content"><div>{references}</div></div></div>
            </main></div></body>"""
        )

        self.assertEqual(
            [
                (section.heading, [block.plain_text for block in section.blocks])
                for section in result.sections
            ],
            [
                ("Abstract", ["Abstract result."]),
                ("Main text", ["Main scientific text."]),
                ("Conflicts of interest", ["There are no conflicts to declare."]),
                ("Data availability", ["Data are included in the ESI."]),
            ],
        )
        self.assertEqual([table.label for table in result.tables], [
            "Table 1", "Table 2", "Table 3", "Table 4"
        ])
        self.assertEqual(
            result.tables[0].title_markdown,
            "Table 1 Measurements 1 with Δ<em>T</em><sub>m</sub>.",
        )
        self.assertEqual(
            result.tables[0].title_plain,
            "Table 1 Measurements 1 with ΔT_{m}.",
        )
        self.assertEqual(
            [reference.plain_text for reference in result.references],
            [
                "1. Author 1, Journal, 2024.",
                "2. Author 2, Journal, 2024.",
            ],
        )


if __name__ == "__main__":
    unittest.main()
