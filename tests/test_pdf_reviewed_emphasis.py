from pathlib import Path
import unittest
from scripts.extraction.pdf_article_extractor import extract_pdf_article, PdfArticleExtractionError
from scripts.extraction.pdf_text_extractor import PdfTextDocument, PdfTextPage
from tests.test_pdf_article_pipeline import _line, _metadata, PDF_RELATIVE_PATH
from dataclasses import replace
from scripts.extraction.pdf_article_extractor import _configured_section_headings

class ReviewedEmphasisTests(unittest.TestCase):
    def test_configured_inline_heading_keeps_body_emphasis(self):
        for rich in ('<strong>Method.</strong> Use <em>Eco</em>RI and <strong>1</strong>.',
                     '**Method.** Use *Eco*RI and **1**.'):
            line = replace(_line(1, 75, 'Method. Use EcoRI and 1.', markdown=rich), source_region_id='method')
            doc = PdfTextDocument(PDF_RELATIVE_PATH, [PdfTextPage(1,595,842,'native_text',[line])], [], [], True)
            config={'preserve_source_emphasis':True,'section_headings':[{'region_id':'method','title':'Method','source_heading_text':'Method.'}]}
            heading = _configured_section_headings(config,doc)[0]
            self.assertEqual(heading['body_plain'], 'Use EcoRI and 1.')
            self.assertEqual(heading['body_markdown'], 'Use <em>Eco</em>RI and <strong>1</strong>.')

    def test_reviewed_meaningful_emphasis_is_retained(self):
        doc = PdfTextDocument(PDF_RELATIVE_PATH, [PdfTextPage(1, 595, 842, 'native_text', [
            _line(1, 50, 'Introduction'),
            _line(1, 75, 'A mismatched T base.', markdown='A mismatched **T** base.'),
            _line(1, 200, 'Figure 1. A labelled site.', markdown='Figure 1. A **labelled** site.'),
        ])], [], [], True)
        crops = [{'asset_id':'figure-1', 'category':'figure', 'label':'Figure 1',
                  'page':1, 'box':[40,110,520,180], 'caption_box':[40,195,520,215]}]
        article = extract_pdf_article(Path('unused.pdf'), PDF_RELATIVE_PATH, _metadata(),
            {'preserve_source_emphasis': True}, crop_specs=crops, text_document=doc)
        self.assertEqual(article.sections[0].blocks[0].markdown, 'A mismatched **T** base.')
        self.assertEqual(article.figures[0].caption_markdown, 'Figure 1. A **labelled** site.')
        with self.assertRaisesRegex(PdfArticleExtractionError, 'must be boolean'):
            extract_pdf_article(Path('unused.pdf'), PDF_RELATIVE_PATH, _metadata(),
                {'preserve_source_emphasis': 'yes'}, text_document=doc)
