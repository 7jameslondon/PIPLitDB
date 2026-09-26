"""A combined bibliography/notes heading must not become body section text."""
from pathlib import Path
import unittest
from scripts.extraction.pdf_article_extractor import extract_pdf_article
from scripts.extraction.pdf_text_extractor import PdfTextDocument, PdfTextPage
from tests.test_pdf_article_pipeline import _line, _metadata, PDF_RELATIVE_PATH

class ReferencesAndNotesTests(unittest.TestCase):
    def test_combined_heading_routes_numbered_notes_and_page_continuations(self):
        for rich in (False, True):
            with self.subTest(rich=rich):
                lines=[_line(1,50,'Introduction'),_line(1,75,'The body ends here.'),
                    _line(1,100,'REFERENCES AND NOTES',markdown='**REFERENCES AND NOTES**' if rich else 'REFERENCES AND NOTES'),
                    _line(1,125,'1. A. Author, First work.'),
                    _line(1,150,'2. Experimental samples contained 10 mM buffer.')]
                doc=PdfTextDocument(PDF_RELATIVE_PATH,[PdfTextPage(1,595,842,'native_text',lines),
                    PdfTextPage(2,595,842,'native_text',[_line(2,50,'Additional experimental details.'),_line(2,75,'3. We thank our colleagues.')])],[],[],True)
                article=extract_pdf_article(Path('unused.pdf'),PDF_RELATIVE_PATH,_metadata(),
                    {'abstract_region_ids':[]},text_document=doc)
                self.assertEqual(len(article.references),3)
                self.assertEqual(article.references[1].plain_text,'2. Experimental samples contained 10 mM buffer. Additional experimental details.')
                self.assertEqual(article.references[2].plain_text,'3. We thank our colleagues.')
                self.assertEqual([b.plain_text for s in article.sections for b in s.blocks],['The body ends here.'])
