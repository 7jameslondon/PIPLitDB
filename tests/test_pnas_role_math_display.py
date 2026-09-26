import tempfile
import unittest
from pathlib import Path

from scripts.extraction.html_extractor import extract_html
from tests.test_pnas_html_extractor import PNAS_HTML


class PnasRoleMathDisplayTests(unittest.TestCase):
    def extract(self, display="true", role="math"):
        formula = ('<div><div role="' + role + '"><div><span id="M1">'
                   '<mjx-container display="' + display + '">'
                   '<mjx-math aria-hidden="true">visual duplicate</mjx-math>'
                   '<mjx-assistive-mml><math><mi>x</mi><mo>=</mo><mn>2</mn></math>'
                   '</mjx-assistive-mml></mjx-container></span></div></div></div>')
        source = PNAS_HTML.replace('A result.</div>', 'Before.' + formula + 'After.</div>')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'main.html'
            path.write_text(source, encoding='utf-8')
            return extract_html(path, 'main.html')

    def test_display_is_segmented_once_between_prose(self):
        article = self.extract()
        blocks = next(s for s in article.sections if s.heading == 'Results').blocks
        self.assertEqual([b.kind for b in blocks], ['paragraph', 'equation', 'paragraph'])
        self.assertEqual([b.plain_text for b in blocks], ['Before.', 'x = 2', 'After.'])

    def test_inline_or_nonmath_wrapper_stays_inline(self):
        for display, role in [('false', 'math'), ('true', 'note')]:
            with self.subTest(display=display, role=role):
                article = self.extract(display, role)
                self.assertFalse(any(b.kind == 'equation' for s in article.sections for b in s.blocks))
