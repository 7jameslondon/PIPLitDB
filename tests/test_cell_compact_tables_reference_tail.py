import unittest
from lxml import html
from scripts.extraction.html_extractor import (
    _normalize_cell_press_compact_quantitative_tables,
    _cell_press_table_title, _cell_press_table_footnotes,
    _cell_press_preview_reference_tail, _parse_rows,
)
from scripts.extraction.models import ContentBlock
from scripts.extraction.html_extractor import _sciencedirect_front_matter
from scripts.extraction.html_extractor import _cell_press_author_correspondence
from tests import test_cell_unnumbered_affiliation as affiliation_fixture


class CompactCellTablesTests(unittest.TestCase):
    def test_correspondence_label_is_not_duplicated_in_value(self):
        author = html.fromstring('''<span><span><div property="author" typeof="Person"><div>
        <div>Correspondence</div><div>Correspondence: A. Reader</div>
        <div><a property="email" href="mailto:a@example.test">Email</a></div>
        </div></div></span></span>''')
        self.assertEqual(_cell_press_author_correspondence(author), 'A. Reader a@example.test')

    def test_compound_history_line_is_retained(self):
        doc = affiliation_fixture.CellUnnumberedAffiliationTests().document()
        info = doc.xpath('.//details[@id="core-content-info"]')[0]
        info.append(html.fromstring('<div>revisions requested 9 March 2001 First published online 8 May 2001</div>'))
        values = [b.plain_text for b in _sciencedirect_front_matter(doc, 'main.html')]
        self.assertIn('Additional article history: revisions requested 9 March 2001 First published online 8 May 2001', values)

    def source(self):
        return html.fromstring('''<article data-extraction-dialect="cell-press-semantic">
        <figure id="TBL1"><div><table><tbody><tr><td>Conjugate</td><td>50% occupancy (nM)</td></tr>
        <tr><td>A</td><td>12</td></tr><tr><td>B</td><td>34</td></tr>
        <tr><td>The reported values are means.</td></tr></tbody></table></div>
        <figcaption><span>Table 1</span><div role="paragraph">Measured affinity</div><div><ul><li>
        <a href="/action/showFullTableHTML?isHtml=true&amp;tableId=TBL1">Open table in a new tab</a>
        </li></ul></div></figcaption></figure></article>''')

    def test_compact_card_preserves_title_axes_note_and_values(self):
        root = self.source()
        _normalize_cell_press_compact_quantitative_tables(root)
        figure = root.xpath('.//figure')[0]
        self.assertEqual(_cell_press_table_title(figure)[1], 'Table 1. Measured affinity')
        self.assertEqual(_cell_press_table_footnotes(figure)[0][0], 'The reported values are means.')
        rows = _parse_rows(root.xpath('.//table')[0])
        self.assertTrue(all(c.header for c in rows[0]))
        self.assertEqual([[c.text for c in row] for row in rows[1:]], [['A', '12'], ['B', '34']])

    def test_unauthenticated_control_is_unchanged(self):
        root = self.source()
        root.xpath('.//a')[0].set('href', '/unrelated')
        before = html.tostring(root)
        _normalize_cell_press_compact_quantitative_tables(root)
        self.assertEqual(html.tostring(root), before)


class CitationTailTests(unittest.TestCase):
    def reference(self):
        return ContentBlock(block_id='reference-001', kind='reference', markdown='1. Existing',
                            plain_text='1. Existing', source_path='main.html', source_locator='ref1')

    def source(self, number=2, title='Title'):
        return html.fromstring(f'''<article data-extraction-dialect="cell-press-semantic"><span>
        <a id="body-ref-BIB{number}-1" href="#" href-manipulated="true" aria-controls="BIB{number}-1">[{number}]</a><div><div><div>
        <div><div>{number}.</div><div>A. Author</div><div><strong>{title}</strong></div><div>Journal 2000; 1:2-3</div></div>
        <div><a target="_blank" href="https://doi.org/10.1234/test">Crossref</a></div>
        </div></div></div></span></article>''')

    def test_contiguous_missing_tail_uses_native_card(self):
        refs = _cell_press_preview_reference_tail(self.source(), [self.reference()], 'main.html')
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[1].plain_text, '2. A. Author Title Journal 2000; 1:2-3 DOI: https://doi.org/10.1234/test')

    def test_gap_is_not_silently_accepted(self):
        self.assertEqual(len(_cell_press_preview_reference_tail(self.source(3), [self.reference()], 'main.html')), 1)

    def test_mismatched_identity_is_ignored(self):
        root = self.source()
        root.xpath('.//a')[0].set('aria-controls', 'wrong')
        self.assertEqual(len(_cell_press_preview_reference_tail(root, [self.reference()], 'main.html')), 1)

    def test_conflicting_repeated_cards_are_not_selected(self):
        root = self.source()
        root.append(self.source(title='Conflict')[0])
        self.assertEqual(len(_cell_press_preview_reference_tail(root, [self.reference()], 'main.html')), 1)
