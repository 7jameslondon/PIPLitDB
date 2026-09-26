import unittest
from lxml import html
from scripts.extraction.html_extractor import (
    _cell_press_header_details, _normalize_cell_press_semantic_snapshot,
    _remove_sciencedirect_table_of_contents_navigation,
)


class CellUnnumberedAffiliationTests(unittest.TestCase):
    def document(self, affiliation='Institute A'):
        return html.fromstring(f'''<article typeof="ScholarlyArticle"><header>
        <h1 property="name">Test article</h1>
        <span property="author" typeof="Person" role="listitem"><span><a>
        <span property="givenName">Ada</span><span property="familyName">Reader</span></a>
        <div><div property="affiliation" typeof="Organization"><span property="name">{affiliation}</span></div></div>
        </span></span>
        <details id="core-affiliations-notes"><div><div property="affiliation" typeof="Organization"><span property="name">Institute A</span></div></div></details>
        <details id="core-content-info"><a property="sameAs" href="https://doi.org/10.1016/test.1">DOI</a></details></header>
        <div role="navigation" aria-label="Article navigation"><ul><li>Share</li></ul></div>
        <section id="author-abstract" property="abstract" typeof="Text" role="doc-abstract"><div role="paragraph" id="simple-para0005">Summary.</div></section>
        <section id="bodymatter" property="articleBody" typeof="Text"><section id="sec-1"><h2>Introduction</h2>
        <div role="paragraph" id="para0005">Before (<span><a id="body-ref-reference1" href-manipulated="true" aria-controls="reference1">Author 2000</a><div><a aria-label="Open PubMed in new tab">Flyout</a></div></span>) after.</div>
        </section></section><section id="backmatter"><section id="references"><h2>References</h2><div id="bibliography" role="doc-bibliography"></div></section></section></article>''')

    def test_shared_unnumbered_affiliation_authenticates(self):
        self.assertIsNotNone(_cell_press_header_details(self.document()))

    def test_mismatched_unnumbered_affiliation_does_not_authenticate(self):
        self.assertIsNone(_cell_press_header_details(self.document('Institute B')))

    def test_complete_semantic_no_hyphen_ids_and_flyout(self):
        doc = self.document()
        _normalize_cell_press_semantic_snapshot(doc)
        self.assertEqual(doc.get('data-extraction-dialect'), 'cell-press-semantic')
        self.assertEqual([p.text_content() for p in doc.xpath('.//p')], ['Summary.', 'Before (Author 2000) after.'])
        _remove_sciencedirect_table_of_contents_navigation(doc)
        self.assertNotIn('Share', doc.text_content())

    def test_numbered_authors_remain_supported(self):
        doc = self.document('Institute B')
        author = doc.xpath('.//*[@role="listitem"]')[0]
        author.append(html.fromstring('<sup><a role="doc-noteref">1</a></sup>'))
        self.assertIsNotNone(_cell_press_header_details(doc))

    def table_document(self):
        doc = self.document()
        doc.xpath('.//section[@id="sec-1"]')[0].append(html.fromstring('''
        <figure id="TBL2"><div><table><tbody>
        <tr><td>Compound</td><td>Sequence</td><td>Kd<sub>app</sub></td><td>Kd<sub>app</sub></td><td>Ratio</td></tr>
        <tr><td> </td><td> </td><td>Probe A (nM)</td><td>Probe B (nM)</td><td> </td></tr>
        <tr><td>C1</td><td>Sequence A</td><td>0.50</td><td>1.0</td><td>0.5</td></tr>
        </tbody></table></div><figcaption><div><span>Table 2</span><div role="paragraph">Binding</div></div>
        <div><div role="paragraph">Measured constants.</div></div><div><ul><li>
        <a href="/action/showFullTableHTML?isHtml=true&amp;tableId=TBL2">Open table in a new tab</a>
        </li></ul></div></figcaption></figure>'''))
        return doc

    def test_two_row_affinity_header_preserves_values_and_rows(self):
        doc = self.table_document()
        before = doc.xpath('.//table')[0].text_content()
        _normalize_cell_press_semantic_snapshot(doc)
        self.assertEqual(doc.xpath('.//table')[0].text_content(), before)
        self.assertEqual(len(doc.xpath('.//table//th')), 10)
        self.assertEqual(len(doc.xpath('.//table//td')), 5)
        self.assertEqual(doc.xpath('.//table//tr[3]/td[3]')[0].text, '0.50')

    def test_affinity_header_near_miss_untouched(self):
        doc = self.table_document()
        doc.xpath('.//table//tr[1]/td[1]')[0].text = 'Observation'
        _normalize_cell_press_semantic_snapshot(doc)
        self.assertFalse(doc.xpath('.//table//th'))


if __name__ == '__main__':
    unittest.main()
