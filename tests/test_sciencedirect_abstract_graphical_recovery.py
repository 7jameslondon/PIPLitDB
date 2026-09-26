from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from tests.test_html_preview_pdf_recovery import HtmlPreviewPdfRecoveryTests
from scripts.extraction.models import FigureItem
from scripts.extraction.pipeline import ExtractionError


class AbstractGraphicalRecoveryTests(HtmlPreviewPdfRecoveryTests):
    def test_graphical_only_archive_preserves_asset_and_recovers_reviewed_references(self):
        self.exercise(False)

    def test_populated_body_is_never_replaced(self):
        self.exercise(True)

    def exercise(self, populated):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'main.html'
            path.write_text('<div id="article"><h1 id="screen-reader-main-title">Example</h1></div>'
                '<div id="abstracts"><figure><img src="data:image/gif;base64,AA=="></figure></div>'
                '<section id="aep-bibliography-id1"></section>'
                + ('<div id="body">Text</div>' if populated else ''), encoding='utf-8')
            self.html = replace(self.html, path=path)
            self.article.sections = self.article.sections[:1]
            figure = FigureItem('graphical_abstract', None, 'Graphical Abstract', 'graphical_abstract', '', '', self.html.relative_path, '#abstracts')
            self.article.figures = [figure]
            self.recovered.references = [replace(self.article.references[0], plain_text='PDF bibliography')]
            self.config['recover_missing_html_body'].update(html_layout='sciencedirect_abstract_graphical_only',
                use_reviewed_pdf_references={'reason':'Malformed HTML references','evidence':'Reviewed PDF bibliography'})
            with patch('scripts.extraction.pipeline.extract_pdf_article', return_value=self.recovered):
                if populated:
                    with self.assertRaises(ExtractionError):
                        self.recover()
                else:
                    result = self.recover()
                    self.assertEqual(result.figures, [figure])
                    self.assertEqual(result.references[0].plain_text, 'PDF bibliography')
                    self.assertEqual(result.page_diagnostics[-1]['retained_html_components'], ['abstract','bibliographic','graphical_abstract'])
