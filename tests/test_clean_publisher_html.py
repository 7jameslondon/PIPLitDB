from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

try:
    from lxml import etree, html
except ModuleNotFoundError:
    etree = None
    html = None
    clean_html = None
    serialize_body_fragment = None
else:
    from scripts.clean_publisher_html import clean_html, serialize_body_fragment


@unittest.skipUnless(html is not None, "lxml is required for publisher HTML tests")
class SerializeBodyFragmentTests(unittest.TestCase):
    def test_complete_document_is_unwrapped(self) -> None:
        root = html.document_fromstring(
            "<html><head><title>Site title</title></head>"
            "<body><article><h1>Article title</h1></article></body></html>"
        )

        output = serialize_body_fragment(root)

        self.assertEqual(
            output,
            "<body>\n<article><h1>Article title</h1></article>\n</body>",
        )
        self.assertNotIn("<html", output)
        self.assertNotIn("<head", output)

    def test_existing_body_is_not_nested(self) -> None:
        root = html.Element("body")
        article = etree.SubElement(root, "article")
        article.text = "Article text"

        output = serialize_body_fragment(root)

        self.assertEqual(output.count("<body>"), 1)
        self.assertEqual(output.count("</body>"), 1)

    def test_prohibited_elements_are_rejected(self) -> None:
        root = html.Element("article")
        etree.SubElement(root, "script").text = "alert('no')"

        with self.assertRaisesRegex(ValueError, "must not contain a <script>"):
            serialize_body_fragment(root)


@unittest.skipUnless(html is not None, "lxml is required for publisher HTML tests")
class ArticleRootSelectionTests(unittest.TestCase):
    def clean_fixture(self, source: str) -> str:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.html"
            manifest_path = root / "manifest.json"
            output_path = root / "output.html"
            source_path.write_text(source, encoding="utf-8")
            manifest_path.write_text('{"assets": []}', encoding="utf-8")

            clean_html(
                source_path,
                manifest_path,
                output_path,
                "https://example.test/article",
            )

            return output_path.read_text(encoding="utf-8")

    def test_sciencedirect_article_excludes_site_shell(self) -> None:
        output = self.clean_fixture(
            "<html><head><title>Publisher</title></head><body>"
            "<header>Publisher navigation</header>"
            "<article><h1>Article title</h1><div id='abstracts'>Abstract</div>"
            "<div id='body'><h2>Results</h2><h2>References</h2></div></article>"
            "<footer>Recommended articles</footer></body></html>"
        )

        self.assertIn("Article title", output)
        self.assertIn("References", output)
        self.assertNotIn("Publisher navigation", output)
        self.assertNotIn("Recommended articles", output)

    def test_content_column_excludes_silverchair_shell(self) -> None:
        output = self.clean_fixture(
            "<html><head><title>Publisher</title></head><body>"
            "<nav>Journal navigation</nav><main id='main'>"
            "<div id='ContentColumn'><h1>Article title</h1>"
            "<section><h2>Methods</h2><p>Article text</p></section>"
            "<section><h2>References</h2><p>Reference one</p></section></div>"
            "<aside>Related content</aside></main></body></html>"
        )

        self.assertIn("Article title", output)
        self.assertIn("Reference one", output)
        self.assertNotIn("Journal navigation", output)
        self.assertNotIn("Related content", output)


if __name__ == "__main__":
    unittest.main()
