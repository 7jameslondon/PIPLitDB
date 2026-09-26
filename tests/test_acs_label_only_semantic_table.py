import unittest
from lxml import html
from scripts.extraction.html_extractor import _table_items, _current_acs_unmarked_semantic_table_details


class AcsLabelOnlySemanticTableTests(unittest.TestCase):
    def document(self):
        return html.fromstring('''<div id="123-content"><a id="123" scrollto-destination="123"></a>
<div content-id="ab123t00001 d7e370"><div id="ab123t00001"><span id="label-ab123t00001">Table 1.</span></div>
<div><table role="presentation" aria-labelledby="label-ab123t00001"><tbody>
<tr><td>A</td><td>Condition</td><td>Value</td></tr><tr><td></td><td>X</td><td>0.38</td></tr>
<tr><td>B</td><td>Condition</td><td>Value</td></tr><tr><td></td><td>Y</td><td>0.11</td></tr>
</tbody></table></div><div></div><div><p><sup>a</sup> Authored note.</p></div>
<div><a href="/view-large/123" target="_blank" rel="nofollow" aria-label="View large Table 1.">View Large</a></div></div></div>''')

    def test_retains_label_rows_and_notes_without_inventing_caption(self):
        tables = _table_items(self.document(), 'synthetic.html')
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].table_id, 'table_001')
        self.assertEqual(tables[0].title_plain, 'Table 1.')
        self.assertEqual(len(tables[0].parts[0].rows), 4)
        self.assertEqual(tables[0].parts[0].rows[-1][-1].text, '0.11')
        self.assertEqual(tables[0].footnotes_plain, ['^{a} Authored note.'])

    def test_rejects_incomplete_publisher_binding(self):
        for change in ('label', 'hidden', 'control'):
            with self.subTest(change=change):
                root = self.document()
                if change == 'label': root.xpath('.//table')[0].set('aria-labelledby', 'other')
                elif change == 'hidden': root[1][2].text = 'Unrelated content'
                else: root.xpath('.//a[@rel]')[0].set('aria-label', 'View large Table 2.')
                self.assertIsNone(_current_acs_unmarked_semantic_table_details(root[1]))
