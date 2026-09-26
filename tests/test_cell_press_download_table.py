import unittest
from lxml import html
from scripts.extraction.html_extractor import _normalize_cell_press_supplement_table_cards, _parse_rows, _cell_press_table_footnotes, _figure_caption

class CellPressDownloadTableTests(unittest.TestCase):
    def test_uppercase_figure_caption_preserves_paragraph_boundaries(self):
        figure=html.fromstring('<figure id="FIG1"><figcaption><div id="FIG1-title"><span>Figure 1</span><span>Title</span></div><div id="FIG1-content"><div role="paragraph">(A) First.</div><div role="paragraph">(B) Second.</div></div></figcaption></figure>')
        self.assertEqual(_figure_caption(figure),('Figure 1','Title\n\n(A) First.\n\n(B) Second.','Title (A) First. (B) Second.'))

    def source(self, extra=''):
        return html.fromstring('''<article><section><h2>Supplementary data:</h2>
<figure id="tabular-3"><div><table><tbody><tr><td><div>
<div><section aria-label="Video player: Movie"><div>Playback controls</div></section></div>
<a download="mmc1.mp4" href="/attachment/mmc1.mp4" aria-labelledby="mmc1-heading mmc1-link"><span id="mmc1-link">Video (2 MB)</span></a>
<div id="mmc1-heading">Movie</div>''' + extra + '''</div></td></tr>
<tr><td><a download="mmc2.pdf" href="/attachment/mmc2.pdf" aria-labelledby="mmc2-heading mmc2-link"><span id="mmc2-link">PDF (1 KB)</span></a><div id="mmc2-heading">Figure S1</div></td></tr>
</tbody></table></div><figcaption>Movie</figcaption></figure></section></article>''')

    def test_download_table_preserves_files_and_discards_player_chrome(self):
        root=self.source()
        _normalize_cell_press_supplement_table_cards(root)
        self.assertFalse(root.xpath('.//figure|.//table'))
        self.assertEqual([e.text_content() for e in root.xpath('.//li')],['Video (2 MB): Movie','PDF (1 KB): Figure S1'])
        self.assertEqual([e.get('href') for e in root.xpath('.//a')],['/attachment/mmc1.mp4','/attachment/mmc2.pdf'])

    def test_extra_authored_content_is_not_discarded(self):
        root=self.source('<p>Additional authored description</p>')
        _normalize_cell_press_supplement_table_cards(root)
        self.assertEqual(len(root.xpath('.//figure')),1)

    def test_inconsistent_file_identity_is_not_rewritten(self):
        root=self.source()
        root.xpath('.//a')[1].set('download','mmc3.pdf')
        _normalize_cell_press_supplement_table_cards(root)
        self.assertEqual(len(root.xpath('.//figure')),1)

    def test_paragraph_wrappers_do_not_duplicate_download_list(self):
        root=self.source()
        section=root.xpath('.//section')[0]
        figure=section.xpath('./figure')[0]
        wrapper=html.Element('div',role='paragraph')
        section.replace(figure,wrapper)
        wrapper.append(figure)
        _normalize_cell_press_supplement_table_cards(root)
        self.assertEqual([e.tag for e in section],['h2','ul'])

    def test_thead_td_cells_are_headers_but_tbody_cells_are_not(self):
        table=html.fromstring('<table><thead><tr><td>Name</td></tr></thead><tbody><tr><td>Value</td></tr></tbody></table>')
        rows=_parse_rows(table)
        self.assertTrue(rows[0][0].header)
        self.assertFalse(rows[1][0].header)

    def test_tfn_footnote_dialect_retains_marker_and_body(self):
        figure=html.fromstring('<figure id="TBL1"><figcaption><div><div role="doc-footnote"><div>a</div><div id="TFN1"><div role="paragraph">Exact note.</div></div></div></div></figcaption></figure>')
        self.assertEqual(_cell_press_table_footnotes(figure),[('[a] Exact note.','[a] Exact note.')])
