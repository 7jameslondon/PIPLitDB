import unittest
from lxml import html
from scripts.extraction.html_extractor import _oup_silverchair_reference_blocks


class MinimalCrossrefToolbarTests(unittest.TestCase):
    def fixture(self, crossref='https://doi.org/10.1000/example', extra=''):
        return html.fromstring(f'''<div><h2>References</h2><div><div content-id="B1"><div><span><a name="jumplink-B1" aria-label="jumplink-B1"></a></span></div><div><div id="ref-auto-B1"><span>1.</span><div>Author. Complete citation.<div><a href="https://scholar.google.com/scholar_lookup?title=Example">Google Scholar</a><div><a href="{crossref}">Crossref</a></div>{extra}</div></div></div></div></div></div></div>''').xpath('./h2')[0]

    def test_complete_two_link_toolbar_preserves_citation(self):
        refs = _oup_silverchair_reference_blocks(self.fixture(), 'fixture')
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].plain_text, '1. Author. Complete citation. DOI: https://doi.org/10.1000/example')

    def test_unrecognized_crossref_url_is_not_discarded(self):
        self.assertIsNone(_oup_silverchair_reference_blocks(self.fixture('https://example.org/unknown'), 'fixture'))

    def test_authored_toolbar_residue_blocks_acceptance(self):
        self.assertIsNone(_oup_silverchair_reference_blocks(self.fixture(extra='Additional text'), 'fixture'))

    def test_software_citation_without_toolbar(self):
        heading = self.fixture()
        body = heading.getnext().xpath('.//*[@id="ref-auto-B1"]/div')[0]
        body.clear()
        for part in html.fragments_fromstring('<p><span><span></span></span></p><div>Author</div> <div>A.B.</div>, <span><div>Other</div> <div>C.D.</div></span>et al. <div>Package 2000</div>. <div>2000</div>.<p></p>'):
            body.append(part)
        refs = _oup_silverchair_reference_blocks(heading, 'fixture')
        self.assertEqual(len(refs), 1)
        self.assertIn('Package 2000. 2000.', refs[0].plain_text)
