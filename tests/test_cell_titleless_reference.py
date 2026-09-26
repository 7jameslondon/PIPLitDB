import unittest
from unittest.mock import patch
from lxml import html
from scripts.extraction.html_extractor import _cell_press_semantic_reference_blocks
from scripts.extraction.html_extractor import _sciencedirect_front_matter
from tests import test_cell_unnumbered_affiliation as fixture


class CellTitlelessReferenceTests(unittest.TestCase):
    def test_direct_published_line_is_preserved(self):
        root = fixture.CellUnnumberedAffiliationTests().document()
        panel = root.xpath('.//*[@id="core-content-info"]')[0]
        panel.append(html.fromstring('<div>Published: August 3, 2004</div>'))
        panel.append(html.fromstring('<div>Published: publisher navigation</div>'))
        rows = _sciencedirect_front_matter(root, 'main.html')
        self.assertEqual([r.plain_text for r in rows if r.plain_text.startswith('Publication date:')],
                         ['Publication date: Published: August 3, 2004'])
    def source(self, journal=True):
        citation = '<em>Collected Chemistry.</em> 2001; <strong>2</strong>:123' if journal else 'Unidentified text'
        return html.fromstring(f'''<article data-extraction-dialect="cell-press-semantic">
          <section id="references"><div id="bibliography" role="doc-bibliography"><div>
          <div><div id="BIB1"><div><div><div><a href="#body-ref-BIB1" title="View in article">1.</a></div>
          <div>A. Author</div><div>{citation}</div></div>
          <div><a href="https://example.org" target="_blank">Google Scholar</a></div>
          </div></div></div></div></div></section></article>''')

    def test_titleless_journal_entry_is_not_dropped(self):
        root = self.source()
        with patch('scripts.extraction.html_extractor._cell_press_header_details', return_value={}):
            refs = _cell_press_semantic_reference_blocks(root.xpath('.//section')[0], 'main.html')
        self.assertEqual([r.plain_text for r in refs], ['1. A. Author Collected Chemistry. 2001; 2:123'])

    def test_missing_title_and_unstructured_tail_remains_rejected(self):
        root = self.source(False)
        with patch('scripts.extraction.html_extractor._cell_press_header_details', return_value={}):
            self.assertIsNone(_cell_press_semantic_reference_blocks(root.xpath('.//section')[0], 'main.html'))

    def test_book_reference_in_label_tail_preserves_inline_notation(self):
        root = self.source()
        authored = root.xpath('.//div[a]')[0].getparent()
        for child in list(authored)[1:]:
            authored.remove(child)
        authored[0].tail = 'Author (2002). Book title (3'
        sup = html.Element('sup'); sup.text = 'rd'; sup.tail = ' edition). Publisher.'
        authored.append(sup)
        with patch('scripts.extraction.html_extractor._cell_press_header_details', return_value={}):
            refs = _cell_press_semantic_reference_blocks(root.xpath('.//section')[0], 'main.html')
        self.assertEqual(refs[0].plain_text, '1. Author (2002). Book title (3^{rd} edition). Publisher.')
        self.assertIn('<sup>rd</sup>', refs[0].markdown)
