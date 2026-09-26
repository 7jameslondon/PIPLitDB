import tempfile
import unittest
from pathlib import Path
from scripts.extraction.html_extractor import extract_html
from tests.test_pnas_html_extractor import PNAS_HTML


class PnasNestedDisplayCardsTests(unittest.TestCase):
    def test_nested_figure_and_table_leave_prose_and_tail_once(self):
        text = PNAS_HTML.replace('A result.</div>', 'A result.')
        text = text.replace('<figure id="sfig01">', ' Tail <i>prose</i>.</div><figure id="sfig01">')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'main.html'
            path.write_text(text, encoding='utf-8')
            article = extract_html(path, 'main.html')
        result = next(section for section in article.sections if section.heading == 'Results')
        self.assertEqual([block.plain_text for block in result.blocks], ['A result. Tail prose.'])
        self.assertEqual(len(article.figures), 1)
        self.assertEqual(len(article.tables), 1)
        self.assertIn('complete PNAS caption', article.figures[0].caption_plain)
        self.assertEqual(article.tables[0].parts[0].rows[1][1].text, '10')

    def test_legacy_spacer_paragraph_footnote_keeps_title_and_note(self):
        text = PNAS_HTML.replace('<div id="t01">', '<figure id="T1">')
        text = text.replace('<div><table><thead>', '<div><div tabindex="0"><table><thead>')
        text = text.replace('</table></div>\n        </div>',
                            '</table></div></div><div></div><div>'
                            '<p role="doc-footnote"><i>T</i><sub>m</sub> values in °C.</p>'
                            '</div></figure>')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'main.html'
            path.write_text(text, encoding='utf-8')
            article = extract_html(path, 'main.html')
        self.assertEqual(article.tables[0].title_plain,
                         'Table 1. Inhibition concentration (K_{i}, nM)')
        self.assertEqual(article.tables[0].footnotes_plain, ['T_{m} values in °C.'])

    def test_article_note_keeps_source_inline_scientific_markup(self):
        text = PNAS_HTML.replace('</article>', '<section id="tab-information">'
                                '<section><div>Notes</div><div role="doc-footnote">'
                                '<sup>*</sup>Coordinates and <i>gene</i> note.'
                                '</div></section></section></article>')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'main.html'
            path.write_text(text, encoding='utf-8')
            article = extract_html(path, 'main.html')
        note = next(block for block in article.front_matter
                    if block.plain_text.startswith('Article note:'))
        self.assertIn(r'<sup>\*</sup>', note.markdown)
        self.assertIn('<em>gene</em>', note.markdown)
