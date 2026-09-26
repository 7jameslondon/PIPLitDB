from pathlib import Path
import unittest
from scripts.extraction.pdf_article_extractor import extract_pdf_article
from scripts.extraction.pdf_text_extractor import PdfTextDocument, PdfTextPage
from tests.test_pdf_article_pipeline import _line, _metadata, PDF_RELATIVE_PATH


class ExperimentalNumericContinuationTests(unittest.TestCase):
    def test_experimental_section_does_not_promote_nmr_continuation(self):
        lines = [_line(1, 70, 'Experimental Section'),
                 _line(1, 95, 'Synthesis: Product characterization.'),
                 _line(1, 107, 'NMR: 7.24 (d, 1H, J ='),
                 _line(1, 119, '1. Hz; CH), 7.13 (d, 1H).')]
        doc = PdfTextDocument(PDF_RELATIVE_PATH,
            [PdfTextPage(1, 595, 842, 'native_text', lines)], [], [], True)
        article = extract_pdf_article(Path('unused.pdf'), PDF_RELATIVE_PATH,
            _metadata(), {'abstract_region_ids': []}, text_document=doc)
        self.assertEqual([s.heading for s in article.sections], ['Experimental Section'])
        prose = ' '.join(b.plain_text for s in article.sections for b in s.blocks)
        self.assertIn('1. Hz; CH), 7.13', prose)

    def test_inline_heading_keeps_scientific_markup_in_remainder(self):
        from scripts.extraction.pdf_article_extractor import _split_heading
        line = _line(1, 75, '1. General. Reagents include Et2NH.',
                     markdown='1. *General.* Reagents include Et<sub>2</sub>NH.')
        heading, rich, plain = _split_heading(line)
        self.assertEqual(heading, '1. General')
        self.assertEqual(rich, 'Reagents include Et<sub>2</sub>NH.')
        self.assertEqual(plain, 'Reagents include Et2NH.')

    def test_continued_values_and_compound_number_are_not_headings(self):
        lines = [_line(1, 70, 'Experimental Part'),
                 _line(1, 95, '1. Synthesis. Initial procedure.'),
                 _line(1, 120, 'IR (film): 1720, 1500,'),
                 _line(1, 132, '1400. NMR: 6.2 and 7.1.'),
                 _line(1, 156, 'The product was purified as described for'),
                 _line(1, 168, '22. The yield was 1.5 mg.'),
                 _line(1, 192, '2. Analysis. A separate method.', left=86)]
        doc = PdfTextDocument(PDF_RELATIVE_PATH,
            [PdfTextPage(1, 595, 842, 'native_text', lines)], [], [], True)
        article = extract_pdf_article(Path('unused.pdf'), PDF_RELATIVE_PATH,
            _metadata(), {'abstract_region_ids': []}, text_document=doc)
        headings = [s.heading for s in article.sections]
        self.assertIn('1. Synthesis', headings)
        self.assertIn('2. Analysis', headings)
        self.assertFalse(any(h.startswith(('1400.', '22.')) for h in headings))
        prose = ' '.join(b.plain_text for s in article.sections for b in s.blocks)
        self.assertIn('1720, 1500, 1400. NMR: 6.2', prose)
        self.assertIn('described for 22. The yield', prose)
