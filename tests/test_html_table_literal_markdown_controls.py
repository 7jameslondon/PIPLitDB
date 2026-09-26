import unittest

from lxml import html

from scripts.extraction.html_extractor import _parse_rows, render_inline
from scripts.extraction.rich_text import inline_markup_to_safe_html, plain_text_from_safe_html


class HtmlTableLiteralMarkdownControlsTests(unittest.TestCase):
    def test_html_projection_escapes_authored_literal_asterisks(self) -> None:
        element = html.fromstring(
            "<td>Daunomycin-TFOs * 2008<br>"
            "Etoposide-Watson–Crick-OTIs ** 2018</td>"
        )

        rendered = render_inline(element, markup="html")

        self.assertEqual(
            rendered,
            "Daunomycin-TFOs \\* 2008<br>"
            "Etoposide-Watson–Crick-OTIs \\*\\* 2018",
        )
        safe_html = inline_markup_to_safe_html(rendered)
        self.assertEqual(
            plain_text_from_safe_html(safe_html),
            "Daunomycin-TFOs * 2008\n"
            "Etoposide-Watson–Crick-OTIs ** 2018",
        )

    def test_parsed_table_cell_preserves_authored_literal_asterisks(self) -> None:
        table = html.fromstring(
            "<table><tbody><tr><td>Agent * 2008<br>Agent ** 2018</td></tr>"
            "</tbody></table>"
        )

        cell = _parse_rows(table)[0][0]

        self.assertEqual(cell.text, "Agent * 2008\nAgent ** 2018")
        self.assertEqual(cell.markdown, "Agent \\* 2008<br>Agent \\*\\* 2018")
        self.assertEqual(
            plain_text_from_safe_html(inline_markup_to_safe_html(cell.markdown)),
            cell.text,
        )


if __name__ == "__main__":
    unittest.main()
