from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.extraction.html_extractor import extract_html
from scripts.extraction.models import SourceFile
from scripts.extraction.rich_text import block_markup_to_safe_html, rich_text_matches_plain
from scripts.extraction.supplements import extract_supplements
from tests.test_pnas_html_extractor import PNAS_HTML


WRAPPER = '<div xmlns:hw="org.highwire.hpp" xmlns="http://www.w3.org/1999/xhtml">{}</div>'


class HighwireFragmentRecoveryTests(unittest.TestCase):
    def extract(self, payload: str):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "support.html"
            raw = payload.encode("utf-8")
            path.write_bytes(raw)
            source = SourceFile("supplement", path, "supplementary/support.html", len(raw),
                                hashlib.sha256(raw).hexdigest(), "text/html")
            result = extract_supplements([source], root / "extraction")[0]
            self.assertEqual(path.read_bytes(), raw)
            preserved = root / "extraction" / result.copied_path
            self.assertEqual(preserved.read_bytes(), raw)
            return result

    def assert_pairs(self, blocks):
        for block in blocks:
            safe = block_markup_to_safe_html(block.markdown, kind=block.kind)
            self.assertTrue(rich_text_matches_plain(block.plain_text, safe), (block, safe))

    def test_methods_preserve_paragraphs_scripts_and_font_scoped_symbols(self):
        result = self.extract(WRAPPER.format(
            '<p><b>Supporting Materials and Methods</b></p>'
            '<p><b>Preparation.</b> m W e: 20 <font face="Symbol">m</font>M; '
            '18 <font face="Symbol">W</font>; <font face="Symbol">e</font> = '
            '40 M<sup>-1</sup> cm<sup>-1</sup>. CO<sub>2</sub>; <i>in vitro</i>.</p>'
            '<p>Second paragraph. <font face="Symbol">a<b>b</b><font face="Arial">m</font>g</font>.</p>'
        ))
        self.assertEqual(result.warnings, [])
        self.assertEqual(len(result.blocks), 3)
        self.assertEqual(result.blocks[0].kind, "subsection_heading")
        self.assertEqual(result.blocks[1].plain_text,
                         'Preparation. m W e: 20 µM; 18 Ω; ε = 40 M^{-1} cm^{-1}. CO_{2}; in vitro.')
        self.assertEqual(result.blocks[2].plain_text, 'Second paragraph. αβmγ.')
        self.assertIn('<strong>Preparation.</strong>', result.blocks[1].markdown)
        self.assertEqual(result.blocks[1].source_locator, '/html/body/div/p[2]')
        self.assertEqual(result.blocks[1].source_path, 'supplementary/support.html')
        self.assert_pairs(result.blocks)

    def test_caption_link_and_multiple_panels_preserve_unknown_source_glyph(self):
        result = self.extract(WRAPPER.format(
            '<br/><br/><a href="originalFigure.pdf">Supporting Figure 9</a>'
            '<p><b>Fig. 9.</b> (<i>a</i>) 4 <font face="Symbol">m</font>M of '
            '<b>8</b>. (<i>b</i>) Washed 2<font face="Symbol">�</font> with medium.</p>'
        ))
        self.assertEqual(result.warnings, [])
        self.assertEqual([b.kind for b in result.blocks], ['text', 'figure_caption'])
        self.assertEqual(result.blocks[0].plain_text, 'Supporting Figure 9')
        self.assertEqual(result.blocks[1].plain_text,
                         'Fig. 9. (a) 4 µM of 8. (b) Washed 2� with medium.')
        self.assertEqual(result.figures, [])  # No asset is guessed from a caption link.
        self.assert_pairs(result.blocks)

    def test_short_caption_is_not_promoted_to_heading_or_lost(self):
        result = self.extract(WRAPPER.format('<p><b>Fig. 12.</b> Binding isotherm for <b>2</b>.</p>'))
        self.assertEqual(result.blocks[0].kind, 'figure_caption')
        self.assertEqual(result.blocks[0].plain_text, 'Fig. 12. Binding isotherm for 2.')
        self.assert_pairs(result.blocks)

    def test_nonfragment_or_extra_scientific_media_remains_unsupported(self):
        for payload in [
            '<div><p>Supporting prose.</p></div>',
            WRAPPER.format('<p>Prose.</p><img src="formula.gif"/>'),
            WRAPPER.format('<p>Prose <img src="formula.gif"/> after.</p>'),
            WRAPPER.format('<p>Prose.</p>Unwrapped authored continuation'),
            WRAPPER.format('<p style="display:none">Hidden prose.</p>'),
            WRAPPER.format('<p>Prose.</p><script>tracking()</script>'),
            WRAPPER.format('Leading prose<p>Prose.</p>'),
            WRAPPER.format('<!--comment-->Authored tail<p>Prose.</p>'),
            WRAPPER.format('<p>Prose.</p><table><tr><td>Scientific data</td></tr></table>'),
            WRAPPER.format('<p>Prose.</p>').replace('<div xmlns:hw', '<div hidden="hidden" xmlns:hw'),
        ]:
            with self.subTest(payload=payload):
                result = self.extract(payload)
                self.assertEqual(result.blocks, [])
                self.assertEqual([w['code'] for w in result.warnings],
                                 ['unsupported_supplement_text_extraction'])


