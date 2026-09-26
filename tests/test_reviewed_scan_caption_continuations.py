import hashlib
import tempfile
import unittest
from pathlib import Path
from reportlab.pdfgen import canvas
from scripts.extraction.models import SourceFile
from scripts.extraction.supplements import extract_supplements


class ReviewedScanCaptionContinuationsTests(unittest.TestCase):
    def test_reviewed_scan_joins_paragraph_then_consolidates_caption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / 'scan.pdf'
            c = canvas.Canvas(str(pdf))
            c.showPage()
            c.showPage()
            c.save()
            source = SourceFile('supplement', pdf, 'scan.pdf', pdf.stat().st_size,
                                hashlib.sha256(pdf.read_bytes()).hexdigest(), 'application/pdf', 2)
            def block(page, position, text, kind='text'):
                return dict(page=page, block_kind=kind, plain_text=text,
                            source_locator=f'page={page};reviewed-block={position}',
                            reason='Scanned source.', evidence='Visible source content.')
            conf = dict(source_path='scan.pdf', source_sha256=source.sha256,
                        reviewed_page_blocks=[block(1,'a','A paragraph crosses'),
                                              block(2,'a','the page.'),
                                              block(2,'b','Figure S1. Complete caption.','figure_caption')],
                        reviewed_page_continuations=[dict(from_locator='page=1;reviewed-block=a',
                                                          to_locator='page=2;reviewed-block=a',
                                                          reason='Continuation.', evidence='No paragraph break.')])
            crop = dict(source_path='scan.pdf', category='supplement_figure',
                        page=2, box=[20,20,100,100],
                        asset_id='s1', label='Figure S1', caption_plain='Complete caption.',
                        caption_source_locator='page=2;reviewed-block=b')
            result = extract_supplements([source], root/'out',
                                         pdf_text_config={'supplements':[conf]}, pdf_crop_specs=[crop])[0]
            self.assertEqual([b.plain_text for b in result.blocks], ['A paragraph crosses the page.'])
            self.assertEqual(len(result.figures), 1)
            self.assertEqual(result.figures[0].caption_plain, 'Complete caption.')


if __name__ == '__main__':
    unittest.main()
