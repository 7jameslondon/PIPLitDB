import unittest
from lxml import html
from scripts.extraction.html_extractor import _table_items, _current_acs_split_semantic_table_details, plain_text, render_inline


def document():
    fragments = [
        '<tr><td>Left</td><td>Center</td><td>Right</td></tr>',
        '<tr><td>First section</td></tr>',
        '<tr><td>A<sup>b</sup></td><td>12</td><td></td></tr><tr><td>B</td><td>34</td><td>C</td></tr>',
        '<tr><td>Second section</td></tr>',
        '<tr><td>D</td><td></td><td>E<sub>2</sub></td></tr>',
    ]
    tables = ''.join('<div><table role="presentation" aria-labelledby="label-synt00001" '
                     'aria-describedby="caption-synt00001"><tbody>' + rows + '</tbody></table></div><div></div>'
                     for rows in fragments)
    return html.fromstring('<div id="123-content"><a id="123" scrollto-destination="123"></a>'
        '<div content-id="synt00001 d7e123"><div id="synt00001"><span id="label-synt00001">Table 1.</span>'
        '<div id="caption-synt00001"><p>Authored contacts<sup>a</sup></p></div></div>' + tables +
        '<div><p><em><sup>a</sup></em> First note.<em><sup>b</sup></em> Second note.</p></div>'
        '<div><a href="/view-large/123" target="_blank" rel="nofollow" aria-label="View large Table 1.">View Large</a></div></div></div>')


class SharedHeaderSplitTableTests(unittest.TestCase):
    def test_script_whitespace_preserves_separate_scientific_tokens(self):
        for tag, marker in [('sub', '_'), ('sup', '^')]:
            with self.subTest(tag=tag):
                node = html.fromstring(f'<td>A<{tag}>2\u2009</{tag}>B</td>')
                self.assertEqual(plain_text(node), f'A{marker}{{2}} B')
                self.assertEqual(render_inline(node, markup='html'), f'A<{tag}>2</{tag}> B')
                joined = html.fromstring(f'<td>A<{tag}>2</{tag}>B</td>')
                self.assertEqual(plain_text(joined), f'A{marker}{{2}}B')
    def test_preserves_single_table_shared_columns_section_spans_and_data_semantics(self):
        root = document()
        before = html.tostring(root)
        tables = _table_items(root, 'synthetic.html')
        self.assertEqual(html.tostring(root), before)
        self.assertEqual(len(tables), 1)
        table = tables[0]
        self.assertIn('Authored contacts', table.title_plain)
        self.assertEqual(len(table.parts), 1)
        rows = table.parts[0].rows
        self.assertEqual(len(rows), 6)
        self.assertEqual([cell.text for cell in rows[0]], ['Left', 'Center', 'Right'])
        self.assertEqual(rows[1][0].colspan, 3)
        self.assertEqual(rows[4][0].colspan, 3)
        self.assertTrue(all(cell.header for cell in rows[0]))
        self.assertFalse(any(cell.header for cell in rows[2] + rows[3] + rows[5]))
        self.assertEqual(rows[2][0].text, 'A^{b}')
        self.assertEqual(rows[5][2].text, 'E_{2}')
        self.assertEqual(table.footnotes_plain, ['[a] First note.', '[b] Second note.'])

    def test_rejects_unbound_or_ambiguous_fragments(self):
        for mutation in ('description', 'label', 'geometry', 'hidden', 'control'):
            with self.subTest(mutation=mutation):
                root = document()
                tables = root.xpath('.//table')
                if mutation == 'description': tables[2].set('aria-describedby', 'elsewhere')
                elif mutation == 'label': tables[3].set('aria-labelledby', 'elsewhere')
                elif mutation == 'geometry': tables[4].xpath('.//td')[0].set('colspan', '2')
                elif mutation == 'hidden': root[1][2].text = 'Authored content'
                else: root.xpath('.//a[@rel]')[0].set('aria-label', 'View large Table 2.')
                self.assertIsNone(_current_acs_split_semantic_table_details(root[1]))
