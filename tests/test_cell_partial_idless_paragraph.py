import unittest
from unittest.mock import patch
from lxml import html
from scripts.extraction.html_extractor import _normalize_cell_press_partial_semantic_snapshot, plain_text


class PartialIdlessCellTests(unittest.TestCase):
    def source(self):
        return html.fromstring('''<article typeof="ScholarlyArticle">
        <section id="author-abstract" property="abstract" typeof="Text" role="doc-abstract"><div role="paragraph">Abstract.</div></section>
        <section id="bodymatter" property="articleBody" typeof="Text"><section id="sec-1"><h2>Introduction</h2><div role="paragraph">Before <span><a id="body-ref-ref0010-1" href="#" href-manipulated="true" aria-controls="ref0010-1">[1–2]</a><div><div>1.</div><a target="_blank">Crossref</a> Hidden preview</div></span> after.</div><figure><figcaption><div role="paragraph">Caption.</div></figcaption></figure></section></section></article>''')

    def test_authenticated_partial_idless_prose_and_numeric_citation(self):
        root = self.source()
        with patch('scripts.extraction.html_extractor._cell_press_header_details', return_value={}):
            _normalize_cell_press_partial_semantic_snapshot(root)
        self.assertEqual([plain_text(p) for p in root.xpath('.//p')], ['Abstract.', 'Before [1–2] after.'])
        self.assertEqual(len(root.xpath('.//figcaption/div[@role="paragraph"]')), 1)

    def test_unauthenticated_idless_content_is_untouched(self):
        root = self.source()
        _normalize_cell_press_partial_semantic_snapshot(root)
        self.assertFalse(root.xpath('.//p'))
        self.assertIn('Hidden preview', plain_text(root))
