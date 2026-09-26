import base64
import io
import unittest
from PIL import Image
from lxml import html
from scripts.extraction.html_extractor import _safe_embedded_svg, _table_items


class AcsSvgTableAndRasterTests(unittest.TestCase):
    def svg(self, href, element='image', extra=''):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
                f'<{element} xlink:href="{href}" {extra}/></svg>').encode()

    def raster(self):
        stream=io.BytesIO()
        Image.new('RGB',(2,2),'white').save(stream,format='JPEG')
        return 'data:image/jpeg;base64,'+base64.b64encode(stream.getvalue()).decode()

    def test_complete_inline_jpeg_is_safe(self):
        self.assertTrue(_safe_embedded_svg(self.svg(self.raster())))

    def test_external_executable_nested_and_invalid_payloads_stay_denied(self):
        for href in ['https://example.test/image.jpg','data:text/html;base64,PHNjcmlwdD4=',
                     'data:image/svg+xml;base64,PHN2Zy8+', 'data:image/jpeg;base64,/9j/YmFk']:
            with self.subTest(href=href):
                self.assertFalse(_safe_embedded_svg(self.svg(href)))
        self.assertFalse(_safe_embedded_svg(self.svg(self.raster(), element='a')))
        self.assertFalse(_safe_embedded_svg(self.svg(self.raster(), extra='onload="alert(1)"')))

    def test_source_prefixed_svg_table_retains_semantic_placeholder(self):
        root=html.fromstring('''<div id="123-content"><figure content-id="ab123t00001 d7e22" id="table-1">
<div id="ab123t00001"><span id="label-ab123t00001">Table 1.</span><div id="caption-ab123t00001"><p>Constants</p></div></div>
<div><table role="presentation" aria-labelledby="label-ab123t00001" aria-describedby="caption-ab123t00001"><tbody><tr><td><div><a><img alt="Graphic. Refer to the image caption for details." path-from-xml="ab123u00001a.svg" src="data:image/svg+xml;base64,PHN2Zy8+"></a></div></td></tr></tbody></table></div>
<div></div><div><a href="/view-large/123" target="_blank" rel="nofollow" aria-label="View large Table 1.">View Large</a></div>
</figure></div>''')
        tables=_table_items(root,'synthetic.html')
        self.assertEqual(len(tables),1)
        self.assertEqual(tables[0].source_kind,'image')
        root.xpath('.//table')[0].set('aria-labelledby','wrong')
        self.assertEqual(_table_items(root,'synthetic.html'),[])
