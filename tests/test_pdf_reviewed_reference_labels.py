from pathlib import Path
import hashlib
import tempfile
import unittest

from scripts.extraction.pdf_article_extractor import extract_pdf_article, PdfArticleExtractionError
from scripts.extraction.pdf_text_extractor import PdfTextDocument, PdfTextPage
from tests.test_pdf_article_pipeline import _line, _metadata, PDF_RELATIVE_PATH


class ReviewedReferenceLabelTests(unittest.TestCase):
    def article(self, labels=None):
        lines = [_line(1, 50, 'Introduction'), _line(1, 75, 'Body.'),
                 _line(1, 100, 'References'), _line(1, 125, '1. Alpha, A. First work.'),
                 _line(1, 150, '3. Beta, B. Third work.')]
        doc = PdfTextDocument(PDF_RELATIVE_PATH, [PdfTextPage(1, 595, 842, 'native_text', lines)], [], [], True)
        config = {'reference_entry_starts': [{'page': 1, 'box': list(l.bbox), 'text': l.plain_text} for l in lines[3:]]}
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source.pdf'
            source.write_bytes(b'fixed source identity')
            if labels is not None:
                config.update(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), reference_label_sequence=labels)
            return extract_pdf_article(source, PDF_RELATIVE_PATH, _metadata(), config, text_document=doc)

    def test_source_authored_gap_retains_labels_and_stable_ids(self):
        refs = self.article([1, 3]).references
        self.assertEqual([r.block_id for r in refs], ['reference-001', 'reference-002'])
        self.assertEqual([r.plain_text for r in refs], ['1. Alpha, A. First work.', '3. Beta, B. Third work.'])

    def test_unreviewed_or_mismatched_gap_fails_closed(self):
        for labels in (None, [1, 2], [True, 3]):
            with self.subTest(labels=labels), self.assertRaisesRegex(PdfArticleExtractionError, 'contiguous'):
                self.article(labels)
