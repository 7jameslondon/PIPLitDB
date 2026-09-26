import unittest
import tempfile
from pathlib import Path
from lxml import html
from tests import test_cell_unnumbered_affiliation as fixture
from scripts.extraction.html_extractor import (
    _normalize_cell_press_semantic_snapshot, _cell_press_semantic_reference_blocks,
    _render_cell_press_mjx_chtml,
    _cell_press_table_title, _cell_press_table_footnotes,
    extract_html,
)


class CellLegacyDoiReferencesTests(unittest.TestCase):
    def test_unmarked_table_note_does_not_hide_marked_definitions(self):
        doc = self.source()
        table = html.fromstring('<figure id="tbl1"><div><table><tr><td>Value</td></tr></table></div><figcaption>'
            '<div><span>Table 1</span><div id="simple-para-0100" role="paragraph">Authored title</div></div>'
            '<div><div id="simple-para-0105" role="paragraph">General note.</div>'
            '<div role="doc-footnote"><div>*</div><div id="tbl1fn1"><div role="paragraph">First definition.</div></div></div>'
            '<div role="doc-footnote"><div>†</div><div id="tbl1fn2"><div role="paragraph">Second definition.</div></div></div></div>'
            '<div><ul><li><a href="/action/showFullTableHTML?isHtml=true&amp;tableId=tbl1">Open table in a new tab</a></li></ul></div>'
            '</figcaption></figure>')
        doc.xpath('.//section[@id="sec-1"]')[0].append(table)
        _normalize_cell_press_semantic_snapshot(doc)
        self.assertEqual([p for p, _ in _cell_press_table_footnotes(table)],
            ['General note.', '[*] First definition.', '[†] Second definition.'])
    def test_numbered_role_list_splits_surrounding_prose_without_duplication(self):
        doc = self.source()
        wrapper=html.fromstring('<div id="para-0200" role="paragraph">Before:'
            '<div id="list-0010" role="list"><div role="listitem"><div>1.</div><div>'
            '<div id="para-0205" role="paragraph">First item.</div></div></div>'
            '<div role="listitem"><div>2.</div><div><div id="para-0210" role="paragraph">Second item.</div>'
            '</div></div></div>After.</div>')
        doc.xpath('.//section[@id="sec-1"]')[0].append(wrapper)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'main.html'
            path.write_text(html.tostring(doc,encoding='unicode'),encoding='utf8')
            article=extract_html(path,'main.html',[])
        lists=[b for s in article.sections for b in s.blocks if b.kind=='list']
        self.assertEqual([b.plain_text for b in lists], ['1. First item.\n2. Second item.'])
        self.assertEqual(sum('First item.' in b.plain_text for s in article.sections for b in s.blocks),1)
        _normalize_cell_press_semantic_snapshot(doc)
        self.assertEqual([x.tag for x in wrapper], ['p','ol','p'])
        self.assertEqual(wrapper.text_content(), 'Before:First item.Second item.After.')
        self.assertEqual(len(wrapper.xpath('./ol/li')),2)
    def test_hyphenated_caption_id_and_double_bar_footnote(self):
        doc = self.source()
        table = html.fromstring('<figure id="tbl1"><div><table><tr><td>Value</td></tr></table></div><figcaption>'
            '<div><span>Table 1</span><div id="simple-para-0100" role="paragraph">Full authored title</div></div>'
            '<div><div role="doc-footnote"><div>‖</div><div id="tbl1fn1"><div role="paragraph">Double-bar note.</div></div></div></div>'
            '<div><ul><li><a href="/action/showFullTableHTML?isHtml=true&amp;tableId=tbl1">Open table in a new tab</a></li></ul></div>'
            '</figcaption></figure>')
        doc.xpath('.//section[@id="sec-1"]')[0].append(table)
        _normalize_cell_press_semantic_snapshot(doc)
        self.assertEqual(table.xpath('./figcaption/div/div[@id="simple-para-0100"]')[0].tag, 'div')
        self.assertIn('Full authored title', _cell_press_table_title(table)[0])
        self.assertEqual(_cell_press_table_footnotes(table), [('[‖] Double-bar note.', '[‖] Double-bar note.')])
    def test_mathjax_bold_vectors_survive_unicode_normalization(self):
        tree = html.fromstring('<mjx-math><mjx-mi><mjx-c>𝐸</mjx-c></mjx-mi><mjx-mo><mjx-c>=</mjx-c></mjx-mo><mjx-mi><mjx-c>𝐅</mjx-c></mjx-mi><mjx-mi><mjx-c>𝐗</mjx-c></mjx-mi></mjx-math>')
        self.assertEqual(_render_cell_press_mjx_chtml(tree), r'E=\mathbf{F}\mathbf{X}')

    def test_legacy_eq_wrapper_uses_authenticated_mathjax_structure(self):
        doc = self.source()
        doc.xpath('.//section[@id="sec-1"]')[0].append(html.fromstring(
            '<div role="paragraph" id="para0010">Before<div id="eq1"><div role="math"><div>'
            '<mjx-container jax="CHTML" aria-label="E equals X"><mjx-math aria-hidden="true">'
            '<mjx-mi><mjx-c>𝐸</mjx-c></mjx-mi><mjx-mo><mjx-c>=</mjx-c></mjx-mo>'
            '<mjx-mi><mjx-c>𝐗</mjx-c></mjx-mi></mjx-math></mjx-container></div></div>'
            '<div>(1)</div></div>After</div>'
        ))
        _normalize_cell_press_semantic_snapshot(doc)
        equation = doc.xpath('.//*[@id="eq1"]')[0]
        self.assertEqual(equation.get('data-extraction-reviewed-equation'), 'true')
        self.assertEqual(equation.text, r'E=\mathbf{X} (1)')
    def source(self):
        doc = fixture.CellUnnumberedAffiliationTests().document()
        doc.xpath('.//a[@property="sameAs"]')[0].set('href', 'https://doi.org/10.1529/journal.000001')
        bib = doc.xpath('.//*[@id="bibliography"]')[0]
        bib.append(html.fromstring('''<div><div><div id="bib1"><div id="sbref1000">
          <div><div><a href="#body-ref-sbref1000-1" title="View in article">1.</a></div>
          <div>A. Author</div><div><strong>Full title</strong></div><div><em>Journal</em> 2000; 1:2-3</div></div>
          <div><a href="https://doi.org/10.1234/ref" target="_blank">Crossref</a></div>
        </div></div></div></div>'''))
        return doc

    def test_legacy_society_doi_preserves_semantic_bibliography(self):
        doc = self.source()
        _normalize_cell_press_semantic_snapshot(doc)
        self.assertEqual(doc.get('data-extraction-dialect'), 'cell-press-semantic')
        refs = _cell_press_semantic_reference_blocks(doc.xpath('.//*[@id="references"]')[0], 'main.html')
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].plain_text, '1. A. Author Full title Journal 2000; 1:2-3 DOI: https://doi.org/10.1234/ref')

    def test_mismatched_legacy_anchor_is_rejected(self):
        doc = self.source()
        doc.xpath('.//a[@title="View in article"]')[0].set('href', '#body-ref-sbref9999-1')
        _normalize_cell_press_semantic_snapshot(doc)
        self.assertIsNone(_cell_press_semantic_reference_blocks(doc.xpath('.//*[@id="references"]')[0], 'main.html'))

    def test_grouped_legacy_citation_requires_existing_authored_anchor(self):
        doc = self.source()
        doc.xpath('.//a[@title="View in article"]')[0].set('href', '#body-ref-sbref2000')
        doc.xpath('.//section[@id="sec-1"]')[0].append(html.fromstring(
            '<div role="paragraph" id="para0010"><span><a id="body-ref-sbref2000" '
            'href="#" href-manipulated="true" aria-controls="sbref2000">Author 1999, 2000</a></span></div>'
        ))
        _normalize_cell_press_semantic_snapshot(doc)
        refs = _cell_press_semantic_reference_blocks(doc.xpath('.//*[@id="references"]')[0], 'main.html')
        self.assertEqual(len(refs), 1)

    def test_unknown_doi_prefix_remains_outside_complete_gate(self):
        doc = self.source()
        doc.xpath('.//a[@property="sameAs"]')[0].set('href', 'https://doi.org/10.9999/test')
        _normalize_cell_press_semantic_snapshot(doc)
        self.assertNotEqual(doc.get('data-extraction-dialect'), 'cell-press-semantic')

    def test_idless_table_footnote_does_not_invalidate_numbered_prose(self):
        doc = self.source()
        doc.xpath('.//section[@id="sec-1"]')[0].append(html.fromstring(
            '<figure id="tbl1"><table><tr><td>Value</td></tr></table>'
            '<figcaption><div id="tbl1fn1"><div role="paragraph">Footnote.</div>'
            '</div></figcaption></figure>'
        ))
        _normalize_cell_press_semantic_snapshot(doc)
        self.assertEqual(doc.get('data-extraction-dialect'), 'cell-press-semantic')
        self.assertIn('Footnote.', doc.text_content())

    def test_idless_non_table_wrapper_remains_outside_complete_gate(self):
        doc = self.source()
        doc.xpath('.//section[@id="sec-1"]')[0].append(html.fromstring(
            '<figure><figcaption><div role="paragraph">Unidentified.</div></figcaption></figure>'
        ))
        _normalize_cell_press_semantic_snapshot(doc)
        self.assertNotEqual(doc.get('data-extraction-dialect'), 'cell-press-semantic')
