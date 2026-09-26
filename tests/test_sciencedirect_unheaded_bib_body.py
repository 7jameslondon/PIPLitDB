from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.extraction.html_extractor import extract_html


class UnheadedBibBodyTests(unittest.TestCase):
    def extract(self, source):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'main.html'
            path.write_text(source, encoding='utf-8')
            return extract_html(path, 'html/main.html')

    def source(self):
        return '''<article><h1 id="screen-reader-main-title">Synthetic title</h1>
        <div id="article-identifier-links"><a href="https://doi.org/10.1016/example">DOI</a></div>
        <div id="abstracts"><div id="aep-abstract-id1"><h2>Abstract</h2><div>Abstract text.</div></div></div>
        <div id="body"><div><div>Opening <em>word</em>.<a href="#BIB1" name="bBIB1"><sup>1</sup></a></div>
        <div><div>Second paragraph.</div><div id="TBL1"><p>Table 1. Values.</p><table><tr><th>A</th></tr><tr><td>2</td></tr></table></div></div>
        <div>Closing paragraph.<a href="#BIB2" name="bBIB2"><sup>2</sup></a></div></div>
        <section id="aep-acknowledgment-id2"><h2>Acknowledgements</h2><div>Support text.</div></section></div>
        <section id="aep-bibliography-id3"><h2>References</h2><ol>
        <li><a id="ref-id-BIB1">1</a><span>First reference.</span></li>
        <li><a id="ref-id-BIB2">2</a><span>Second reference.</span></li></ol></section></article>'''

    def test_unheaded_body_order_markup_and_asset_exclusion(self):
        result = self.extract(self.source())
        main = [s for s in result.sections if s.heading == 'Main text']
        self.assertEqual(len(main), 1)
        self.assertEqual([b.plain_text for b in main[0].blocks],
                         ['Opening word.^{1}', 'Second paragraph.', 'Closing paragraph.^{2}'])
        self.assertIn('<em>word</em>', main[0].blocks[0].markdown)
        self.assertEqual(len(result.tables), 1)
        self.assertEqual([s.heading for s in result.sections],
                         ['Abstract', 'Main text', 'Acknowledgements'])

    def test_unrelated_doi_does_not_trigger_anonymous_body_recovery(self):
        result = self.extract(self.source().replace('10.1016/example', '10.9999/example'))
        self.assertFalse(any(s.heading == 'Main text' for s in result.sections))

    def test_lowercase_bib_reference_ids_authenticate_unheaded_body(self):
        source = self.source().replace('ref-id-BIB1', 'ref-id-bib1').replace(
            'ref-id-BIB2', 'ref-id-bib2'
        )
        result = self.extract(source)
        main = next(section for section in result.sections if section.heading == 'Main text')
        self.assertEqual(
            [block.plain_text for block in main.blocks],
            ['Opening word.^{1}', 'Second paragraph.', 'Closing paragraph.^{2}'],
        )

    def test_supplementary_anonymous_sibling_does_not_hide_unheaded_body(self):
        source = self.source().replace(
            '<section id="aep-acknowledgment-id2">',
            '<div><section><h2>Supplementary data</h2><p>Attachment.</p></section></div>'
            '<section id="aep-acknowledgment-id2">',
        )
        result = self.extract(source)
        main = next(section for section in result.sections if section.heading == 'Main text')
        self.assertEqual(
            [block.plain_text for block in main.blocks],
            ['Opening word.^{1}', 'Second paragraph.', 'Closing paragraph.^{2}'],
        )

    def test_inline_figure_link_restores_typeset_word_boundary(self):
        source = self.source().replace(
            'Second paragraph.',
            'As shown in<a href="#fig8">Figure 8</a>, the value increased.',
        )
        result = self.extract(source)
        main = next(section for section in result.sections if section.heading == 'Main text')
        self.assertEqual(
            main.blocks[1].plain_text,
            'As shown in Figure 8, the value increased.',
        )
        self.assertIn('shown in Figure 8', main.blocks[1].markdown)

    def test_uppercase_author_note_marker_preserves_author_and_note(self):
        source = self.source().replace('<div id="abstracts">', '''
        <div id="author-group"><span><span>Alex Example</span><span id="bFN1"><sup>†</sup></span></span></div>
        <dl><dt><a href="#bFN1">†</a></dt><dd><div>Current address: Example University.</div></dd></dl>
        <div id="abstracts">''')
        result = self.extract(source)
        values = [b.plain_text for b in result.front_matter]
        self.assertIn('Author note assignments: Alex Example (†)', values)
        self.assertIn('Author note †: Current address: Example University.', values)
