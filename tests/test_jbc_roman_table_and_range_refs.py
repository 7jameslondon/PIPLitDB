from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.extraction.html_extractor import extract_html
from scripts.extraction.html_extractor import _cell_press_author_correspondence
from lxml import html


class JbcRomanTableAndRangeRefsTests(unittest.TestCase):
    def test_direct_roman_caption_and_prefixed_footnote(self):
        article = self.extract('''<article><h1>Example</h1><section><h2>Results</h2>
        <figure id="tblII"><div><table><tr><th>Value</th></tr><tr><td>5<sup>1-a</sup></td></tr></table></div>
        <figcaption><span>Table II</span><div id="spara20" role="paragraph">Measured activity</div>
        <div><div role="doc-footnote"><div>1-a</div><div id="tbl2fn1"><div role="paragraph">Definition.</div></div></div></div>
        </figcaption></figure></section></article>''')
        self.assertEqual(article.tables[0].label, 'Table II')
        self.assertEqual(article.tables[0].title_plain, 'Table II. Measured activity')
        self.assertEqual(article.tables[0].footnotes_plain, ['[1-a] Definition.'])
    def test_normalized_paragraph_definition_preamble_is_scoped_once(self):
        article = self.extract('''<article><h1>Example</h1>
        <section><h2>Abstract</h2><p>Only abstract.</p></section>
        <section id="bodymatter" property="articleBody" typeof="Text"><div>
        <dl id="deflist10"><dt id="defterm10">ABC</dt><dd id="cedefdes10"><p id="para10" role="paragraph">A definition</p></dd></dl>
        <p id="para20" role="paragraph">Opening prose.</p>
        <section id="sec-1"><h2>Methods</h2><p>Method.</p></section></div></section></article>''')
        self.assertEqual([s.heading for s in article.sections], ['Abstract', 'Abbreviations', 'Introduction', 'Methods'])
        self.assertEqual([b.plain_text for s in article.sections for b in s.blocks], ['Only abstract.', '- ABC: A definition', 'Opening prose.', 'Method.'])
    def test_author_flyout_keeps_full_contact_and_rejects_other_panels(self):
        source = '''<span property="author"><span><div property="author" typeof="Person">
        <div><div><div>Correspondence</div><div>Institute, Street. Tel.: 123; E-mail:</div>
        <div><a property="email" href="mailto:a@example.org">Send email</a></div></div>
        <div><div>Affiliations</div><div>Other institution</div></div></div>
        </div></span></span>'''
        self.assertEqual(_cell_press_author_correspondence(html.fromstring(source)),
                         'Institute, Street. Tel.: 123; E-mail: a@example.org')
        self.assertIsNone(_cell_press_author_correspondence(html.fromstring(source.replace('Correspondence', 'Affiliations'))))
    def extract(self, source):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'main.html'
            path.write_text(source, encoding='utf-8')
            return extract_html(path, 'main.html')

    def test_definition_preamble_does_not_duplicate_or_extend_abstract(self):
        article = self.extract('''<article><h1>Example</h1>
        <section><h2>Abstract</h2><p>Only abstract.</p></section>
        <section id="bodymatter" property="articleBody" typeof="Text"><div>
        <dl id="deflist10"><dt id="defterm10">ABC</dt><dd id="cedefdes10">
        <div id="para10" role="paragraph">A definition</div></dd></dl>
        <div id="para20" role="paragraph">Opening prose.</div>
        <section id="sec-1"><h2>Methods</h2><p>Method.</p></section>
        </div></section></article>''')
        self.assertEqual([s.heading for s in article.sections],
                         ['Abstract', 'Abbreviations', 'Introduction', 'Methods'])
        self.assertEqual([b.plain_text for s in article.sections for b in s.blocks],
                         ['Only abstract.', '- ABC: A definition', 'Opening prose.', 'Method.'])

    def test_compact_legend_without_panel_intro_does_not_repeat_figure_label(self):
        article = self.extract('''<article><header><h1 property="name">Example</h1>
        <div property="author" typeof="Person" role="listitem"><span><a>
        <span property="givenName">A.</span><span property="familyName">Author</span></a></span>
        <sup><a role="doc-noteref" href="#FN1">‡</a></sup></div>
        <div id="core-affiliations-notes"><div id="FN1" role="doc-footnote"><p>Institute.</p></div></div>
        <div id="core-content-info"><a property="sameAs" href="https://doi.org/10.1074/example">DOI</a></div>
        </header><section><h2>Results</h2><figure id="fig2"><img src="data:image/png;base64,iVBORw0KGgo="/>
        <figcaption><div id="fig2-title"><span>Figure 2</span><span><b>Title.</b> Complete prose with <i>A</i> and <i>B</i>.</span>
        </div></figcaption></figure></section></article>''')
        self.assertEqual(article.figures[0].caption_plain, 'Title. Complete prose with A and B.')

    def test_roman_table_is_not_an_empty_graphical_abstract(self):
        article = self.extract('''<article><h1>Example</h1><section><h2>Results</h2>
        <figure id="tblI"><div><table><tr><th>Agent</th><th>Value</th></tr>
        <tr><td>Example</td><td>0.36</td></tr></table></div>
        <figcaption>Table I. Values.</figcaption></figure></section></article>''')
        self.assertEqual(len(article.tables), 1)
        self.assertEqual(article.figures, [])

    def test_roman_table_keeps_title_and_linked_footnote(self):
        article = self.extract('''<article><h1>Example</h1><section><h2>Results</h2>
        <figure id="tblI"><div><table><tr><th>Value</th></tr><tr><td>0.36</td></tr></table></div>
        <figcaption><div><span>Table I</span><div id="spara80">Measured values</div></div>
        <div><div role="doc-footnote"><div>a</div><div id="tbl1fn1">
        <div role="paragraph">Measured after four days.</div></div></div></div></figcaption>
        </figure></section></article>''')
        table = article.tables[0]
        self.assertEqual(table.label, 'Table I')
        self.assertEqual(table.title_plain, 'Table I. Measured values')
        self.assertEqual(table.footnotes_plain, ['[a] Measured after four days.'])

    def test_grouped_decadic_citation_without_occurrence_suffix(self):
        def source(label):
            entries = ''.join(
                f'<div id="bib{n}"><div id="sbref{n*10}"><div>'
                f'<div><a href="#body-ref-sbref20">{n}.</a></div>'
                f'<div>Author {n}</div><div>Journal 2002; 9:1-2</div></div>'
                '<div><a href="https://example.org" target="_blank">PubMed</a>'
                '</div></div></div>' for n in (1, 2))
            return ('<article><h1>Example</h1><section><h2>Results</h2><p>'
                    f'<a id="body-ref-sbref20">{label}</a>.</p></section>'
                    '<section id="references"><h2>References</h2>'
                    f'<div id="bibliography">{entries}</div></section></article>')
        parsed = self.extract(source('1–2'))
        self.assertEqual([r.plain_text for r in parsed.references],
                         ['1. Author 1 Journal 2002; 9:1-2',
                          '2. Author 2 Journal 2002; 9:1-2'])
        self.assertEqual(self.extract(source('2–3')).references, [])
