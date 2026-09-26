from pathlib import Path
import unittest

from scripts.extraction.pdf_article_extractor import extract_pdf_article
from scripts.extraction.pdf_text_extractor import PdfTextDocument, PdfTextPage
from tests.test_pdf_article_pipeline import _line, _metadata, PDF_RELATIVE_PATH


class PdfCropSourceScopeTests(unittest.TestCase):
    def test_supplement_crops_do_not_suppress_main_text_or_route_captions(self):
        doc = PdfTextDocument(PDF_RELATIVE_PATH, [PdfTextPage(
            1, 595, 842, 'native_text', [
                _line(1, 50, 'Introduction'),
                _line(1, 75, 'Essential main article methods.'),
            ])], [], [], True)
        crops = [
            {'asset_id': 'supp-table', 'category': 'table',
             'source_path': 'papers (private)/00001/supplementary/data.pdf',
             'page': 1, 'box': [0, 60, 550, 100]},
            {'asset_id': 'supp-figure', 'category': 'figure',
             'source': 'papers (private)/00001/supplementary/figures.pdf',
             'page': 9, 'box': [0, 0, 500, 500]},
        ]
        article = extract_pdf_article(Path('unused.pdf'), PDF_RELATIVE_PATH,
                                      _metadata(), crop_specs=crops, text_document=doc)
        self.assertEqual(article.sections[0].blocks[0].plain_text,
                         'Essential main article methods.')
        self.assertEqual(article.figures, [])

    def test_main_crop_still_excludes_its_own_visual_text(self):
        doc = PdfTextDocument(PDF_RELATIVE_PATH, [PdfTextPage(
            1, 595, 842, 'native_text', [
                _line(1, 50, 'Introduction'),
                _line(1, 75, 'Main prose.'),
                _line(1, 110, 'Visual pixel text.'),
            ])], [], [], True)
        crop = {'asset_id':'main-table','category':'table',
                'source_path':PDF_RELATIVE_PATH.replace('/', '\\'),
                'page':1,'box':[0,100,550,130]}
        article=extract_pdf_article(Path('unused.pdf'),PDF_RELATIVE_PATH,
                                   _metadata(),crop_specs=[crop],text_document=doc)
        self.assertEqual(article.sections[0].blocks[0].plain_text,'Main prose.')


if __name__ == '__main__':
    unittest.main()
