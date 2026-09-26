from pathlib import Path
from dataclasses import replace
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from scripts.extraction.html_extractor import extract_html
from scripts.extraction.pipeline import ExtractionError
from tests.test_html_preview_pdf_recovery import HtmlPreviewPdfRecoveryTests


class EmptyNaturePreviewTests(HtmlPreviewPdfRecoveryTests):
    def test_empty_preview_requires_explicit_layout_and_source_structure(self):
        self.article.sections[1].blocks = []
        self.config['recover_missing_html_body']['html_layout'] = 'nature_empty_pdf_preview'
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'main.html'
            self.html = replace(self.html, path=path)
            path.write_text('<article><section aria-labelledby="Abs1"><p>Abstract.</p></section><section aria-labelledby="preview"><div id="preview-content"></div></section></article>')
            with patch('scripts.extraction.pipeline.extract_pdf_article', return_value=self.recovered):
                self.assertEqual(self.recover().sections[-1].heading, 'Body')
            path.write_text('<article><section aria-labelledby="Abs1"></section><section aria-labelledby="preview"><div id="preview-content">Actual content</div></section></article>')
            with self.assertRaises(ExtractionError):
                self.recover()

    def test_access_notice_is_not_abstract_prose(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'main.html'
            path.write_text('<article><h1>Example</h1><section aria-labelledby="Abs1"><h2>Abstract</h2><p>Authored abstract.</p></section><div><p>You have full access to this article via <strong>LIBRARY</strong>.</p></div><section aria-labelledby="preview"><h2>Article PDF</h2><div id="preview-content"></div></section></article>')
            result = extract_html(path, 'private/html/main.html')
            self.assertEqual([b.plain_text for s in result.sections for b in s.blocks], ['Authored abstract.'])

    def test_access_notice_is_not_full_article_prose(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'main.html'
            path.write_text('<article><h1>Example</h1><section aria-labelledby="Abs1"><h2>Abstract</h2><p>Authored abstract.</p></section><div><p>You have full access to this article via <strong>LIBRARY</strong>.</p></div><section><h2>Results</h2><p>Authored result.</p></section></article>')
            result = extract_html(path, 'private/html/main.html')
            self.assertEqual(
                [b.plain_text for s in result.sections for b in s.blocks],
                ['Authored abstract.', 'Authored result.'],
            )


if __name__ == '__main__':
    unittest.main()
