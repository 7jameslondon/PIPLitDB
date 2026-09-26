import unittest
from lxml import html
from scripts.extraction.html_extractor import _normalize_acs_prose_with_figures, plain_text


class AcsProseFigureTests(unittest.TestCase):
    def document(self, doi='ab123456'):
        return html.fromstring(f'''<html><body><a href="https://doi.org/10.1021/{doi}">DOI</a>
<div id="123-content"><div><strong>Synthesis.</strong> Authored text.
<figure reveal-group-id="[parent-legacy-section-id]" id="figure-1"><img path-from-xml="ab123456f00001.tif"/><p>Caption one.</p></figure> Continued text.
<figure reveal-group-id="[parent-legacy-section-id]" id="figure-2"><img path-from-xml="ab123456f00002.tif"/><p>Caption two.</p></figure></div></div></body></html>''')

    def test_prose_and_tail_survive_without_captions_or_duplicate_cards(self):
        doc = self.document()
        _normalize_acs_prose_with_figures(doc)
        host = doc.xpath('//div[@id="123-content"]')[0]
        self.assertEqual([child.tag for child in host], ['p', 'figure', 'figure'])
        self.assertEqual(plain_text(host[0]), 'Synthesis. Authored text. Continued text.')
        self.assertEqual([plain_text(f) for f in host.xpath('./figure')], ['Caption one.', 'Caption two.'])

    def test_unrelated_doi_and_structural_wrappers_are_unchanged(self):
        for doc in (self.document('other'), self.document()):
            if doc.xpath('//a')[0].get('href').endswith('ab123456'):
                doc.xpath('//div[@id="123-content"]/div')[0].append(html.Element('p'))
            before = html.tostring(doc)
            _normalize_acs_prose_with_figures(doc)
            self.assertEqual(html.tostring(doc), before)