AUTHOR_NOTES = """
<div><section id="tab-contributors"><h3>Authors</h3>
<section><h4>Affiliations</h4>
<div id="con1" property="author" typeof="Person"><h5><span property="givenName">Alice B.</span>
<span property="familyName">Clark</span><sup><a role="doc-noteref" href="#equal">*</a></sup></h5>
<div property="affiliation" typeof="Organization"><span property="name">Department A</span></div></div>
<div id="con2" property="author" typeof="Person"><h5><span property="givenName">David E.</span>
<span property="familyName">Fox</span><sup><a role="doc-noteref" href="#equal">*</a>
<a role="doc-noteref" href="#contact">†</a><a role="doc-noteref" href="#present">‡</a></sup></h5>
<div property="affiliation" typeof="Organization"><span property="name">Department B</span></div></div>
</section><section><h4>Notes</h4>
<div role="doc-footnote"><div>†</div><div role="paragraph" id="contact">To whom correspondence should be addressed. E-mail: <a href="mailto:fox@example.org">fox@example.org</a>.</div></div>
<div role="doc-footnote"><div>*</div><div role="paragraph" id="equal">These authors contributed equally.</div></div>
<div role="doc-footnote"><div>‡</div><div role="paragraph" id="present">Present address: Department C.</div></div>
<div role="doc-footnote">Contributed by A. Editor, December 1, 2023</div>
</section></section>
<section id="tab-information"><h3>Information</h3><section><h4>Copyright</h4>
<div role="paragraph">Copyright © 2024, Example Academy.</div></section></section></div>
"""


