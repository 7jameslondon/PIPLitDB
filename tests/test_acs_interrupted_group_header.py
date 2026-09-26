import unittest
from lxml import html
from scripts.extraction.html_extractor import _table_items, _current_acs_split_semantic_table_details


def document():
    fragments = [
        '<tr><td></td><td colspan="2">Shared</td></tr>',
        '<tr><td></td><td></td><td></td><td>Other</td><td>Last</td></tr>'
        '<tr><td>Compound</td><td>A</td><td>B</td><td>C</td><td>D</td></tr>'
        '<tr><td>X</td><td>1</td><td>2</td><td>3</td><td>4</td></tr>',
        '<tr><td></td><td>First</td><td>Second</td><td colspan="2">Shared again</td></tr>',
        '<tr><td>Compound</td><td>E</td><td>F</td><td>G</td><td>H</td></tr>'
        '<tr><td>Y</td><td>5</td><td>6</td><td>7</td><td>8</td></tr>',
    ]
    tables = ''.join('<div><table role="' + ('table' if i == 0 else 'presentation') +
        '" aria-labelledby="label-synt00001" aria-describedby="caption-synt00001"><tbody>' + rows +
        '</tbody></table></div><div></div>' for i, rows in enumerate(fragments))
    return html.fromstring('<div id="123-content"><a id="123" scrollto-destination="123"></a>'
        '<div content-id="synt00001 d7e123"><div id="synt00001"><span id="label-synt00001">Table 1.</span>'
        '<div id="caption-synt00001"><p>Constants</p></div></div>' + tables +
        '<div><p><em><sup>a</sup></em> Source note.</p></div>'
        '<div><a href="/view-large/123" target="_blank" rel="nofollow" aria-label="View large Table 1.">View Large</a></div></div></div>')


class InterruptedGroupHeaderTests(unittest.TestCase):
    def test_preserves_two_parts_and_merges_only_blank_overlap(self):
        root = document()
        before = html.tostring(root)
        tables = _table_items(root, 'synthetic.html')
        self.assertEqual(html.tostring(root), before)
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0].parts), 2)
        first, second = tables[0].parts
        self.assertEqual([c.text for c in first.rows[0]], ['', 'Shared', 'Other', 'Last'])
        self.assertEqual([c.colspan for c in first.rows[0]], [1, 2, 1, 1])
        self.assertEqual(len(first.rows), 3)
        self.assertEqual(len(second.rows), 3)
        self.assertTrue(all(c.header for c in first.rows[0] + first.rows[1] + second.rows[0] + second.rows[1]))
        self.assertFalse(any(c.header for c in first.rows[2] + second.rows[2]))

    def test_does_not_guess_absent_span(self):
        root = document()
        root.xpath('.//table')[2].xpath('.//td')[-1].attrib.pop('colspan')
        table = _table_items(root, 'synthetic.html')[0]
        self.assertEqual(sum(c.colspan for c in table.parts[1].rows[0]), 4)

    def test_rejects_conflicting_overlap_or_unbound_fragments(self):
        for mutation in ('overlap', 'binding', 'width', 'control'):
            with self.subTest(mutation=mutation):
                root = document()
                tables = root.xpath('.//table')
                if mutation == 'overlap': tables[1].xpath('.//td')[0].text = 'Conflicting header'
                elif mutation == 'binding': tables[2].set('aria-describedby', 'wrong')
                elif mutation == 'width': tables[3].xpath('.//td')[0].set('colspan', '2')
                else: root.xpath('.//a[@rel]')[0].set('aria-label', 'View large Table 2.')
                self.assertIsNone(_current_acs_split_semantic_table_details(root[1]))
