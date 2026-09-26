import unittest
from lxml import html
from scripts.extraction.html_extractor import _normalize_acs_numbered_charts, _figure_items, _inside_excluded_body_region


class AcsNumberedChartTests(unittest.TestCase):
    def document(self):
        return html.fromstring('''<div><a href="https://doi.org/10.1021/ab123">Article</a>
<div id="123-content"><a id="123"></a><div reveal-group-id="d7e151">
<span id="viewTranscriptId_"></span><div>Chart 1</div>
<div><img src="data:image/png;base64,eA==" path-from-xml="ab123_0001.tif" alt="Chart 1. Refer to the image caption for details.">
<a aria-label="View large Chart 1." path-from-xml="ab123_0001.tif">View Large</a></div>
<div><div>Chart 1.</div><div><p>Structures of <strong>1</strong>–<strong>4</strong>.</p></div></div>
</div></div></div>''')

    def test_chart_identity_caption_and_prose_exclusion(self):
        root = self.document()
        _normalize_acs_numbered_charts(root)
        figures = _figure_items(root, 'synthetic.html')
        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0].figure_id, 'chart_001')
        self.assertEqual(figures[0].label, 'Chart 1')
        self.assertEqual(figures[0].caption_plain, 'Structures of 1–4.')
        self.assertTrue(_inside_excluded_body_region(root.xpath('.//p')[0]))

    def test_mismatched_label_stays_unmodified(self):
        root = self.document()
        root.xpath('.//img')[0].set('alt', 'Chart 2. Refer to the image caption for details.')
        _normalize_acs_numbered_charts(root)
        self.assertEqual(root.xpath('.//figure'), [])

    def test_nested_div_caption_body_is_supported(self):
        root = self.document()
        caption_body = root.xpath('.//div[@reveal-group-id]/div[last()]/div[2]')[0]
        paragraph = caption_body.xpath('./p')[0]
        paragraph.tag = 'div'

        _normalize_acs_numbered_charts(root)
        figures = _figure_items(root, 'synthetic.html')

        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0].figure_id, 'chart_001')
        self.assertEqual(figures[0].label, 'Chart 1')
        self.assertEqual(figures[0].caption_plain, 'Structures of 1–4.')
