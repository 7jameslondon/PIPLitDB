"""Preserve mixed bibliography/footnote sections under their authored heading."""
from pathlib import Path
import unittest
from scripts.extraction.pdf_article_extractor import extract_pdf_article
from scripts.extraction.pdf_text_extractor import PdfTextDocument, PdfTextPage
from tests.test_pdf_article_pipeline import _line, _metadata, PDF_RELATIVE_PATH


class ReferencesAndFootnotesTests(unittest.TestCase):
    def test_native_and_configured_heading_route_reference_continuations(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                lines = [_line(1, 50, 'Introduction'), _line(1, 75, 'Body.'),
                         _line(1, 100, 'References and Footnotes'),
                         _line(1, 125, '1. A. Author, First work.'),
                         _line(1, 150, '2. In the noncooperative ligand case,')]
                doc = PdfTextDocument(PDF_RELATIVE_PATH,
                    [PdfTextPage(1, 595, 842, 'native_text', lines),
                     PdfTextPage(2, 595, 842, 'native_text',
                         [_line(2, 50, 'the equations can be rearranged.'),
                          _line(2, 75, '3. B. Author, Third work.')])], [], [], True)
                config = {'abstract_region_ids': []}
                if configured:
                    config['section_headings'] = [{'page': 1, 'box': [0, 99, 595, 115],
                        'title': 'References and Footnotes', 'level': 2}]
                article = extract_pdf_article(Path('unused.pdf'), PDF_RELATIVE_PATH,
                                              _metadata(), config, text_document=doc)
                self.assertEqual(len(article.references), 3)
                self.assertEqual(article.references[1].plain_text,
                    '2. In the noncooperative ligand case, the equations can be rearranged.')
                self.assertEqual([b.plain_text for s in article.sections for b in s.blocks], ['Body.'])
