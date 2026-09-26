import unittest
from unittest.mock import patch
from lxml import html
from tests.test_html_preview_pdf_recovery import HtmlPreviewPdfRecoveryTests
from scripts.extraction.html_extractor import _normalize_cell_press_partial_semantic_snapshot, _semantic_cell_press_table_figure_details
from scripts.extraction.pipeline import _recover_truncated_html_tail, ExtractionError


class PartialCellMarkupTests(unittest.TestCase):
    def source(self):
        return html.fromstring('''<article typeof="ScholarlyArticle">
        <section id="author-abstract" property="abstract" typeof="Text" role="doc-abstract"><div id="simple-para0105" role="paragraph">Summary.</div></section>
        <section id="bodymatter" property="articleBody" typeof="Text"><section><div id="para0005" role="paragraph">Before (<span><a id="body-ref-reference1" aria-controls="reference1" href-manipulated="true">Author 2000</a><div><a aria-label="Open Crossref in new tab">Bibliography UI</a></div></span>) after.</div></section></section></article>''')

    def test_partial_archive_paragraphs_and_citation_labels(self):
        root=self.source()
        with patch('scripts.extraction.html_extractor._cell_press_header_details',return_value={}):
            _normalize_cell_press_partial_semantic_snapshot(root)
        self.assertEqual([x.text_content() for x in root.xpath('.//p')],['Summary.','Before (Author 2000) after.'])

    def test_unauthenticated_header_is_untouched(self):
        root=self.source()
        _normalize_cell_press_partial_semantic_snapshot(root)
        self.assertFalse(root.xpath('.//p'))
        self.assertIn('Bibliography UI',root.text_content())

    def test_unmatched_flyout_identity_retains_content(self):
        root=self.source(); root.xpath('.//a')[0].set('aria-controls','other')
        with patch('scripts.extraction.html_extractor._cell_press_header_details',return_value={}):
            _normalize_cell_press_partial_semantic_snapshot(root)
        self.assertIn('Bibliography UI',root.text_content())

    def test_partial_table_title_and_note_keep_exact_authentication(self):
        root=self.source()
        figure=html.fromstring('''<figure id="TBL1"><div><table><tbody><tr><td>Header</td></tr></tbody></table></div><figcaption><div><span>Table 1</span><div id="simple-para0085" role="paragraph">Scientific title</div></div><div><div id="simple-para0090" role="paragraph">Scientific note.</div></div><div><ul><li><a href="/action/showFullTableHTML?isHtml=true&amp;tableId=TBL1">Open table in a new tab</a></li></ul></div></figcaption></figure>''')
        root.xpath('.//section[@id="bodymatter"]')[0].append(figure)
        with patch('scripts.extraction.html_extractor._cell_press_header_details',return_value={}):
            _normalize_cell_press_partial_semantic_snapshot(root)
        details=_semantic_cell_press_table_figure_details(figure)
        self.assertEqual(details['title'].text_content(),'Scientific title')
        self.assertEqual([n.text_content() for n in details['notes']],['Scientific note.'])
        figure.xpath('.//a')[0].set('href','/action/showFullTableHTML?isHtml=true&tableId=TBL2')
        self.assertIsNone(_semantic_cell_press_table_figure_details(figure))


class TailRecoveryTests(HtmlPreviewPdfRecoveryTests):
    def tail(self):
        self.article.references=[]
        self.recovered.sections=self.recovered.sections[1:]
        self.recovered.sections[0].heading='Article Text'
        self.config['recover_truncated_html_tail']={'html_sha256':self.html.sha256,
            'last_html_block_id':'preview','last_html_text':'Download to read the full article text',
            'pdf_opening_text':'PDF body','reason':'Archive cut off','evidence':'PDF tail'}
        return _recover_truncated_html_tail(self.article,self.html,self.pdf,self.metadata,self.config)

    def test_tail_keeps_html_assets_and_uses_unique_pdf_ids(self):
        figure=object(); self.article.figures=[figure]
        with patch('scripts.extraction.pipeline.extract_pdf_article',return_value=self.recovered):
            result=self.tail()
        self.assertIs(result.figures[0],figure)
        self.assertEqual(result.sections[-1].blocks[0].plain_text,'PDF body')
        self.assertTrue(result.sections[-1].blocks[0].block_id.startswith('pdf-tail-'))
        self.assertEqual(result.sections[0].blocks[0].plain_text,'HTML abstract')

    def test_tail_rejects_wrong_pdf_opening(self):
        self.recovered.sections[1].blocks[0].plain_text='Unexpected text'
        with patch('scripts.extraction.pipeline.extract_pdf_article',return_value=self.recovered):
            with self.assertRaises(ExtractionError): self.tail()

    def test_tail_rejects_changed_final_html(self):
        self.article.sections[-1].blocks[0].plain_text='Other text'
        with self.assertRaises(ExtractionError): self.tail()
