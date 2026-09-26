import unittest
from lxml import html
from scripts.extraction.html_extractor import _normalize_acs_unnumbered_artwork, _figure_items, plain_text


class AcsUnnumberedArtworkTests(unittest.TestCase):
    def make(self, prefix='ab123456'):
        return html.fromstring(f'''<html><body><a href="https://doi.org/10.1021/{prefix}">DOI</a><div id="content"><div>Authored distamy<div reveal-group-id="[parent-legacy-section-id]"><span id="viewTranscriptId_"></span><div><a href="/view-large/figure/123/ab123456f1.tif"><img src="data:image/png;base64,eA==" alt="Figure. Refer to the image caption for details." path-from-xml="ab123456f1.tif"/></a><a role="button" path-from-xml="ab123456f1.tif">View Large</a></div></div>cin prose.</div></div></body></html>''')

    def test_keeps_paragraph_tail_and_distinct_unnumbered_asset(self):
        doc=self.make(); _normalize_acs_unnumbered_artwork(doc)
        self.assertEqual(plain_text(doc.xpath('//p')[0]), 'Authored distamycin prose.')
        figures=_figure_items(doc,'main.html')
        self.assertEqual([(f.figure_id,f.label) for f in figures], [('unnumbered_artwork_001','Unnumbered artwork 1')])
        self.assertEqual(len(doc.xpath('//img')),1)

    def test_mismatched_doi_does_not_reclassify_generic_div(self):
        doc=self.make('different'); before=html.tostring(doc)
        _normalize_acs_unnumbered_artwork(doc)
        self.assertEqual(html.tostring(doc),before)

    def test_svg_artwork_keeps_heading_prose_and_distinct_asset(self):
        doc = self.make()
        for node in doc.xpath('//*[@path-from-xml or @href]'):
            for attribute in ('path-from-xml', 'href'):
                if node.get(attribute):
                    node.set(attribute, node.get(attribute).replace('.tif', '.svg'))
        doc.xpath('//img')[0].set('src', 'data:image/svg+xml;base64,PHN2Zy8+')
        parent = doc.xpath('//div[@id="content"]/div')[0]
        heading = html.Element('strong')
        heading.text = '3. Synthesis. '
        heading.tail = parent.text
        parent.text = None
        parent.insert(0, heading)
        _normalize_acs_unnumbered_artwork(doc)
        self.assertEqual(plain_text(doc.xpath('//p')[0]), '3. Synthesis. Authored distamycin prose.')
        self.assertEqual([item.figure_id for item in _figure_items(doc, 'main.html')], ['unnumbered_artwork_001'])
        self.assertEqual(doc.xpath('//figure//img')[0].get('path-from-xml'), 'ab123456f1.svg')

    def test_svg_with_mismatched_view_control_stays_unchanged(self):
        doc = self.make()
        doc.xpath('//img')[0].set('path-from-xml', 'ab123456f1.svg')
        before = html.tostring(doc)
        _normalize_acs_unnumbered_artwork(doc)
        self.assertEqual(html.tostring(doc), before)

    def test_inline_heading_at_start_of_div_does_not_hide_prose(self):
        doc=self.make(); parent=doc.xpath('//div[@id="content"]/div')[0]
        parent.text=None
        heading=html.Element('strong'); heading.text='Heading. '
        heading.tail='Authored distamy'; parent.insert(0,heading)
        _normalize_acs_unnumbered_artwork(doc)
        self.assertEqual(plain_text(doc.xpath('//p')[0]),'Heading. Authored distamycin prose.')
