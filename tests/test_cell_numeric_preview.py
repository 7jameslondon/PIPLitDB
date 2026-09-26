import unittest
from unittest.mock import patch

from lxml import html

from scripts.extraction.html_extractor import render_inline, plain_text, _cell_press_semantic_reference_blocks


class CellNumericPreviewTests(unittest.TestCase):
    def bibliography(self):
        entries=''.join(f'''<div><div id="BIB{n}"><div id="ref{5*n:04d}"><div>
        <div><a href="#body-ref-ref0010-1" title="View in article">{n}.</a></div>
        <div>Author {n}</div><div><strong>Title {n}</strong></div><div>Journal 2000</div></div>
        <div><a target="_blank" href="https://doi.org/10.1000/{n}">Crossref</a></div>
        </div></div></div>''' for n in (1,2))
        return html.fromstring(f'''<article data-extraction-dialect="cell-press-semantic">
        <a id="body-ref-ref0010-1" href="#" href-manipulated="true" aria-controls="ref0010-1">[1–2]</a>
        <section id="references"><div id="bibliography" role="doc-bibliography"><div>{entries}</div></div></section></article>''')

    def test_numbered_shells_and_grouped_backlinks_recover_all_references(self):
        root=self.bibliography()
        with patch('scripts.extraction.html_extractor._cell_press_header_details',return_value={}):
            refs=_cell_press_semantic_reference_blocks(root.xpath('.//section')[0],'html/main.html')
        self.assertEqual([r.plain_text for r in refs], ['1. Author 1 Title 1 Journal 2000 DOI: https://doi.org/10.1000/1', '2. Author 2 Title 2 Journal 2000 DOI: https://doi.org/10.1000/2'])

    def test_mismatched_group_membership_or_shell_fails_closed(self):
        for change in ('label','shell'):
            root=self.bibliography()
            if change=='label': root.xpath('./a')[0].text='[2]'
            else: root.xpath('.//div[@id="ref0005"]')[0].set('id','ref0006')
            with patch('scripts.extraction.html_extractor._cell_press_header_details',return_value={}):
                self.assertIsNone(_cell_press_semantic_reference_blocks(root.xpath('.//section')[0],'html/main.html'))

    def source(self, *, dialect=True, controls="ref0025-1", linkout=True):
        marker = ' data-extraction-dialect="cell-press-semantic"' if dialect else ""
        target = ' target="_blank"' if linkout else ""
        return html.fromstring(f'''<article{marker}><p>Before <span>
        <a href="#" id="body-ref-ref0025-1" href-manipulated="true"
        aria-controls="{controls}">[1–5]</a><div><div><div>1.</div>
        <div>Hidden reference</div><a href="https://doi.org/10.1000/test"{target}>Crossref</a>
        </div></div></span>. After β<sub>2</sub>.</p></article>''')

    def test_numeric_range_preview_is_removed_without_losing_prose(self):
        paragraph = self.source().xpath('.//p')[0]
        self.assertEqual(plain_text(paragraph), 'Before [1–5]. After β_{2}.')
        self.assertEqual(render_inline(paragraph, markup='html'), 'Before [1–5]. After β<sub>2</sub>.')

    def test_near_misses_retain_authored_content(self):
        for options in ({'dialect': False}, {'controls': 'different'}, {'linkout': False}):
            with self.subTest(options=options):
                paragraph = self.source(**options).xpath('.//p')[0]
                self.assertIn('Hidden reference', plain_text(paragraph))
                self.assertIn('Hidden reference', render_inline(paragraph, markup='html'))
