from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from reportlab.pdfgen.canvas import Canvas
from scripts.extraction.pdf_text_extractor import extract_pdf_text, _has_horizontal_baseline


class DiagonalWatermarkTests(unittest.TestCase):
    def test_shear_is_horizontal_but_diagonal_baseline_is_not(self):
        self.assertTrue(_has_horizontal_baseline({'upright': True, 'matrix': (1, 0, .25, 1, 0, 0)}))
        self.assertFalse(_has_horizontal_baseline({'upright': True, 'matrix': (.707, .707, -.707, .707, 0, 0)}))

    def test_diagonal_watermark_cannot_scramble_article_text(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'synthetic.pdf'
            canvas = Canvas(str(path), pagesize=(612, 792))
            canvas.setFont('Times-Roman', 10)
            canvas.drawString(50, 700, 'The article retains its complete first line.')
            canvas.drawString(50, 686, 'A second line contains 500 nM and 24 hours.')
            canvas.saveState()
            canvas.translate(50, 655)
            canvas.rotate(45)
            canvas.setFont('Helvetica', 30)
            canvas.drawString(0, 0, 'DIAGONAL WATERMARK')
            canvas.restoreState()
            canvas.save()
            doc = extract_pdf_text(path, 'synthetic.pdf', reading_regions=[{'region_id':'body','page':1,'box':[0,0,612,792]}])
            body = [line.plain_text for line in doc.pages[0].lines if not line.rotated]
            self.assertEqual(body, ['The article retains its complete first line.', 'A second line contains 500 nM and 24 hours.'])
            self.assertTrue(any(line.rotated for line in doc.pages[0].lines))
