import unittest
from lxml import html

from scripts.extraction.html_extractor import (
    _remove_terminal_crossref_appendices,
    plain_text,
)
from tests import test_sciencedirect_html_extractor as helpers


class TableTitleNoteTests(unittest.TestCase):
    extract = helpers.ScienceDirectHtmlExtractionTests.extract

    def test_sentence_final_table_note_survives_complete_extraction(self):
        result = self.extract('''<body><article><h1>Synthetic title</h1>
        <div id="body"><section><h2>Results</h2><div id="p0010">Body.</div>
        <div id="tbl4"><span><span id="cap0055"><p id="tspara0025">
        <span>Table 4</span>. Values.<a href="#tbl4fna" name="btbl4fna"><span><span><sup>a</sup></span></span></a>
        </p></span></span><div><table><thead><tr><th>Item</th><th>Value</th></tr></thead>
        <tbody><tr><td>A</td><td>2</td></tr></tbody></table></div>
        <dl><dt id="tbl4fna">a</dt><dd><div id="ntpara0010">Authored conditions.</div></dd></dl>
        </div></section></div></article></body>''')
        self.assertEqual(result.tables[0].title_plain, 'Table 4. Values.^{a}')
        self.assertIn('<sup>a</sup>', result.tables[0].title_markdown)
        self.assertEqual(result.tables[0].footnotes_plain, ['[a] Authored conditions.'])

    def test_numbered_linkouts_removed_but_note_and_contextual_links_remain(self):
        root = html.fromstring('''<div><p>Finished.<a href="#fig1">1</a>, <a href="#tbl2">2</a></p>
        <p>Title.<a href="#tbl2fn1"><sup>1</sup></a></p>
        <p>See Table <a href="#tbl2">2</a>.</p></div>''')
        _remove_terminal_crossref_appendices(root)
        self.assertEqual([plain_text(p) for p in root.xpath('./p')],
                         ['Finished.', 'Title.^{1}', 'See Table 2.'])


if __name__ == '__main__':
    unittest.main()
