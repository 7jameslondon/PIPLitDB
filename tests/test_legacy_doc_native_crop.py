import tempfile
import unittest
from pathlib import Path
from unittest import mock
from PIL import Image
from scripts.extraction.docx_supplement import render_docx_figure_crops, DocxFigureCropRequest


class LegacyDocNativeCropTests(unittest.TestCase):
    def test_compositor_receives_original_doc_suffix_and_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/'source.doc'
            source.write_bytes(b'legacy-native-art')
            def render(staged, output, converted, profile):
                self.assertEqual(staged.suffix, '.doc')
                self.assertEqual(staged.read_bytes(), source.read_bytes())
                from reportlab.pdfgen import canvas
                target = converted/'input.pdf'
                pdf = canvas.Canvas(str(target), pagesize=(200,200))
                pdf.drawString(20,100,'Native original')
                pdf.save()
                return target, 'test compositor', '1'
            with mock.patch('scripts.extraction.docx_supplement._render_docx_pdf',side_effect=render):
                results = render_docx_figure_crops(source,root/'render',(
                    DocxFigureCropRequest('figure_s1',1,(10,10,190,190),1),),150)
            self.assertEqual(len(results),1)
            self.assertGreater(Image.open(results[0].path).width,100)
            self.assertEqual(source.read_bytes(),b'legacy-native-art')
