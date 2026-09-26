"""Regression coverage for legacy Nature full-text pages without body headings."""

import unittest

from lxml import html

from scripts.extraction.html_extractor import (
    _body_sections,
    _nature_unheaded_article_body_root,
    _nature_unheaded_front_matter,
    _springer_header_details,
)


def _article(body: str, *, second_reference: bool = True) -> object:
    reference_two = '<p id="ref-CR2">Second citation.</p>' if second_reference else ""
    return html.fromstring(
        f'''<article lang="en">
        <div><header><ul><li>News &amp; Views</li>
        <li>Published: <time datetime="1998-01-29">29 January 1998</time></li></ul>
        <h1>Reading the minor groove</h1><ul><li>
        <a href="#auth-Claude-Helene-Aff1">Claude Hélène</a><sup><a href="#Aff1">1</a></sup>
        </li></ul><p><i>Nature</i> volume 391, pages 436–438 (1998) Cite this article</p>
        </header></div>
        <div><div><a href="/articles/example.pdf">Download PDF</a></div>
        <div>{body}</div>
        <div><div id="MagazineFulltextArticleBodySuffix"><h2>References</h2>
        <p id="ref-CR1">First citation.</p>{reference_two}</div>
        <section aria-labelledby="author-information"><h2 id="author-information">Author information</h2>
        <h3 id="affiliations">Authors and Affiliations</h3><ol><li id="Aff1">
        <p>Laboratoire de Biophysique, Paris, France</p><p>Claude Hélène</p>
        </li></ol></section>
        <section aria-labelledby="article-info"><h2 id="article-info">About this article</h2>
        <h3 id="citeas">Cite this article</h3><p>Example citation.</p></section></div>
        </div></article>'''
    )


class NatureUnheadedArticleTests(unittest.TestCase):
    def test_complete_authenticated_body_gets_one_main_text_section(self) -> None:
        document = _article(
            '<div><p>Opening article prose with A˙T notation.</p></div>'
            '<div><p>Closing article prose.</p>'
            '<div id="figure-1"><figure><figcaption>Figure 1.</figcaption></figure></div>'
            '</div>'
        )
        self.assertIsNotNone(_nature_unheaded_article_body_root(document))
        sections, supporting = _body_sections(document, "article.html")
        self.assertEqual(supporting, [])
        self.assertEqual([section.heading for section in sections], ["Main text"])
        self.assertEqual(
            [block.plain_text for block in sections[0].blocks],
            [
                "Opening article prose with A˙T notation.",
                "Closing article prose.",
            ],
        )
        details = _springer_header_details(document)
        self.assertIsNotNone(details)
        self.assertEqual(
            (details["volume"], details["pages"], details["date"]),
            ("391", "436–438", "29 January 1998"),
        )
        front_matter = _nature_unheaded_front_matter(document, "article.html")
        self.assertEqual(
            [block.plain_text for block in front_matter],
            [
                "Affiliation — Claude Hélène: "
                "Laboratoire de Biophysique, Paris, France"
            ],
        )

    def test_near_misses_do_not_create_a_body_scope(self) -> None:
        valid_body = (
            '<div><p>Opening article prose.</p></div>'
            '<div><p>Closing article prose.</p></div>'
        )
        for document in (
            _article(valid_body, second_reference=False),
            _article('<div><p>Only one wrapper.</p></div>'),
            _article(
                '<div><p>Opening article prose.</p></div>'
                '<div class="layout"><p>Closing article prose.</p></div>'
            ),
        ):
            with self.subTest(html=html.tostring(document, encoding="unicode")[:120]):
                self.assertIsNone(_nature_unheaded_article_body_root(document))


if __name__ == "__main__":
    unittest.main()
