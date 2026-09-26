import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from scripts.extraction.html_extractor import extract_html

PIXEL = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=='

def source(doi='https://doi.org/10.1021/jacs.example'):
    return f'''<html><body><div id="main"><section><h1 id="aria123">Communication</h1>
    <a href="{doi}">DOI</a><div id="ContentTab"><div>
    <div id="10-content"><p>First authored paragraph.</p></div>
    <div id="11-content"><figure id="figure-1"><img src="{PIXEL}"/><p>Figure 1. Caption.</p></figure></div>
    <div id="12-content"><p>Second authored paragraph with K<sub>a</sub>.</p></div>
    <h2 id="20">Acknowledgments</h2><div><div id="21-content"><p>Support.</p></div></div>
    <h2 id="30">References</h2><div id="30-content"><p>1. Reference.</p></div>
    </div></div></section></div></body></html>'''

class AcsUnheadedCommunicationTests(unittest.TestCase):
    def extract(self, text):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'article.html'
            path.write_text(text, encoding='utf-8')
            return extract_html(path, 'html/article.html')

    def test_no_abstract_communication_retains_opening_and_continuation(self):
        result = self.extract(source())
        self.assertEqual([s.heading for s in result.sections], ['Main text', 'Acknowledgments'])
        self.assertEqual([b.plain_text for b in result.sections[0].blocks],
                         ['First authored paragraph.', 'Second authored paragraph with K_{a}.'])
        self.assertEqual([b.plain_text for b in result.sections[1].blocks], ['Support.'])

    def test_unrelated_numeric_page_does_not_gain_article_prose(self):
        result = self.extract(source('https://example.org/other'))
        self.assertNotIn('Main text', [s.heading for s in result.sections])
