import base64
import unittest
from lxml import html
from scripts.extraction.html_extractor import _preserve_acs_raster_formulas


class AcsTruncatedFormulaTests(unittest.TestCase):
    def source(self, doi='example', wrapper='examplee00001'):
        return html.fromstring(
            '<html><body><div id="ContentColumn"><h1 id="aria123">Title</h1>'
            f'<a href="https://doi.org/10.1021/{doi}">DOI</a>'
            '<div id="ContentTab"><h2 id="1">Methods</h2>'
            '<div id="1-content">Before '
            f'<div content-id="{wrapper}"><div id="jumplink-{wrapper}">'
            '<div id="examplee00001.gif"><a><img path-from-xml="examplee00001.gif" '
            'alt="Formula. Refer to the image caption for details." '
            'src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">'
            '</a></div></div></div> After [Truncated]</div></div></div></body></html>'
        )

    def test_equation_pixels_survive_missing_terminal_bibliography(self):
        root=self.source()
        assets,warnings=_preserve_acs_raster_formulas(root,'source.html')
        self.assertEqual(len(assets),1)
        self.assertEqual(assets[0].data,base64.b64decode('R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=='))
        self.assertEqual(warnings[0]['code'],'unresolved_raster_formula')
        self.assertIn('Before',root.text_content())
        self.assertIn('After [Truncated]',root.text_content())

    def test_unrelated_doi_and_mismatched_wrapper_do_not_authenticate(self):
        for root in (self.source(doi='other'),self.source(wrapper='othere00001')):
            self.assertEqual(_preserve_acs_raster_formulas(root,'source.html'),([],[]))

    def test_semantic_alternative_preserves_pixels_and_one_equation(self):
        from scripts.extraction.html_extractor import _display_equation_containers, plain_text
        root = self.source()
        wrapper = root.xpath('//div[@id="examplee00001.gif"]')[0]
        wrapper.append(html.fromstring('<math display="block"><mi>θ</mi><mo>=</mo><mn>1</mn></math>'))
        assets, warnings = _preserve_acs_raster_formulas(root, 'source.html')
        self.assertEqual(len(assets), 1)
        self.assertEqual(warnings, [])
        equations = _display_equation_containers(root)
        self.assertEqual(len(equations), 1)
        self.assertEqual(plain_text(equations[0]), 'θ = 1')
        self.assertNotIn('Unresolved', root.text_content())

    def test_empty_or_non_display_alternative_is_not_accepted(self):
        for markup in ('<math display="block"></math>', '<math><mi>x</mi></math>'):
            root = self.source()
            root.xpath('//div[@id="examplee00001.gif"]')[0].append(html.fromstring(markup))
            self.assertEqual(_preserve_acs_raster_formulas(root, 'source.html'), ([], []))

if __name__=='__main__':
    unittest.main()
