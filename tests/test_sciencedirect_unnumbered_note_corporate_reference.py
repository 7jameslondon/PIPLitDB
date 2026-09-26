import unittest
from lxml import html
from tests import test_sciencedirect_html_extractor as helpers
from scripts.extraction.html_extractor import _sciencedirect_reference


class ScienceDirectNoteAndCorporateReferenceTests(unittest.TestCase):
    extract = helpers.ScienceDirectHtmlExtractionTests.extract

    def test_simple_para_after_table_is_note_not_body(self):
        article = self.extract('''<article><h1>Example</h1><div id="body"><section><h2>Results</h2>
        <div id="TABLE1"><span><span><p>Table 1. Distances</p></span></span>
        <div><table><tr><th>R</th></tr><tr><td>2.9</td></tr></table></div>
        <div><div id="simple-para0050">Distances <em>R</em><sub>xy</sub> use both strands.</div></div></div>
        <div id="simple-para0051">Ordinary prose.</div></section></div></article>''')
        self.assertEqual(article.tables[0].footnotes_plain, ['Distances R_{xy} use both strands.'])
        self.assertIn('<sub>xy</sub>', article.tables[0].footnotes_markdown[0])
        self.assertNotIn('Distances R', ' '.join(b.plain_text for s in article.sections for b in s.blocks))

    def test_corporate_author_with_digit_has_field_boundaries(self):
        item = html.fromstring('''<li><span><a id="ref-id-BIB10" href="#bBIB10">CCP4 1994</a></span>
        <span id="reference0050"><div><div>Corporate Project No. 4</div><div id="ref-id-reference0050">Title</div></div>
        <div>Journal, 50 (1994), pp. 1-2</div><div lang="en"><a href="https://scholar.google.com/">Google Scholar</a></div></span></li>''')
        self.assertEqual(_sciencedirect_reference(item)[1], 'CCP4 1994. Corporate Project No. 4 Title Journal, 50 (1994), pp. 1-2')

    def test_thesis_with_empty_control_wrapper_has_field_boundaries(self):
        item = html.fromstring('''<li><span><a id="ref-id-BIB50" href="#bBIB50">Author 1998</a></span>
        <span id="reference0250"><div><div>S Author</div></div><div id="ref-id-reference0250">University (1998)</div>
        <div>PhD thesis</div><div lang="en"></div></span></li>''')
        self.assertEqual(_sciencedirect_reference(item)[1], 'Author 1998. S Author University (1998) PhD thesis')
        item.xpath('.//div[@id]')[0].set('id','ref-id-reference0999')
        self.assertIsNone(_sciencedirect_reference(item))
