import unittest
from lxml import html
from scripts.extraction.html_extractor import _legacy_acs_semantic_table_details, _table_items


class AcsSuperscriptTableNoteTests(unittest.TestCase):
    def document(self, marker='<sup>a</sup>'):
        return html.fromstring(f'''<div id="123-content"><a id="123"></a>
<div content-id="tbl1 sec1.3"><div id="tbl1"><span id="label-tbl1">Table 1.</span>
<div id="caption-tbl1"><p>Measured values<a reveal-id="tbl1-fn1">a</a></p></div></div>
<div><table role="table" aria-labelledby="label-tbl1" aria-describedby="caption-tbl1">
<thead><tr><th>Group</th><th colspan="2">Treatment</th></tr></thead>
<tbody><tr><td>X</td><td>2</td><td>3</td></tr></tbody></table></div><div></div>
<div><div id="tbl1-fn1" content-id="tbl1-fn1"><span><span rel="nofollow">{marker}</span></span>
<p>Authored note.</p></div></div><div><a href="/view-large/123" target="_blank"
rel="nofollow" aria-label="View large Table 1.">View Large</a></div></div></div>''')

    def test_superscript_marker_preserves_table_and_note(self):
        root = self.document()
        tables = _table_items(root, 'synthetic.html')
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].table_id, 'table_001')
        self.assertEqual(tables[0].footnotes_plain, ['[a] Authored note.'])
        self.assertEqual(len(tables[0].parts[0].rows), 2)

    def test_plain_marker_still_works(self):
        node = self.document('a')[1]
        self.assertEqual(_legacy_acs_semantic_table_details(node)['notes'][0]['label'], 'a')

    def test_mixed_or_complex_marker_is_not_silently_flattened(self):
        for marker in ('x<sup>a</sup>', '<sup>a</sup>x', '<sup><em>a</em></sup>'):
            with self.subTest(marker=marker):
                self.assertIsNone(_legacy_acs_semantic_table_details(self.document(marker)[1]))
