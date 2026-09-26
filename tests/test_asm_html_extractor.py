from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.extraction.html_extractor import extract_html


IMAGE = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII='
SOURCE = f'''<body><article><header><h1 property="name">Synthetic ASM article</h1>
<a property="sameAs" href="https://doi.org/10.1128/example.2000">https://doi.org/10.1128/example.2000</a></header><div>
<div id="abstracts"><section id="abstract" role="doc-abstract" property="abstract"><h2>ABSTRACT</h2><div role="paragraph">Abstract α.</div></section></div>
<section id="bodymatter" property="articleBody" typeof="Text"><div><div role="paragraph">First CD4<sup>+</sup> paragraph.</div><div role="paragraph">Second <i>K<sub>a</sub></i> &gt;10<sup>9</sup> M<sup>−1</sup> (<a role="doc-biblioref" href="#R1">1</a>).</div>
<div role="paragraph"><div><header><div>FIG. 1.</div></header><figure id="F1"><span><img src="{IMAGE}"/></span><span><img src="{IMAGE}"/></span><figcaption><span>FIG. 1</span>. Complete <i>K<sub>a</sub></i> caption.</figcaption></figure></div>FIG. 1—<i>Continued.</i></div>
<section id="acknowledgments" role="doc-acknowledgments"><h2>Acknowledgments</h2><div role="paragraph">Grant support.</div></section></div></section>
<section id="backmatter"><div><section id="bibliography" role="doc-bibliography"><h2>REFERENCES</h2><div>
<div><div>1.</div><div id="R1"><div><div>A. Author. Citation title. <em>Journal</em> 2:3–4.</div><div><a href="/servlet/linkout?doi=10.1128%2Fexample.2000&amp;key=10.1128%2Fref.1999&amp;dbid=4&amp;site=asmj">View</a><a href="https://scholar.google.com/x">Google Scholar</a></div></div></div></div>
</div></section></div></section></div><div role="dialog"><section id="tab-contributors"><section><h4>Authors</h4><div property="author" typeof="Person"><h5><span property="givenName">A.</span><span property="familyName">Author</span></h5><div property="affiliation" typeof="Organization"><span property="name">Institute One</span></div><div property="affiliation" typeof="Organization"><span property="name">Institute Two</span></div></div></section></section>
<section id="tab-information"><section><h4>History</h4><div>Received: 1 January 2000</div><div>Accepted: 2 February 2000</div></section></section><div role="paragraph">UI noise.</div></div></article></body>'''


