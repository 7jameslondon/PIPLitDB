import base64
import unittest
from tests import test_sciencedirect_html_extractor as helpers


class LegacyParaFigureTests(unittest.TestCase):
    extract = helpers.ScienceDirectHtmlExtractionTests.extract

    def source(self, doi='10.1016/j.example.2002.1'):
        image = base64.b64encode(helpers.PNG_BYTES).decode('ascii')
        card = f'<span><img src="data:image/png;base64,{image}"><ol><li><a download="" title="Download full-size image" href="https://ars.els-cdn.com/content/image/example.gif">Download: Download full-size image</a></li></ol></span>'
        return f'''<article><h1 id="screen-reader-main-title">Example</h1>
        <div id="article-identifier-links"><a href="https://doi.org/{doi}">DOI</a></div>
        <div id="abstracts"><div id="aep-abstract-id1"><h2>Abstract</h2><div id="aep-abstract-sec-id2"><div id="simple-para1">Abstract text.</div></div></div>
        <div id="aep-abstract-id3"><div id="aep-abstract-sec-id4"><div id="simple-para2">Graphical description.</div></div><figure id="figure0010">{card}</figure></div></div>
        <div id="body"><div><div><div id="para0005">Opening prose.<span><figure id="FIGFX1">{card}<span><p><span>1</span>. </p></span></figure> After artwork.</span></div></div>
        <div id="para0010">Second paragraph.</div><section id="section0005"><h2>Results</h2><div id="para0015">Before artwork.<span><figure id="figure0015">{card}</figure></span> After artwork.</div></section></div></div>
        <section><h2>References</h2><ol><li><a id="ref-id-BIB1">1</a><span>First reference.</span></li><li><a id="ref-id-BIB2">2</a><span>Second reference.</span></li></ol></section></article>'''

    def test_prose_boundaries_and_neutral_artwork_labels(self):
        article = self.extract(self.source())
        self.assertEqual([s.heading for s in article.sections], ['Abstract', 'Graphical abstract', 'Main text', 'Results'])
        self.assertEqual([b.plain_text for b in article.sections[2].blocks], ['Opening prose. After artwork.', 'Second paragraph.'])
        self.assertEqual([b.plain_text for b in article.sections[3].blocks], ['Before artwork. After artwork.'])
        self.assertEqual([(f.kind, f.label) for f in article.figures], [('graphical_abstract', 'Graphical Abstract'), ('figure', '1'), ('figure', 'Unnumbered artwork 2')])
        self.assertEqual(len(article.embedded_assets), 3)

    def test_unrelated_doi_does_not_normalize(self):
        from lxml import html
        from scripts.extraction.html_extractor import _normalize_sciencedirect_legacy_para_figures
        doc = html.fromstring(self.source('10.1234/other'))
        before = html.tostring(doc)
        _normalize_sciencedirect_legacy_para_figures(doc)
        self.assertEqual(html.tostring(doc), before)
