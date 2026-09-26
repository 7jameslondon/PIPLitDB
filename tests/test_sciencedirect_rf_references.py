import sys
import unittest
from pathlib import Path

from lxml import html

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from extraction.html_extractor import _sciencedirect_reference


class ScienceDirectRFReferencesTests(unittest.TestCase):
    def test_reference_id_wrapper_and_matching_title_field(self):
        item = html.fromstring('''<li><span><a id="ref-id-BIB7" href="#bBIB7">Author et al 1999</a></span>
        <span id="reference0035"><div><div>A. Author</div><div id="ref-id-reference0035">Scientific <em>title</em></div></div>
        <div>Journal, 3 (1999), pp. 1-2</div><div lang="en"><a href="https://doi.org/10.1000/example">Crossref</a><a href="https://scholar.google.com/">Google Scholar</a></div></span></li>''')
        rich, plain = _sciencedirect_reference(item)
        self.assertEqual(plain, 'Author et al 1999. A. Author Scientific title Journal, 3 (1999), pp. 1-2 DOI: https://doi.org/10.1000/example')
        self.assertIn('<em>title</em>', rich)
        self.assertNotIn('Google Scholar', plain)
        item.xpath('.//div[@id]')[0].set('id', 'ref-id-reference0040')
        self.assertIsNone(_sciencedirect_reference(item))

    def test_author_year_bib_label_with_matching_backref(self):
        item = html.fromstring('''<li><span><a id="ref-id-BIB79" href="#bBIB79">Author et al 1999</a></span>
        <span><div><div>A. Author</div><div>Scientific title</div></div>
        <div>Journal, 3 (1999), pp. 1-2</div><div lang="en"><a href="https://scholar.google.com/">Google Scholar</a></div></span></li>''')
        self.assertEqual(_sciencedirect_reference(item)[1], 'Author et al 1999. A. Author Scientific title Journal, 3 (1999), pp. 1-2')
        item.xpath('.//a[@id]')[0].set('href', '#bBIB80')
        self.assertIsNone(_sciencedirect_reference(item))

    def test_matching_rf_number_preserves_fields_removes_controls(self):
        item = html.fromstring('''<li><span><a id="ref-id-RF2" href="#bRF2">2</a></span>
        <span><div><div>A. Author</div><div>Title with <em>italics</em></div></div>
        <div>Journal, 3 (2001), pp. 1-2</div><div lang="en"><a href="https://scholar.google.com/">Google Scholar</a></div></span></li>''')
        rendered, visible = _sciencedirect_reference(item)
        self.assertEqual(visible, '2. A. Author Title with italics Journal, 3 (2001), pp. 1-2')
        self.assertIn('<em>italics</em>', rendered)
        self.assertNotIn('Google Scholar', rendered)

    def test_mismatched_rf_target_does_not_authorize(self):
        item = html.fromstring('<li><a id="ref-id-RF2" href="#bRF3">2</a><span><div>Source note</div></span></li>')
        self.assertIsNone(_sciencedirect_reference(item))

    def test_mismatched_rf_number_does_not_authorize(self):
        item = html.fromstring('<li><a id="ref-id-RF2" href="#bRF2">3</a><span><div>Source note</div></span></li>')
        self.assertIsNone(_sciencedirect_reference(item))