class PnasMetadataRecoveryTests(unittest.TestCase):
    def extract(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'article.html'
            path.write_text(payload, encoding='utf-8')
            return extract_html(path, 'article.html')

    def article(self, notes=AUTHOR_NOTES):
        return self.extract(PNAS_HTML.replace('</article>', notes + '</article>'))

    def test_exact_noterefs_preserve_correspondence_equal_authors_and_address(self):
        article = self.article()
        values = [b.plain_text for b in article.front_matter]
        self.assertIn('Correspondence — David E. Fox: To whom correspondence should be addressed. E-mail: fox@example.org.', values)
        self.assertIn('Author note — Alice B. Clark; David E. Fox: These authors contributed equally.', values)
        self.assertIn('Author note — David E. Fox: Present address: Department C.', values)
        self.assertIn('Affiliation — Alice B. Clark: Department A', values)
        self.assertIn('Affiliation — David E. Fox: Department B', values)
        self.assertEqual(values.count('Editorial history: Contributed by A. Editor, December 1, 2023'), 1)
        self.assertEqual(values.count('Copyright: Copyright © 2024, Example Academy.'), 1)
        self.assertFalse(any(v.startswith('Access: Copyright') for v in values))
        for block in article.front_matter:
            self.assertTrue(rich_text_matches_plain(block.plain_text, block_markup_to_safe_html(block.markdown, kind=block.kind)))

    def test_notes_only_editorial_history_and_already_named_author(self):
        notes = AUTHOR_NOTES.replace('To whom correspondence should be addressed.',
                                    'Correspondence to David E. Fox.').replace(
                                        'These authors contributed equally.',
                                        'A.B.C. and D.E.F. contributed equally.')
        source = PNAS_HTML.replace('<div role="paragraph">Contributed by A. Editor, December 1, 2023</div>', '')
        article = self.extract(source.replace('</article>', notes + '</article>'))
        values = [b.plain_text for b in article.front_matter]
        self.assertIn('Correspondence: Correspondence to David E. Fox. E-mail: fox@example.org.', values)
        self.assertIn('Author note: A.B.C. and D.E.F. contributed equally.', values)
        self.assertIn('Editorial history: Contributed by A. Editor, December 1, 2023', values)

    def test_ambiguous_shared_initials_do_not_erase_explicit_author_links(self):
        notes = AUTHOR_NOTES.replace('David E.', 'Andrew B.').replace('Fox', 'Cole').replace(
            'These authors contributed equally.', 'A.B.C. contributed equally.')
        values = [b.plain_text for b in self.article(notes).front_matter]
        self.assertIn('Author note — Alice B. Clark; Andrew B. Cole: A.B.C. contributed equally.', values)

    def test_email_without_exact_author_link_does_not_infer_correspondent(self):
        notes = AUTHOR_NOTES.replace('href="#contact"', 'href="#missing"')
        values = [b.plain_text for b in self.article(notes).front_matter]
        self.assertIn('Correspondence: To whom correspondence should be addressed. E-mail: fox@example.org.', values)
        self.assertFalse(any(v.startswith('Correspondence —') for v in values))

    def test_duplicate_note_id_does_not_make_ambiguous_author_association(self):
        notes = AUTHOR_NOTES.replace('<div role="doc-footnote"><div>*</div>',
                                    '<div id="contact"></div><div role="doc-footnote"><div>*</div>')
        values = [b.plain_text for b in self.article(notes).front_matter]
        self.assertFalse(any(v.startswith('Correspondence —') for v in values))

    def test_distinct_copyright_and_license_are_not_removed(self):
        notes = '<p>Copyright © 2023, Another Academy.</p>' + AUTHOR_NOTES + '<p><a href="https://creativecommons.org/licenses/by/4.0/">License</a></p>'
        values = [b.plain_text for b in self.article(notes).front_matter]
        self.assertIn('Copyright: Copyright © 2024, Example Academy.', values)
        self.assertIn('Copyright: Copyright © 2023, Another Academy.', values)
        self.assertTrue(any(v.startswith('License:') for v in values))

    def test_legacy_si_titles_survive_once_without_download_size_controls(self):
        source = PNAS_HTML.replace('Supporting Information (PDF)', 'Supporting Figure 12 Legend').replace(
            '<div>Supporting Information</div>', '<div>Supporting Figure 12 Legend</div>').replace(
            'article.si.pdf', 'example-legend.html').replace('1.20 MB', '.21 KB')
        article = self.extract(source)
        self.assertEqual([b.plain_text for b in article.supporting_information], ['Supporting Figure 12 Legend'])
        self.assertNotIn('Download', article.supporting_information[0].markdown)

    def test_si_near_miss_wrapper_text_and_tails_survive(self):
        for source in [
            PNAS_HTML.replace('<div><div>Supporting Information (PDF)', '<div>Authored wrapper text<div>Supporting Information (PDF)'),
            PNAS_HTML.replace('</a></li><li>1.20 MB', '</a>Authored tail</li><li>1.20 MB'),
            PNAS_HTML.replace('<li>1.20 MB</li>', '<li>1.20 MB</li><li>Scientific note</li>'),
            PNAS_HTML.replace('</ul></div>\n    </div></section>', '</ul></div>\n    </div>Authored card tail</section>'),
            PNAS_HTML.replace('>Download</a>', '>Download<img src="scientific.png"/></a>'),
        ]:
            with self.subTest(source=source):
                from lxml import html
                from scripts.extraction.html_extractor import _normalize_pnas_silverchair_snapshot
                document = html.fromstring(source)
                before = document.xpath('.//*[@id="supplementary-materials"]')[0].text_content()
                _normalize_pnas_silverchair_snapshot(document)
                after = document.xpath('.//*[@id="supplementary-materials"]')[0].text_content()
                self.assertEqual(before, after)

    def test_unmatched_si_card_keeps_authored_paragraph(self):
        source = PNAS_HTML.replace('<li>1.20 MB</li>', '<li>1.20 MB</li><li>Authored note.</li>').replace(
            '<h2>Supporting Information</h2>', '<h2>Supporting Information</h2><p>Authored description.</p>')
        article = self.extract(source)
        values = ' '.join(b.plain_text for b in article.supporting_information)
        self.assertIn('Authored description.', values)
        self.assertIn('Authored note.', values)


if __name__ == '__main__':
    unittest.main()
