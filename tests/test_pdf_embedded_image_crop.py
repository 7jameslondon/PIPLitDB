import tempfile
import unittest
from pathlib import Path
from PIL import Image
from reportlab.pdfgen import canvas
from scripts.extraction.pdf_extractor import render_pdf_crop, PdfExtractionError


class EmbeddedImageCropTests(unittest.TestCase):
    def test_native_image_excludes_overlaid_text_and_reproduces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new('RGB', (80, 60), 'red').save(root / 'image.png')
            c = canvas.Canvas(str(root / 'source.pdf'), pagesize=(200, 200))
            c.drawImage(str(root / 'image.png'), 20, 100, width=80, height=60)
            c.setFillColorRGB(0, 0, 0)
            c.drawString(20, 110, 'Overlaid caption')
            c.save()
            spec = {'source_path': 'source.pdf', 'asset_id': 'figure_001',
                    'output_path': 'figure.png', 'page': 1, 'box': [20, 40, 100, 100],
                    'embedded_image_index': 1}
            one = render_pdf_crop(root, root / 'one', spec)
            two = render_pdf_crop(root, root / 'two', spec)
            self.assertEqual(one['sha256'], two['sha256'])
            with Image.open(root / 'one/figure.png') as image:
                self.assertEqual(image.size, (80, 60))
                self.assertEqual(image.getcolors(), [(4800, (255, 0, 0))])
            for updates in ({'embedded_image_index': 2}, {'embedded_image_index': True},
                            {'box': [21.1, 40, 100, 100]}):
                with self.assertRaises(PdfExtractionError):
                    render_pdf_crop(root, root / 'bad', {**spec, **updates})
            with self.assertRaises(PdfExtractionError):
                render_pdf_crop(root, root / 'limited', spec, max_output_pixels=100)
