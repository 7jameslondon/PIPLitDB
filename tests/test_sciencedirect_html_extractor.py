from __future__ import annotations

import base64
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


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0publisher-image\xff\xd9"


@unittest.skipUnless(lxml_html is not None, "lxml is required for extraction tests")
class ScienceDirectHtmlExtractionTests(unittest.TestCase):
    def extract(self, source: str):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "article.html"
            path.write_text(source, encoding="utf-8")
            return extract_html(path, "html/main.html")

    def test_rendered_dom_is_extracted_as_structured_primary_content(self) -> None:
        png = base64.b64encode(PNG_BYTES).decode("ascii")
        jpeg = base64.b64encode(JPEG_BYTES).decode("ascii")
        result = self.extract(
            f"""<body>
<h2>Publisher banner</h2>
<div><span>Date: </span>15 May 2018</div>
<div><span>Pages: </span>2337-2344</div>
<div><a>Volume 26, Issue 9</a></div>
<h1>Hydrophobic <em>in vivo</em> study</h1>
<div id="abstracts">
  <div><h2>Highlights</h2><ul><li><span>•</span><span>Primary finding.</span></li></ul></div>
  <div><h2>Abstract</h2><div id="as001"><div id="sp001">
    Compound <strong>1</strong> retained <em>in vivo</em>.
  </div></div></div>
  <div><h2>Graphical abstract</h2><figure id="f0030">
    <img src="data:image/png;base64,{png}" alt="">
  </figure></div>
  <div><h2>Keywords</h2><div id="k0005">Polyamide</div><span>; </span>
    <div id="k0010">Hydrophobicity</div></div>
</div>
<ul><li><a href="/science/article/pii/previous">Previous article in this issue</a></li>
    <li><a href="/science/article/pii/next">Next article in this issue</a></li></ul>
<div id="body">
  <section><h2>1. Introduction</h2><div id="p0020">
    Prior work<a href="#b0010"><span><span>2</span></span></a> and
    another source<a href="#b0015"><span><span><sup>3</sup></span></span></a>.
    Fluorescence quantum yield (<em>ϕ<sub>f</sub></em>) was calculated as
    <math><mrow>
      <msub><mo>∅</mo><mi>f</mi></msub><mo>=</mo>
      <msub><mo>∅</mo><mi>s</mi></msub><mo>×</mo>
      <mfenced><mfrac>
        <mrow><msub><mi>F</mi><mi>x</mi></msub><mo>/</mo><msub><mi>A</mi><mi>x</mi></msub></mrow>
        <mrow><msub><mi>F</mi><mi>s</mi></msub><mo>/</mo><msub><mi>A</mi><mi>s</mi></msub></mrow>
      </mfrac></mfenced>
    </mrow></math>.
  </div><div id="p0021">A terminal display equation follows.
    <span><span id="e0005"><span><math><mrow>
      <msub><mi>F</mi><mi>x</mi></msub><mo>=</mo><mn>2</mn>
    </mrow></math></span></span>
    <span id="e0010"><span>(2) </span><span id="e0015"><math><mrow>
      <mi>x</mi><mo>&lt;</mo><mn>3</mn>
    </mrow></math></span></span> after equations.</span>
  </div></section>
  <section><h2>2. Results</h2>
    <section><h3>2.1. <em>In vivo</em> measurements</h3><div id="p0025">β-form was observed.</div>
      <section><h4>2.1.1. Controls</h4><div id="p0030">Control text.</div></section>
    </section>
    <div id="p0031">Parent text after the nested section.</div>
    <figure id="f0005"><img src="data:image/jpeg;base64,{jpeg}" alt="">
      <span><span id="cn0005"><p><span>Fig. 1</span>. Response <em>in vivo</em>
        (<sup>**</sup><em>p</em> &lt; 0.01).</p></span></span>
    </figure>
    <figure id="f0010"><img src="https://example.invalid/scheme.jpg" alt="">
      <span><span id="cn0010"><p><span>Scheme 2</span>. Remote scheme.</p></span></span>
    </figure>
    <div id="t0005"><span><span id="cn0030"><p><span>Table 1</span>.
      Partition coefficients (log<em>P<sub>OW</sub></em>).</p></span></span>
      <table><thead><tr><th>Compound</th><th>log<em>P<sub>OW</sub></em></th></tr></thead>
      <tbody><tr><td><strong>1</strong></td><td>1.5</td></tr></tbody></table>
    </div>
  </section>
  <section><h2>A. Supplementary data</h2><div id="p0900">
    <span><a href="https://example.invalid/mmc1.pptx">Download Powerpoint document</a>
      <span><p>Supplementary Fig. SI1.</p></span></span>
  </div></section>
  <section id="article-references"><h2>References</h2><ol><li>
    <span><a href="#bb0005" id="ref-id-b0005"><span>1</span></a></span>
    <span id="h0005"><div><div>A. Author, B. Writer</div></div>
      <div id="ref-id-h0005"><em>J Test</em>, 1 (2020), pp. 1-2</div>
      <div lang="en"><a href="https://doi.org/10.1234/example">Crossref</a>
        <a href="https://scopus.invalid/record">View in Scopus</a></div>
    </span>
  </li></ol></section>
</div></body>"""
        )

        self.assertEqual(result.title, "Hydrophobic in vivo study")
        self.assertEqual(
            result.bibliographic,
            {
                "date": "15 May 2018",
                "issue": "9",
                "pages": "2337-2344",
                "volume": "26",
            },
        )
        self.assertEqual(
            [(section.heading, section.level) for section in result.sections],
            [
                ("Highlights", 2),
                ("Abstract", 2),
                ("Keywords", 2),
                ("1. Introduction", 2),
                ("2. Results", 2),
                ("2.1. <em>In vivo</em> measurements", 3),
                ("2.1.1. Controls", 4),
            ],
        )
        self.assertEqual(result.sections[0].blocks[0].markdown, "- Primary finding.")
        self.assertEqual(result.sections[0].blocks[0].plain_text, "- Primary finding.")
        self.assertIn("<strong>1</strong>", result.sections[1].blocks[0].markdown)
        self.assertEqual(
            [block.plain_text for block in result.sections[2].blocks],
            ["Polyamide", "Hydrophobicity"],
        )
        all_body_text = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertNotIn("Previous article", all_body_text)
        introduction = next(
            section for section in result.sections if section.heading == "1. Introduction"
        )
        self.assertIn(
            "∅<sub>f</sub> = ∅<sub>s</sub>", introduction.blocks[0].markdown
        )
        self.assertIn("F<sub>x</sub>", introduction.blocks[0].markdown)
        self.assertIn("work<sup>2</sup>", introduction.blocks[0].markdown)
        self.assertIn("source<sup>3</sup>", introduction.blocks[0].markdown)
        self.assertIn("work^{2}", introduction.blocks[0].plain_text)
        self.assertIn("source^{3}", introduction.blocks[0].plain_text)
        self.assertEqual(
            [(block.block_id, block.kind) for block in introduction.blocks[1:]],
            [
                ("main-paragraph-0006", "paragraph"),
                ("equation-001", "equation"),
                ("equation-002", "equation"),
                ("main-paragraph-0007", "paragraph"),
            ],
        )
        self.assertEqual(introduction.blocks[2].plain_text, "F_{x} = 2")
        self.assertEqual(introduction.blocks[2].markdown, "F<sub>x</sub> = 2")
        self.assertEqual(introduction.blocks[3].plain_text, "(2) x < 3")
        self.assertEqual(introduction.blocks[3].markdown, "(2) x &lt; 3")
        self.assertEqual(introduction.blocks[4].plain_text, "after equations.")

        results = next(
            section for section in result.sections if section.heading == "2. Results"
        )
        controls = next(
            section for section in result.sections if section.heading == "2.1.1. Controls"
        )
        self.assertEqual(
            [block.plain_text for block in results.blocks],
            ["Parent text after the nested section."],
        )
        self.assertEqual(
            [block.plain_text for block in controls.blocks],
            ["Control text."],
        )

        self.assertEqual(
            [(item.figure_id, item.kind, item.label) for item in result.figures],
            [
                ("graphical_abstract", "graphical_abstract", "Graphical Abstract"),
                ("figure_001", "figure", "Figure 1"),
                ("scheme_002", "scheme", "Scheme 2"),
            ],
        )
        self.assertIn("Response <em>in vivo</em>", result.figures[1].caption_markdown)
        self.assertIn(r"<sup>\*\*</sup>", result.figures[1].caption_markdown)
        self.assertEqual(
            [asset.asset_id for asset in result.embedded_assets],
            ["graphical_abstract", "figure_001"],
        )
        self.assertEqual(result.embedded_assets[0].data, PNG_BYTES)
        self.assertEqual(
            result.embedded_assets[0].output_path,
            "figures/main/graphical_abstract.png",
        )
        self.assertEqual(result.embedded_assets[1].data, JPEG_BYTES)
        self.assertEqual(result.embedded_assets[1].output_path, "figures/main/figure_001.jpg")
        self.assertFalse(result.embedded_assets[1].ocr_performed)

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual((table.table_id, table.source_id), ("table_001", "t0005"))
        self.assertIn("<em>P<sub>OW</sub></em>", table.title_markdown)
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [["Compound", "logP_{OW}"], ["1", "1.5"]],
        )
        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            ["Supplementary Fig. SI1."],
        )
        self.assertNotIn("Download", result.supporting_information[0].plain_text)

        self.assertEqual(len(result.references), 1)
        reference = result.references[0]
        self.assertIn("A. Author, B. Writer", reference.plain_text)
        self.assertIn("https://doi.org/10.1234/example", reference.markdown)
        self.assertNotIn("Crossref", reference.markdown)
        self.assertNotIn("Scopus", reference.markdown)
        warning = next(
            warning
            for warning in result.warnings
            if warning["code"] == "possible_publisher_math_symbol_substitution"
        )
        self.assertIn("retained unchanged", warning["message"])

    def test_invalid_and_remote_figure_sources_are_not_materialized(self) -> None:
        mismatched = base64.b64encode(JPEG_BYTES).decode("ascii")
        result = self.extract(
            f"""<body><h1>Asset safety</h1><section><h2>Results</h2>
<figure id="f0005"><img src="data:image/png;base64,{mismatched}">
  <span id="cn0005"><p>Fig. 1. Invalid declaration.</p></span></figure>
<figure id="f0010"><img src="https://example.invalid/figure.jpg">
  <span id="cn0010"><p>Fig. 2. Remote source.</p></span></figure>
<figure id="f0015"><img src="data:text/plain;base64,SGVsbG8=">
  <span id="cn0015"><p>Fig. 3. Non-image data.</p></span></figure>
</section></body>"""
        )

        self.assertEqual(result.embedded_assets, [])
        invalid = [
            warning
            for warning in result.warnings
            if warning["code"] == "invalid_embedded_figure_image"
        ]
        self.assertEqual(len(invalid), 1)
        self.assertIn("do not match declared media type", invalid[0]["message"])

    def test_ordered_list_keeps_nested_number_and_spaced_ellipsis(self) -> None:
        result = self.extract(
            """<body><h1>Lists</h1><section><h2>Outline</h2><ol>
            <li>1. Introduction</li><li>These ...</li>
            </ol></section></body>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(block.kind, "list")
        self.assertEqual(
            block.markdown, "1. 1. Introduction\n1. These ..."
        )
        self.assertEqual(
            block.plain_text, "1. 1. Introduction\n1. These ..."
        )


if __name__ == "__main__":
    unittest.main()
