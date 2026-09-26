from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from lxml import html as lxml_html

from scripts.extraction.html_extractor import (
    _acs_single_semantic_figure_table_details,
    _anonymous_acs_numeric_snapshot_root,
    extract_html,
)


PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="


def acs_page() -> str:
    return f"""<!doctype html><html><body><div id="main" role="main"><section>
<div><h1 id="aria757493">Wrapped ACS communication</h1>
<a href="https://doi.org/10.1021/jacs.example">DOI</a></div>
<div id="ContentTab"><div>
  <div id="99-content"><figure><h2>Visual Abstract</h2>
    <img src="{PIXEL}" alt="Graphic. Refer to the image caption for details.">
  </figure></div>
  <h2 id="100">Abstract</h2><div>
    <div id="100-content"><section aria-label="Main abstract"><p>Authored summary.</p></section></div>
    <div id="101-content"><p>Unheaded article opening.</p></div>
    <div id="102-content"><a id="102" scrollto-destination="102"></a>
      <figure content-id="tbl1 " id="table-1">
        <div id="tbl1"><span id="label-tbl1">Table 1.</span>
          <div id="caption-tbl1">Measurements<a reveal-id="tbl1-fn1">a</a></div></div>
        <div id="source-table.tif"><a><img src="{PIXEL}"
          path-from-xml="source-table.tif"
          alt="Graphic. Refer to the image caption for details."></a></div>
        <div content-id="tbl1"></div>
        <div><table role="table" aria-labelledby="label-tbl1"
          aria-describedby="caption-tbl1"><thead><tr><th>Sample</th><th>Value</th></tr></thead>
          <tbody><tr><td>A</td><td>1</td></tr></tbody></table></div>
        <div></div>
        <div><div id="tbl1-fn1" content-id="tbl1-fn1"><span>a</span>
          <p>Author note.</p></div></div>
        <div><a href="/view-large/102" target="_blank" rel="nofollow"
          aria-label="View large Table 1.">View Large</a></div>
      </figure>
    </div>
    <div id="103-content"><p>Continuation of article body.</p></div>
  </div>
  <h2 id="200">Supporting Information</h2><div id="200-content"><p>Download.</p></div>
  <h2 id="250">Acknowledgments</h2><div id="250-content"><p>Supported.</p></div>
  <h2 id="300">References</h2><div id="300-content"><p>Reference content.</p></div>
</div></div></section></div></body></html>"""


def legacy_div_card_page() -> str:
    return f"""<!doctype html><html><body><main><header>
<h1 id="aria3800923">Legacy ACS div cards</h1>
<a href="https://doi.org/10.1021/ja021011q">DOI</a></header>
<div id="ContentTab"><div>
  <div id="10-content"><div><h2>Visual Abstract</h2><div><div><a>
    <img src="{PIXEL}" alt="Figure. Refer to the image caption for details.">
  </a></div></div></div></div>
  <h2 id="20">Abstract</h2><div id="20-content"><p>Summary.</p></div>
  <h2 id="30">Introduction</h2><div id="30-content">
    <div id="31-content"><a id="31"></a><div>
      <div>Figure 1</div><div><a><img src="{PIXEL}"
        alt="Figure 1. Refer to the image caption for details."></a></div>
      <div><div>Figure 1.</div><div><p>Complete caption.</p></div></div>
    </div></div>
    <div id="32-content"><a id="32"></a><div>
      <div>Figure 2</div><div><a><img src="{PIXEL}"
        alt="Figure 2. Refer to the image caption for details."></a></div>
      <div><div>Figure 2.</div></div>
    </div></div>
    <div id="33-content"><a id="33"></a><div>
      <div id="ja021011qt00001"><span id="label-ja021011qt00001">Table 1.</span>
        <div id="caption-ja021011qt00001"><p>Measurements<em><sup>a</sup></em></p></div></div>
      <div><table><tbody><tr><td>Sample</td><td>Value</td></tr>
        <tr><td>A</td><td>1</td></tr></tbody></table></div>
      <div><p><em><sup>a</sup></em> Author note.</p></div>
    </div></div>
  </div>
  <h2 id="40">Supporting Information</h2><div id="40-content"><p>Download.</p></div>
  <h2 id="50">Acknowledgments</h2><div id="50-content"><p>Supported.</p></div>
  <h2 id="60">References</h2><div id="60-content"><p>Reference content.</p></div>
</div></div></main></body></html>"""


class AcsContentTabPageShellTests(unittest.TestCase):
    def extract(self, source: str):
        with TemporaryDirectory() as directory:
            source_path = Path(directory) / "article.html"
            source_path.write_text(source, encoding="utf-8")
            return extract_html(source_path, "html/article.html")

    def test_page_wide_section_does_not_capture_numeric_body_or_table(self) -> None:
        result = self.extract(acs_page())

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Introduction", "Acknowledgments"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Authored summary."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Unheaded article opening.", "Continuation of article body."],
        )
        self.assertEqual(
            [(table.table_id, table.source_kind) for table in result.tables],
            [("table_001", "html")],
        )
        self.assertEqual(
            [(figure.figure_id, figure.kind) for figure in result.figures],
            [("graphical_abstract", "graphical_abstract")],
        )

    def test_source_named_direct_note_variant_requires_acs_authentication(self) -> None:
        document = lxml_html.fromstring(
            acs_page().replace(
                "https://doi.org/10.1021/jacs.example",
                "https://example.org/not-acs",
            )
        )
        table = document.xpath('//figure[@id="table-1"]')[0]

        self.assertIsNone(_anonymous_acs_numeric_snapshot_root(table))
        self.assertIsNone(_acs_single_semantic_figure_table_details(table))

    def test_legacy_div_cards_preserve_captionless_image_and_source_table(self) -> None:
        result = self.extract(legacy_div_card_page())

        self.assertEqual(
            [(figure.figure_id, figure.kind) for figure in result.figures],
            [
                ("graphical_abstract", "graphical_abstract"),
                ("figure_001", "figure"),
                ("figure_002", "figure"),
            ],
        )
        self.assertEqual(len(result.embedded_assets), 3)
        self.assertEqual(
            [(table.table_id, table.source_kind) for table in result.tables],
            [("table_001", "html")],
        )
        self.assertEqual(result.tables[0].footnotes_plain, ["[a] Author note."])
        self.assertTrue(result.tables[0].parts[0].rows[0][0].header)


if __name__ == "__main__":
    unittest.main()
