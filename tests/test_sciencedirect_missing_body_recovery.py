from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from dataclasses import replace

from tests import test_html_preview_pdf_recovery as helpers
from scripts.extraction.models import Section
from scripts.extraction.pipeline import ExtractionError


class ScienceDirectMissingBodyTests(helpers.HtmlPreviewPdfRecoveryTests):
    def test_explicit_pinned_abstract_keywords_layout_keeps_keywords(self):
        self._exercise_layout(False)

    def test_body_container_rejects_preview_recovery(self):
        self._exercise_layout(True)

    def test_explicit_pinned_abstract_abbreviations_layout_keeps_abbreviations(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'main.html'
            path.write_text('''<article><h1 id="screen-reader-main-title">Example</h1>
            <div id="abstracts">Abstract</div>
            <div id="aep-keywords-id1"><h2>Abbreviations</h2><div>Im imidazole</div></div>
            <section id="aep-bibliography-id1">References</section></article>''', encoding='utf-8')
            self.html = replace(self.html, path=path)
            self.article.sections[1] = Section('abbreviations', 'Abbreviations', blocks=[])
            self.config['recover_missing_html_body']['html_layout'] = (
                'sciencedirect_abstract_abbreviations_only'
            )
            with patch('scripts.extraction.pipeline.extract_pdf_article', return_value=self.recovered):
                result = self.recover()
            self.assertEqual(
                [section.heading for section in result.sections],
                ['Abstract', 'Abbreviations', 'Body'],
            )

    def _exercise_layout(self, populated):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'main.html'
            path.write_text('''<article><h1 id="screen-reader-main-title">Example</h1>
            <div id="abstracts">Abstract</div><div id="aep-keywords-id1">Keywords</div>
            <section id="aep-bibliography-id1">References</section>'''
            + ('<div id="body">Authored text</div>' if populated else '') + '</article>', encoding='utf-8')
            self.html = replace(self.html, path=path)
            self.article.sections[1] = Section('keywords', 'Keywords', blocks=[])
            self.config['recover_missing_html_body']['html_layout'] = 'sciencedirect_abstract_keywords_only'
            with patch('scripts.extraction.pipeline.extract_pdf_article', return_value=self.recovered):
                if populated:
                    with self.assertRaises(ExtractionError):
                        self.recover()
                else:
                    result = self.recover()
                    self.assertEqual([s.heading for s in result.sections], ['Abstract', 'Keywords', 'Body'])


if __name__ == '__main__':
    unittest.main()
