from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from lxml import html

from scripts.extraction.html_extractor import (
    _sciencedirect_reference,
    extract_html,
    plain_text,
    render_inline,
)


class InlineMathReferenceBoundaryTests(unittest.TestCase):
    def extract_reference(self, item: str):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "article.html"
            path.write_text(
                '<html><body><article><h1>Synthetic reference boundaries</h1>'
                '<section id="article-references"><h2>References</h2><ol>'
                + item + '</ol></section></article></body></html>', encoding="utf-8"
            )
            return extract_html(path, "html/main.html").references[0]

    def assert_pair(self, pair, expected: str):
        self.assertIsNotNone(pair)
        rich, plain = pair
        self.assertEqual(plain, expected)
        self.assertEqual(plain_text(html.fromstring('<div>' + rich + '</div>')), plain)

    def test_inline_math_preserves_adjacent_sequence_and_explicit_spaces(self):
        for before, after, expected in (
            ("5′-CCCT", "A-3′", "5′-CCCTAA-3′"),
            ("5′-AGTC", "-3′", "5′-AGTCA-3′"),
            ("Value ", " remains.", "Value A remains."),
        ):
            with self.subTest(expected=expected):
                node = html.fromstring(
                    '<div>' + before + '<span><span></span>'
                    '<span id="MathJax-Element-1-Frame" role="presentation">'
                    '<svg aria-hidden="true"><text>A</text></svg>'
                    '<span role="presentation"><math><mtext>A</mtext></math>'
                    '</span></span></span>' + after + '</div>'
                )
                self.assert_pair((render_inline(node), plain_text(node)), expected)

    def test_display_math_preserves_unauthored_boundary_separation(self):
        for markup in (
            '<math display="block"><mi>x</mi><mo>=</mo><mn>2</mn></math>',
            '<span id="e0005"><math><mi>x</mi><mo>=</mo><mn>2</mn></math></span>',
        ):
            with self.subTest(markup=markup):
                node = html.fromstring('<div>Before' + markup + 'After</div>')
                self.assert_pair((render_inline(node), plain_text(node)), 'Before x = 2 After')

    def test_exact_lowercase_and_uppercase_single_references(self):
        for dialect in ("bib", "BIB"):
            with self.subTest(dialect=dialect):
                item = html.fromstring(
                    f'<li><span><a id="ref-id-{dialect}1" href="#b{dialect}1">1</a></span>'
                    '<span><div>A. Author</div><div><i>Journal</i>, 1 (2000), p. 2</div>'
                    '<div lang="en"><a href="https://doi.org/10.1234/one">Crossref</a>'
                    '<a href="https://scopus.invalid">View in Scopus</a></div></span></li>'
                )
                pair = _sciencedirect_reference(item)
                self.assert_pair(pair, '1. A. Author Journal, 1 (2000), p. 2 DOI: https://doi.org/10.1234/one')
                self.assertIn('<em>Journal</em>', pair[0])

    def test_current_numeric_reference_mismatches_do_not_use_old_fallback(self):
        for anchor_id, href, label, body in (
            ('ref-id-BIB3', '#bBIB2', '3', 'h3'),
            ('ref-id-BIB3', '#bbib3', '3', 'h3'),
            ('ref-id-bib3', '#bBIB3', '3', 'h3'),
            ('ref-id-BIB3', '#bBIB3', '2', 'h3'),
            ('ref-id-BIB3', '#bBIB3', '3', 'h2'),
        ):
            with self.subTest(anchor_id=anchor_id, href=href, label=label, body=body):
                item = html.fromstring(
                    f'<li><span><a id="{anchor_id}" href="{href}">{label}</a></span>'
                    f'<span id="{body}"><div>Authored citation.</div></span></li>'
                )
                self.assertIsNone(_sciencedirect_reference(item))

    def test_sciencedirect_multicitation_dois_and_authored_note_tails(self):
        item = html.fromstring(
            '<li><span><a id="ref-id-BIB14" href="#bBIB14">14</a></span>'
            '<span><div><span>(a)</span> Authored <i>chapter</i>, <strong>1989</strong>;</div>'
            '<div lang="en"><a href="https://doi.org/10.1234/first">Crossref</a>'
            '<a href="https://doi.org/10.1234/FIRST">Duplicate</a></div> authored tail.</span>'
            ' Between entries. <span><span>(b)</span><div>B. Author</div><div>Journal, 2.</div>'
            '<div lang="en"><a href="https://doi.org/10.1234/second">Crossref</a>'
            '<a href="https://scholar.google.com/">Google Scholar</a></div> Final tail.</span></li>'
        )
        pair = _sciencedirect_reference(item)
        self.assert_pair(pair, '14. (a) Authored chapter, 1989; DOI: https://doi.org/10.1234/first authored tail. Between entries. (b) B. Author Journal, 2. DOI: https://doi.org/10.1234/second Final tail.')
        self.assertIn('<em>chapter</em>', pair[0])
        self.assertIn('<strong>1989</strong>', pair[0])

    def test_wiley_multiple_citations_keep_local_dois_markup_and_tails(self):
        reference = self.extract_reference(
            '<li><span>18</span> For examples see [8b] and A. Author, <i>First Journal</i>, 1;'
            '<div><span>10.1234/first</span><a href="#">CAS</a></div>'
            ' authored tail; B. Author, <i>Second Journal</i>, 2.'
            '<div><span>10.1234/second</span><a href="#">Google Scholar</a></div> Final tail.</li>'
        )
        self.assert_pair((reference.markdown, reference.plain_text),
            '18. For examples see [8b] and A. Author, First Journal, 1; DOI: https://doi.org/10.1234/first authored tail; B. Author, Second Journal, 2. DOI: https://doi.org/10.1234/second Final tail.')
        self.assertIn('<em>First Journal</em>', reference.markdown)
        self.assertIn('<em>Second Journal</em>', reference.markdown)

    def test_wiley_repeated_control_dois_are_deduplicated_without_losing_tail(self):
        reference = self.extract_reference(
            '<li><span>1</span> Citation.'
            '<div><span>10.1234/once</span><a href="https://doi.org/10.1234/ONCE">Crossref</a></div>'
            ' Tail one.<div><span>10.1234/once</span><a href="#">CAS</a></div> Tail two.</li>'
        )
        self.assert_pair((reference.markdown, reference.plain_text),
            '1. Citation. DOI: https://doi.org/10.1234/once Tail one. Tail two.')

    def test_wiley_authored_doi_does_not_suppress_other_citation_doi(self):
        reference = self.extract_reference(
            '<li><span>1</span> First, doi:10.1234/one.'
            '<div><span>10.1234/one</span><a href="#">CAS</a></div> Second.'
            '<div><span>10.1234/two</span><a href="#">CAS</a></div></li>'
        )
        self.assert_pair((reference.markdown, reference.plain_text),
            '1. First, doi:10.1234/one. Second. DOI: https://doi.org/10.1234/two')

    def test_doi_deduplication_uses_exact_decoded_identity(self):
        reference = self.extract_reference(
            '<li><span>1</span> First, doi:10.1234/ab. Second.'
            '<div><span>10.1234/a</span><a href="#">CAS</a></div>'
            ' Third, doi:10.1234/with(1).'
            '<div><span>10.1234/with%281%29</span><a href="#">CAS</a></div></li>'
        )
        self.assert_pair((reference.markdown, reference.plain_text),
            '1. First, doi:10.1234/ab. Second. DOI: https://doi.org/10.1234/a Third, doi:10.1234/with(1).')


if __name__ == "__main__":
    unittest.main()
