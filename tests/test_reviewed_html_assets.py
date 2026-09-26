import base64
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from PIL import Image
from scripts.extraction.models import SourceFile
from scripts.extraction.reviewed_html_assets import select_reviewed_html_assets


class ReviewedHtmlAssetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        out = io.BytesIO(); Image.new('RGB',(3,2),'blue').save(out,format='PNG')
        self.pixels=out.getvalue()
        markup='<figure id="s1"><img src="data:image/png;base64,'+base64.b64encode(self.pixels).decode()+'"></figure>'
        self.path=Path(self.tmp.name)/'main.html'; self.path.write_text(markup,encoding='utf8')
        digest=hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.source=SourceFile('main_html',self.path,'private/main.html',self.path.stat().st_size,digest,'text/html')
        self.spec={'asset_id':'s1','source_path':'private/main.html','source_sha256':digest,
                   'xpath':'//figure[@id="s1"]/img','image_sha256':hashlib.sha256(self.pixels).hexdigest(),
                   'output_path':'figures/s1.png','reason':'higher quality','evidence':'same complete panels'}
        self.crop={'asset_id':'s1','category':'supplement_figure'}
        self.supplements=[NS(figures=[NS(figure_id='s1',label='Figure S1')])]

    def select(self,spec=None):
        return select_reviewed_html_assets([spec or self.spec],[self.crop],[self.source],self.supplements)

    def test_original_bytes_and_provenance_replace_only_selected_crop(self):
        pending,crops=self.select()
        self.assertEqual(crops,[]); self.assertEqual(pending[0].data,self.pixels)
        self.assertEqual(pending[0].source_path,'private/main.html')
        self.assertTrue(pending[0].source_locator.endswith('/figure/img'))
        self.assertEqual(self.supplements[0].figures[0].figure_id,'s1')

    def test_mismatched_source_or_image_hash_fails_closed(self):
        for key in ('source_sha256','image_sha256'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError,'hash mismatch'):
                self.select({**self.spec,key:'0'*64})

    def test_ambiguous_missing_and_nonimage_selectors_fail(self):
        for xpath in ('//missing','//figure','//figure | //img'):
            with self.subTest(xpath=xpath), self.assertRaisesRegex(ValueError,'exactly one img'):
                self.select({**self.spec,'xpath':xpath})

    def test_remote_image_is_not_fetched(self):
        self.path.write_text('<img src="https://example.invalid/pixel.png">',encoding='utf8')
        digest=hashlib.sha256(self.path.read_bytes()).hexdigest()
        source=SourceFile('main_html',self.path,'private/main.html',self.path.stat().st_size,digest,'text/html')
        with self.assertRaisesRegex(ValueError,'data URI'):
            select_reviewed_html_assets([{**self.spec,'source_sha256':digest,'xpath':'//img'}],
                                       [self.crop],[source],self.supplements)

    def test_duplicate_or_unknown_target_and_wrong_extension_fail(self):
        with self.assertRaisesRegex(ValueError,'one existing'):
            select_reviewed_html_assets([self.spec,self.spec],[self.crop],[self.source],self.supplements)
        with self.assertRaisesRegex(ValueError,'one existing'):
            self.select({**self.spec,'asset_id':'missing'})
        with self.assertRaisesRegex(ValueError,'extension'):
            self.select({**self.spec,'output_path':'figures/s1.jpg'})
