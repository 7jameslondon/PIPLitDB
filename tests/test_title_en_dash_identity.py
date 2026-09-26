from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from scripts.extraction.pipeline import ExtractionError, _reconcile_article_title


class EnDashTitleIdentityTests(unittest.TestCase):
    def reconcile(self, source, curated, markup):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "article.html"
            path.write_text(markup, encoding="utf-8")
            article = SimpleNamespace(title=source)
            metadata = SimpleNamespace(title=curated, doi="10.1000/example")
            _reconcile_article_title(article, metadata, source_html=path)
            self.assertEqual(article.title, curated)

    def test_accepts_en_dash_only_with_article_doi(self):
        for markup in [
            '<meta name="citation_doi" content="10.1000/example">',
            '<div id="getCitation"><a href="https://doi.org/10.1000/example">DOI</a></div>',
        ]:
            with self.subTest(markup=markup):
                self.reconcile("Alpha–beta conjugates", "Alpha-beta conjugates", markup)

    def test_rejects_unverified_or_other_title_difference(self):
        for source, markup in [
            ("Alpha–beta conjugates", '<a href="https://doi.org/10.1000/example">Reference</a>'),
            ("Alpha–beta conjugates", '<div id="getCitation"><a href="https://doi.org/10.1000/other">DOI</a></div>'),
            ("Alpha—beta conjugates", '<meta name="citation_doi" content="10.1000/example">'),
            ("Alpha–gamma conjugates", '<meta name="citation_doi" content="10.1000/example">'),
        ]:
            with self.subTest(source=source, markup=markup), self.assertRaises(ExtractionError):
                self.reconcile(source, "Alpha-beta conjugates", markup)
