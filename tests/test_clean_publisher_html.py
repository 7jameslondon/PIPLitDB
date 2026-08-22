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

    def test_modern_cell_press_article_keeps_erratum_and_discards_ui(self) -> None:
        output = self.clean_fixture(
            "<html><body><header>Publisher navigation</header><article>"
            "<header><div class='article-header__download-full-issue'>Download issue</div>"
            "<h1>Correction title</h1><div class='contributors'><span class='authors'>"
            "<span property='author'><span class='dropBlock'>"
            "<a href='#' role='button' data-db-target-for='author-1'>First Author</a>"
            "<div class='dropBlock__holder'>First Author Search for articles by this author</div>"
            "</span></span><span data-hidden-on='all'> · <span property='author'>"
            "<span class='dropBlock'><a href='#' role='button' data-db-target-for='author-2'>"
            "Second Author</a><div class='dropBlock__holder'>Second Author duplicate</div>"
            "</span></span></span><span data-displayed-on='all'> … Show more</span>"
            "</span></div></header>"
            "<div class='core-nav-wrapper'>Download PDF Share Cite</div>"
            "<div id='dummy-sticky-observer'></div>"
            "<div class='core-sections-menu-outer'>Previous article Next article</div>"
            "<div class='core-relations'>Refers to: Original article</div>"
            "<section id='bodymatter'><p>The corrected scientific text.</p>"
            "<figure id='fig1'><a class='icon-full-screen' href='/figure.gif'>"
            "<img src='/figure.gif' alt='Figure 1'></a>"
            "<figcaption>Figure 1. Correct structure.</figcaption></figure></section>"
            "<aside data-core-aside='right-rail'>Advertisement</aside>"
            "<section id='core-collateral-metrics'><h2>Article metrics</h2></section>"
            "<section id='core-collateral-supplementary'>"
            "<a href='/supplement.pdf'>Supplementary material</a></section>"
            "<section id='core-collateral-relatedArticles'><h2>Related articles</h2>"
            "<p>Unrelated recommendation</p></section>"
            "<div class='core-authors-details'>Duplicate author details</div>"
            "</article><footer>Publisher footer</footer></body></html>"
        )

        self.assertIn("Correction title", output)
        self.assertEqual(output.count("First Author"), 1)
        self.assertEqual(output.count("Second Author"), 1)
        self.assertIn("Refers to: Original article", output)
        self.assertIn("The corrected scientific text.", output)
        self.assertIn("Figure 1. Correct structure.", output)
        self.assertIn("Supplementary material", output)
        self.assertIn('href="https://example.test/supplement.pdf"', output)
        self.assertNotIn("Search for articles", output)
        self.assertNotIn("Download PDF Share Cite", output)
        self.assertNotIn("dummy-sticky-observer", output)
        self.assertNotIn("Previous article", output)
        self.assertNotIn("Advertisement", output)
        self.assertNotIn("Article metrics", output)
        self.assertNotIn("Unrelated recommendation", output)
        self.assertNotIn("Duplicate author details", output)
        self.assertNotIn("href=\"/figure.gif\"", output)

    def test_modern_acs_correction_keeps_body_and_discards_controls(self) -> None:
        output = self.clean_fixture(
            "<html><body><header>Publisher shell</header>"
            "<div id='ContentColumn'><h1>Correction title</h1>"
            "<div class='article-deck-wrap'>Duplicate correction title</div>"
            "<div class='al-author-name'><a href='javascript:;' "
            "class='linked-name'>First Author</a>"
            "<div class='al-author-info-wrap'>First Author Search for works</div></div>"
            "<div class='author-expand-collapse-metadata-wrap'>Article information control</div>"
            "<div class='js-metadata-wrap'>Duplicate metadata</div>"
            "<div class='pub-history-wrap'>Journal citation "
            "<a href='https://doi.org/10.test/correction'>DOI</a>"
            "<a class='history-label' href='javascript:;'>Article history</a></div>"
            "<a class='article-pdf-button' href='/correction.pdf'>Open PDF</a>"
            "<div class='article-body'><div class='vt-toolbar-wrap'>"
            "Citation Search Advanced Search</div>"
            "<p>The omitted supporting-information paragraph.</p>"
            "<h2>Supporting Information</h2>"
            "<a href='http://pubs.acs.org/doi/suppl/10.test/correction'>SI</a>"
            "<span id='sr-fig-viewer-action'>Open figure viewer</span>"
            "<p>Available at <a href='http://pubs.acs.org.04/22/2003'>"
            "http://pubs.acs.org.04/22/2003</a></p></div>"
            "</div><footer>Recommended articles</footer></body></html>"
        )

        self.assertIn("Correction title", output)
        self.assertEqual(output.count("First Author"), 1)
        self.assertIn("Journal citation", output)
        self.assertIn("The omitted supporting-information paragraph.", output)
        self.assertIn("Supporting Information", output)
        self.assertIn('href="https://pubs.acs.org"', output)
        self.assertIn(
            'href="https://pubs.acs.org/doi/suppl/10.test/correction"',
            output,
        )
        self.assertIn("https://pubs.acs.org.", output)
        self.assertNotIn("Publisher shell", output)
        self.assertNotIn("Duplicate correction title", output)
        self.assertNotIn("Search for works", output)
        self.assertNotIn("Article information control", output)
        self.assertNotIn("Duplicate metadata", output)
        self.assertNotIn("Open PDF", output)
        self.assertNotIn("Advanced Search", output)
        self.assertNotIn("Article history", output)
        self.assertNotIn("Open figure viewer", output)
        self.assertNotIn("04/22/2003", output)
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
