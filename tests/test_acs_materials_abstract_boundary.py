import unittest

from tests import test_extraction_pipeline


class ACSMaterialsAbstractBoundaryTests(unittest.TestCase):
    extract = test_extraction_pipeline.HtmlExtractionTests.extract
    def test_materials_and_methods_separates_unheaded_main_text(self):
        result = self.extract('''<html><body><section><div>
<h1>Archived ACS article</h1>
<a href="https://doi.org/10.1021/example">DOI</a>
<h2 id="100">Abstract</h2><div><div id="100-content">
<section aria-label="Main abstract"><p>Authored abstract.</p></section>
</div></div><div><div id="101-content"><p>Authored introduction.</p></div></div>
<h2 id="102">MATERIALS AND METHODS</h2><div><p>Authored method.</p></div>
</div></section></body></html>''')
        self.assertEqual([(s.heading, [b.plain_text for b in s.blocks]) for s in result.sections],
                         [('Abstract', ['Authored abstract.']), ('Main text', ['Authored introduction.']),
                          ('MATERIALS AND METHODS', ['Authored method.'])])


if __name__ == '__main__':
    unittest.main()
