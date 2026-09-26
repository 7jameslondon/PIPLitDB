from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.extraction.html_extractor import extract_html


AHA_HTML = """
<body><article><header><div>
  <h1>AHA semantic snapshot</h1>
  <a href="https://doi.org/10.1161/HYPERTENSIONAHA.108.112797">https://doi.org/10.1161/HYPERTENSIONAHA.108.112797</a>
</div></header>
<div><div id="abstracts"><div><section id="abstract" property="abstract" typeof="Text" role="doc-abstract">
  <h2>Abstract</h2><div role="paragraph">Complete abstract prose.</div>
</section></div></div>
<section id="bodymatter" property="articleBody" typeof="Text"><div>
  <div role="paragraph">Unheaded introduction prose.</div>
  <section id="sec-1"><h2>Results</h2><div role="paragraph">Complete result prose.
    <div><figure id="FIG1"><img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=" aria-labelledby="FIG1"><figcaption><b>Figure 1. </b>Complete AHA <em>caption</em>. <i>P</i>&lt;0.05.</figcaption></figure></div></div>
  </section>
  <section id="acknowledgments" role="doc-acknowledgments"><h2>Acknowledgments</h2><div role="paragraph">Thank you.</div></section>
</div></section>
<section id="backmatter"><div>
  <section id="bibliography" role="doc-bibliography"><h2>References</h2>
    <div id="bibliography-collapsible-text">
      <div><div>1.</div><div id="R1-112797"><div><div>A. Author. First citation. <em>Journal</em> 2024; 1: 1–2.</div><div><div><a href="#core-R1-112797-1">Go to Citation</a></div><div><a href="https://doi.org/10.1000/first">Crossref</a></div><div><a href="https://pubmed.ncbi.nlm.nih.gov/1/">PubMed</a></div><div><a href="https://scholar.google.com/one">Google Scholar</a></div></div></div></div></div>
      <div><div>2.</div><div id="R2-112797"><div><div>B. Author. Second citation.</div><div><div><a href="#core-R2-112797-1">Go to Citation</a></div><div><a href="https://scholar.google.com/two">Google Scholar</a></div></div></div></div></div>
    </div>
  </section>
</div></section></div>
<div><section id="tab-contributors"><h3>Authors</h3>
  <section><h4>Affiliations</h4>
    <div id="con1" property="author" typeof="Person">
      <div role="button"><h5><span property="givenName">A.</span> <span property="familyName">Author</span></h5></div>
      <div id="con1_content" role="region"><div><div property="affiliation" typeof="Organization"><span property="name">Institute One.</span></div></div></div>
    </div>
  </section>
  <section><h4>Notes</h4><div role="doc-footnote">Correspondence to A. Author. E-mail <a href="mailto:a@example.org">a@example.org</a></div></section>
</section>
<section id="tab-information"><h3>Information</h3>
  <section><h4>Published In</h4><div><div property="isPartOf" typeof="Periodical"><span property="name">Hypertension</span></div><span property="volumeNumber">52</span><span property="issueNumber">1</span><span property="datePublished">1 July 2008</span><span property="pageStart">86</span><span property="pageEnd">92</span><a property="sameAs" href="https://pubmed.ncbi.nlm.nih.gov/18519843/">18519843</a></div></section>
  <section><h4>Copyright</h4><div role="paragraph">© 2008.</div></section>
  <section><h4>History</h4><div><b>Received</b>: 29 February 2008</div><div><b>Revision received</b>: 26 March 2008</div><div><b>Accepted</b>: 2 May 2008</div><div><b>Published online</b>: 2 June 2008</div><div><b>Published in print</b>: 1 July 2008</div></section>
  <section property="keywords"><h4>Keywords</h4><ol><li>basic science</li><li>polyamide</li></ol></section>
  <section><h4>Subjects</h4><div><ol><li>Gene Therapy</li><li role="presentation" aria-hidden="true"></li><li>Restenosis</li></ol></div></section>
</section></div>
</article></body>
"""


class AhaHtmlExtractorTests(unittest.TestCase):
    def extract(self, source: str):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(source, encoding="utf-8")
            return extract_html(path, "article.html")

    def test_semantic_snapshot_recovers_authored_prose_captions_and_references(self) -> None:
        article = self.extract(AHA_HTML)

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
        self.assertEqual(article.figures[0].label, "Figure 1")
        self.assertEqual(
            article.figures[0].caption_plain,
            "Complete AHA caption. P <0.05.",
        )
        self.assertEqual(
            article.figures[0].caption_markdown,
            "Complete AHA <em>caption</em>. <em>P</em> &lt;0.05.",
        )
        self.assertEqual(len(article.references), 2)
        self.assertEqual(
            article.references[0].plain_text,
            "1. A. Author. First citation. Journal 2024; 1: 1–2. "
            "DOI: https://doi.org/10.1000/first",
        )
        self.assertNotIn("Google Scholar", article.references[0].plain_text)
        self.assertEqual(
            article.references[1].plain_text,
            "2. B. Author. Second citation.",
        )
        self.assertEqual(
            [block.plain_text for block in article.front_matter],
            [
                "Affiliations: Institute One.",
                "Correspondence: Correspondence to A. Author. E-mail a@example.org",
                "Published in: Hypertension; volume 52; issue 1; 1 July 2008; pages 86–92; PubMed 18519843",
                "Copyright: © 2008.",
                "Publication history: Received 29 February 2008; Revision received 26 March 2008; Accepted 2 May 2008; Published online 2 June 2008; Published in print 1 July 2008",
                "Keywords: basic science; polyamide",
                "Subjects: Gene Therapy; Restenosis",
            ],
        )

    def test_noncontiguous_bibliography_ids_fail_closed(self) -> None:
        article = self.extract(AHA_HTML.replace("R2-112797", "R3-112797"))

        self.assertFalse(any(section.heading == "Introduction" for section in article.sections))
        self.assertEqual(article.references, [])

    def test_unknown_reference_control_fails_closed(self) -> None:
        article = self.extract(AHA_HTML.replace(">Google Scholar</a>", ">Download RIS</a>"))

        self.assertEqual(article.references, [])


if __name__ == "__main__":
    unittest.main()
