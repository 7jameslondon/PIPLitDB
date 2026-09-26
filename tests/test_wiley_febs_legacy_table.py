import unittest
from lxml import html
from scripts.extraction.html_extractor import _table_items, _is_table_container_id, _wiley_front_matter, _bibliographic_details


class WileyFebsLegacyTableTests(unittest.TestCase):
    def test_combined_issue_range_is_preserved(self):
        root = html.fromstring('<article><a>Volume 471, Issue 2-3</a><span>pp. 173-176</span></article>')
        self.assertEqual(_bibliographic_details(root)['issue'], '2-3')

    def test_pii_table_and_notes_are_recognized(self):
        root = html.fromstring('''<article><div id="feb2s1234567890123456-TBL1">
          <header>Table Table 1. Comparison</header><div><table><tbody>
          <tr><th></th><th>A</th><th>B<a href="#feb2s1234567890123456-TBLFN1A"><sup>a</sup></a></th></tr>
          <tr><td>Value</td><td>3</td><td>60</td></tr></tbody></table></div>
          <div><ul><li id="feb2s1234567890123456-TBLFN1A"><span>a </span>From [10].</li></ul></div>
        </div></article>''')
        tables = _table_items(root, 'html/main.html')
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].label, 'Table 1')
        self.assertEqual(tables[0].parts[0].rows[1][2].text, '60')
        self.assertTrue(tables[0].footnotes_plain)
        for value in ('layout-TBL1', 'feb2s123-TBL1', 'feb2s1234567890123456-TBLFN1A'):
            self.assertFalse(_is_table_container_id(value))

    def test_direct_co_correspondence_is_bound_and_deduplicated(self):
        root = html.fromstring('''<article>
          <a id="author1" href="/authored-by/Person/A" aria-controls="a1">A. Person</a>
          <div id="a1"><p>A. Person</p><p>Institute</p>
          Also corresponding author. Fax: 123. E-mail: person@example.org
          <a href="/authored-by/Person/A">Search for more papers by this author</a></div>
        </article>''')
        blocks = _wiley_front_matter(root, 'html/main.html')
        text = '\n'.join(b.plain_text for b in blocks)
        self.assertIn('Correspondence: A. Person: Also corresponding author. Fax: 123. E-mail: person@example.org', text)
        self.assertNotIn('Search for', text)

    def test_current_linked_title_note_is_preserved(self):
        root = html.fromstring('''<article><div id="tbl1">
          <header><span>Table 1. </span>Small molecules
          <span><a href="#tln1">a)</a></span></header>
          <div><table><thead><tr><th>Compound</th></tr></thead>
          <tbody><tr><td>RG-108</td></tr></tbody></table></div>
          <div><ul>
            <li id="tln1"><span>a) </span>Abbreviations: <b>DNMTi</b>,
            DNA methyl transferase inhibitor.</li>
            <li id="unlinked"><span>b) </span>Unrelated publisher list.</li>
          </ul></div>
        </div></article>''')

        tables = _table_items(root, 'html/main.html')

        self.assertEqual(len(tables), 1)
        self.assertEqual(
            tables[0].footnotes_plain,
            ['a) Abbreviations: DNMTi, DNA methyl transferase inhibitor.'],
        )
        self.assertEqual(len(tables[0].footnotes_markdown), 1)
        self.assertIn('<strong>DNMTi</strong>', tables[0].footnotes_markdown[0])
