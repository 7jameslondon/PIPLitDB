from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.extraction.html_extractor import extract_html

PIXEL = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='

class MultipartCaptionTests(unittest.TestCase):
    def extract(self, target='panel-cd.jpg', image=''):
        source = f'''<article><h1>Multipart article</h1><section><h2>Results</h2>
        <p>Authored body.</p><figure id="FIGGR4">
        <img src="data:image/png;base64,{PIXEL}"><img src="data:image/png;base64,{PIXEL}">
        <a download="" href="panel-cd.jpg">Download full-size image</a>
        <p><span>Figure 4</span>. (A) First panel. (B) Second panel.</p></figure>
        <figure id="FIGGR4B">{image}<a download="" href="{target}">Download full-size image</a>
        <p><span>Figure 4</span>. (C) Third H<sub>2</sub> panel. (D) Fourth panel.</p></figure>
        </section></article>'''
        with TemporaryDirectory() as folder:
            path=Path(folder)/'main.html'; path.write_text(source,encoding='utf-8')
            return extract_html(path,'html/main.html')

    def test_shared_download_caption_continuation_joins_full_image_group(self):
        result=self.extract()
        self.assertEqual(len(result.figures),1)
        self.assertEqual(result.figures[0].figure_id,'figure_004')
        self.assertEqual(result.figures[0].caption_plain,'(A) First panel. (B) Second panel. (C) Third H_{2} panel. (D) Fourth panel.')
        self.assertIn('H<sub>2</sub>',result.figures[0].caption_markdown)
        self.assertEqual(len(result.embedded_assets),1)
        self.assertEqual(result.embedded_assets[0].media_type,'image/png')

    def test_unrelated_download_does_not_merge(self):
        self.assertEqual(len(self.extract(target='different.jpg').figures),2)

    def test_continuation_with_its_own_image_does_not_merge(self):
        self.assertEqual(len(self.extract(image=f'<img src="data:image/png;base64,{PIXEL}">').figures),2)

    def test_graphic_abstract_opaque_figure_number_is_not_article_number(self):
        source=f'''<article><h1>Example</h1><div id="abstracts">
        <div id="aep-abstract-id15"><div id="aep-abstract-sec-id16"><div>Graphic</div></div>
        <figure id="figure0010"><img src="data:image/png;base64,{PIXEL}"></figure></div>
        </div><section><h2>Results</h2><p>Body.</p></section></article>'''
        with TemporaryDirectory() as folder:
            path=Path(folder)/'main.html';path.write_text(source,encoding='utf-8')
            result=extract_html(path,'html/main.html')
        self.assertEqual(result.figures[0].figure_id,'graphical_abstract')
        self.assertEqual(result.figures[0].kind,'graphical_abstract')
        self.assertEqual(result.figures[0].label,'Graphical Abstract')
