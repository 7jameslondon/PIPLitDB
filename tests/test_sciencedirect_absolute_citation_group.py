import sys
import unittest
from pathlib import Path

from lxml import html

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from extraction.html_extractor import plain_text, render_inline
from extraction.rich_text import inline_markup_to_safe_html, plain_text_from_safe_html


class ScienceDirectAbsoluteCitationGroupTests(unittest.TestCase):
    def test_absolute_backlinks_inside_literal_group_do_not_nest_markdown(self):
        paragraph = html.fromstring(
            '<p>Genes [<a href="https://www.sciencedirect.com/science/article/pii/'
            'S0169409X19300171?via%3Dihub#bb0265" name="bbb0265">53</a>,'
            '<a href="https://www.sciencedirect.com/science/article/pii/'
            'S0169409X19300171?via%3Dihub#bb0300" name="bbb0300">[60]</a>, '
            '<a href="https://www.sciencedirect.com/science/article/pii/'
            'S0169409X19300171?via%3Dihub#bb0305" name="bbb0305">[61]</a>].</p>'
        )

        markup = render_inline(paragraph)
        safe_html = inline_markup_to_safe_html(markup)

        self.assertEqual(markup, "Genes [53,60, 61].")
        self.assertEqual(plain_text(paragraph), "Genes [53,60, 61].")
        self.assertEqual(plain_text_from_safe_html(safe_html), "Genes [53,60, 61].")
        self.assertNotIn("href", safe_html)

    def test_unpaired_absolute_link_remains_an_external_link(self):
        paragraph = html.fromstring(
            '<p><a href="https://www.sciencedirect.com/science/article/pii/'
            'S0169409X19300171#bb0265" name="wrong">53</a></p>'
        )

        markup = render_inline(paragraph)

        self.assertTrue(markup.startswith("[53](https://"))


if __name__ == "__main__":
    unittest.main()
