import tempfile
import unittest
from pathlib import Path
from PIL import Image
from reportlab.pdfgen import canvas
from scripts.extraction.pdf_extractor import render_pdf_crop, PdfExtractionError
from scripts.extraction.pdf_article_extractor import _visual_regions

class PdfSourceLayoutCropTests(unittest.TestCase):
    def test_retains_positions_and_excludes_intervening_nonfigure_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            c=canvas.Canvas(str(root/'source.pdf'),pagesize=(200,200))
            c.setFillColorRGB(1,0,0); c.rect(20,100,40,80,fill=1,stroke=0)
            c.setFillColorRGB(0,0,1); c.rect(100,140,50,40,fill=1,stroke=0)
            c.setFillColorRGB(0,0,0); c.rect(100,100,50,20,fill=1,stroke=0)
            c.save()
            spec={'source_path':'source.pdf','asset_id':'figure_001','category':'figure',
                  'output_path':'figure.png','preserve_source_layout':True,
                  'parts':[{'page':1,'box':[10,10,70,110]}, {'page':1,'box':[90,10,160,70]}]}
            result=render_pdf_crop(root,root/'out',spec,dpi=72)
            self.assertEqual(result['composition'],
                             {'layout':'source','alignment':'source_coordinates'})
            self.assertEqual([part['placement_pixels']['left'] for part in result['parts']], [0,80])
            with Image.open(root/'out/figure.png') as im:
                self.assertEqual(im.size,(150,100))
                self.assertEqual(im.getpixel((20,20)),(255,0,0))
                self.assertEqual(im.getpixel((100,20)),(0,0,255))
                self.assertEqual(im.getpixel((100,85)),(255,255,255))
            self.assertEqual([v['box'] for v in _visual_regions([spec])],
                             [(10.,10.,70.,110.),(90.,10.,160.,70.)])
            spec['parts'][1]['page']=2
            with self.assertRaisesRegex(PdfExtractionError,'same-page'):
                render_pdf_crop(root,root/'out2',spec,dpi=72)
