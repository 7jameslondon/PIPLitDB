from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.extraction.html_extractor import extract_html
from scripts.extraction.rich_text import block_markup_to_safe_html


PIXEL = "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="


def figure(source_id: str, label: str) -> str:
    return (
        f'<section><figure id="{source_id}"><img '
        f'src="data:image/gif;base64,{PIXEL}" alt="{label}.">'
        f'<figcaption><div><strong>{label}</strong></div>'
        f'<div>Complete {label.lower()} caption.</div></figcaption>'
        '</figure></section>'
    )


def snapshot() -> str:
    return f'''<!doctype html><html><body><div id="article__content">
<div id="journal-banner-text">Rapid Communications in Mass Spectrometry</div>
<h1>Older RCM letter</h1><a href="https://doi.org/10.1002/rcm.1234">DOI</a>
<article lang="en"><section><i>To the Editor-in-Chief. Sir,</i>
<section id="sec1-bdy-1"><p>First paragraph with MS<sup>2</sup> notation.</p>
{figure('sch1', 'Scheme 1')}{figure('fig1', 'Figure 1')}
<p>Second paragraph with <i>K</i><sub>d</sub>.</p>
{figure('sch2', 'Scheme 2')}<p>Final scientific paragraph.</p></section>
<div><h2>Acknowledgements</h2><p>Supported by Example Foundation.</p><ol></ol></div>
<section id="sec-bibl-1"><section id="article-references-section-1">
<div><h2>REFERENCES</h2><ul>
<li><span>1</span><span>First A. First reference.</span></li>
<li><span>2</span><span>Second B. Second reference.</span></li>
</ul></div></section></section>
<div id=""><p>ALICE AUTHOR*, BOB AUTHOR*, * Department of Chemistry.</p></div>
</section></article></div></body></html>'''


def inline_salutation_snapshot() -> str:
    return f'''<!doctype html><html><body><div id="article__content">
<div id="journal-banner-text">Rapid Communications in Mass Spectrometry</div>
<div><span><span>Letter to the Editor</span></span><div>Full Access</div></div>
<h1>Earlier RCM letter</h1><a href="https://doi.org/10.1002/rcm.4321">DOI</a>
<article lang="en"><section><section id="sec1-bdy-1">
<p><i>To the Editor-in-Chief</i></p><p><i>Sir,</i></p>
<p>First paragraph with MS<sup>2</sup> notation.</p>
{figure('sch1', 'Scheme 1')}<p>Second paragraph with <i>K</i><sub>d</sub>.</p>
{figure('sch2', 'Scheme 2')}<p>Final scientific paragraph.</p></section>
<div><h2>Acknowledgements</h2><p>Supported by Example Foundation.</p><ol></ol></div>
<section id="article-references-section-1"><div><h2>REFERENCES</h2>
<div role="region"><ul><li><span>1</span>First A. First reference.</li>
<li><span>2</span>Second B. Second reference.</li></ul></div></div></section>
</section></article></div></body></html>'''


