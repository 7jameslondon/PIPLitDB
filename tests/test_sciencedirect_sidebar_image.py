import base64
import unittest
from tests import test_sciencedirect_html_extractor as helpers


class SidebarImageTests(unittest.TestCase):
    extract = helpers.ScienceDirectHtmlExtractionTests.extract

    def make(self, target='FIG1', alt='Figure 1. Authored caption.', inline=False, duplicate=False):
        data='data:image/png;base64,'+base64.b64encode(helpers.PNG_BYTES).decode('ascii')
        image=f'<a href="#{target}"><img alt="{alt}" src="{data}"></a>'
        return self.extract(f'''<div id="toc-figures">{image}{image if duplicate else ''}</div>
          <article><h1>Example</h1><div id="body"><section><h2>Results</h2><p>Text.</p>
          <figure id="FIG1">{'<img src="'+data+'">' if inline else ''}
          <figcaption>Figure 1. Authored caption.</figcaption></figure></section></div></article>''')

    def test_exact_sidebar_image_is_retained_with_original_bytes(self):
        result=self.make()
        self.assertEqual(len(result.embedded_assets),1)
        self.assertEqual(result.embedded_assets[0].data,helpers.PNG_BYTES)
        self.assertIn('div/a/img', result.embedded_assets[0].source_locator)

    def test_mismatched_ambiguous_or_unlabelled_links_are_not_assigned(self):
        for kwargs in ({'target':'FIG2'},{'alt':'Figure 2. Other.'},{'duplicate':True}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.make(**kwargs).embedded_assets,[])

    def test_inline_asset_remains_authoritative_and_is_not_duplicated(self):
        result=self.make(inline=True)
        self.assertEqual(len(result.embedded_assets),1)
        self.assertIn('/figure/img',result.embedded_assets[0].source_locator)

    def test_mixed_aep_legacy_sections_retain_all_paragraphs(self):
        result=self.extract('''<article><h1>Example</h1><div id="body">
          <section id="aep-section-id1"><h2>Results</h2><div>Result text.</div></section>
          <section id="SEC9"><h2>Discussion</h2><div>First interpretation.</div>
          <div>Second <em>interpretation</em>.</div></section></div></article>''')
        section=next(s for s in result.sections if s.heading=='Discussion')
        self.assertEqual([b.plain_text for b in section.blocks],['First interpretation.','Second interpretation.'])

    def test_generic_legacy_section_does_not_trigger_aep_rule(self):
        result=self.extract('''<article><h1>Example</h1><div id="body">
          <section id="SEC9"><h2>Discussion</h2><div>Unclassified div.</div></section></div></article>''')
        self.assertEqual(next(s for s in result.sections if s.heading=='Discussion').blocks,[])

if __name__=='__main__':
    unittest.main()
