import unittest
from lxml import html
from scripts.extraction.html_extractor import _preserve_acs_raster_formulas


class AcsDivRasterFormulaTests(unittest.TestCase):
    def source(self, region='123-content', doi='example', container='div'):
        return html.fromstring(
            '<html><body><div id="ContentColumn"><h1 id="aria123">Title</h1>'
            f'<a href="https://doi.org/10.1021/{doi}">DOI</a>'
            f'<div id="ContentTab"><div id="{region}"><{container}>Before '
            '<span><img path-from-xml="examplee30002.gif" alt="" '
            'src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">'
            f'</span> After</{container}></div></div></div></body></html>'
        )

    def test_div_prose_formula_is_retained_and_flagged_without_guessing(self):
        root = self.source()
        assets, warnings = _preserve_acs_raster_formulas(root, 'source.html')
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].category, 'equation')
        self.assertEqual(warnings[0]['code'], 'unresolved_raster_formula')
        self.assertIn('Before', root.text_content())
        self.assertIn('After', root.text_content())
        self.assertIn('[Unresolved formula image: acs_formula_001]', root.text_content())

    def test_unrelated_regions_doi_and_figure_do_not_match(self):
        for root in (self.source(region='toolbar'), self.source(doi='other'),
                     self.source(container='figure'), self.source(container='table')):
            self.assertEqual(_preserve_acs_raster_formulas(root, 'source.html'), ([], []))