class LegacyRcmLetterTests(unittest.TestCase):
    def extract(self, source: str):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "main.html"
            path.write_text(source, encoding="utf-8")
            return extract_html(path, "html/main.html")

    def test_complete_prose_and_signoff_are_scoped_once_with_italic_salutation(self):
        result = self.extract(snapshot())
        self.assertEqual(
            [(section.heading, [block.plain_text for block in section.blocks])
             for section in result.sections],
            [
                ("Main text", [
                    "To the Editor-in-Chief. Sir,",
                    "First paragraph with MS^{2} notation.",
                    "Second paragraph with K_{d}.",
                    "Final scientific paragraph.",
                    "ALICE AUTHOR*, BOB AUTHOR*, * Department of Chemistry.",
                ]),
                ("Acknowledgements", ["Supported by Example Foundation."]),
            ],
        )
        salutation = result.sections[0].blocks[0]
        self.assertEqual(
            block_markup_to_safe_html(salutation.markdown, kind="paragraph"),
            "<em>To the Editor-in-Chief. Sir,</em>",
        )
        self.assertTrue(salutation.source_locator.endswith("/i"))
        self.assertEqual(
            block_markup_to_safe_html(result.sections[0].blocks[2].markdown, kind="paragraph"),
            "Second paragraph with <em>K</em><sub>d</sub>.",
        )

    def test_mixed_figure_sequences_and_references_retain_identity(self):
        result = self.extract(snapshot())
        self.assertEqual(
            [(item.source_id, item.label) for item in result.figures],
            [("sch1", "Scheme 1"), ("fig1", "Figure 1"), ("sch2", "Scheme 2")],
        )
        self.assertEqual(len(result.embedded_assets), 3)
        self.assertEqual(len(result.references), 2)
        self.assertIn("First reference.", result.references[0].plain_text)
        self.assertIn("Second reference.", result.references[1].plain_text)
        self.assertEqual(result.tables, [])

    def test_ambiguous_or_unrelated_layouts_do_not_recover_the_letter_body(self):
        mutations = [
            ('id="article__content"', 'id="article-content"'),
            ('id="journal-banner-text"', 'id="other-banner"'),
            ('10.1002/rcm.1234', '10.1002/jms.1234'),
            ('<article lang="en">', '<article lang="fr">'),
            ('To the Editor-in-Chief. Sir,', 'Dear reader,'),
            ('<i>To the Editor-in-Chief. Sir,</i>', 'To the Editor-in-Chief. Sir,'),
            ('id="sec1-bdy-1"', 'id="unrelated-body"'),
            ('id="sch2"', 'id="sch3"'),
            ('id="fig1"', 'id="sch1"'),
            ('id="sec-bibl-1"', 'id="related-reading"'),
            ('id="article-references-section-1"', 'id="related-references"'),
            ('<h2>Acknowledgements</h2>', '<h2>Unrelated heading</h2>'),
            ('<div id=""><p>', '<div id="related"><p>'),
            ('<i>To the Editor-in-Chief. Sir,</i>',
             '<i>To the Editor-in-Chief. Sir,</i>Unscoped authored text.'),
            ('<section id="sec1-bdy-1">', '<section id="sec1-bdy-1"><h2>Results</h2>'),
            ('<section id="sec-bibl-1">', '<p>Unscoped paragraph.</p><section id="sec-bibl-1">'),
            ('<figure id="sch1">', '<p>Unscoped figure note.</p><figure id="sch1">'),
            ('<div id=""><p>', '<div id="">Unique direct signoff text.<p>'),
            ('</ul></div></section></section>', '</ul></div></section>Unique bibliography tail.</section>'),
        ]
        for before, after in mutations:
            with self.subTest(mutation=(before, after)):
                source = snapshot()
                self.assertIn(before, source)
                result = self.extract(source.replace(before, after, 1))
                self.assertFalse(any(
                    section.heading == "Main text" and section.blocks
                    and section.blocks[0].plain_text == "To the Editor-in-Chief. Sir,"
                    for section in result.sections
                ))

    def test_inline_salutations_recover_all_prose_once_without_inventing_a_signoff(self):
        result = self.extract(inline_salutation_snapshot())
        self.assertEqual(
            [(section.heading, [block.plain_text for block in section.blocks])
             for section in result.sections],
            [
                ("Main text", [
                    "To the Editor-in-Chief", "Sir,",
                    "First paragraph with MS^{2} notation.",
                    "Second paragraph with K_{d}.",
                    "Final scientific paragraph.",
                ]),
                ("Acknowledgements", ["Supported by Example Foundation."]),
            ],
        )
        blocks = result.sections[0].blocks
        self.assertEqual(
            [block_markup_to_safe_html(b.markdown, kind="paragraph") for b in blocks[:2]],
            ["<em>To the Editor-in-Chief</em>", "<em>Sir,</em>"],
        )
        self.assertTrue(blocks[0].source_locator.endswith("/p[1]"))
        self.assertTrue(blocks[1].source_locator.endswith("/p[2]"))
        self.assertEqual(
            block_markup_to_safe_html(blocks[3].markdown, kind="paragraph"),
            "Second paragraph with <em>K</em><sub>d</sub>.",
        )
        self.assertEqual([(f.source_id, f.label) for f in result.figures],
                         [("sch1", "Scheme 1"), ("sch2", "Scheme 2")])
        self.assertEqual(len(result.embedded_assets), 2)
        self.assertEqual(len(result.references), 2)
        self.assertEqual(result.tables, [])

    def test_source_letter_designation_is_retained_only_when_explicit_in_the_header(self):
        source = inline_salutation_snapshot()
        result = self.extract(source)
        labels = [b for b in result.front_matter
                  if b.plain_text == "Article type: Letter to the Editor"]
        self.assertEqual(len(labels), 1)
        self.assertTrue(labels[0].source_locator.endswith("/span/span"))
        for altered in [
            source.replace("Letter to the Editor", ""),
            source.replace("<span><span>Letter to the Editor</span></span>", ""),
            source.replace("Earlier RCM letter", "Letter to the Editor")
                  .replace("<span><span>Letter to the Editor</span></span>", ""),
            source.replace("<span><span>Letter to the Editor</span></span>",
                           "<span><span>Letter to the Editor</span><span>Review</span></span>"),
        ]:
            with self.subTest(source=altered):
                self.assertFalse(any(b.plain_text.startswith("Article type:")
                                     for b in self.extract(altered).front_matter))

    def test_inline_greeting_layout_rejects_ambiguous_or_unscoped_content(self):
        mutations = [
            ('id="article__content"', 'id="article-content"'),
            ('id="journal-banner-text"', 'id="other-banner"'),
            ('10.1002/rcm.4321', '10.1002/jms.4321'),
            ('<article lang="en">', '<article lang="fr">'),
            ('To the Editor-in-Chief', 'Dear reader'),
            ('<p><i>Sir,</i></p>', '<p>Sir,</p>'),
            ('<p><i>Sir,</i></p>', '<p><i>Sir,</i>Extra text.</p>'),
            ('id="sec1-bdy-1"', 'id="unrelated-body"'),
            ('id="sch2"', 'id="sch3"'),
            ('id="sch2"', 'id="fig1"'),
            ('id="article-references-section-1"', 'id="related-references"'),
            ('<h2>Acknowledgements</h2>', '<h2>Unrelated heading</h2>'),
            ('<section id="sec1-bdy-1">', '<section id="sec1-bdy-1">Unscoped text.'),
            ('<section id="sec1-bdy-1">', '<section id="sec1-bdy-1"><h2>Results</h2>'),
            ('<figure id="sch1">', '<p>Unscoped figure note.</p><figure id="sch1">'),
            ('<section id="article-references-section-1">',
             '<p>Unscoped paragraph.</p><section id="article-references-section-1">'),
            ('</ol></div>', '<li>Unscoped list content.</li></ol></div>'),
            ('</article>', '</article><article lang="en"><section></section></article>'),
            ('<h1>Earlier RCM letter</h1>',
             '<h1>Earlier RCM letter</h1><a href="https://doi.org/10.1002/rcm.5432">Other DOI</a>'),
        ]
        for before, after in mutations:
            with self.subTest(mutation=(before, after)):
                source = inline_salutation_snapshot()
                self.assertIn(before, source)
                result = self.extract(source.replace(before, after, 1))
                self.assertFalse(any(section.heading == "Main text" for section in result.sections))


if __name__ == "__main__":
    unittest.main()
