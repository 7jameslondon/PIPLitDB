import base64
import unittest
from tests import test_sciencedirect_html_extractor as helpers


class NavigationSidebarTests(unittest.TestCase):
    extract = helpers.ScienceDirectHtmlExtractionTests.extract

    def make(self, role='navigation', label='Table of contents', target='fig1', alt='Figure 1. Caption.', duplicate=False):
        data='data:image/png;base64,'+base64.b64encode(helpers.PNG_BYTES).decode('ascii')
        image=f'<li><a href="#{target}"><img src="{data}" alt="{alt}"></a></li>'
        return self.extract(f'''<main><article><h1>Example</h1><div role="{role}" aria-label="{label}"><h2>Figures</h2>
          <ol>{image}{image if duplicate else ''}</ol></div>
          <div id="body"><section><h2>Results</h2><p>Text.</p><figure id="fig1">
          <figcaption>Figure 1. Caption.</figcaption></figure></section></div></article></main>''')

    def test_accessible_navigation_recovers_exact_embedded_asset(self):
        result=self.make()
        self.assertEqual(len(result.embedded_assets),1)
        self.assertEqual(result.embedded_assets[0].data,helpers.PNG_BYTES)

    def test_unrelated_or_ambiguous_navigation_does_not_supply_asset(self):
        for kwargs in ({'role':'none'},{'label':'Other links'},{'target':'fig2'},
                       {'alt':'Figure 2. Different.'},{'duplicate':True}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.make(**kwargs).embedded_assets,[])
