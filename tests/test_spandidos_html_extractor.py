from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from lxml import html as lxml_html

from scripts.extraction.html_extractor import (
    _spandidos_snapshot_root,
    extract_html,
)


_PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8"
    b"\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")


class SpandidosHtmlExtractorTests(unittest.TestCase):
    def source(self, *, doi: str = "10.3892/ijo.2020.1234") -> str:
        return f"""<article><h1>Archived Spandidos study</h1>
<div><ul><li><span>Authors:</span><ul id="authorshipNames"><li>Ada Author</li></ul></li>
<li><div><span>Affiliations: </span><span>Example Institute, London, UK</span></div></li>
<li><div><span>Pages:</span><span>1-4</span></div><div><span>Published online on:</span><span id="publishedOn">May 22, 2020</span></div>
<div><span id="doi">https://doi.org/{doi}</span></div></li></ul></div>
<h2>Abstract</h2><div id="articleAbstract">Complete abstract text.</div>
<div id="mainArticle"><h4>Introduction</h4><p>Introduction prose.</p>
<h4>Materials and methods</h4><h5>Cell assay</h5><p>Methods dose 5.0×10<span>3</span> cells in 5% CO<span>2</span> <span>in vitro</span> (P&lt;0.05).</p>
<figure id="figure-1"><a href="/article_images/ijo/1/1/example-g00.jpg" title="Figure 1 - Complete authored caption."><div><table><tbody><tr>
<td><img src="data:image/png;base64,{_PNG}"></td><td><h4>Figure 1</h4><p>Complete authored caption.</p></td>
</tr></tbody></table></div></a></figure>
<h4>Results</h4><h5>Primary result</h5><p>Results prose.</p>
<h4>Discussion</h4><p>Discussion prose.</p><h4>Acknowledgements</h4><p>Thanks.</p>
<h4>References</h4><table><tr><td><p><span>1</span><a id="b1-ijo-1-1-1"></a></p></td><td><p><a id="d1"></a>Author A: Citation title. Journal. 1:1-2. 2019. <a href="http://dx.doi.org/10.1000/example" target="xrefwindow">View Article</a> : <a href="http://scholar.google.com/example" target="xrefwindow">Google Scholar</a> : <a href="http://www.ncbi.nlm.nih.gov/pubmed/1" target="xrefwindow">PubMed/NCBI</a></p></td></tr></table>
</div></article>"""

    def extract(self, source: str):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "main.html"
            path.write_text(source, encoding="utf-8")
            return extract_html(path, "papers (private)/00000/html/main.html")

    def legacy_current_article_source(self) -> str:
        return f"""<body><div id="current-article"><div><div>
<h1>Archived legacy Spandidos study</h1>
<div><div><ul><li><span>Authors:</span><ul id="authorshipNames"><li>Ada Author</li></ul></li>
<li><div><span>Affiliations: </span><span>Example Institute, London, UK</span></div></li>
<li><div><span>Pages:</span><span>1-4</span></div><div><span>Published online on:</span><span id="publishedOn">May 22, 2020</span></div>
<div><span id="doi">https://doi.org/10.3892/ijo.2020.1234</span></div></li></ul></div></div>
<h4>Abstract</h4><div id="articleAbstract">Complete abstract text.</div>
<div><div id="mainArticle"><h4>Introduction</h4><p>Introduction prose.</p>
<h4>Materials and methods</h4><h5>Cell assay</h5><p>Methods prose.</p>
<div id="t1-ijo-1-1-1"><a href="#tb_t1-ijo-1-1-1" title="Table I. - Patient background."></a>
<table><tbody><tr><td></td><td><h4>Table I.</h4><p>Patient background.</p></td></tr></tbody></table>
<div><div id="tb_t1-ijo-1-1-1"><h4>Table I.</h4><p>Patient background.</p>
<table><thead><tr><th>Category</th><th>N</th></tr></thead><tbody><tr><td>Cases</td><td>4</td></tr></tbody></table>
</div></div></div>
<figure id="figure-1"><a href="/article_images/ijo/1/1/example-g00.jpg" title="Figure 1. - Complete authored caption."><div><table><tbody><tr>
<td><img src="data:image/png;base64,{_PNG}"></td><td><h4>Figure 1.</h4><p>Complete authored caption.</p></td>
</tr></tbody></table></div></a></figure>
<h4>Results</h4><h5>Primary result</h5><p>Results prose.</p>
<h4>Discussion</h4><p>Discussion prose.</p><h4>Acknowledgements</h4><p>Thanks.</p>
<h4>References</h4><table><tbody><tr><td><p><span>1.</span><a id="b1-ijo-1-1-1"></a></p></td><td><p><a id="d11" name="d11"></a>Author A: Citation title. Journal. 1:1-2. 2019. <a href="http://www.ncbi.nlm.nih.gov/pubmed/1" target="xrefwindow">PubMed/NCBI</a></p></td></tr></tbody></table>
</div></div></div></div></div></body>"""

    def test_exact_snapshot_recovers_complete_semantics(self) -> None:
        source = self.source()
        self.assertIsNotNone(_spandidos_snapshot_root(lxml_html.fromstring(source)))
        result = self.extract(source)

        self.assertEqual(
            [section.heading for section in result.sections],
            [
                "Abstract",
                "Introduction",
                "Materials and methods",
                "Cell assay",
                "Results",
                "Primary result",
                "Discussion",
                "Acknowledgements",
            ],
        )
        self.assertEqual(result.sections[0].blocks[0].plain_text, "Complete abstract text.")
        cell_assay = next(
            section for section in result.sections if section.heading == "Cell assay"
        )
        self.assertEqual(
            cell_assay.blocks[0].markdown,
            "Methods dose 5.0×10<sup>3</sup> cells in 5% CO<sub>2</sub> "
            "<em>in vitro</em> (<em>P</em> &lt;0.05).",
        )
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].caption_plain, "Complete authored caption.")
        self.assertEqual(
            [block.plain_text for block in result.references],
            [
                "1. Author A: Citation title. Journal. 1:1-2. 2019. "
                "DOI: https://doi.org/10.1000/example"
            ],
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliations: Example Institute, London, UK",
                "Published online: May 22, 2020",
            ],
        )
        self.assertNotIn("Figure 1", [section.heading for section in result.sections])

    def test_near_miss_without_spandidos_doi_is_not_promoted(self) -> None:
        source = self.source(doi="10.1000/not-spandidos")
        self.assertIsNone(_spandidos_snapshot_root(lxml_html.fromstring(source)))
        result = self.extract(source)
        self.assertEqual(result.front_matter, [])
        self.assertEqual(result.references, [])
        self.assertEqual(result.figures[0].caption_plain, "")
        self.assertEqual(result.sections[0].blocks, [])
        self.assertNotIn("Cell assay", [section.heading for section in result.sections])

    def test_legacy_current_article_recovers_references_and_expanded_table(self) -> None:
        source = self.legacy_current_article_source()
        document = lxml_html.fromstring(source)
        self.assertIsNotNone(_spandidos_snapshot_root(document))
        result = self.extract(source)

        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].caption_plain, "Complete authored caption.")
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].title_plain, "Table I. Patient background.")
        self.assertEqual(
            [section.heading for section in result.sections],
            [
                "Abstract",
                "Introduction",
                "Materials and methods",
                "Cell assay",
                "Results",
                "Primary result",
                "Discussion",
                "Acknowledgements",
            ],
        )
        self.assertEqual(result.sections[0].blocks[0].plain_text, "Complete abstract text.")
        self.assertEqual(
            [[cell.text for cell in row] for row in result.tables[0].parts[0].rows],
            [["Category", "N"], ["Cases", "4"]],
        )
        self.assertEqual(
            [block.plain_text for block in result.references],
            ["1. Author A: Citation title. Journal. 1:1-2. 2019."],
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliations: Example Institute, London, UK",
                "Published online: May 22, 2020",
            ],
        )


if __name__ == "__main__":
    unittest.main()
