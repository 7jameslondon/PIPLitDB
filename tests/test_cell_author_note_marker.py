import unittest
from lxml import html
from scripts.extraction.html_extractor import _sciencedirect_front_matter
from scripts.extraction.html_extractor import _normalize_wiley_molar_unit_spans
from scripts.extraction.record_json import _block
from tests.test_cell_unnumbered_affiliation import CellUnnumberedAffiliationTests

class CellAuthorNoteMarkerTests(unittest.TestCase):
    def test_semantic_molarity_inverse_and_table_units(self):
        doc = CellUnnumberedAffiliationTests().document()
        body = doc.xpath('.//section[@id="sec-1"]')[0]
        body.append(html.fromstring('<div><p>10<sup>9</sup><span>m</span><sup>−1</sup>; 7<span>m</span> urea; 5 m<span>m</span>CaCl2.</p><table><tr><td><i>μ<span>m</span></i></td></tr></table><p>Variable <span>m</span>.</p></div>'))
        _normalize_wiley_molar_unit_spans(doc)
        self.assertEqual([n.text for n in body.xpath('.//span[not(@*) and not(*)]')], ['M','M','M','M','m'])
    def test_literal_double_star_note_marker_survives_canonical_rendering(self):
        doc = CellUnnumberedAffiliationTests().document()
        notes = doc.xpath('.//details[@id="core-affiliations-notes"]')[0]
        notes.append(html.fromstring('<div><div role="doc-footnote"><span>**</span><div>Contact: author@example.test.</div></div></div>'))
        blocks = _sciencedirect_front_matter(doc, 'fixture.html')
        result = [_block(b) for b in blocks if b.plain_text.startswith('Author note **:')]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['content']['plain_text'], 'Author note **: Contact: author@example.test.')
        self.assertIn('<strong>Author note **:</strong>', result[0]['content']['html'])

if __name__ == '__main__':
    unittest.main()
