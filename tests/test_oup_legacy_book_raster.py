import unittest
from lxml import html
from tests.test_oup_book_toolbar_orcid_math import OupBookToolbarOrcidMathTests
from tests.test_oup_legacy_modal_cards import fixture, OupLegacyModalTests
from scripts.extraction.html_extractor import _oup_silverchair_reference_blocks


class OupLegacyBookRasterTests(unittest.TestCase):
    def test_book_toolbar_without_crossref(self):
        heading = OupBookToolbarOrcidMathTests().bibliography()
        controls = heading.xpath('following-sibling::div//div[@id="ref-auto-B1"]/div/div[last()]')[0]
        controls.remove(controls[1])
        controls.remove(controls[1])
        controls.insert(2, html.fromstring('<p><a href="">OpenURL Placeholder Text</a></p>'))
        refs = _oup_silverchair_reference_blocks(heading, 'synthetic.html')
        self.assertEqual(len(refs), 1)
        self.assertIn('A book.', refs[0].plain_text)
        controls[-1][0].set('href', 'https://example.com/')
        self.assertIsNone(_oup_silverchair_reference_blocks(heading, 'synthetic.html'))

    def source(self, alternative=''):
        formula = ('<div>Binding before. <div content-id=""><div id="jumplink-">'
            '<div><div id="exampleum1.gif"><img alt="formula" '
            'src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">'
            + alternative + '</div></div></div></div> After binding.</div>')
        return fixture().replace('<p>Calculate using the following formula:</p>', formula)

    def test_unreviewed_raster_warns_and_keeps_surrounding_prose(self):
        result = OupLegacyModalTests().extract(self.source())
        self.assertTrue(any(w['code'] == 'unresolved_raster_formula' for w in result.warnings))
        blocks = [b for s in result.sections for b in s.blocks]
        self.assertIn('Binding before.', [b.plain_text for b in blocks])
        self.assertIn('After binding.', [b.plain_text for b in blocks])
        self.assertTrue(any(a.asset_id == 'oup_formula_001' for a in result.embedded_assets))

    def test_reviewed_math_and_original_pixels_survive(self):
        result = OupLegacyModalTests().extract(self.source('<math display="block"><mi>x</mi><mo>=</mo><mn>2</mn></math>'))
        blocks = [b for s in result.sections for b in s.blocks]
        self.assertEqual(sum(b.kind == 'equation' and b.plain_text == 'x = 2' for b in blocks), 1)
        self.assertFalse(any(w['code'] == 'unresolved_raster_formula' for w in result.warnings))
        self.assertTrue(any(a.asset_id == 'oup_formula_001' for a in result.embedded_assets))

    def test_unrecognized_image_is_not_promoted(self):
        result = OupLegacyModalTests().extract(self.source().replace('alt="formula"', 'alt="portrait"'))
        self.assertFalse(any(a.asset_id.startswith('oup_formula') for a in result.embedded_assets))
