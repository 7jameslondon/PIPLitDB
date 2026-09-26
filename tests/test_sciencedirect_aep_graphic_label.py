import base64
import unittest
from tests import test_sciencedirect_html_extractor as helpers


class AepGraphicLabelTests(unittest.TestCase):
    extract = helpers.ScienceDirectHtmlExtractionTests.extract

    def test_graphic_label_is_not_abstract_prose_but_artwork_is_retained(self):
        image = base64.b64encode(helpers.PNG_BYTES).decode('ascii')
        article = self.extract(f'''<article><h1>Example</h1><div id="abstracts">
          <div id="aep-abstract-id1"><h2>Abstract</h2>
            <div id="aep-abstract-sec-id2"><div>Authored summary.</div></div></div>
          <div id="aep-abstract-id3"><div id="aep-abstract-sec-id4"><div>Graphic</div></div>
            <figure id="aep-figure-id5"><img src="data:image/png;base64,{image}"></figure></div>
          </div><div id="body"><section id="aep-section-id6"><h2>Results</h2>
          <div>Graphic</div><div>Graphic detail is authored prose.</div></section></div></article>''')
        abstract = next(s for s in article.sections if s.heading == 'Abstract')
        self.assertEqual([b.plain_text for b in abstract.blocks], ['Authored summary.'])
        results = next(s for s in article.sections if s.heading == 'Results')
        self.assertEqual([b.plain_text for b in results.blocks], ['Graphic', 'Graphic detail is authored prose.'])
        self.assertEqual(len(article.figures), 1)

    def test_verbatim_abstract_fallback_beside_graphical_art_is_not_duplicated(self):
        image = base64.b64encode(helpers.PNG_BYTES).decode('ascii')
        article = self.extract(f'''<article><h1>Example</h1><div id="abstracts">
          <div id="aep-abstract-id1"><h2>Abstract</h2>
            <div id="aep-abstract-sec-id2"><div>Authored <strong>summary</strong>.</div></div></div>
          <div id="aep-abstract-id3">
            <div id="aep-abstract-sec-id4"><div>Authored <strong>summary</strong>.</div></div>
            <figure id="aep-figure-id5"><img src="data:image/png;base64,{image}"></figure></div>
          </div><div id="body"><section id="aep-section-id6"><h2>Results</h2>
          <div>Result prose.</div></section></div></article>''')
        abstract = next(s for s in article.sections if s.heading == 'Abstract')
        self.assertEqual([b.plain_text for b in abstract.blocks], ['Authored summary.'])
        self.assertEqual(len(article.figures), 1)

    def test_alphabetic_affiliation_markers_preserve_author_assignments(self):
        article = self.extract('''<article><h1 id="screen-reader-main-title">Example</h1>
          <div id="author-group"><span><span>Ada Example</span><span id="bAFFA"><sup>a</sup></span></span>
          <a><span>Bea Sample</span><span id="bAFFB"><sup>b</sup></span></a></div>
          <div id="body"><section><h2>Results</h2><p>Body.</p></section></div></article>''')
        self.assertIn('Affiliation assignments: Ada Example (a); Bea Sample (b)',
                      [b.plain_text for b in article.front_matter])


if __name__ == '__main__':
    unittest.main()
