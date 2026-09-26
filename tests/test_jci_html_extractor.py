from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.extraction.html_extractor import extract_html


PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "/w8AAusB9Y9Z4WQAAAAASUVORK5CYII="
)


def jci_fragment(
    *,
    doi_number: str = "12345",
    legacy_figure_id: bool = False,
    anonymous_root: bool = False,
) -> str:
    def section(target: str, label: str, body: str) -> str:
        return (
            f'<dl><dd><a href="#{target}">{label}</a>'
            f'<div id="{target}">{body}</div></dd></dl>'
        )

    return f"""
<body><div{' ' if anonymous_root else ' id="jci-article-12345"'}>
  <p><a href="https://doi.org/10.1172/JCI{doi_number}">10.1172/JCI{doi_number}</a></p>
  <h1>JCI test article</h1>
  <h4><a>A One<sup>1</sup></a>, <a>B Two<sup>2</sup></a></h4>
  <div id="author-affiliation-0"><p><sup>1</sup>First institute.</p><p><sup>2</sup>Second institute.</p></div>
  <div>Authorship note: A and B contributed equally.</div>
  <p>Published September 14, 2023 - More info</p>
  <div id="full-publication-dropdown">Published in Volume 7, Issue 8 on November 15, 2023. J Clin Invest. 2023;7(8):e12345.</div>
  {section('section-abstract', 'Abstract', '<p>Abstract prose.</p>')}
  {section('section-1', 'Introduction', '<p>Introduction prose.</p>')}
  {section('section-2', 'Results', '<p><span>Result heading.</span> Result prose.</p>')}
  {section('section-3', 'Discussion', '<p>Discussion prose.</p>')}
  {section('section-4', 'Methods', '<p><span>Method heading.</span> Method prose.</p>')}
  {section('supplemental-material', 'Supplemental material', '<p><a href="/sd/1">View Supplemental data</a></p>')}
  {section('references', 'References', '<ol><li value="1">First citation.<div><a href="https://doi.org/10.1000/one">CrossRef</a></div></li><li value="2">Second citation.</li></ol>')}
  <figure id="{'figure-1' if legacy_figure_id else 'JCI12345-figure-1'}"><a><img src="data:image/png;base64,{PNG}" alt="Figure 1. Short title."/></a><a>Figure 1</a><p>Short title. Full panel legend.</p></figure>
</div></body>
"""


class JciHtmlExtractorTests(unittest.TestCase):
    def extract(self, value: str):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "main.html"
            path.write_text(value, encoding="utf-8")
            return extract_html(path, "papers (private)/00123/html/main.html")

    def test_exact_jci_fragment_restores_sections_front_matter_captions_and_refs(self) -> None:
        result = self.extract(jci_fragment())
        headings = [section.heading for section in result.sections]
        self.assertNotIn("A One,<sup>1</sup> B Two<sup>2</sup>", headings)
        self.assertIn("Abstract", headings)
        self.assertIn("Result heading.", headings)
        self.assertIn("Method heading.", headings)
        self.assertEqual(result.bibliographic["volume"], "7")
        self.assertEqual(result.bibliographic["issue"], "8")
        self.assertEqual(result.bibliographic["article_number"], "e12345")
        self.assertEqual(result.bibliographic["first_published"], "September 14, 2023")
        self.assertTrue(any("First institute" in block.plain_text for block in result.front_matter))
        self.assertEqual(result.figures[0].caption_plain, "Short title. Full panel legend.")
        self.assertEqual(len(result.references), 2)
        self.assertEqual(
            result.references[0].plain_text,
            "1. First citation. DOI: https://doi.org/10.1000/one",
        )
        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            ["View Supplemental data"],
        )

    def test_doi_mismatch_does_not_activate_jci_rewrite(self) -> None:
        result = self.extract(jci_fragment(doi_number="99999"))
        self.assertEqual(result.front_matter, [])
        self.assertEqual(result.references, [])
        self.assertTrue(any("A One" in section.heading for section in result.sections))

    def test_legacy_contiguous_figure_ids_activate_jci_rewrite(self) -> None:
        result = self.extract(jci_fragment(legacy_figure_id=True))
        self.assertIn("Abstract", [section.heading for section in result.sections])
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Short title. Full panel legend.",
        )
        self.assertEqual(len(result.references), 2)

    def test_anonymous_root_with_legacy_figure_ids_activates_jci_rewrite(self) -> None:
        result = self.extract(
            jci_fragment(legacy_figure_id=True, anonymous_root=True)
        )
        self.assertIn("Abstract", [section.heading for section in result.sections])
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Short title. Full panel legend.",
        )
        self.assertEqual(len(result.references), 2)


if __name__ == "__main__":
    unittest.main()
