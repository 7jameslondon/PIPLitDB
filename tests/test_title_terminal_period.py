from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from scripts.extraction.pipeline import ExtractionError, _reconcile_article_title


class TerminalTitlePeriodTests(unittest.TestCase):
    def check_title(self, source, curated, doi="10.1000/example", markup=None):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "article.html"
            path.write_text(markup or '<a property="sameAs" href="https://doi.org/10.1000/example">DOI</a>', encoding="utf-8")
            article = SimpleNamespace(title=source)
            metadata = SimpleNamespace(title=curated, doi=doi)
            _reconcile_article_title(article, metadata, source_html=path)
            self.assertEqual(article.title, curated)

    def test_terminal_period_requires_matching_explicit_article_doi(self):
        self.check_title("Synthetic article", "Synthetic article.")
        self.check_title("Synthetic article.", "Synthetic article")
        self.check_title("Synthetic article", "Synthetic article.", markup='<meta name="citation_doi" content="10.1000/example">')
        self.check_title("Synthetic article", "Synthetic article.", markup='<a property="sameAs" href="https://doi.org/10.1000/example">DOI</a><a property="sameAs" href="https://pubmed.ncbi.nlm.nih.gov/123/">PubMed</a>')
        self.check_title(
            "Synthetic article",
            "Synthetic article.",
            markup=(
                '<a href="https://doi.org/10.1000/example">'
                'https://doi.org/10.1000/example'
                '<span>Digital Object Identifier (DOI)</span></a>'
            ),
        )

    def test_rejects_other_title_changes_and_unverified_identity(self):
        for source, curated, doi, markup in [
            ("Synthetic article", "Synthetic article?", "10.1000/example", None),
            ("Synthetic article", "Different article.", "10.1000/example", None),
            ("Synthetic article", "Synthetic article..", "10.1000/example", None),
            ("Synthetic article", "Synthetic article.", "10.1000/other", None),
            ("Synthetic article", "Synthetic article.", None, None),
            ("Synthetic article", "Synthetic article.", "10.1000/example", '<a href="https://doi.org/10.1000/example">Reference</a>'),
            ("Synthetic article", "Synthetic article.", "10.1000/example", '<a href="https://doi.org/10.1000/other"><span>Digital Object Identifier (DOI)</span></a>'),
            ("Synthetic article", "Synthetic article.", "10.1000/example", '<meta name="citation_doi" content="10.1000/other"><a property="sameAs" href="https://doi.org/10.1000/example">DOI</a>'),
        ]:
            with self.subTest(source=source, curated=curated, doi=doi, markup=markup):
                with self.assertRaises(ExtractionError):
                    self.check_title(source, curated, doi, markup)

    def test_no_source_html_still_rejects_period_difference(self):
        with self.assertRaises(ExtractionError):
            _reconcile_article_title(SimpleNamespace(title="Synthetic article"), SimpleNamespace(title="Synthetic article.", doi="10.1000/example"))
