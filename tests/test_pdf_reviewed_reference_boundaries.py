from pathlib import Path
import unittest
from scripts.extraction.pdf_article_extractor import extract_pdf_article, PdfArticleExtractionError, _join_fragments
from scripts.extraction.pdf_text_extractor import PdfTextDocument, PdfTextPage
from tests.test_pdf_article_pipeline import _line, _metadata, PDF_RELATIVE_PATH

class ReviewedReferenceBoundariesTests(unittest.TestCase):
    def run_article(self, starts):
        lines=[_line(1,50,'Introduction'),_line(1,75,'Body.'),
            _line(1,100,'References'),_line(1,125,'Alpha, A., and'),
            _line(1,140,'Beta, B. (2000). First work.'),
            _line(1,165,'Gamma, G. (2001). Second work.')]
        doc=PdfTextDocument(PDF_RELATIVE_PATH,[PdfTextPage(1,595,842,'native_text',lines)],[],[],True)
        specs=[{'page':1,'box':list(lines[i].bbox),'text':lines[i].plain_text} for i in starts]
        return extract_pdf_article(Path('unused.pdf'),PDF_RELATIVE_PATH,_metadata(),
            {'reference_entry_starts':specs},text_document=doc)

    def test_multiline_author_list_does_not_start_another_entry(self):
        refs=self.run_article([3,5]).references
        self.assertEqual(len(refs),2)
        self.assertIn('Beta, B.',refs[0].plain_text)
        self.assertEqual(refs[1].block_id,'reference-002')

    def test_reordered_boundaries_fail_closed(self):
        with self.assertRaisesRegex(PdfArticleExtractionError,'out of order'):
            self.run_article([5,3])

    def test_emphasis_after_line_end_hyphen_does_not_add_space(self):
        self.assertEqual(_join_fragments(['*Drosoph-*','*ila* study.']),'*Drosoph-ila* study.')
        self.assertEqual(_join_fragments(['**well-**','**known** fact.']),'**well-known** fact.')
        self.assertEqual(_join_fragments(['*First*','*Second*']),'*First* *Second*')
