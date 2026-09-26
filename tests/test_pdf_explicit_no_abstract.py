from pathlib import Path
import unittest

from scripts.extraction.pdf_article_extractor import extract_pdf_article
from scripts.extraction.pdf_text_extractor import PdfTextDocument, PdfTextPage
from tests.test_pdf_article_pipeline import _line, _metadata, PDF_RELATIVE_PATH


class ExplicitNoAbstractTests(unittest.TestCase):
    def test_reviewed_empty_selection_routes_first_page_to_body(self):
        document = PdfTextDocument(PDF_RELATIVE_PATH, [PdfTextPage(
            1, 595, 842, 'native_text', [_line(1, 100, 'An unheaded communication begins here.')]
        )], [], [], True)
        article = extract_pdf_article(Path('unused.pdf'), PDF_RELATIVE_PATH,
            _metadata(), {'abstract_region_ids': []}, text_document=document)
        self.assertEqual(article.sections[0].heading, 'Article Text')
        self.assertEqual(article.sections[0].blocks[0].plain_text,
                         'An unheaded communication begins here.')
        default = extract_pdf_article(Path('unused.pdf'), PDF_RELATIVE_PATH,
            _metadata(), {}, text_document=document)
        self.assertEqual(default.sections[0].heading, 'Abstract')
