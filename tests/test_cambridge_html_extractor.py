import unittest

from lxml import html

from scripts.extraction.html_extractor import (
    _cambridge_reference_blocks,
    _figure_items,
    _normalize_cambridge_snapshot,
)


PIXEL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


class CambridgeHtmlExtractorTests(unittest.TestCase):
    def test_figures_and_reference_cards_are_restored_semantically(self) -> None:
        document = html.fromstring(
            f"""
            <html><body>
              <a href="https://doi.org/10.1017/S0000000000000001">DOI</a>
              <div><h2>Keywords</h2><div>
                <a href="/core/search?filters[keywords]=first"><span>first keyword</span></a>
                <a href="/core/search?filters[keywords]=second"><span>second keyword</span></a>
              </div></div>
              <p>Citation (Author <a href="#ref1"><span>Reference Author</span>2020</a>).</p>
              <p>DOI <a href="https://doi.org/10.1017/S0000000000000001"><span>https://doi.org/10.1017/S0000000000000001</span><span>[Opens in a new window]</span></a></p>
              <div id="fig01"><div><span>Fig. 1.</span><p>First caption.</p></div>
                <div><img src="{PIXEL}" alt="Figure 0"/></div></div>
              <div id="sec2"><h3>Second result</h3><p>Result text.</p>
                <div id="fig02"><div><span>Fig. 2.</span><p>Second caption.</p></div>
                  <div></div></div></div>
              <div id="sec2"><h3>Second result</h3><p>Result text.</p>
                <div id="fig02"><div><span>Fig. 2.</span><p>Second caption.</p></div>
                  <div><img src="{PIXEL}" alt="Figure 0"/></div></div></div>
              <div id="references-list"><h2>References</h2>
                <div id="manual_ref-1"><div><div id="reference-1-content">
                  <span>Author A.</span> (2020). First reference.
                  <a target="_blank" aria-label="CrossRef link for First reference"
                     href="https://dx.doi.org/10.1000/one">CrossRef</a>
                  <a target="_blank" aria-label="Google Scholar link for First reference"
                     href="https://scholar.google.com/example">Google Scholar</a>
                </div></div></div>
                <div id="manual_ref-2"><div><div id="reference-2-content">
                  <span>Author B.</span> (2021). Second reference.
                  <a target="_blank" aria-label="PubMed link for Second reference"
                     href="https://www.ncbi.nlm.nih.gov/pubmed/1">PubMed</a>
                </div></div></div>
              </div>
            </body></html>
            """
        )

        _normalize_cambridge_snapshot(document)
        figures = _figure_items(document, "article.html")
        references = _cambridge_reference_blocks(document, "article.html")

        self.assertEqual([item.figure_id for item in figures], ["figure_001", "figure_002"])
        self.assertEqual([item.label for item in figures], ["Figure 1", "Figure 2"])
        self.assertEqual([item.caption_plain for item in figures], ["First caption.", "Second caption."])
        self.assertEqual(len(document.xpath('.//div[@id="sec2"]')), 1)
        self.assertEqual(len(document.xpath('.//h3[normalize-space()="Second result"]')), 1)
        self.assertEqual(len(document.xpath('.//p[normalize-space()="Result text."]')), 1)
        self.assertEqual(
            document.xpath('normalize-space(.//h2[normalize-space()="Keywords"]/following-sibling::p[1])'),
            "first keyword; second keyword",
        )
        self.assertEqual(
            document.xpath('normalize-space(.//p[starts-with(normalize-space(),"Citation")])'),
            "Citation (Author 2020).",
        )
        self.assertNotIn("Opens in a new window", document.text_content())
        self.assertIsNotNone(references)
        self.assertEqual(len(references or []), 2)
        self.assertEqual(
            references[0].plain_text,
            "1. Author A. (2020). First reference. DOI: https://doi.org/10.1000/one",
        )
        self.assertNotIn("Google Scholar", references[0].plain_text)
        self.assertEqual(references[1].plain_text, "2. Author B. (2021). Second reference.")


if __name__ == "__main__":
    unittest.main()
