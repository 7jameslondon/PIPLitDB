import base64
import unittest
from tests import test_sciencedirect_html_extractor as helpers


class AepAbstractH3Tests(unittest.TestCase):
    extract = helpers.ScienceDirectHtmlExtractionTests.extract

    def test_h3_graphical_abstract_and_numbered_highlight_prose(self):
        image = base64.b64encode(helpers.PNG_BYTES).decode('ascii')
        article = self.extract(f'''<article><h1>Example</h1>
        <div id="abspara0001">Navigation.</div>
        <div id="abstracts"><div id="aep-abstract-id1"><h2>Abstract</h2>
        <div id="aep-abstract-sec-id2"><div id="abspara0010">Authored abstract.</div></div>
        <div id="aep-abstract-sec-id3"><h3>Graphical abstract</h3>
        <div id="abspara0015"><span><figure><img src="data:image/png;base64,{image}"></figure></span></div></div>
        <div id="aep-abstract-sec-id4"><h3>Highlights</h3><div id="abspara0020">▶ First finding. ▶ Second finding.</div></div>
        </div></div><div id="body"><section><h2>Results</h2><p>Body.</p></section></div></article>''')
        text = [b.plain_text for s in article.sections for b in s.blocks]
        self.assertIn('Authored abstract.', text)
        self.assertIn('▶ First finding. ▶ Second finding.', text)
        self.assertNotIn('Navigation.', text)
        self.assertEqual(len(article.figures), 1)
        self.assertEqual(article.figures[0].kind, 'graphical_abstract')

    def test_tspara_title_is_not_a_table_footnote(self):
        article = self.extract('''<article><h1>Example</h1><div id="body"><section><h2>Results</h2>
        <div id="tbl1"><span><span><p id="tspara0010"><span>Table 1</span>. Measured constants.</p></span></span>
        <div><table><tr><th>Sample</th><th>Value</th></tr><tr><td>A</td><td>2</td></tr></table></div></div>
        </section></div></article>''')
        self.assertEqual(article.tables[0].title_plain, 'Table 1. Measured constants.')
        self.assertFalse(article.tables[0].footnotes_plain)

    def test_word_symbol_plus_minus_is_font_gated(self):
        from scripts.extraction.docx_supplement import _decode_run_text
        self.assertEqual(_decode_run_text('2 \uf0b1 1', 'Symbol'), '2 ± 1')
        self.assertEqual(_decode_run_text('2 \uf0b1 1', 'Arial'), '2 \uf0b1 1')

    def test_legacy_attachment_control_is_removed_but_description_remains(self):
        article = self.extract('''<article><h1>Example</h1><div id="body"><section><h2>Appendix A. Supplementary data</h2>
        <div id="p0010">Authored description.<span><span id="ec1"><span><a download="" title="Download Word document (2MB)" href="https://ars.els-cdn.com/content/image/1-s2.0-EXAMPLE-mmc1.doc">Download: Download Word document (2MB)</a></span></span></span></div>
        </section></div></article>''')
        self.assertIn('Authored description.', [b.plain_text for s in article.sections for b in s.blocks])

    def test_publisher_summary_fsabs_and_chapter_navigation(self):
        article = self.extract('''<article><h1 id="screen-reader-main-title">Example</h1>
        <div id="article-identifier-links"><a href="https://doi.org/10.1016/example">DOI</a></div>
        <div id="abstracts"><div id="aep-abstract-id3"><h2>Publisher Summary</h2>
        <div id="aep-abstract-sec-id4"><div id="fsabs022">Authored publisher summary.</div></div>
        </div></div><ul id="issue-navigation"><li><a href="/previous">Previous chapter in volume</a></li>
        <li><a href="/next">Next chapter in volume</a></li></ul></article>''')
        text = [
            block.plain_text
            for section in article.sections
            for block in section.blocks
        ]
        self.assertEqual(text, ['Authored publisher summary.'])

    def test_nested_textbox_paragraph_preserves_boundary(self):
        from xml.etree import ElementTree as ET
        from scripts.extraction.docx_supplement import _paragraph_text
        p = ET.fromstring('''<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:r><w:t>Methods.</w:t></w:r><w:r><w:pict><w:txbxContent><w:p><w:r><w:t>Positioned label.</w:t></w:r></w:p></w:txbxContent></w:pict></w:r></w:p>''')
        self.assertEqual(_paragraph_text(p)[1], 'Methods. Positioned label.')


if __name__ == '__main__':
    unittest.main()
