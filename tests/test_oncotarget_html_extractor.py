import base64
import tempfile
import unittest
from pathlib import Path

from scripts.extraction.html_extractor import extract_html


_PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8"
    b"\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")


class OncotargetHtmlExtractorTests(unittest.TestCase):
    def _extract(self, body: str):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.html"
            source.write_text(body, encoding="utf-8")
            return extract_html(source, "papers (private)/99999/html/main.html")

    def test_exact_flat_snapshot_recovers_article_and_references(self):
        extracted = self._extract(
            f"""
            <html><body><div id="content">
              <div><h1>Exact archived title</h1></div>
              <div><a href="https://doi.org/10.18632/oncotarget.12345">DOI</a></div>
              <div id="extendedInfo"><div id="primitiveHeader"><p>Author<span id="A1">1</span></p></div></div>
              <p>ABSTRACT</p><p>Abstract text with H<sub>3</sub>K4me2.</p>
              <div><div>
                <p>INTRODUCTION</p><p>Introduction prose.</p>
                <p>RESULTS</p><p>Study design</p><p>Results prose.</p>
                <a><figure id="figure-1"><div><img alt="Figure 1" src="data:image/png;base64,{_PNG}"></div>
                  <figcaption>Figure 1: Complete caption.</figcaption></figure></a>
                <p>DISCUSSION</p><p>Discussion prose.</p>
                <p>MATERIALS AND METHODS</p><p>Cell culture</p><p>Methods prose.</p>
                <p>REFERENCES</p><p id="R1">1. Example A. Example article.</p>
              </div></div>
              <div id="trendmd-suggestions"><p>Recommended article noise.</p></div>
            </div></body></html>
            """
        )

        self.assertEqual(extracted.title, "Exact archived title")
        self.assertEqual(
            [section.heading for section in extracted.sections],
            [
                "ABSTRACT",
                "INTRODUCTION",
                "RESULTS",
                "Study design",
                "DISCUSSION",
                "MATERIALS AND METHODS",
                "Cell culture",
            ],
        )
        self.assertEqual(len(extracted.figures), 1)
        self.assertEqual(
            [block.plain_text for block in extracted.front_matter], ["Author1"]
        )
        self.assertEqual(
            [reference.plain_text for reference in extracted.references],
            ["1. Example A. Example article."],
        )
        combined = " ".join(
            block.plain_text
            for section in extracted.sections
            for block in section.blocks
        )
        self.assertIn("Abstract text with H_{3}K4me2.", combined)
        self.assertNotIn("Recommended article noise", combined)

    def test_near_miss_without_oncotarget_doi_is_not_promoted(self):
        extracted = self._extract(
            f"""
            <html><body><div id="content">
              <div><h1>Other page</h1></div>
              <p>ABSTRACT</p><p>Abstract prose.</p>
              <div><div><p>INTRODUCTION</p><p>Introduction prose.</p>
                <p>RESULTS</p><p>Results prose.</p>
                <a><figure id="figure-1"><img alt="Figure 1" src="data:image/png;base64,{_PNG}"></figure></a>
                <p>DISCUSSION</p><p>Discussion prose.</p>
                <p>MATERIALS AND METHODS</p><p>Methods prose.</p>
                <p>REFERENCES</p><p id="R1">1. Reference.</p>
              </div></div>
            </div></body></html>
            """
        )
        self.assertEqual(extracted.sections, [])
        self.assertEqual(extracted.references, [])


if __name__ == "__main__":
    unittest.main()
