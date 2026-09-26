import unittest

from lxml import html

from scripts.extraction.html_extractor import plain_text, render_inline
from scripts.extraction.rich_text import (
    inline_markup_to_safe_html,
    rich_text_matches_plain,
)


class ExternalLinkBracketLabelTests(unittest.TestCase):
    def test_accessibility_suffix_in_square_brackets_keeps_link_and_text_parity(self) -> None:
        node = html.fragment_fromstring(
            '<p>DOI: <a href="https://doi.org/10.1/example">'
            '<span>https://doi.org/10.1/example</span> '
            '<span>[Opens in a new window]</span></a></p>'
        )

        markup = render_inline(node)
        safe_html = inline_markup_to_safe_html(markup)

        self.assertIn(
            '<a href="https://doi.org/10.1/example">'
            'https://doi.org/10.1/example [Opens in a new window]</a>',
            safe_html,
        )
        self.assertTrue(rich_text_matches_plain(plain_text(node), safe_html))


if __name__ == "__main__":
    unittest.main()
