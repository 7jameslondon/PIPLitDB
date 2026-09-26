import unittest

from tests import test_sciencedirect_html_extractor as helpers


class ScienceDirectTableWordIdTests(unittest.TestCase):
    extract = helpers.ScienceDirectHtmlExtractionTests.extract

    def test_table_word_id_retains_native_cells_notes_and_omits_prose_duplicate(self):
        result = self.extract('''<article><h1>Example</h1><div id="body">
        <section id="aep-section-id1"><h2>Results</h2><div>Before.</div>
        <div id="TABLE1"><span><span><p><span>Table 1</span>. Measurements</p></span></span>
        <div><table><thead><tr><th>Item</th><th>Value</th></tr></thead>
        <tbody><tr><td>A<sup>a</sup></td><td>0.028</td></tr></tbody></table></div>
        <dl><dt id="TBLFN1">a</dt><dd><div>Authored note.</div></dd></dl></div>
        <div>After.</div></section></div></article>''')
        self.assertEqual(len(result.tables), 1)
        self.assertIn('Measurements', result.tables[0].title_plain)
        self.assertEqual(result.tables[0].parts[0].rows[1][1].text, '0.028')
        prose = ' '.join(block.plain_text for section in result.sections for block in section.blocks)
        self.assertNotIn('Measurements', prose)
        self.assertIn('Before.', prose)
        self.assertIn('After.', prose)

    def test_layout_id_without_table_is_not_scientific_table(self):
        result = self.extract('<article><h1>Example</h1><div id="TABLE1">Layout</div></article>')
        self.assertEqual(result.tables, [])

    def test_simple_para_id_caption_preserves_title(self):
        result = self.extract('''<article><h1>Example</h1><div id="body">
        <div id="TABLE1"><span><span><p id="simple-para0040"><span>Table 1</span>. Native <strong>1</strong> title</p></span></span>
        <div><table><tr><th>Value</th></tr><tr><td>25</td></tr></table></div></div></div></article>''')
        self.assertEqual(result.tables[0].title_plain, 'Table 1. Native 1 title')
        self.assertIn('<strong>1</strong>', result.tables[0].title_markdown)

    def test_table_word_requires_sciencedirect_article_ancestry(self):
        result = self.extract('''<article><h1>Example</h1><div id="TABLE1">
        <span><span><p>Table 1. Layout</p></span></span>
        <div><table><tr><td>Not authenticated AEP</td></tr></table></div></div></article>''')
        self.assertEqual(result.tables, [])


if __name__ == '__main__':
    unittest.main()
