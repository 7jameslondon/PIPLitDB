from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from lxml import html as lxml_html

from scripts.extraction.html_extractor import (
    _jstage_snapshot_details,
    extract_html,
)


class JstageHtmlExtractorTests(unittest.TestCase):
    def source(self) -> str:
        pixel = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "/wcAAusB9Wl2uXsAAAAASUVORK5CYII="
        )
        encoded = base64.b64encode(pixel).decode("ascii")
        return f"""<body><div>
<div>
  <div></div><div>Regular Articles</div><div>Synthetic J-STAGE Study</div><div></div>
  <div>
    <a href="https://www.jstage.jst.go.jp/search/global/_search/-char/en?item=8&amp;word=Ada+Author"><span>Ada Author</span></a>
    <div><div id="authInfo">Author information</div><div><ul><li><span>Ada Author</span><a id="saPopupOpenAuth">Corresponding author</a><p>Example Institute</p></li></ul></div></div>
  </div>
  <div>Keywords: <a href="/search/global/_search/-char/en?item=5&amp;word=alpha">alpha</a>, <a href="/search/global/_search/-char/en?item=5&amp;word=beta">beta</a></div>
  <div><span title="JOURNAL">JOURNAL</span><span title="FULL-TEXT HTML">FULL-TEXT HTML</span></div>
  <p>2020 Volume 43 Issue 1 Pages 124-128</p>
  <div id="fig-doi"><span>DOI</span><a href="https://doi.org/10.9999/example.1">https://doi.org/10.9999/example.1</a></div>
  <div></div>
  <div><a href="https://www.jstage.jst.go.jp/example">Advance version</a></div>
  <div><div>Details</div><div><ul><li><span>Published: January 01, 2020</span><span>Received: August 01, 2019</span><span>Accepted: October 03, 2019</span><span>Advance online publication: October 24, 2019</span></li></ul></div></div>
</div>
<div id="article-overiew-abstract-wrap">
  <div id="abstract"><div>Abstract</div><p>Abstract body.</p></div>
  <div id="sec01"><p></p><div>INTRODUCTION</div><p>Introduction body.</p></div>
  <div id="sec02"><p></p><div>RESULTS</div><p></p><span id="sec0201">Validation</span>
    <p>Before equation.</p><span id="math1"><div id="math1"><table width="95%"><tbody><tr><td valign="center">Accuracy (%) = (measured − theoretical) / theoretical × 100</td></tr></tbody></table></div></span>
    <figure id="figure-1"><img src="data:image/png;base64,{encoded}"><div>Fig. 1. Synthetic result<p>Authored figure note.</p></div><span></span></figure>
    <div id="table1">Table 1. Synthetic values (<i>n</i> = 2)<div><table id="fixTable1"><thead><tr><th>Group</th><th>Value</th></tr></thead><tbody><tr><td>A</td><td>1</td></tr></tbody></table></div><div>Values are means.</div></div>
  </div>
  <div id="ack"><div>Acknowledgments</div><p>Thanks.</p></div>
  <div id="note01"><div>Conflict of Interest</div><p>None.</p></div>
  <div id="article-overiew-references-wrap"><div>REFERENCES</div><ul id="article-overview-references-list"><li id="R1"><span></span><span>1) Author A. Synthetic reference. <i>Journal</i>, <b>1</b>, 1–2 (2019).</span><div></div></li></ul></div>
  <div><div id="socialmedia-share-plugins"></div></div>
</div>
</div></body>"""

    def extract(self, source: str):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "main.html"
            path.write_text(source, encoding="utf-8")
            return extract_html(path, "papers (private)/00000/html/main.html")

    def test_exact_jstage_snapshot_restores_complete_semantics(self) -> None:
        source = self.source()
        document = lxml_html.fromstring(source)
        self.assertIsNotNone(_jstage_snapshot_details(document))

        result = self.extract(source)

        self.assertEqual(result.title, "Synthetic J-STAGE Study")
        self.assertEqual(
            result.bibliographic,
            {
                "volume": "43",
                "issue": "1",
                "pages": "124-128",
                "date": "January 01, 2020",
                "first_published": "October 24, 2019",
            },
        )
        self.assertEqual(
            [section.heading for section in result.sections],
            [
                "Abstract",
                "INTRODUCTION",
                "RESULTS",
                "Validation",
                "Acknowledgments",
                "Conflict of Interest",
            ],
        )
        equations = [
            block
            for section in result.sections
            for block in section.blocks
            if block.kind == "equation"
        ]
        self.assertEqual(len(equations), 1)
        self.assertEqual(
            equations[0].plain_text,
            "Accuracy (%) = (measured − theoretical) / theoretical × 100",
        )
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertIn("Synthetic result", result.figures[0].caption_plain)
        self.assertIn("Authored figure note", result.figures[0].caption_plain)
        self.assertEqual(len(result.embedded_assets), 1)
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].title_plain, "Table 1. Synthetic values (n = 2)")
        self.assertEqual(result.tables[0].parts[0].rows[1][1].text, "1")
        self.assertEqual(result.tables[0].footnotes_plain, ["Values are means."])
        self.assertEqual(len(result.references), 1)
        self.assertTrue(result.references[0].plain_text.startswith("1) Author A."))
        front = [block.plain_text for block in result.front_matter]
        self.assertIn("Article type: Regular Articles", front)
        self.assertIn("Affiliation — Ada Author: Example Institute", front)
        self.assertIn("Corresponding author: Ada Author", front)
        self.assertIn("Keywords: alpha; beta", front)
        self.assertIn("DOI: https://doi.org/10.9999/example.1", front)

    def test_near_miss_wrapper_is_not_normalized(self) -> None:
        source = self.source().replace(
            "article-overiew-abstract-wrap",
            "article-overview-abstract-wrap",
            1,
        )
        self.assertIsNone(_jstage_snapshot_details(lxml_html.fromstring(source)))
        result = self.extract(source)
        self.assertEqual(result.title, "")
        self.assertEqual(result.front_matter, [])
        self.assertEqual(result.tables, [])

    def test_unheaded_first_section_is_recovered_as_introduction(self) -> None:
        source = self.source().replace("<div>INTRODUCTION</div>", "<div></div>", 1)

        result = self.extract(source)

        self.assertEqual(result.title, "Synthetic J-STAGE Study")
        self.assertEqual(
            [section.heading for section in result.sections],
            [
                "Abstract",
                "Introduction",
                "RESULTS",
                "Validation",
                "Acknowledgments",
                "Conflict of Interest",
            ],
        )
        self.assertEqual(result.sections[1].blocks[0].plain_text, "Introduction body.")
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(len(result.references), 1)

    def test_empty_jstage_equation_placeholder_does_not_invent_content(self) -> None:
        source = self.source().replace(
            "Accuracy (%) = (measured − theoretical) / theoretical × 100",
            "",
            1,
        )
        result = self.extract(source)
        equations = [
            block
            for section in result.sections
            for block in section.blocks
            if block.kind == "equation"
        ]
        self.assertEqual(equations, [])

    def test_semantic_jstage_archive_recovers_reference_tail_and_header_metadata(self) -> None:
        source = """<body><article lang="en"><header>
<p>Example Journal</p><h1>Semantic J-STAGE Study</h1><p>Ada Author</p>
<p>Example Journal 2021, 44 (2), 1460–1465.</p>
<p>Published online: September 1, 2021.</p>
<p><a href="https://doi.org/10.9999/example.2">https://doi.org/10.9999/example.2</a></p>
<p><a href="https://www.jstage.jst.go.jp/article/example/44/2/example_2/_html/-char/en">Publisher full text</a></p>
</header><main><div id="abstract"><h2>Abstract</h2><p>Abstract body.</p></div>
<div id="sec01"><h2>Introduction</h2><p>Introduction body.</p>
<figure id="figure1"><img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk/wcAAusB9Wl2uXsAAAAASUVORK5CYII="><figcaption>Fig. 1. Complete authored figure title <em>in Vitro</em><p>Authored panel legend.</p></figcaption></figure></div>
<div id="article-overiew-references-wrap"><h2>References</h2>
<ul id="article-overview-references-list">
<li id="R1"><a href="https://example.test/one?lang;=ja&amp;from;=J-STAGE"><span>1) Author A. <em>First reference.</em></span></a></li>
<li id="R2"><span>2) Author B. Second reference, pp. 801–802.</span></li>
</ul></div><div id="supporting-information"><h2>Supplementary Material</h2>
<p>Publisher file: <a href="https://www.jstage.jst.go.jp/supplement.pdf">Supplemental Information</a></p>
</div></main></article></body>"""

        result = self.extract(source)

        self.assertEqual(
            result.bibliographic,
            {
                "volume": "44",
                "issue": "2",
                "pages": "1460–1465",
                "date": "September 1, 2021.",
                "first_published": "September 1, 2021.",
            },
        )
        self.assertEqual(len(result.references), 2)
        self.assertTrue(result.references[0].plain_text.startswith("1) Author A."))
        self.assertNotIn("](", result.references[0].markdown)
        self.assertIn("<em>First reference.</em>", result.references[0].markdown)
        self.assertIn("pp. 801–802", result.references[1].plain_text)
        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Introduction"],
        )
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(
            result.figures[0].caption_plain,
            "Complete authored figure title in Vitro Authored panel legend.",
        )
        self.assertIn("<em>in Vitro</em>", result.figures[0].caption_markdown)
        self.assertEqual(len(result.supporting_information), 1)
        self.assertIn("Supplemental Information", result.supporting_information[0].plain_text)
        front = [block.plain_text for block in result.front_matter]
        self.assertIn(
            "Citation details: Example Journal 2021, 44 (2), 1460–1465.",
            front,
        )
        self.assertIn("Publication date: September 1, 2021.", front)
        self.assertIn("DOI: https://doi.org/10.9999/example.2", front)


if __name__ == "__main__":
    unittest.main()
