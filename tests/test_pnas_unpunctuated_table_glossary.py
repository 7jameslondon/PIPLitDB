from pathlib import Path
import tempfile
import unittest

from scripts.extraction.html_extractor import extract_html
from tests.test_pnas_html_extractor import PNAS_HTML


class PnasUnpunctuatedTableGlossaryTests(unittest.TestCase):
    def test_unpunctuated_table_header_preserves_caption_and_note(self):
        source = PNAS_HTML.replace('Table 1.</div>', 'Table 1</div>')
        source = source.replace('<div id="t01">', '<figure id="T1">')
        source = source.replace('<div><table><thead>', '<div><div tabindex="0"><table><thead>')
        source = source.replace(
            '</table></div>\n        </div>\n      </div>\n      <figure id="sfig01">',
            '</table></div></div><div></div>'
            '<div><div role="doc-footnote">Values are means with uncertainty.</div></div>'
            '</figure>\n      </div>\n      <figure id="sfig01">',
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.html'
            path.write_text(source, encoding='utf-8')
            article = extract_html(path, 'source.html')
        self.assertEqual(article.tables[0].title_plain, 'Table 1. Inhibition concentration (K_{i}, nM)')
        self.assertEqual(article.tables[0].footnotes_plain, ['Values are means with uncertainty.'])
        self.assertNotIn('Values are means with uncertainty.', [b.plain_text for s in article.sections for b in s.blocks])

    def test_singular_abbreviation_heading_preserves_term_definition_boundary(self):
        source = PNAS_HTML.replace(
            '<section id="sec-2">',
            '<section id="glossary" role="doc-glossary"><h2>Abbreviation</h2>'
            '<dl><div><dt>ABC</dt><dd>authored definition</dd></div></dl></section>'
            '<section id="sec-2">',
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.html'
            path.write_text(source, encoding='utf-8')
            article = extract_html(path, 'source.html')
        glossary = next(s for s in article.sections if s.heading == 'Abbreviation')
        self.assertEqual([b.plain_text for b in glossary.blocks], ['- ABC: authored definition'])
