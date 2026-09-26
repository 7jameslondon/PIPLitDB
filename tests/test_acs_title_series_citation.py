import unittest
from lxml import html
from scripts.extraction.html_extractor import _article_title


class AcsTitleSeriesCitationTests(unittest.TestCase):
    def test_series_citation_removed_without_losing_title_tail(self):
        document = html.fromstring('''<html><body><h1>Series. 41. (<a ref-data-modal-source-id="ab123456b00001">1</a>) Study of H<sub>2</sub>O</h1><a href="https://doi.org/10.1021/ab123456">DOI</a></body></html>''')
        original = html.tostring(document)
        self.assertEqual(_article_title(document), 'Series. 41. Study of H_{2}O')
        self.assertEqual(html.tostring(document), original)

    def test_near_misses_retain_scientific_numbers(self):
        for marker, doi in [('1', 'different'), ('2', 'ab123456')]:
            document = html.fromstring(f'''<html><body><h1>Study (<a ref-data-modal-source-id="ab123456b00001">{marker}</a>) result</h1><a href="https://doi.org/10.1021/{doi}">DOI</a></body></html>''')
            self.assertEqual(_article_title(document), f'Study ({marker}) result')
