import unittest
from lxml import html
from scripts.extraction.html_extractor import _acs_source_prefixed_presentation_raster_table_details, _table_items, _figure_items


class AcsRasterTableScientificNotesTests(unittest.TestCase):
    def document(self, group='d7e208'):
        return html.fromstring(f'''<div id="123-content"><figure content-id="ab123t00003 {group}" id="table-3">
<div id="ab123t00003"><span id="label-ab123t00003">Table 3.</span><div id="caption-ab123t00003"><p>Affinity</p></div></div>
<div><table role="presentation" aria-labelledby="label-ab123t00003" aria-describedby="caption-ab123t00003"><tbody><tr><td><div><a><img src="data:image/png;base64,AA==" alt="Graphic. Refer to the image caption for details." path-from-xml="ab123_0001.tif"></a></div></td></tr></tbody></table></div>
<div></div><div><p><em><sup>a</sup></em> Constants in M<sup>-1</sup>, repeated M<sup>-1</sup>. <em><sup>b</sup></em> 10<sup>8</sup> scale.</p></div>
<div><a href="/view-large/123" target="_blank" rel="nofollow" aria-label="View large Table 3.">View Large</a></div></figure></div>''')

    def test_scientific_exponents_do_not_prevent_raster_table_recognition(self):
        for group in ('d7e208','[parent-legacy-section-id]'):
            with self.subTest(group=group):
                root=self.document(group)
                details=_acs_source_prefixed_presentation_raster_table_details(root[0])
                self.assertIsNotNone(details)
                tables=_table_items(root,'synthetic.html')
                self.assertEqual(len(tables),1)
                self.assertEqual(tables[0].source_kind,'image')
                self.assertIn('M^{-1}',tables[0].footnotes_plain[0])
                self.assertEqual(len(tables[0].footnotes_plain),2)

    def test_rejects_wrong_aria_binding_and_unknown_group(self):
        root=self.document()
        root.xpath('.//table')[0].set('aria-describedby','other')
        self.assertIsNone(_acs_source_prefixed_presentation_raster_table_details(root[0]))
        self.assertIsNone(_acs_source_prefixed_presentation_raster_table_details(self.document('unknown')[0]))

    def test_legacy_gif_raster_is_recognized_with_same_structural_guards(self):
        root = self.document()
        graphic = root.xpath('.//img')[0]
        graphic.set('src', 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==')
        graphic.set('path-from-xml', 'ab123u00001a.gif')
        details = _acs_source_prefixed_presentation_raster_table_details(root[0])
        self.assertIsNotNone(details)
        self.assertEqual(len(_table_items(root, 'synthetic.html')), 1)
        root.xpath('.//table')[0].set('aria-labelledby', 'wrong-label')
        self.assertIsNone(_acs_source_prefixed_presentation_raster_table_details(root[0]))