class AsmHtmlExtractorTests(unittest.TestCase):
    def extract(self, source=SOURCE):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'main.html'
            path.write_text(source, encoding='utf-8')
            return extract_html(path, 'main.html')

    def test_complete_semantic_article_and_split_figure(self):
        article = self.extract()
        blocks = [block.plain_text for section in article.sections for block in section.blocks]
        self.assertEqual(blocks, ['Abstract α.', 'First CD4^{+} paragraph.', 'Second K_{a} >10^{9} M^{−1} (1).', 'Grant support.'])
        self.assertEqual([s.heading for s in article.sections], ['ABSTRACT', 'Main text', 'Acknowledgments'])
        self.assertEqual(article.figures[0].caption_plain, 'Complete K_{a} caption.')
        self.assertEqual(len(article.embedded_assets), 1)
        self.assertIn('DOI: https://doi.org/10.1128/ref.1999', article.references[0].plain_text)
        self.assertNotIn('Google Scholar', article.references[0].plain_text)
        self.assertIn('Institute One; Institute Two', article.front_matter[0].plain_text)
        self.assertIn('Received: 1 January 2000; Accepted: 2 February 2000', article.front_matter[1].plain_text)

    def test_bibliography_b_ids_and_figure_label_without_period(self):
        raster_table = '''<div><header><div>TABLE 1</div></header><figure id="T1"><figcaption><span>TABLE 1</span> Raster title<a href="#T1F1" role="doc-noteref"><sup><i>a</i></sup></a></figcaption><img src="{image}"/><div id="T1F1"><sup><i>a</i></sup> Reviewed note.</div></figure></div>'''.format(image=IMAGE)
        source = SOURCE.replace('id="R1"', 'id="B1"').replace('FIG. 1', 'FIG 1')
        source = source.replace('</section></div></section>\n<section id="backmatter">', raster_table + '</section></div></section>\n<section id="backmatter">')
        article = self.extract(source)
        blocks = [block.plain_text for section in article.sections for block in section.blocks]
        self.assertIn('First CD4^{+} paragraph.', blocks)
        self.assertEqual(article.figures[0].label, 'Figure 1')
        self.assertEqual(article.figures[0].caption_plain, 'Complete K_{a} caption.')
        self.assertEqual(len(article.references), 1)
        self.assertEqual(len(article.tables), 1)
        self.assertEqual(article.tables[0].table_id, 'table_001')
        self.assertEqual(article.tables[0].source_kind, 'image')
        self.assertEqual(article.tables[0].footnotes_plain, ['[a] Reviewed note.'])
        self.assertEqual(article.embedded_assets[-1].category, 'table')

    def test_native_table_title_and_footnotes_are_preserved(self):
        semantic_table = '''<figure id="T1"><figcaption><span>TABLE 1</span> Native title<a href="#T1F1" role="doc-noteref"><sup><i>a</i></sup></a></figcaption><div><table><thead><tr><th>Parameter</th><th>Value</th></tr></thead><tbody><tr><td>Alpha</td><td>1</td></tr></tbody></table></div><div><div role="doc-footnote"><div><sup>a</sup></div><div id="T1F1" role="paragraph">Reviewed <i>note</i>.</div></div></div></figure>'''
        source = SOURCE.replace(
            '</section></div></section>\n<section id="backmatter">',
            semantic_table + '</section></div></section>\n<section id="backmatter">',
        )
        article = self.extract(source)
        self.assertEqual(len(article.tables), 1)
        self.assertEqual(article.tables[0].title_plain, 'TABLE 1 Native title^{a}')
        self.assertEqual(article.tables[0].footnotes_plain, ['[a] Reviewed note.'])
        self.assertEqual(article.tables[0].parts[0].rows[1][1].text, '1')

    def test_supplement_download_control_is_not_prose(self):
        download = '''<section id="supplementary-materials"><h2>Supplemental Material</h2><div role="list"><div role="listitem"><div><span>File</span> <span>(supplement.pdf)</span></div><div><ul><li><a href="/supplement.pdf" download="supplement.pdf">Download</a></li><li>1.25 MB</li></ul></div></div></div></section>'''
        source = SOURCE.replace(
            '</section></div></section>\n<section id="backmatter">',
            '</section></div></section>' + download + '\n<section id="backmatter">',
        )
        article = self.extract(source)
        self.assertEqual(article.supporting_information, [])

    def test_unrelated_doi_is_not_authorized(self):
        article = self.extract(SOURCE.replace('10.1128/example.2000', '10.9999/example.2000'))
        self.assertFalse(any('First CD4' in b.plain_text for s in article.sections for b in s.blocks))

    def test_malformed_reference_number_does_not_normalize(self):
        article = self.extract(SOURCE.replace('id="R1"', 'id="R2"'))
        self.assertFalse(any('First CD4' in b.plain_text for s in article.sections for b in s.blocks))

    def test_exact_duplicate_reference_is_emitted_once(self):
        duplicate = '<div><div>1.</div><div id="R1"><div><div>A. Author. Citation title. <em>Journal</em> 2:3–4.</div><div><a href="/servlet/linkout?doi=10.1128%2Fexample.2000&amp;key=10.1128%2Fref.1999&amp;dbid=4&amp;site=asmj">View</a><a href="https://scholar.google.com/x">Google Scholar</a></div></div></div></div>'
        source = SOURCE.replace('</div></section></div></section></div><div role="dialog">', duplicate + '</div></section></div></section></div><div role="dialog">')
        article = self.extract(source)
        self.assertEqual(len(article.references), 1)

    def test_cross_record_linkout_not_rewritten(self):
        article = self.extract(SOURCE.replace('doi=10.1128%2Fexample.2000', 'doi=10.1128%2Fother.2000'))
        self.assertNotIn('DOI:', article.references[0].plain_text)
