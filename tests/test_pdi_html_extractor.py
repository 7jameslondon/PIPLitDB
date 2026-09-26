from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.extraction.html_extractor import extract_html


PDI_HTML = """
<body><article><header>
  <h1>PDI semantic snapshot</h1>
  <p>Research article</p>
  <a href="https://doi.org/10.3747/pdi.2024.00001">https://doi.org/10.3747/pdi.2024.00001</a>
</header>
<div><div id="abstracts"><div><section id="abstract" property="abstract" typeof="Text" role="doc-abstract">
  <h2>Abstract</h2><div role="paragraph">Complete abstract prose.</div>
</section></div></div>
<section id="bodymatter" property="articleBody" typeof="Text"><div>
  <div role="paragraph">Unheaded introduction prose.</div>
  <section id="sec-1"><h2>Results</h2><div role="paragraph">Complete result prose.</div>
    <div><figure id="fig1-pdi-2024-00001"><img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="><figcaption><span>Figure 1</span> Complete <i>caption</i>.</figcaption></figure></div>
  </section>
</div></section>
<section id="backmatter"><div>
  <section id="acknowledgments" role="doc-acknowledgments"><h2>Acknowledgments</h2><div role="paragraph">Thank you.</div></section>
  <section id="bibliography" role="doc-bibliography"><h2>References</h2><div><div>
    <div id="bibr1-pdi-2024-00001"><div>
      <div><span>1.</span> A. Author. First citation. <em>Journal</em> 2024; 1: 1-2.</div>
      <div><div><a href="https://doi.org/10.1000/first">Crossref</a></div><div><a href="https://pubmed.ncbi.nlm.nih.gov/1/">PubMed</a></div><div id="to-citation__accordion-bibr1-pdi-2024-00001" role="menu"><ul role="none"><li role="none"><a role="menuitem" href="#core-bibr1-pdi-2024-00001-1">a [...] cited passage</a></li></ul></div></div>
    </div></div>
    <div id="bibr2-pdi-2024-00001"><div>
      <div><span>2.</span> B. Author. Second citation. <em>Journal</em> 2023; 2: 3-4.</div>
      <div><div><a href="#core-bibr2-pdi-2024-00001-1">Go to Reference</a></div><div><a href="https://scholar.google.com/two">Google Scholar</a></div></div>
    </div></div>
  </div></div></section>
</div></section></div></article></body>
"""


class PdiHtmlExtractorTests(unittest.TestCase):
    def extract(self, source: str):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(source, encoding="utf-8")
            return extract_html(path, "article.html")

    def test_semantic_snapshot_recovers_role_paragraphs_and_authored_references(self) -> None:
        article = self.extract(PDI_HTML)

        self.assertEqual(
            [section.heading for section in article.sections],
            ["Abstract", "Introduction", "Results", "Acknowledgments"],
        )
        self.assertEqual(
            [block.plain_text for section in article.sections for block in section.blocks],
            [
                "Complete abstract prose.",
                "Unheaded introduction prose.",
                "Complete result prose.",
                "Thank you.",
            ],
        )
        self.assertEqual(len(article.figures), 1)
        self.assertEqual(article.figures[0].caption_plain, "Complete caption.")
        self.assertEqual(article.figures[0].caption_markdown, "Complete <em>caption</em>.")
        self.assertEqual(len(article.references), 2)
        self.assertEqual(
            article.references[0].plain_text,
            "1. A. Author. First citation. Journal 2024; 1: 1-2. "
            "DOI: https://doi.org/10.1000/first",
        )
        self.assertNotIn("cited passage", article.references[0].plain_text)
        self.assertEqual(
            article.references[1].plain_text,
            "2. B. Author. Second citation. Journal 2023; 2: 3-4.",
        )

    def test_noncontiguous_bibliography_ids_do_not_enable_dialect(self) -> None:
        article = self.extract(PDI_HTML.replace("bibr2-pdi", "bibr3-pdi"))

        self.assertFalse(any(section.heading == "Introduction" for section in article.sections))
        self.assertEqual(article.references, [])

    def test_unknown_reference_control_fails_closed(self) -> None:
        article = self.extract(PDI_HTML.replace(">Google Scholar</a>", ">Download RIS</a>"))

        self.assertEqual(article.references, [])


if __name__ == "__main__":
    unittest.main()
