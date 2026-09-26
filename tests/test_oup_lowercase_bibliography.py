import unittest
from tests import test_oup_legacy_modal_cards as legacy
from lxml import etree, html
from scripts.extraction.html_extractor import _oup_silverchair_reference_blocks

fixture = legacy.fixture


class OupLowercaseBibliographyTests(unittest.TestCase):
    extract = legacy.OupLegacyModalTests.extract
    def test_lowercase_contiguous_ids_authenticate_article(self):
        source = fixture().replace('AB1C1', 'b1').replace('AB1C2', 'b2')
        result = self.extract(source)
        self.assertEqual(len(result.references), 2)
        self.assertIn('A book', result.references[0].plain_text)
        self.assertNotIn('Cite', [s.heading for s in result.sections])

    def test_divided_address_and_correspondence_marker(self):
        source = fixture().replace('<div>Institute</div>', '<div><div>Institute City</div><div>ST 12345, USA</div></div><div><div content-id="cor1"><sup>*</sup> To whom correspondence should be addressed. Email: author@example.org</div></div>')
        result = self.extract(source)
        front = '\n'.join(b.plain_text for b in result.front_matter)
        self.assertIn('Institute City ST 12345, USA', front)
        self.assertIn('To whom correspondence should be addressed.', front)
        self.assertNotIn('^{*}', front)

    def test_mixed_case_ids_do_not_authenticate_contiguity(self):
        result = self.extract(fixture().replace('AB1C1', 'b1').replace('AB1C2', 'B2'))
        self.assertEqual(result.figures, [])

    def test_empty_worldcat_toolbar_label_retains_complete_citation(self):
        source = '<div><h2>References</h2><div><div content-id="b1"><div><span><a name="jumplink-b1" aria-label="jumplink-b1"></a></span></div><div><div id="ref-auto-b1"><span>1</span><div>Author. A complete citation.<div><a href="https://scholar.google.com/scholar_lookup?title=Test">Google Scholar</a><a href="https://doi.org/10.1000/test">Crossref</a><a href="https://www.worldcat.org/search?q=ti:Test&amp;qt=advanced&amp;dblist=638"></a></div></div></div></div></div></div></div>'
        h = html.fromstring(source).xpath('./h2')[0]
        refs = _oup_silverchair_reference_blocks(h, 'fixture')
        self.assertEqual(len(refs), 1)
        self.assertIn('A complete citation.', refs[0].plain_text)
        self.assertNotIn('Scholar', refs[0].plain_text)
        bad = html.fromstring(source.replace('www.worldcat.org', 'unknown.example')).xpath('./h2')[0]
        self.assertIsNone(_oup_silverchair_reference_blocks(bad, 'fixture'))

    def test_lowercase_table_without_duplicate_retains_unmarked_note(self):
        table = '<div swap-content-for-modal="true"><div><div id="tbl1"><span id="label-20"><strong>Table 1</strong></span><div><a role="button" target="_blank" href="/view-large/20" aria-describedby="label-20">Open in new tab</a></div><div id="caption-20"><p>Binding constants (M<sup>−1</sup>)</p></div></div><div><table role="table" aria-labelledby="label-tbl1" aria-describedby="caption-tbl1"><thead><tr><th>Sample</th><th>Value</th></tr></thead><tbody><tr><td>A</td><td>7.7 × 10<sup>10</sup></td></tr></tbody></table></div><div></div><div><span id="fn-"></span><div content-id=""><span><p>Mean of three experiments.</p></span></div></div></div></div>'
        result = self.extract(fixture().replace('<h2 id="3">References', table + '<h2 id="3">References'))
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].footnotes_plain, ['Mean of three experiments.'])

    def test_trailing_author_notes_preserves_article_and_semantic_table(self):
        table = '<div swap-content-for-modal="true"><div><div id="tbl1"><span id="label-20"><strong>Table 1</strong></span><div><a role="button" target="_blank" href="/view-large/20" aria-describedby="label-20">Open in new tab</a></div><div id="caption-20"><p>Binding constants</p></div></div><div><table role="table" aria-labelledby="label-tbl1" aria-describedby="caption-tbl1"><thead><tr><th>Sample</th><th>Value</th></tr></thead><tbody><tr><td>A</td><td>7.7</td></tr></tbody></table></div><div></div><div><span id="fn-"></span><div content-id=""><span><p>Mean of three experiments.</p></span></div></div></div></div>'
        document = html.fromstring(
            fixture().replace('<h2 id="3">References', table + '<h2 id="3">References')
        )
        article = document.xpath('//*[@id="ContentTab"]/div')[0]
        author_notes = etree.SubElement(
            article, 'h2', id='authorNotesSectionTitle'
        )
        author_notes.text = 'Author notes'

        result = self.extract(html.tostring(document, encoding='unicode'))

        self.assertEqual([figure.label for figure in result.figures], ['Figure 1', 'Scheme 1'])
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].label, 'Table 1')
        self.assertEqual(result.tables[0].footnotes_plain, ['Mean of three experiments.'])

    def test_unheaded_acknowledgement_after_discussion(self):
        tail = '<h2 id="7">DISCUSSION</h2><p>Scientific discussion.</p><p>The work was supported by a fund. We thank the laboratory.</p><p><em>Conflict of interest statement.</em> None declared.</p>'
        result = self.extract(fixture().replace('<h2 id="3">References', tail + '<h2 id="3">References'))
        self.assertEqual(result.sections[-1].heading, 'ACKNOWLEDGEMENTS')
        self.assertEqual(len(result.sections[-1].blocks), 2)

    def test_unheaded_acknowledgement_after_conclusion(self):
        tail = (
            '<h2 id="7">CONCLUSION</h2><p>Scientific conclusion.</p>'
            '<p>The authors thank the laboratory and acknowledge research support from a fund.</p>'
            '<p><em>Conflict of interest statement.</em> None declared.</p>'
        )
        result = self.extract(
            fixture().replace('<h2 id="3">References', tail + '<h2 id="3">References')
        )
        self.assertEqual(result.sections[-2].heading, 'CONCLUSION')
        self.assertEqual(len(result.sections[-2].blocks), 1)
        self.assertEqual(result.sections[-1].heading, 'ACKNOWLEDGEMENTS')
        self.assertEqual(len(result.sections[-1].blocks), 2)


if __name__ == '__main__':
    unittest.main()
