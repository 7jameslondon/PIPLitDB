import unittest
from lxml import html
from scripts.extraction.html_extractor import _table_items, _current_acs_split_semantic_table_details


def document():
    return html.fromstring('''<div id="456-content"><a id="456" scrollto-destination="456"></a>
      <div content-id="synt00001 d7e9"><div id="synt00001"><span id="label-synt00001">Table 1.</span>
      <div id="caption-synt00001"><p>Reaction protocol</p></div></div>
      <div><table role="table" aria-labelledby="label-synt00001" aria-describedby="caption-synt00001">
      <tbody><tr><td colspan="2">cycle</td></tr></tbody></table></div><div></div>
      <div><table role="table" aria-labelledby="label-synt00001" aria-describedby="caption-synt00001">
      <tbody><tr><td></td><td></td><td>time</td></tr><tr><td>1. wash</td><td>solvent</td><td>30 s</td></tr>
      <tr><td colspan="5">authored spanning row</td></tr></tbody></table></div><div></div>
      <div><a href="/view-large/456" target="_blank" rel="nofollow" aria-label="View large Table 1.">View Large</a></div>
      </div></div>''')


class TwoFragmentHeaderTests(unittest.TestCase):
    def test_group_header_rejoins_and_body_is_not_a_header(self):
        root = document()
        original = html.tostring(root)
        tables = _table_items(root, 'synthetic.html')
        self.assertEqual(html.tostring(root), original)
        self.assertEqual(len(tables), 1)
        rows = tables[0].parts[0].rows
        self.assertEqual([c.text for c in rows[0]], ['cycle', 'time'])
        self.assertEqual(rows[0][0].colspan, 2)
        self.assertTrue(all(c.header for c in rows[0]))
        self.assertFalse(any(c.header for c in rows[1]))
        self.assertEqual(rows[2][0].colspan, 5)

    def test_rejects_mismatched_bindings_or_nonempty_header_placeholders(self):
        for field in ('aria-describedby', 'placeholder'):
            with self.subTest(field=field):
                root = document()
                table = root.xpath('.//table')[1]
                if field == 'placeholder':
                    table.xpath('.//td')[0].text = 'lost data'
                else:
                    table.set(field, 'elsewhere')
                self.assertIsNone(_current_acs_split_semantic_table_details(root[1]))


if __name__ == '__main__':
    unittest.main()
