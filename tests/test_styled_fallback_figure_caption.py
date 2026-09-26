from __future__ import annotations

import unittest
from unittest.mock import patch
from lxml import html
from scripts.extraction.html_extractor import _figure_caption, _oup_silverchair_figure_caption, _oup_silverchair_toolbar_free_reference_body, _oup_silverchair_front_matter


class StyledFallbackFigureCaptionTests(unittest.TestCase):
    def test_legacy_table_without_rendering_duplicate_keeps_marked_note(self):
        from scripts.extraction.html_extractor import _oup_silverchair_tbl_semantic_table_details
        root = html.fromstring('''<article><div swap-content-for-modal="true"><div>
          <div id="ABC123TB1"><span id="label-99"><strong>Table 1.</strong></span>
          <div><a role="button" target="_blank" href="/view-large/99" aria-describedby="label-99">Open in new tab</a></div>
          <div id="caption-99"><p>Synthetic values</p></div></div>
          <div><table role="table" aria-labelledby="label-ABC123TB1" aria-describedby="caption-ABC123TB1"><tbody>
          <tr><td>Group</td><td>Value<sup>a</sup></td></tr><tr><td>X</td><td>2.2</td></tr></tbody></table></div>
          <div></div><div><p><sup>a</sup> Authored note.</p></div></div></div></article>''')
        with patch('scripts.extraction.html_extractor._oup_silverchair_article_root', return_value=root):
            detail = _oup_silverchair_tbl_semantic_table_details(root[0])
            self.assertEqual(detail['notes'][0]['label'], 'a')
            self.assertEqual(''.join(detail['notes'][0]['body'].itertext()).strip(), 'Authored note.')
            self.assertEqual(detail['table'].xpath('.//tr[2]/td[2]/text()'), ['2.2'])
            root.xpath('.//table')[0].set('aria-labelledby', 'wrong')
            self.assertIsNone(_oup_silverchair_tbl_semantic_table_details(root[0]))

    def test_oup_partial_history_keeps_correspondence_and_publication(self):
        doc = html.fromstring('''<div><h1>Example article</h1>
          <span id="author-flyout-1"><div><div>Example Author</div>
          <div><div content-id="COR1">Email: example@example.test</div></div>
          <div>Search for other works by this author on:</div><div>Oxford Academic</div><div>PubMed</div></div></span>
          <div>Example J., Volume 3, Issue 4, 01 May 2003, Pages 5–6,
          <a href="https://doi.org/10.1000/example">https://doi.org/10.1000/example</a></div>
          <div><div>Published:</div><div>01 May 2003</div></div></div>''')
        values = [block.plain_text for block in _oup_silverchair_front_matter(doc, 'html/main.html')]
        self.assertEqual(values[0], 'Correspondence: Email: example@example.test')
        self.assertEqual(values[-1], 'Publication history: Published: 01 May 2003')
        self.assertFalse(any('Received:' in value for value in values))

    def test_legacy_reference_retains_already_authored_punctuation(self):
        body = html.fromstring('<div><p>Example,A. (</p><div>2003</div>) Synthetic title. '
                               '<div>Example J.</div>, <div>3</div>, <div>4</div>–8.<p></p></div>')
        rich, plain = _oup_silverchair_toolbar_free_reference_body(body)
        self.assertEqual(plain, 'Example,A. (2003) Synthetic title. Example J., 3, 4–8.')
        self.assertEqual(rich, plain)
        body.xpath('./div')[2].tail = ' unrelated toolbar'
        self.assertIsNone(_oup_silverchair_toolbar_free_reference_body(body))

    def test_legacy_oup_card_recognized_without_promoting_toolbar(self):
        figure = html.fromstring('''<figure id="figure-2" swap-content-for-modal="true"><div>
          <img src="data:image/png;base64,AA==" alt="Figure 2. Synthetic caption."><div>
          <div><p><strong>Figure 2.</strong> Synthetic <em>caption</em>.</p></div>
          <div><a aria-describedby="label-123" href="/view-large/figure/123/img.jpeg">Open in new tab</a>
          <a aria-describedby="label-123" href="/DownloadFile/DownloadImage.aspx?image=img">Download slide</a></div>
          </div></div></figure>''')
        self.assertEqual(_oup_silverchair_figure_caption(figure),
                         ('Figure 2', 'Synthetic <em>caption</em>.', 'Synthetic caption.'))
        figure.xpath('.//a')[1].text = 'Unrelated action'
        self.assertIsNone(_oup_silverchair_figure_caption(figure))

    def test_standalone_styled_label_is_removed_from_both_forms(self):
        for style in ('strong', 'em'):
            with self.subTest(style=style):
                figure = html.fromstring(
                    f'<figure><div><p><{style}>Figure 2.</{style}> '
                    '<strong>A</strong> Authored H<sub>2</sub>O caption.</p></div></figure>'
                )
                label, markup, plain = _figure_caption(figure)
                self.assertEqual(label, 'Figure 2')
                self.assertEqual(plain, 'A Authored H_{2}O caption.')
                self.assertEqual(markup, '<strong>A</strong> Authored H<sub>2</sub>O caption.')

    def test_caption_emphasis_is_not_removed(self):
        figure = html.fromstring(
            '<figure><p>Figure 2. <strong>Authored caption.</strong></p></figure>'
        )
        self.assertEqual(_figure_caption(figure)[1], '<strong>Authored caption.</strong>')
