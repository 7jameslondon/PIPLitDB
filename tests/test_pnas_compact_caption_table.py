from pathlib import Path
import tempfile
import unittest

from lxml import html
from scripts.extraction.html_extractor import _figure_caption, extract_html
from tests.test_pnas_html_extractor import PNAS_HTML


class PnasCompactCaptionTableTests(unittest.TestCase):
    def test_inline_bibliography_link_preserves_caption_text_and_tails(self):
        node = html.fromstring('<figure id="F8"><img src="x.png"><figcaption>'
            'Measured response (<i>A</i>), with H<sub>2</sub>O, as described '
            '(<a href="#core-collateral-B7" role="doc-biblioref">7</a>). '
            'The final sentence remains.</figcaption></figure>')
        _, rich, plain = _figure_caption(node)
        self.assertEqual(plain, 'Measured response (A), with H_{2}O, as described (7). The final sentence remains.')
        self.assertIn('H<sub>2</sub>O', rich)
        self.assertIn('The final sentence remains.', rich)

    def test_compact_semantic_figure_table_preserves_title_and_units(self):
        source = PNAS_HTML.replace('Table 1.</div>', 'Table 1</div>')
        source = source.replace('<div id="t01">', '<figure id="T1">')
        source = source.replace('</table></div>\n        </div>\n      </div>\n      <figure id="sfig01">',
            '</table></div>\n        </figure>\n      </div>\n      <figure id="sfig01">')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.html'
            path.write_text(source, encoding='utf-8')
            article = extract_html(path, 'source.html')
        self.assertEqual(len(article.tables), 1)
        self.assertEqual(article.tables[0].title_plain, 'Table 1. Inhibition concentration (K_{i}, nM)')
        self.assertIn('<sub>i</sub>', article.tables[0].title_markdown)
