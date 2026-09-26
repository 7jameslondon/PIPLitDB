import tempfile
import unittest
from pathlib import Path

from scripts.extraction.html_extractor import extract_html
from tests.test_pnas_html_extractor import PNAS_HTML


class PnasVariableSiSectionTests(unittest.TestCase):
    def test_authenticated_workbook_card_retains_filename_not_download_chrome(self):
        source = PNAS_HTML.replace('article.si.pdf', 'article.sd01.xls').replace(
            '<div>Supporting Information (PDF)</div><div>Supporting Information</div>',
            '<div>article.sd01.xls</div>')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'article.html'
            path.write_text(source, encoding='utf-8')
            article = extract_html(path, 'article.html')
        self.assertEqual([b.plain_text for b in article.supporting_information], ['article.sd01.xls'])

    def test_si_heading_is_authenticated_independently_of_number_and_table_tag(self):
        source = PNAS_HTML.replace('id="sec-2"', 'id="sec-4"').replace(
            '<div id="st01"><table><tr><td>SI value</td></tr></table></div>',
            '<figure id="st01"><figcaption>Primer table</figcaption><table><tr><td>SI value</td></tr></table></figure>')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'article.html'
            path.write_text(source, encoding='utf-8')
            article = extract_html(path, 'article.html')
        self.assertEqual([x.label for x in article.figures], ['Figure 1'])
        self.assertEqual(len(article.tables), 1)
        text = ' '.join(x.plain_text for s in article.sections for x in s.blocks)
        self.assertNotIn('SI prose', text)
        self.assertNotIn('SI value', text)
        self.assertIn('A result.', text)

    def test_other_numbered_section_is_retained(self):
        source = PNAS_HTML.replace('id="sec-2"', 'id="sec-4"').replace('SI Materials and Methods', 'Discussion')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'article.html'
            path.write_text(source, encoding='utf-8')
            article = extract_html(path, 'article.html')
        self.assertIn('Discussion', [s.heading for s in article.sections])
