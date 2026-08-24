from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

try:
    from lxml import html as lxml_html
except ModuleNotFoundError:
    lxml_html = None

from scripts.extraction.paths import (
    UnsafePathError,
    canonical_json,
    ensure_within,
    sha256_file,
    validate_record_id,
)
from scripts.extraction.models import ContentBlock, SourceFile, TableCell, TableItem, TablePart
from scripts.extraction.metadata import load_record_metadata
from scripts.extraction.pipeline import (
    ExtractionError,
    _apply_front_matter_overrides,
    _apply_reference_entries,
    _attach_assets,
    _record_missing_asset_warnings,
)
from scripts.extraction.renderer import write_table_derivatives
from scripts.extraction.rich_text import (
    block_markup_to_safe_html,
    plain_text_from_safe_html,
    rich_text_matches_plain,
)
from scripts.extraction.table_schema import TableSchemaError, validate_table_payload
from scripts.extraction.validation import (
    _required_asset_findings,
    _table_derivative_findings,
    validate_candidate,
)

if lxml_html is not None:
    from scripts.extraction.html_extractor import (
        _sciencedirect_reference,
        extract_html,
    )
else:  # pragma: no cover - documents the optional dependency boundary
    extract_html = None


class ExtractionMetadataTests(unittest.TestCase):
    def test_canonical_author_name_is_preferred_for_rendering(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "00001.yaml"
            path.write_text(
                "title: Synthetic article\n"
                "authors:\n"
                '  - name: "Thomas\u2005G. Example"\n'
                '    canonical_name: "Thomas G. Example"\n'
                "publication_year: 2000\n"
                "journal: Synthetic Journal\n"
                "doi: 10.0000/example\n"
                "document_type: research_article\n",
                encoding="utf-8",
            )

            metadata = load_record_metadata(path, "00001")

        self.assertEqual(metadata.authors, ("Thomas G. Example",))

@unittest.skipUnless(lxml_html is not None, "lxml is required for extraction tests")
class HtmlExtractionTests(unittest.TestCase):
    def extract(self, source: str):
        with TemporaryDirectory() as directory:
            source_path = Path(directory) / "article.html"
            source_path.write_text(source, encoding="utf-8")
            return extract_html(source_path, "html/article.html")

    def test_old_acs_title_author_note_marker_is_not_title_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Scientific title<a href="#bi9912847AF2">†</a></h1>
<section><h2>Abstract</h2><p>Text.</p></section>
<div id="bi9912847AF2">Author note.</div>
</article></body></html>"""
        )

        self.assertEqual(result.title, "Scientific title")

    def test_non_acs_title_link_and_authored_marker_are_preserved(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Scientific <a href="#topic">title</a> †</h1>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "Scientific title †")

    def test_old_acs_redundant_citation_parentheses_are_collapsed(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS citations</h1><section><h2>Results</h2>
<p>Evidence (<em> (<a ref-data-modal-source-id="b1">1</a>, <a ref-data-modal-source-id="b2">2</a>)</em>).</p>
<p>Authored <em>(nested words)</em> and (<em>(3)</em>) stay unchanged.</p>
</section></article></body></html>"""
        )

        first, second = result.sections[0].blocks
        self.assertEqual(first.plain_text, "Evidence (1, 2).")
        self.assertEqual(first.markdown, "Evidence (<em>1, 2</em>).")
        self.assertEqual(
            second.plain_text,
            "Authored (nested words) and ((3)) stay unchanged.",
        )

    def test_utf8_inline_notation_citations_and_terminal_linkouts(self) -> None:
        result = self.extract(
            """<!doctype html>
<html><body><article>
  <div><a>Volume 12, Issue 3</a><span> pp. 101-109</span></div>
  <div><span>First published: </span><span>07 June 2020</span></div>
  <h1>β-Lactam formation in H₂O</h1>
  <section>
    <h2>Results</h2>
    <p>Rate <i>k</i><sub>obs</sub> was 10<sup>−3</sup> s<sup>−1</sup>;
       see <a href="#bib1">1</a>.</p>
    <p>A complete sentence.<a href="#fig1">1</a></p>
    <p>Previously described.<span><a href="#bib1">1</a></span><a href="#fig1">1</a>, <a href="#fig2">2</a></p>
  </section>
  <figure id="fig1"><figcaption><p><strong>Figure 1.</strong> Synthetic caption.</p></figcaption></figure>
  <section id="article-references">
    <h2>References</h2>
    <ol><li id="bib1"><span>1.</span> Example, α study.
      <div><span>10.1234/example</span></div></li></ol>
  </section>
</article></body></html>"""
        )

        self.assertEqual(result.title, "β-Lactam formation in H₂O")
        self.assertEqual(len(result.sections), 1)
        paragraphs = result.sections[0].blocks
        self.assertIn("<em>k</em><sub>obs</sub>", paragraphs[0].markdown)
        self.assertIn("10<sup>−3</sup> s<sup>−1</sup>", paragraphs[0].markdown)
        self.assertIn("see [1].", paragraphs[0].markdown)
        self.assertEqual(paragraphs[1].markdown, "A complete sentence.")
        self.assertEqual(paragraphs[2].markdown, "Previously described.[1]")
        self.assertEqual(len(result.references), 1)
        self.assertIn("α study", result.references[0].plain_text)
        self.assertIn(
            "https://doi.org/10.1234/example",
            result.references[0].markdown,
        )
        self.assertEqual(
            result.bibliographic,
            {
                "volume": "12",
                "issue": "3",
                "pages": "101-109",
                "first_published": "07 June 2020",
            },
        )

    def test_aacr_flat_div_references_exclude_page_navigation(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><section>
<h1>AACR flat references</h1>
<nav><ul><li>Previous Article</li><li>Share</li><li>Export citation</li></ul></nav>
<h2 id="8733021">References</h2>
<div><div id="8733021-content"><div>
  <div xmlns:helper="urn:XsltStringHelper"><div><div><span>1</span><div>
    Alpha A. First study. <div><em>Journal One</em></div>
    <div>2007</div>;<div>6</div>:<div>346</div>–54.
    <div><a>Crossref</a></div><div><a>Search ADS</a></div>
    <div><a>OpenURL</a></div><div><a>Google Scholar</a></div>
  </div></div></div></div>
  <div xmlns:helper="urn:XsltStringHelper"><div><div><span>2</span><div>
    Beta B. Second study. <div>Journal Two</div>
    <div>2008</div>;<div>7</div>:<div>10</div>–20. 10.1000/source-literal.
  </div></div></div></div>
  <div xmlns:helper="urn:XsltStringHelper"><div><div><span>3</span><div>
    Gamma G. Third study.
  </div></div></div></div>
</div></div></div>
</section></body></html>"""
        )

        self.assertEqual(len(result.references), 3)
        self.assertEqual(
            [reference.block_id for reference in result.references],
            ["reference-001", "reference-002", "reference-003"],
        )
        self.assertEqual(
            result.references[0].plain_text,
            "1. Alpha A. First study. Journal One 2007;6:346–54.",
        )
        self.assertEqual(
            result.references[1].plain_text,
            "2. Beta B. Second study. Journal Two 2008;7:10–20. "
            "10.1000/source-literal.",
        )
        combined = "\n".join(
            reference.plain_text for reference in result.references
        )
        self.assertNotIn("Previous Article", combined)
        self.assertNotIn("Share", combined)
        self.assertNotIn("Export citation", combined)
        self.assertNotIn("Crossref", combined)
        self.assertNotIn("Search ADS", combined)
        self.assertNotIn("OpenURL", combined)
        self.assertNotIn("Google Scholar", combined)
        self.assertNotIn("https://doi.org/", result.references[1].markdown)
        for reference in result.references:
            safe_html = block_markup_to_safe_html(
                reference.markdown, kind="reference"
            )
            self.assertTrue(
                rich_text_matches_plain(reference.plain_text, safe_html)
            )
            self.assertEqual(
                plain_text_from_safe_html(safe_html), reference.plain_text
            )

    def test_legacy_acs_reference_doi_anchor_keeps_word_boundary(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><section>
<h1>Legacy ACS DOI boundary</h1>
<h2 id="flat">References</h2>
<div id="flat-content"><div>
  <div><div><div><span>1.</span><div>
    Example A. <em>J. Am. Chem. Soc.</em> <strong>1992</strong>, 114,
    5911-5919.<div><a href="https://doi.org/10.1000/example">
      https://doi.org/10.1000/example</a></div>
  </div></div></div></div>
</div></div>
</section></body></html>"""
        )

        self.assertEqual(len(result.references), 1)
        reference = result.references[0]
        self.assertIn("5911-5919. https://doi.org/", reference.plain_text)
        self.assertIn("5911-5919. https://doi.org/", reference.markdown)
        safe_html = block_markup_to_safe_html(
            reference.markdown, kind="reference"
        )
        self.assertEqual(
            plain_text_from_safe_html(safe_html), reference.plain_text
        )
        self.assertTrue(
            rich_text_matches_plain(reference.plain_text, safe_html)
        )

    def test_legacy_acs_composite_reference_keeps_all_subentries(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><section>
<h1>Legacy ACS composite bibliography</h1>
<h2 id="flat">References</h2>
<div id="flat-content"><div>
  <div><div><div><span>1.</span>
    <div><span>(a)</span><span>Alpha A.</span> First study.</div><div>(b) Beta B. Second study.</div>
    <div><span>(c)</span><span>Gamma G.</span> Third study.
      <div><a>Crossref</a></div><div><a>Google Scholar</a></div></div>
  </div></div></div>
</div></div>
</section></body></html>"""
        )

        self.assertEqual(len(result.references), 1)
        reference = result.references[0]
        self.assertEqual(
            reference.plain_text,
            "1. (a) Alpha A. First study. (b) Beta B. Second study. "
            "(c) Gamma G. Third study.",
        )
        self.assertNotIn("Crossref", reference.plain_text)
        self.assertNotIn("Google Scholar", reference.plain_text)
        safe_html = block_markup_to_safe_html(
            reference.markdown, kind="reference"
        )
        self.assertEqual(
            plain_text_from_safe_html(safe_html), reference.plain_text
        )
        self.assertTrue(
            rich_text_matches_plain(reference.plain_text, safe_html)
        )

    def test_aacr_issue_date_and_compact_citation_are_bibliographic(self) -> None:
        result = self.extract(
            """<!doctype html><html><body>
<div id="issueInfo-IssueInfo_Article"><div>
  <div><span>Volume 6, Issue 1</span></div>
  <div>1 January 2007</div>
</div></div>
<div><em>Mol Cancer Ther</em> (2007) 6 (1): 346–354.</div>
<article><h1>AACR bibliography</h1></article>
</body></html>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "date": "1 January 2007",
                "volume": "6",
                "issue": "1",
                "pages": "346–354",
            },
        )

    def test_aacr_bibliographic_grammar_is_marker_gated_and_strict(self) -> None:
        without_marker = self.extract(
            """<!doctype html><html><body>
<div><em>Mol Cancer Ther</em> (2007) 6 (1): 346–354.</div>
<article><h1>Non-AACR bibliography</h1></article>
</body></html>"""
        )
        malformed_aacr = self.extract(
            """<!doctype html><html><body>
<div id="issueInfo-IssueInfo_Article"><div>Published 1 January 2007</div></div>
<div><em>Mol Cancer Ther</em> (2007) 6 (1): e346.</div>
<article><h1>Malformed AACR bibliography</h1></article>
</body></html>"""
        )

        self.assertEqual(without_marker.bibliographic, {})
        self.assertEqual(malformed_aacr.bibliographic, {})

    def test_aacr_flat_div_references_reject_noncontiguous_labels(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Malformed AACR target</h1>
<section><h2 id="flat">References</h2>
  <div id="flat-content"><div>
    <div><div><div><span>1</span><div>Flat first reference.</div></div></div></div>
    <div><div><div><span>3</span><div>Flat noncontiguous reference.</div></div></div></div>
  </div></div>
  <ol><li><span>1.</span> Fallback list reference.</li></ol>
</section></article></body></html>"""
        )

        self.assertEqual(len(result.references), 1)
        self.assertEqual(
            result.references[0].plain_text,
            "1. Fallback list reference.",
        )

    def test_old_acs_split_div_figure_caption_is_extracted(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS figure dialect</h1><section><h2>Results</h2>
<figure id="fig1"><div>
  <div>Figure 1.</div>
  <div><p>Cleavage at 5′-TGGT-3′ by <strong>7R</strong>.</p></div>
</div></figure>
</section></article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Cleavage at 5′-TGGT-3′ by 7R.",
        )
        self.assertEqual(
            result.figures[0].caption_markdown,
            "Cleavage at 5′-TGGT-3′ by <strong>7R</strong>.",
        )

    def test_old_acs_author_and_article_information_is_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS front matter</h1>
<div>
  <a rel="nofollow" aria-haspopup="true">Peter B. Dervan</a>
  <span><a reveal-id="ja123AF1" aria-label="Corresponding author note"></a></span>
  <div content-id="ja123AF1">Corresponding author. No contact information available.</div>
  <span><a reveal-id="ja123AF1" aria-label="Corresponding author note"></a></span>
</div>
<div>Publisher: American Chemical Society</div>
<div><span>Received:</span><span>December 13, 1999</span></div>
<div><span>Published Online:</span><span>April 27, 2000</span></div>
<div><span>Published in Issue:</span><span>May 24, 2000</span></div>
<div>Online ISSN: 1520-5126</div>
<div>Print ISSN: 0002-7863</div>
<div>Copyright © 2000 American Chemical Society</div>
<section><h2>Abstract</h2><p>Text.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Copyright: Copyright © 2000 American Chemical Society",
                "Corresponding author: Peter B. Dervan. No contact information available.",
                "Publisher: American Chemical Society",
                (
                    "Article history: Received: December 13, 1999; "
                    "Published Online: April 27, 2000; "
                    "Published in Issue: May 24, 2000"
                ),
                "Online ISSN: 1520-5126",
                "Print ISSN: 0002-7863",
            ],
        )
        self.assertEqual(
            len({block.block_id for block in result.front_matter}),
            len(result.front_matter),
        )

    def test_old_acs_adjacent_superscript_tokens_are_one_script(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Old ACS superscript dialect</h1><section><h2>Results</h2>
<p>ImImPyPy-γ<sup>(</sup><em><sup>R</sup></em><sup>)-</sup><em><sup>seco</sup></em><sup>-CBI</sup>.</p>
<p>ImImPyPy-(R)<sup>H</sup><sub><sup>2</sup></sub><sup>N</sup>γ.</p>
<p><strong>ImImPyPy-(R)<sup>H</sup></strong><sub><sup>2</sup></sub><strong><sup>N</sup></strong><strong>γ.</strong></p>
</section></article></body></html>"""
        )

        first, second, third = result.sections[0].blocks
        self.assertEqual(first.plain_text, "ImImPyPy-γ^{(R)-seco-CBI}.")
        self.assertEqual(
            first.markdown,
            "ImImPyPy-γ<sup>(<em>R</em>)-<em>seco</em>-CBI</sup>.",
        )
        self.assertEqual(second.plain_text, "ImImPyPy-(R)^{H_{2}N}γ.")
        self.assertEqual(
            second.markdown,
            "ImImPyPy-(R)<sup>H<sub>2</sub>N</sup>γ.",
        )
        self.assertEqual(third.plain_text, "ImImPyPy-(R)^{H_{2}N}γ.")
        self.assertEqual(
            third.markdown,
            "<strong>ImImPyPy-(R)<sup>H<sub>2</sub>N</sup>γ.</strong>",
        )
        for block in (first, second, third):
            safe_html = block_markup_to_safe_html(block.markdown, kind="paragraph")
            self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))
    def test_elsevier_citation_part_is_not_treated_as_markdown_link(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Multipart citation</h1>
<section><h2>Results</h2>
<p>Prior work <a href="#b0035"><span>[6](b)</span></a> established this.</p>
</section>
<section id="references"><h2>References</h2><ol>
<li id="b0035"><span>[6]</span> Synthetic reference.</li>
</ol></section>
</article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.markdown,
            "Prior work <sup>[6](b)</sup> established this.",
        )
        safe_html = block_markup_to_safe_html(block.markdown, kind="paragraph")
        self.assertEqual(
            safe_html,
            "Prior work <sup>[6](b)</sup> established this.",
        )
        self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_sciencedirect_hash_bib_citation_style_is_label_gated(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Mixed citation styles</h1>
<section><h2>Results</h2>
<p>Numeric <a href="#bib2">2</a>; already bracketed <a href="#bib3">[3]</a>;
multipart <a href="#bib6b">6(b)</a>; author-year
(<a href="#bib41">Van Regenmortel et al., 2000</a>); noncitation
<a href="#bibliography">References</a>.</p>
<p>Comma list [<a href="#bib4">4</a>,<a href="#bib12">12</a>]; range
[<a href="#bib2-11">2–11</a>]; bracketed multipart
[<a href="#bib6b">6(b)</a>].</p>
<p>Expanded bracket labels [<a href="#bib5">[5]</a>,
<a href="#bib6">[6]</a>, <a href="#bib7b">[7](b)</a>].</p>
<p>Superscript <sup><a href="#bib7">7</a></sup> and Elsevier multipart
<a href="#b0035"><span>[6](b)</span></a>.</p>
</section>
<section id="references"><h2>References</h2><ol>
<li id="bib2">2. Numeric.</li><li id="bib3">3. Bracketed.</li>
<li id="bib6b">6(b). Part.</li><li id="bib7">7. Superscript.</li>
<li id="bib41">41. Author-year.</li><li id="b0035">6(b). Multipart.</li>
<li id="bib4">4. List.</li><li id="bib12">12. List.</li>
<li id="bib2-11">2–11. Range.</li>
<li id="bib5">5. Expanded.</li><li id="bib6">6. Expanded.</li>
<li id="bib7b">7(b). Expanded.</li>
</ol></section>
</article></body></html>"""
        )

        mixed, bracket_groups, expanded_brackets, superscript = result.sections[0].blocks
        self.assertEqual(
            mixed.markdown,
            (
                "Numeric [2]; already bracketed [3]; multipart [6(b)]; "
                "author-year (Van Regenmortel et al., 2000); noncitation References."
            ),
        )
        self.assertNotIn("[[3]]", mixed.markdown)
        self.assertEqual(
            bracket_groups.markdown,
            "Comma list [4,12]; range [2–11]; bracketed multipart [6(b)].",
        )
        bracket_safe_html = block_markup_to_safe_html(
            bracket_groups.markdown, kind="paragraph"
        )
        self.assertEqual(
            plain_text_from_safe_html(bracket_safe_html), bracket_groups.plain_text
        )
        self.assertTrue(
            rich_text_matches_plain(bracket_groups.plain_text, bracket_safe_html)
        )
        self.assertEqual(
            expanded_brackets.markdown,
            "Expanded bracket labels [5, 6, 7(b)].",
        )
        self.assertEqual(
            expanded_brackets.plain_text,
            "Expanded bracket labels [5, 6, 7(b)].",
        )
        self.assertEqual(
            superscript.markdown,
            "Superscript <sup>[7]</sup> and Elsevier multipart <sup>[6](b)</sup>.",
        )
        author_year_plain = "Author-year (Van Regenmortel et al., 2000)."
        author_year = self.extract(
            """<!doctype html><html><body><article><h1>Author-year</h1>
<section><h2>Results</h2><p>Author-year
(<a href="#bib41">Van Regenmortel et al., 2000</a>).</p></section>
<section id="references"><h2>References</h2><ol>
<li id="bib41">41. Author-year.</li></ol></section></article></body></html>"""
        ).sections[0].blocks[0]
        safe_html = block_markup_to_safe_html(author_year.markdown, kind="paragraph")
        self.assertEqual(author_year.plain_text, author_year_plain)
        self.assertEqual(plain_text_from_safe_html(safe_html), author_year_plain)
        self.assertTrue(rich_text_matches_plain(author_year_plain, safe_html))

        abbreviated = self.extract(
            """<!doctype html><html><body><article><h1>Author-year continuation</h1>
<section><h2>Results</h2><p>Prior work
(<a href="#bib171">Weber et al., 2021</a>, <a href="#bib172">2022</a>).</p></section>
<section id="references"><h2>References</h2><ol>
<li id="bib171">171. Weber 2021.</li><li id="bib172">172. Weber 2022.</li>
</ol></section></article></body></html>"""
        ).sections[0].blocks[0]
        abbreviated_plain = "Prior work (Weber et al., 2021, 2022)."
        abbreviated_html = block_markup_to_safe_html(
            abbreviated.markdown, kind="paragraph"
        )
        self.assertEqual(abbreviated.markdown, abbreviated_plain)
        self.assertEqual(abbreviated.plain_text, abbreviated_plain)
        self.assertEqual(
            plain_text_from_safe_html(abbreviated_html), abbreviated_plain
        )
        self.assertTrue(
            rich_text_matches_plain(abbreviated_plain, abbreviated_html)
        )

    def test_external_link_parentheses_are_safe_in_rich_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Parenthesized link</h1>
<section><h2>Results</h2>
<p>See <a href="https://example.test/search?q=alpha(beta)">the source</a>.</p>
</section>
</article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        self.assertIn("alpha%28beta%29", block.markdown)
        safe_html = block_markup_to_safe_html(block.markdown, kind="paragraph")
        self.assertIn('href="https://example.test/search?q=alpha%28beta%29"', safe_html)
        self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_sciencedirect_paragraph_caption_recovers_scheme_identity(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect caption</h1>
<section><h2>Results</h2>
<figure id="f0015"><span><img alt=""></span><span><span>
<p id="sp025"><span>Scheme 1</span>. Reagents used SOCl<sub>2</sub>.</p>
</span></span></figure>
</section>
</article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        scheme = result.figures[0]
        self.assertEqual(
            (scheme.figure_id, scheme.kind, scheme.label),
            ("scheme_001", "scheme", "Scheme 1"),
        )
        self.assertEqual(scheme.caption_plain, "Reagents used SOCl_{2}.")
        self.assertEqual(
            scheme.caption_markdown,
            "Reagents used SOCl<sub>2</sub>.",
        )

    def test_sciencedirect_keyword_divs_without_ids_are_included(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect keywords</h1>
<div id="aep-keywords"><h2>Keywords</h2>
<div><span>Polyamides</span></div><div><span>Minor groove</span></div>
</div>
</article></body></html>"""
        )

        self.assertEqual(len(result.sections), 1)
        self.assertEqual(result.sections[0].heading, "Keywords")
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Polyamides", "Minor groove"],
        )
        self.assertEqual(
            [block.kind for block in result.sections[0].blocks],
            ["keyword", "keyword"],
        )

    def test_sciencedirect_research_highlights_exclude_issue_navigation(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect highlights</h1>
<div><h3>Research highlights</h3>
<div id="sp005">► First authored highlight. ► Second authored highlight.</div>
</div>
<ul class="issue-navigation"><li>Previous article in issue</li>
<li>Next article in issue</li></ul>
</article></body></html>"""
        )

        self.assertEqual(len(result.sections), 1)
        self.assertEqual(result.sections[0].heading, "Research highlights")
        self.assertEqual(len(result.sections[0].blocks), 1)
        block = result.sections[0].blocks[0]
        self.assertEqual(block.kind, "list")
        self.assertEqual(
            block.plain_text,
            "- First authored highlight.\n- Second authored highlight.",
        )
        self.assertNotIn("article in issue", block.plain_text)

    def test_sciencedirect_abspara_is_scoped_to_abstract(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect abstract dialect</h1>
<section id="abstract"><h2>Abstract</h2>
<div id="abspara0010"><span>The ability of <i>AVP</i> to inhibit β virus was evaluated.</span></div>
<div id="sp0015"><span>Existing abstract dialect remains visible.</span></div>
</section>
<section id="methods"><h2>Methods</h2>
<div id="abspara0090"><span>Wrongly named non-abstract UI text.</span></div>
<p>Authored method prose.</p>
</section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            [
                "The ability of AVP to inhibit β virus was evaluated.",
                "Existing abstract dialect remains visible.",
            ],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Authored method prose."],
        )
        self.assertNotIn(
            "Wrongly named non-abstract UI text",
            "\n".join(
                block.plain_text
                for section in result.sections
                for block in section.blocks
            ),
        )
        for block in result.sections[0].blocks:
            safe_html = block_markup_to_safe_html(block.markdown, kind=block.kind)
            self.assertEqual(plain_text_from_safe_html(safe_html), block.plain_text)
            self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_sciencedirect_bibliographic_and_affiliation_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<div id="publication"><div>Volume 404, Issue 3, 21 January 2011,
Pages 848-852</div></div>
<h1>ScienceDirect front matter</h1>
<div id="banner"><div><div id="author-group">
<span type="button"><span>Alice Example</span><span id="baff1"><sup>a</sup></span></span>
<a href="/author/example"><span>Bob Example</span><span id="baff2"><sup>b</sup></span></a>
<dl><dt><sup>a</sup></dt><dd>Department A, University A</dd></dl>
<dl><dt><sup>b</sup></dt><dd>Department B, University B</dd></dl>
</div><p>Received 8 December 2010, Available online 23 December 2010.</p></div></div>
<section><h2>Results</h2><p>Body.</p></section>
<div><span>Copyright © 2010 Publisher. All rights reserved.</span></div>
</article></body></html>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "volume": "404",
                "issue": "3",
                "pages": "848-852",
                "date": "21 January 2011",
            },
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation assignments: Alice Example (a); Bob Example (b)",
                "Affiliation a: Department A, University A",
                "Affiliation b: Department B, University B",
                "Article history: Received 8 December 2010, Available online 23 December 2010.",
                "Copyright: Copyright © 2010 Publisher. All rights reserved.",
            ],
        )

    def test_sciencedirect_article_number_author_note_and_license_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<div id="publication"><h2>Example Journal</h2><div><a href="/journal/example/vol/230">Volume 230</a>, 15 June 2023, 115256</div></div>
<h1>ScienceDirect current front matter</h1>
<div id="banner"><div id="author-group">
<span>Author links open overlay panel</span>
<span type="button"><span>Alice Example</span><span id="bfn1"><sup>1</sup></span></span>,
<a href="/author/bob"><span>Bob Example</span><span id="bfn1"><sup>1</sup></span></a>
</div></div>
<div id="article-identifier-links"><a href="https://doi.org/10.1000/example">DOI</a></div>
<div><div><span>Under a Creative Commons </span>
<a href="http://creativecommons.org/licenses/by/4.0/">license</a></div>
<div>Open access</div></div>
<section><h2>Results</h2><p>Body.</p></section>
<div><dl><dt><a href="#bfn1"><sup>1</sup></a></dt>
<dd><div id="ntpara0010">These authors contributed equally to this work.</div></dd>
</dl></div>
<div><span>© 2023 The Authors. Published by Example Publisher.</span></div>
</article></body></html>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "article_number": "115256",
                "date": "15 June 2023",
                "volume": "230",
            },
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Author note assignments: Alice Example (1); Bob Example (1)",
                "Author note 1: These authors contributed equally to this work.",
                "License: Creative Commons license: http://creativecommons.org/licenses/by/4.0/",
                "Access: Open access",
                "Copyright: © 2023 The Authors. Published by Example Publisher.",
            ],
        )

    def test_sciencedirect_reference_nested_blocks_keep_word_boundaries(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect references</h1>
<section><h2>References</h2><ol><li>
  <span><a id="ref-id-b0005"><span>[1]</span></a></span>
  <span id="h0005">
    <div><div>A. Example, <em>et al.</em></div>
      <div id="ref-id-h0005">A β-target <i>study</i></div></div>
    <div>Journal Name, 2 (2020), p. 3,
      <a href="https://doi.org/10.1000/example">10.1000/example</a></div>
    <div lang="en"><a href="https://scholar.example/">Google Scholar</a></div>
  </span>
</li></ol></section>
</article></body></html>"""
        )

        self.assertEqual(len(result.references), 1)
        reference = result.references[0]
        self.assertIn("A. Example, et al. A β-target study Journal Name", reference.plain_text)
        self.assertIn(
            "A. Example, <em>et al.</em> A β-target <em>study</em> Journal Name",
            reference.markdown,
        )
        self.assertNotIn("Google Scholar", reference.plain_text)
        self.assertIn("DOI: https://doi.org/10.1000/example", reference.plain_text)

    def test_sciencedirect_aria_caption_keeps_split_multi_panel_legend(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect split caption</h1>
<section><h2>Results</h2><p>Body.</p>
<figure id="fig2"><span>
  <img aria-describedby="cap0020"
    src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    alt="Fig. 2">
  <ol><li><a href="https://example.invalid/figure">Download image</a></li></ol>
</span><span><span id="cap0020">
  <p id="fspara0010"><span>Fig. 2</span>. Synthetic result.</p>
  <div id="fspara0015"><strong>(A)</strong> First panel. <strong>(B)</strong> Second panel.</div>
</span></span></figure></section>
</article></body></html>"""
        )

        self.assertEqual(len(result.figures), 1)
        caption = result.figures[0]
        self.assertEqual(caption.label, "Figure 2")
        self.assertEqual(
            caption.caption_plain,
            "Synthetic result. (A) First panel. (B) Second panel.",
        )
        self.assertEqual(
            caption.caption_markdown,
            "Synthetic result.\n\n<strong>(A)</strong> First panel. "
            "<strong>(B)</strong> Second panel.",
        )

    def test_sciencedirect_bib_sref_references_preserve_authored_labels_and_ui_rules(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>New ScienceDirect reference dialect</h1>
<section><h2>References</h2><ol>
<li><span><a href="#bbib1" id="ref-id-bib1">Abend et al., 2007</a></span>
  <span id="sref1"><div><div>J.R. Abend, J.A. Low, M.J. Imperiale</div>
    <div id="ref-id-sref1">Inhibitory effect on viral replication</div></div>
    <div>J. Virol., 81 (2007), pp. 272-279,
      <a href="https://doi.org/10.1000/example">10.1000/example</a></div>
    <div lang="en"><a href="https://scopus.example/">View in Scopus</a>
      <a href="https://scholar.example/">Google Scholar</a></div></span></li>
<li><span><a href="#bbib2" id="ref-id-bib2">2</a></span>
  <span id="sref2"><div><div>Numeric authors</div>
    <div id="ref-id-sref2">Numeric-title study</div></div>
    <div>Journal Two, 2 (2008), pp. 10-20</div></span></li>
<li><span><a href="#bbib3" id="ref-id-bib3">Verhalen et al., 2015</a></span>
  <span id="sref3"><div><div>B. Verhalen, J.L. Justic[Truncated]</div></div></span></li>
</ol></section></article></body></html>"""
        )

        self.assertEqual(
            [reference.block_id for reference in result.references],
            ["reference-001", "reference-002", "reference-003"],
        )
        first, second, third = result.references
        self.assertEqual(
            first.plain_text,
            (
                "Abend et al., 2007. J.R. Abend, J.A. Low, M.J. Imperiale "
                "Inhibitory effect on viral replication J. Virol., 81 (2007), "
                "pp. 272-279, 10.1000/example DOI: "
                "https://doi.org/10.1000/example"
            ),
        )
        self.assertNotIn("View in Scopus", first.plain_text)
        self.assertNotIn("Google Scholar", first.plain_text)
        self.assertEqual(
            first.plain_text.count("https://doi.org/10.1000/example"), 1
        )
        self.assertEqual(
            first.markdown.count("DOI: https://doi.org/10.1000/example"), 1
        )
        self.assertEqual(
            second.plain_text,
            "2. Numeric authors Numeric-title study Journal Two, 2 (2008), pp. 10-20",
        )
        self.assertIn("[Truncated]", third.plain_text)
        self.assertTrue(third.plain_text.startswith("Verhalen et al., 2015. "))
        for reference in result.references:
            safe_html = block_markup_to_safe_html(
                reference.markdown, kind="reference"
            )
            self.assertEqual(
                plain_text_from_safe_html(safe_html), reference.plain_text
            )
            self.assertTrue(
                rich_text_matches_plain(reference.plain_text, safe_html)
            )

    def test_sciencedirect_reference_requires_matching_id_suffixes(self) -> None:
        item = lxml_html.fromstring(
            """<li><span><a id="ref-id-bib4">4</a></span>
<span id="sref5"><div>Mismatched content.</div>
<div lang="en"><a href="https://scholar.example/">Google Scholar</a></div></span></li>"""
        )

        self.assertIsNone(_sciencedirect_reference(item))

    def test_bibliographic_fallback_excludes_structural_reference_ranges(self) -> None:
        reference_only = self.extract(
            """<!doctype html><html><body><article>
<h1>Reference-only page ranges</h1>
<section><h2>Results</h2><p>Body prose without article-level pagination.</p></section>
<section><h2>References</h2><ol><li>
<span><a id="ref-id-bib1">Prior et al., 2007</a></span>
<span id="sref1"><div>J. Virol., 81 (2007), pp. 272-279</div></span>
</li><li><span><a id="ref-id-b0002">[2]</a></span>
<span id="h0002"><div>Journal Two, 2 (2008), pp. 10-20</div></span></li>
</ol></section></article></body></html>"""
        )
        legitimate_front_matter = self.extract(
            """<!doctype html><html><body>
<div>Pages: 68-75</div><article><h1>Fallback pages</h1>
<section><h2>References</h2><ol><li>Prior study, pp. 272-279.</li></ol></section>
</article></body></html>"""
        )
        explicit_publication = self.extract(
            """<!doctype html><html><body>
<div>Pages: 1-2</div>
<div id="publication">Volume 152, Issue 4, 16 February 2018, Pages 68-75</div>
<article><h1>Explicit pages win</h1>
<section><h2>References</h2><ol><li>Prior study, pp. 272-279.</li></ol></section>
</article></body></html>"""
        )

        self.assertNotIn("pages", reference_only.bibliographic)
        self.assertEqual(legitimate_front_matter.bibliographic["pages"], "68-75")
        self.assertEqual(explicit_publication.bibliographic["pages"], "68-75")

    def test_sciencedirect_cap_table_title_is_scoped_and_lower_precedence(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>ScienceDirect table captions</h1>
<span id="cap-outside"><p>Table 4. Unrelated outside caption.</p></span>
<div id="tbl1"><span><span id="cap0045"><p id="tspara0010">
  <span>Table 1</span>. Antiviral <i>β</i> activity (with variance).</p></span></span>
  <table><thead><tr><th>Agent</th><th>IC50</th></tr></thead>
  <tbody><tr><td>PA1</td><td>100 (±15)</td></tr></tbody></table>
  <ul><li id="note-a">[a] Authored table note.</li></ul></div>
<div id="tbl2"><header>Table 2. Header title.</header>
  <span id="cn0020"><p>Table 2. CN title.</p></span>
  <span id="cap0020"><p>Table 2. CAP title.</p></span>
  <table><tr><th>H</th></tr><tr><td>2</td></tr></table></div>
<div id="tbl3"><span id="cn0030"><p>Table 3. CN title.</p></span>
  <span id="cap0030"><p>Table 3. CAP title.</p></span>
  <table><tr><th>H</th></tr><tr><td>3</td></tr></table></div>
<div id="tbl4"><table><tr><th>H</th></tr><tr><td>4</td></tr></table></div>
</article></body></html>"""
        )

        self.assertEqual(len(result.tables), 4)
        self.assertEqual(
            [table.title_plain for table in result.tables],
            [
                "Table 1. Antiviral β activity (with variance).",
                "Table 2. Header title.",
                "Table 3. CN title.",
                "Table 4",
            ],
        )
        first = result.tables[0]
        self.assertEqual(
            first.title_markdown,
            "Table 1. Antiviral <em>β</em> activity (with variance).",
        )
        self.assertEqual(
            [[cell.text for cell in row] for row in first.parts[0].rows],
            [["Agent", "IC50"], ["PA1", "100 (±15)"]],
        )
        self.assertEqual(first.footnotes_plain, ["[a] Authored table note."])
        safe_html = block_markup_to_safe_html(
            first.title_markdown, kind="table_title"
        )
        self.assertEqual(plain_text_from_safe_html(safe_html), first.title_plain)
        self.assertTrue(rich_text_matches_plain(first.title_plain, safe_html))

    def test_table_definition_list_notes_are_scoped_ordered_and_deduplicated(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Table definition-list notes</h1>
<dl><dt>a</dt><dd>Outside-table affiliation must remain unrelated.</dd></dl>
<div id="tbl1"><span id="cap0005"><p>Table 1. Binding energies.</p></span>
  <table><tr><th>DNA</th><th>K<sub>d,app</sub></th></tr>
    <tr><td>SFTT</td><td>0.0077<sup>b</sup></td></tr></table>
  <ul><li id="note-a">[a] Existing list note.</li></ul>
  <dl>
    <dt>a</dt><dd>Existing list note.</dd>
    <dt>b</dt><dd>Fit <em>error</em> for K<sub>d,app</sub>.</dd>
    <dt>⁎</dt><dd>Authors' star marker.</dd>
    <dt>orphan</dt><dt>c</dt><dd></dd>
  </dl>
  <div><dl><dt>d</dt><dd>Nested unrelated definition.</dd></dl></div>
</div></article></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(
            table.footnotes_plain,
            [
                "[a] Existing list note.",
                "[b] Fit error for K_{d,app}.",
                "[⁎] Authors' star marker.",
            ],
        )
        self.assertEqual(
            table.footnotes_markdown,
            [
                "[a] Existing list note.",
                "[b] Fit <em>error</em> for K<sub>d,app</sub>.",
                "[⁎] Authors' star marker.",
            ],
        )
        for plain, markup in zip(
            table.footnotes_plain, table.footnotes_markdown, strict=True
        ):
            safe_html = block_markup_to_safe_html(markup, kind="table_footnote")
            self.assertEqual(plain_text_from_safe_html(safe_html), plain)
            self.assertTrue(rich_text_matches_plain(plain, safe_html))

        with TemporaryDirectory() as directory:
            extraction_root = Path(directory)
            write_table_derivatives(result.tables, extraction_root)
            payload = json.loads(
                (extraction_root / "tables/main/table_001.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(
            payload["footnotes_typed"],
            [
                {
                    "label": "a",
                    "scope": "table",
                    "text": "Existing list note.",
                },
                {
                    "label": "b",
                    "scope": "table",
                    "text": "Fit error for K_{d,app}.",
                },
            ],
        )

    def test_wiley_fn_table_notes_are_scoped_and_preserve_rich_text(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley table notes</h1>
<ul><li id="fn99"><span>z </span>Outside-table note.</li></ul>
<div id="tbl1"><header>Table 1. Ion abundances.</header>
  <table><thead><tr><th>Ion</th><th>Value<a href="#fn1_22">a</a></th></tr></thead>
    <tbody><tr><td>ds<sup>5−</sup></td><td>N.D.</td></tr></tbody></table>
  <div><ul><li id="fn1" title="Footnote 1"><span>a </span>
    N.D. = not <em>detectable</em>.</li></ul></div>
</div></article></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.footnotes_plain, ["[a] N.D. = not detectable."])
        self.assertEqual(
            table.footnotes_markdown,
            ["[a] N.D. = not <em>detectable</em>."],
        )
        self.assertNotIn("Outside-table note", table.footnotes_plain)

    def test_wiley_figure_controls_citations_and_reference_labels_are_normalized(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley figure structure</h1>
<section><h2>Results</h2><p>See Figure 2.</p>
<figure id="cas123-fig-0002"><figcaption>
  <div><strong>Figure 2</strong><div><a href="#">Open in figure viewer</a>
    <a href="/download">PowerPoint</a></div></div>
  <div>Visible caption.<span><sup><a href="#cas123-bib-0007">7</a></sup></span></div>
</figcaption></figure></section>
<section id="article-references"><h2>References</h2><ol><li>
  <span>7</span><span>Example A. Visible reference.</span>
</li></ol></section>
</article></body></html>"""
        )

        self.assertEqual(
            [(item.figure_id, item.kind, item.label) for item in result.figures],
            [("figure_002", "figure", "Figure 2")],
        )
        caption = result.figures[0]
        self.assertEqual(caption.caption_markdown, "Visible caption.<sup>7</sup>")
        self.assertEqual(caption.caption_plain, "Visible caption.^{7}")
        self.assertNotIn("Open in figure viewer", caption.caption_plain)
        self.assertNotIn("PowerPoint", caption.caption_plain)
        self.assertEqual(result.references[0].plain_text, "7. Example A. Visible reference.")

    def test_wiley_author_popovers_yield_deduplicated_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<div id="journal-banner-text">Synthetic Wiley Journal</div>
<h1>Wiley front matter</h1>
<div><span><a href="/authored-by/Example/Alice" aria-controls="am1"
 id="am1_Ctrl"><span>Alice Example</span></a><div role="region"
 aria-labelledby="am1_Ctrl" id="am1">
 <p>Corresponding Author</p><p>Alice Example</p>
 <ul><li><a href="https://orcid.org/0000-0001-2345-678X">orcid.org/0000-0001-2345-678X</a></li></ul>
 <p>Department A, Example University</p><p><b>Correspondence</b></p>
 <p>Alice Example, Department A, Example University.</p>
 <p>Email: <a href="mailto:alice@example.test">alice@example.test</a></p>
</div></span><span><a href="/authored-by/Example/Bob" aria-controls="am2"
 id="am2_Ctrl"><span>Bob Example</span></a><div role="region"
 aria-labelledby="am2_Ctrl" id="am2"><p>Bob Example</p>
 <p>Department B, Example Institute</p></div></span></div>
<div id="sb-1"><span><a href="/authored-by/Example/Alice" aria-controls="a1"
 id="a1_Ctrl"><span>Alice Example</span></a><div role="region"
 aria-labelledby="a1_Ctrl" id="a1">
 <p>Corresponding Author</p><p>Alice Example</p>
 <ul><li><a href="https://orcid.org/0000-0001-2345-678X">orcid.org/0000-0001-2345-678X</a></li></ul>
 <p>Department A, Example University</p><p><b>Correspondence</b></p>
 <p>Alice Example, Department A, Example University.</p>
 <p>Email: <a href="mailto:alice@example.test">alice@example.test</a></p>
</div></span></div>
<section><h2>Results</h2><p>Body.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation (Alice Example): Department A, Example University",
                "Affiliation (Bob Example): Department B, Example Institute",
                (
                    "Correspondence: Alice Example, Department A, Example "
                    "University. Email: alice@example.test"
                ),
                "ORCID (Alice Example): https://orcid.org/0000-0001-2345-678X",
            ],
        )
        self.assertEqual(
            len({block.block_id for block in result.front_matter}),
            len(result.front_matter),
        )
        self.assertTrue(
            all("Search for more papers" not in block.plain_text for block in result.front_matter)
        )

    def test_wiley_legacy_popover_raw_correspondence_is_included_once(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Legacy Wiley correspondence</h1>
<div><span><a href="/authored-by/Example/Alice" aria-controls="am1"
 id="am1_Ctrl"><span>Alice Example</span></a><div role="region"
 aria-labelledby="am1_Ctrl" id="am1">
 <p>Corresponding Author</p><p>Alice Example</p>
 <p>Department A, Example University</p>
 To whom correspondence should be addressed. E-mail:
 <a href="mailto:alice@example.test"><span>alice@example.test</span></a>
 <a href="/authored-by/Example/Alice">Search for more papers by this author</a>
</div></span></div>
<div><span><a href="/authored-by/Example/Alice" aria-controls="a1"
 id="a1_Ctrl"><span>Alice Example</span></a><div role="region"
 aria-labelledby="a1_Ctrl" id="a1">
 <p>Corresponding Author</p><p>Alice Example</p>
 <p>Department A, Example University</p>
 To whom correspondence should be addressed. E-mail:
 <a href="mailto:alice@example.test"><span>alice@example.test</span></a>
 <a href="/authored-by/Example/Alice">Search for more papers by this author</a>
</div></span></div>
<section><h2>Results</h2><p>Body.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation (Alice Example): Department A, Example University",
                (
                    "Correspondence: To whom correspondence should be addressed. "
                    "E-mail: alice@example.test"
                ),
            ],
        )
        self.assertTrue(
            all("Search for more papers" not in block.plain_text for block in result.front_matter)
        )

    def test_wiley_abbreviations_table_and_unheaded_intro_are_preserved(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Legacy Wiley abbreviations</h1>
<section><h3>Abbreviations:</h3><div><table><tbody>
<tr><th><li>GI<sub>50</sub></li></th><td><li>50% growth inhibition concentration</li></td></tr>
<tr><th><li>Py</li></th><td><li>pyrrole</li></td></tr>
</tbody></table></div>
<section id="ss100"><p>Unheaded introductory prose.</p></section>
<section id="ss1"><h2>Materials and Methods</h2><p>Method prose.</p></section>
</section></article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abbreviations:", "Introduction", "Materials and Methods"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            [
                "- GI_{50}: 50% growth inhibition concentration\n- Py: pyrrole"
            ],
        )
        self.assertEqual(result.sections[0].blocks[0].kind, "list")
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Unheaded introductory prose."],
        )
        self.assertEqual(result.tables, [])

    def test_wiley_simple_header_yields_bibliographic_and_author_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><main><header>
<p>Synthetic Wiley Journal; Volume 20, Issue 5; pp. 1310-1317</p>
<h1>Wiley simple header</h1>
<section id="authors"><h2>Authors</h2><ul>
<li><strong>Alice Example</strong><p>Department A, Example University</p></li>
<li><strong>Dr. Bob Example</strong><span> — Corresponding Author</span>
<p>Department B, Example Institute</p><p>bob@example.test</p></li>
</ul></section>
<p>First published: 30 December 2013; https://doi.org/10.0000/example</p>
</header><article><section><h2>Results</h2><p>Body.</p></section></article>
</main></body></html>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "volume": "20",
                "issue": "5",
                "pages": "1310-1317",
                "first_published": "30 December 2013",
            },
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation (Alice Example): Department A, Example University",
                "Affiliation (Dr. Bob Example): Department B, Example Institute",
                "Correspondence: Dr. Bob Example: bob@example.test",
            ],
        )

    def test_wiley_information_panel_and_top_note_are_front_matter(self) -> None:
        result = self.extract(
            """<!doctype html><html><body>
<div id="pbc28789-note-0001"><div>Tomoo Daifu and Masamitsu Mikami contributed equally to this work.</div></div>
<article><h1>Wiley information panel</h1>
<section><h2>Results</h2><p>Body.</p></section></article>
<div id="pane-pcw-details"><section>
  <section><h3>Details</h3><p>© 2020 Wiley Periodicals LLC</p><p></p>
    <ul><li><a role="button">Check for updates</a></li></ul></section>
  <section><h3>Research funding</h3><ul>
    <li>Japan Society for the Promotion of Science. Grant Number: 17H03597</li>
    <li>Japan Agency for Medical Research and Development. Grant Number: BINDS/19am0101101j0003</li>
  </ul></section>
  <section><h3>Keywords</h3><div><ul>
    <li><a href="/action/doSearch?text1=polyamide">polyamide</a></li>
    <li><a href="/action/doSearch?text1=RUNX1">RUNX1</a></li>
  </ul></div></section>
  <section><h3>Publication History</h3><ul>
    <li><label>Issue Online: </label>22 December 2020</li>
    <li><label>Manuscript received: </label>17 December 2019</li>
  </ul></section>
</section></div></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Author note: Tomoo Daifu and Masamitsu Mikami contributed equally to this work.",
                "Copyright: © 2020 Wiley Periodicals LLC",
                (
                    "Research funding: Japan Society for the Promotion of Science. "
                    "Grant Number: 17H03597; Japan Agency for Medical Research and "
                    "Development. Grant Number: BINDS/19am0101101j0003"
                ),
                "Keywords: polyamide; RUNX1",
                (
                    "Publication history: Issue Online: 22 December 2020; "
                    "Manuscript received: 17 December 2019"
                ),
            ],
        )
        for block in result.front_matter:
            safe_html = block_markup_to_safe_html(block.markdown, kind=block.kind)
            self.assertEqual(
                plain_text_from_safe_html(safe_html),
                block.plain_text,
            )
            self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_wiley_front_matter_excludes_controls_body_notes_and_duplicates(self) -> None:
        result = self.extract(
            """<!doctype html><html><body>
<a id="link_chem202002166-note-1001">title control</a>
<div id="empty-note-1001"></div>
<div id="abc-note-1002"><div>Equal contribution note.</div></div>
<div id="def-note-1003"><div>Equal contribution note.</div></div>
<div id="ghi-note-1004-controller"><div>controller content</div></div>
<article><h1>Wiley front-matter exclusions</h1><section><h2>Results</h2>
<p>Body.</p><ul><li id="note-p-61">Body note.</li></ul>
<table><tbody><tr><td><a id="tbl-note-0001_1-controller">table control</a></td></tr></tbody></table>
</section></article>
<div id="pane-pcw-details"><section>
  <section><h3>Details</h3><p>© 2020 Wiley Periodicals LLC</p>
    <ul><li><a role="button">Check for updates</a></li></ul></section>
  <section><h3>Related</h3><ul><li>Recommended article.</li></ul></section>
</section></div></body></html>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Author note: Equal contribution note.",
                "Copyright: © 2020 Wiley Periodicals LLC",
            ],
        )
        combined = "\n".join(block.plain_text for block in result.front_matter)
        for excluded in (
            "title control",
            "controller content",
            "Body note",
            "table control",
            "Check for updates",
            "Recommended article",
        ):
            self.assertNotIn(excluded, combined)
        for block in result.front_matter:
            safe_html = block_markup_to_safe_html(block.markdown, kind=block.kind)
            self.assertEqual(
                plain_text_from_safe_html(safe_html),
                block.plain_text,
            )
            self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_graphical_abstract_prose_remains_machine_readable(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Graphical abstract prose</h1>
<section id="abstract"><h2>Abstract</h2><p>Main summary.</p></section>
<section id="abstract-graphical"><h2>Graphical Abstract</h2>
<p>Distinct authored graphical summary.</p>
<figure id="graphical-abstract"><img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" alt="Graphical abstract"></figure>
</section>
<section><h2>Results</h2><p>Body.</p></section>
</article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Graphical Abstract", "Results"],
        )
        self.assertEqual(
            result.sections[1].blocks[0].plain_text,
            "Distinct authored graphical summary.",
        )
        self.assertEqual(
            [(figure.figure_id, figure.kind) for figure in result.figures],
            [("graphical_abstract", "graphical_abstract")],
        )

    def test_unheaded_intro_after_graphical_abstract_gets_own_section(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Graphical abstract followed by introduction</h1>
<section id="abstract-graphical"><h2>Graphical Abstract</h2>
<p>Distinct authored graphical summary.</p>
<figure><img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" alt="Graphical abstract"></figure>
</section>
<section><section id="intro"><p>Unheaded introductory prose.</p></section>
<section id="results"><h2>Results</h2><p>Result prose.</p></section></section>
</article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Graphical Abstract", "Introduction", "Results"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Distinct authored graphical summary."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Unheaded introductory prose."],
        )

    def test_emphasis_preserves_unicode_boundary_whitespace(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley significance spacing</h1>
<section><h2>Results</h2><p>See Figure 1.</p>
<figure id="fig1"><figcaption>
  <div><strong>Figure 1</strong></div>
  <div>Significance: *<i>P&#x2003;&lt;&#x2003;</i>0.05.</div>
</figcaption></figure></section>
</article></body></html>"""
        )

        caption = result.figures[0]
        self.assertEqual(
            caption.caption_markdown,
            "Significance: \\*<em>P &lt;</em> 0.05.",
        )
        self.assertEqual(caption.caption_plain, "Significance: *P < 0.05.")
        safe_html = block_markup_to_safe_html(
            caption.caption_markdown, kind="figure_caption"
        )
        self.assertTrue(rich_text_matches_plain(caption.caption_plain, safe_html))

    def test_numbered_image_alt_recovers_figure_and_scheme_captions(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>AACR image alt captions</h1><section><h2>Results</h2>
<figure id="figure-2"><img alt="Figure 2. Effect at 10 μmol/L on 5′-TTGGT-3′."></figure>
<figure id="scheme-3"><img alt="Scheme 3. Reagents A &amp; B &lt; C."></figure>
</section></article></body></html>"""
        )

        self.assertEqual(
            [(item.figure_id, item.kind, item.label) for item in result.figures],
            [
                ("figure_002", "figure", "Figure 2"),
                ("scheme_003", "scheme", "Scheme 3"),
            ],
        )
        self.assertEqual(
            [item.caption_plain for item in result.figures],
            ["Effect at 10 μmol/L on 5′-TTGGT-3′.", "Reagents A & B < C."],
        )
        self.assertEqual(
            [item.caption_markdown for item in result.figures],
            [
                "Effect at 10 μmol/L on 5′-TTGGT-3′.",
                "Reagents A &amp; B &lt; C.",
            ],
        )
        for caption in result.figures:
            safe_html = block_markup_to_safe_html(
                caption.caption_markdown, kind="figure_caption"
            )
            self.assertTrue(
                rich_text_matches_plain(caption.caption_plain, safe_html)
            )
            self.assertEqual(
                plain_text_from_safe_html(safe_html), caption.caption_plain
            )

    def test_numbered_image_alt_rejects_empty_generic_and_punctuation_bodies(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Generic image alts</h1><section><h2>Results</h2>
<figure id="figure-1"><img alt=""></figure>
<figure id="figure-2"><img alt="Figure 2. Refer to the image caption for details."></figure>
<figure id="figure-3"><img alt="Figure 3. Description unavailable."></figure>
<figure id="figure-4"><img alt="Figure 4. The caption contains a description of this image."></figure>
<figure id="figure-5"><img alt="Figure 5. ..."></figure>
<figure id="figure-6"><img alt="thumbnail"></figure>
</section></article></body></html>"""
        )

        self.assertEqual(len(result.figures), 6)
        self.assertTrue(
            all(not item.caption_plain and not item.caption_markdown for item in result.figures)
        )

    def test_semantic_figcaption_precedes_conflicting_numbered_image_alt(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Caption precedence</h1><section><h2>Results</h2>
<figure id="figure-7"><img alt="Figure 7. Conflicting alt caption."><figcaption>
  <div><strong>Figure 7</strong></div><div>Authored β caption.</div>
</figcaption></figure>
</section></article></body></html>"""
        )

        caption = result.figures[0]
        self.assertEqual(caption.caption_plain, "Authored β caption.")
        self.assertEqual(caption.caption_markdown, "Authored β caption.")
        safe_html = block_markup_to_safe_html(
            caption.caption_markdown, kind="figure_caption"
        )
        self.assertTrue(rich_text_matches_plain(caption.caption_plain, safe_html))
        self.assertEqual(plain_text_from_safe_html(safe_html), caption.caption_plain)

    def test_leading_decimals_keep_authored_space_during_punctuation_cleanup(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Wiley leading decimals</h1>
<section><h2>Statistical analysis</h2>
<p>Data are expressed as mean&#xa0;±&#xa0;SD. Differences in mean values between groups were analyzed by the Student's <i>t</i>-test using JMP 13 (SAS Institute Inc., Cary, NC). <i>P</i>-values&#xa0;&lt;&#xa0;.01 or .05 were considered to be significant.</p>
</section></article></body></html>"""
        )

        block = result.sections[0].blocks[0]
        expected_plain = (
            "Data are expressed as mean ± SD. Differences in mean values between "
            "groups were analyzed by the Student's t-test using JMP 13 (SAS "
            "Institute Inc., Cary, NC). P-values < .01 or .05 were considered "
            "to be significant."
        )
        self.assertEqual(block.plain_text, expected_plain)
        self.assertEqual(
            block.markdown,
            (
                "Data are expressed as mean ± SD. Differences in mean values "
                "between groups were analyzed by the Student's <em>t</em>-test "
                "using JMP 13 (SAS Institute Inc., Cary, NC). <em>P</em>-values "
                "&lt; .01 or .05 were considered to be significant."
            ),
        )
        safe_html = block_markup_to_safe_html(block.markdown, kind="paragraph")
        self.assertEqual(plain_text_from_safe_html(safe_html), expected_plain)
        self.assertTrue(rich_text_matches_plain(block.plain_text, safe_html))

    def test_unheaded_structural_introduction_is_not_folded_into_abstract(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Implicit introduction</h1>
<section id="abstract"><h2>Abstract</h2><p>Summary only.</p></section>
<section id="body">
  <section id="opening"><p>Opening context.</p><p>Study rationale.</p></section>
  <section id="methods"><h2>Methods</h2><p>Experimental details.</p></section>
</section>
</article></body></html>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Introduction", "Methods"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Summary only."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Opening context.", "Study rationale."],
        )
        self.assertEqual(
            result.sections[1].source_locator,
            "/html/body/article/section[2]/section[1]",
        )

    def test_unequal_diagram_rows_preserve_intentional_blank_cell(self) -> None:
        result = self.extract(
            """<!doctype html>
<html><body><article>
  <h1>Synthetic table study</h1>
  <section><h2>Data</h2><p>Results are tabulated.</p></section>
  <div id="tbl7">
    <header><strong>Table 7.</strong> Synthetic measurements</header>
    <table>
      <thead><tr><th>Entry</th><th>Species</th><th>Selectivity</th></tr></thead>
      <tbody>
        <tr><td><a href="#for001">-1</a></td><td>X<sub>2</sub></td><td></td></tr>
        <tr><td>7</td><td>product β</td></tr>
      </tbody>
    </table>
  </div>
</article></body></html>"""
        )

        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].source_kind, "html")
        rows = result.tables[0].parts[0].rows
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [cell.text for cell in rows[1]], ["7", "X_{2} product β", ""]
        )
        self.assertEqual(rows[1][1].markdown, "X<sub>2</sub><br>product β")
        self.assertEqual(rows[1][2].markdown, "")
        self.assertEqual(rows[1][2].as_dict()["html"], "")

    def test_standalone_bold_paragraph_becomes_subsection_heading(self) -> None:
        result = self.extract(
            """<!doctype html><html><body><article>
<h1>Synthetic hierarchy</h1>
<section><h2>Methods</h2><p><b>Preparation details</b></p>
<p><b>Compound A</b>: Synthetic procedure.</p></section>
</article></body></html>"""
        )

        blocks = result.sections[0].blocks
        self.assertEqual(blocks[0].kind, "subsection_heading")
        self.assertEqual(blocks[0].markdown, "### Preparation details")
        self.assertEqual(blocks[1].kind, "paragraph")


class ReferenceEntryOverrideTests(unittest.TestCase):
    @staticmethod
    def source() -> SourceFile:
        return SourceFile(
            role="main_pdf",
            path=Path("main.pdf"),
            relative_path="papers (private)/00001/pdf/main.pdf",
            size=3,
            sha256="0" * 64,
            detected_format="application/pdf",
        )

    @staticmethod
    def block(number: int, value: str) -> ContentBlock:
        return ContentBlock(
            block_id=f"reference-{number:03d}",
            kind="reference",
            markdown=f"{number}. {value}",
            plain_text=f"{number}. {value}",
            source_path="papers (private)/00001/html/main.html",
            source_locator=f"#ref-{number}",
        )

    def test_exact_source_entries_replace_and_contiguously_extend(self) -> None:
        source = self.source()
        article = SimpleNamespace(
            references=[self.block(1, "Complete."), self.block(2, "Truncated")]
        )
        _apply_reference_entries(
            article,
            [
                {
                    "number": 2,
                    "value": "Recovered second reference.",
                    "source_path": source.relative_path,
                    "source_locator": "physical page 1, reference 2",
                },
                {
                    "number": 3,
                    "value": "Recovered third reference.",
                    "source_path": source.relative_path,
                    "source_locator": "physical page 2, reference 3",
                },
            ],
            [source],
        )

        self.assertEqual(
            [block.plain_text for block in article.references],
            [
                "1. Complete.",
                "2. Recovered second reference.",
                "3. Recovered third reference.",
            ],
        )
        self.assertEqual(article.references[2].source_path, source.relative_path)

    def test_reference_entry_gap_fails_closed(self) -> None:
        source = self.source()
        article = SimpleNamespace(references=[self.block(1, "Complete.")])
        with self.assertRaisesRegex(ExtractionError, "bibliography gap"):
            _apply_reference_entries(
                article,
                [
                    {
                        "number": 3,
                        "value": "Gap.",
                        "source_path": source.relative_path,
                        "source_locator": "physical page 1, reference 3",
                    }
                ],
                [source],
            )


class FrontMatterOverrideTests(unittest.TestCase):
    def test_matching_label_replaces_partial_publisher_block(self) -> None:
        source = SourceFile(
            role="main_pdf",
            path=Path("main.pdf"),
            relative_path="papers (private)/00001/pdf/main.pdf",
            size=3,
            sha256="0" * 64,
            detected_format="application/pdf",
        )
        article = SimpleNamespace(
            front_matter=[
                ContentBlock(
                    block_id="front-matter-html-001",
                    kind="front_matter",
                    markdown="**Affiliation assignments:** Alice Example (a)",
                    plain_text="Affiliation assignments: Alice Example (a)",
                    source_path="papers (private)/00001/html/main.html",
                    source_locator="//*[@id='author-group']",
                )
            ]
        )

        _apply_front_matter_overrides(
            article,
            [
                {
                    "label": "Affiliation assignments",
                    "value": "Alice Example (a); Bob Example (b)",
                    "source_path": source.relative_path,
                    "source_locator": "physical page 1, author line",
                }
            ],
            [source],
        )

        self.assertEqual(len(article.front_matter), 1)
        self.assertEqual(
            article.front_matter[0].plain_text,
            "Affiliation assignments: Alice Example (a); Bob Example (b)",
        )
        self.assertEqual(article.front_matter[0].source_path, source.relative_path)


class PathSafetyAndDeterminismTests(unittest.TestCase):
    def test_record_ids_and_parent_traversal_are_rejected(self) -> None:
        self.assertEqual(validate_record_id("01234"), "01234")
        for unsafe_id in ("1234", "123456", "../12", "12/34", "abcde"):
            with self.subTest(record_id=unsafe_id):
                with self.assertRaises(ValueError):
                    validate_record_id(unsafe_id)

        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            root = temporary / "approved"
            root.mkdir()
            inside = root / "inside.txt"
            outside = temporary / "outside.txt"
            inside.write_text("inside", encoding="utf-8")
            outside.write_text("outside", encoding="utf-8")

            self.assertEqual(ensure_within(inside, root), inside.resolve())
            with self.assertRaises(UnsafePathError):
                ensure_within(root / ".." / "outside.txt", root)

    def test_canonical_json_is_stable_unicode_and_key_sorted(self) -> None:
        first = {"z": [3, 2, 1], "a": "β"}
        second = {"a": "β", "z": [3, 2, 1]}

        rendered = canonical_json(first)

        self.assertEqual(rendered, canonical_json(second))
        self.assertEqual(rendered, '{\n  "a": "β",\n  "z": [\n    3,\n    2,\n    1\n  ]\n}\n')

    def test_table_derivatives_include_typed_scientific_records_without_csv(self) -> None:
        table = TableItem(
            table_id="table_001",
            source_id="tbl1",
            label="Table 1",
            title_markdown="K<sub>a</sub>",
            title_plain="K_{a} [M^{−1}] measurements",
            parts=[
                TablePart(
                    part_id="part-01",
                    rows=[
                        [
                            TableCell("Polyamide on pTEST", "Polyamide on pTEST", True),
                            TableCell("5′-aTGGACAt-3′", "5′-aTGGACAt-3′", True),
                        ],
                        [
                            TableCell("2 b", "2 b", False),
                            TableCell(
                                "≤1×10^{8} [≥190]^{[e]}",
                                "<strong>≤1×10<sup>8</sup></strong>",
                                False,
                            ),
                        ],
                    ],
                )
            ],
            footnotes_markdown=["[a] Synthetic scope. [e] Alternate match."],
            footnotes_plain=["[a] Synthetic scope. [e] Alternate match."],
            source_path="html/main.html",
            source_locator="//table[1]",
            source_kind="html",
            structure_assets={"2b": "tables/main/table_001_cells/structure_2b.png"},
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            structure_image = (
                root
                / "tables"
                / "main"
                / "table_001_cells"
                / "structure_2b.png"
            )
            structure_image.parent.mkdir(parents=True)
            structure_image.write_bytes(b"synthetic table-cell image")
            write_table_derivatives([table], root)
            payload = json.loads((root / table.json_path).read_text(encoding="utf-8"))
            record = payload["machine_records"][0]

            self.assertEqual(record["compound_id"], "2b")
            self.assertEqual(
                record["structure_asset"],
                "tables/main/table_001_cells/structure_2b.png",
            )
            self.assertEqual(record["association_constant"]["relation"], "<=")
            self.assertEqual(record["association_constant"]["exponent"], 8)
            self.assertEqual(record["specificity"], {"relation": ">=", "value": 190.0})
            self.assertTrue(record["match_site"])
            self.assertEqual(record["footnotes"], ["e"])
            self.assertEqual(
                payload["footnotes_typed"],
                [
                    {"label": "a", "scope": "table", "text": "Synthetic scope."},
                    {"label": "e", "scope": "table", "text": "Alternate match."},
                ],
            )
            self.assertEqual(payload["source_kind"], "html")
            self.assertNotIn("source", payload)
            self.assertEqual(list(root.rglob("*.csv")), [])
            self.assertEqual(list(root.rglob("table_001.png")), [])
            self.assertTrue(structure_image.is_file())

    def test_table_schema_marker_is_the_only_change_to_faithful_json_content(self) -> None:
        table = TableItem(
            table_id="table_009",
            source_id="tbl9",
            label="Table 9",
            title_markdown="β scope and selectivity",
            title_plain="β scope and selectivity",
            parts=[
                TablePart(
                    part_id="tbl9-part-01",
                    rows=[
                        [
                            TableCell(
                                "Condition",
                                "<strong>Condition</strong>",
                                True,
                                rowspan=2,
                            ),
                            TableCell("Results", "Results", True, colspan=2),
                        ],
                        [
                            TableCell("Yield (%)", "Yield (%)", True),
                            TableCell("ee (%)", "<em>ee</em> (%)", True),
                        ],
                        [
                            TableCell("A", "A", False),
                            TableCell(
                                "≤1×10^{−3}",
                                "≤1×10<sup>−3</sup>",
                                False,
                            ),
                        ],
                        [TableCell("Not reported", "<span>Not reported</span>", False)],
                    ],
                ),
                TablePart(
                    part_id="tbl9-part-02",
                    rows=[
                        [TableCell("Continued", "<u>Continued</u>", True)],
                        [TableCell("β-form", "β-form", False)],
                    ],
                ),
            ],
            footnotes_markdown=[
                "[a] Values are means; *n* = 3.",
                "[e] As reported by the authors—unchanged.",
            ],
            footnotes_plain=[
                "[a] Values are means; n = 3.",
                "[e] As reported by the authors—unchanged.",
            ],
            source_path="html/main.html",
            source_locator="//table[9]",
            source_kind="html",
            structure_assets={
                "β-form": "tables/main/table_009_cells/structure_beta.png"
            },
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_table_derivatives([table], root)
            payload = json.loads((root / str(table.json_path)).read_text("utf-8"))

        # This is the complete pre-schema table payload. Removing the new
        # path-base declaration must leave it byte-for-data identical: the
        # schema describes this representation and does not normalize it.
        expected_pre_schema_payload = {
            "schema_version": "1.0",
            "table_id": "table_009",
            "label": "Table 9",
            "title": "β scope and selectivity",
            "parts": [
                {
                    "part_id": "tbl9-part-01",
                    "rows": [
                        [
                            {
                                "text": "Condition",
                                "html": "<strong>Condition</strong>",
                                "header": True,
                                "rowspan": 2,
                                "colspan": 1,
                            },
                            {
                                "text": "Results",
                                "html": "Results",
                                "header": True,
                                "rowspan": 1,
                                "colspan": 2,
                            },
                        ],
                        [
                            {
                                "text": "Yield (%)",
                                "html": "Yield (%)",
                                "header": True,
                                "rowspan": 1,
                                "colspan": 1,
                            },
                            {
                                "text": "ee (%)",
                                "html": "<em>ee</em> (%)",
                                "header": True,
                                "rowspan": 1,
                                "colspan": 1,
                            },
                        ],
                        [
                            {
                                "text": "A",
                                "html": "A",
                                "header": False,
                                "rowspan": 1,
                                "colspan": 1,
                            },
                            {
                                "text": "≤1×10^{−3}",
                                "html": "≤1×10<sup>−3</sup>",
                                "header": False,
                                "rowspan": 1,
                                "colspan": 1,
                            },
                        ],
                        [
                            {
                                "text": "Not reported",
                                "html": "<span>Not reported</span>",
                                "header": False,
                                "rowspan": 1,
                                "colspan": 1,
                            }
                        ],
                    ],
                },
                {
                    "part_id": "tbl9-part-02",
                    "rows": [
                        [
                            {
                                "text": "Continued",
                                "html": "<u>Continued</u>",
                                "header": True,
                                "rowspan": 1,
                                "colspan": 1,
                            }
                        ],
                        [
                            {
                                "text": "β-form",
                                "html": "β-form",
                                "header": False,
                                "rowspan": 1,
                                "colspan": 1,
                            }
                        ],
                    ],
                },
            ],
            "footnotes": [
                "[a] Values are means; n = 3.",
                "[e] As reported by the authors—unchanged.",
            ],
            "footnotes_typed": [
                {
                    "label": "a",
                    "scope": "table",
                    "text": "Values are means; n = 3.",
                },
                {
                    "label": "e",
                    "scope": "table",
                    "text": "As reported by the authors—unchanged.",
                },
            ],
            "structure_assets": {
                "β-form": "tables/main/table_009_cells/structure_beta.png"
            },
            "machine_records": [],
            "source_kind": "html",
        }
        self.assertEqual(payload["asset_path_base"], "extraction_root")
        self.assertEqual(
            {key: value for key, value in payload.items() if key != "asset_path_base"},
            expected_pre_schema_payload,
        )
        self.assertEqual(
            [len(row) for row in payload["parts"][0]["rows"]], [2, 2, 2, 1]
        )
        self.assertEqual(len(payload["parts"]), 2)
        validate_table_payload(payload)

    def test_every_table_emits_json_even_when_rows_are_not_rectangular(self) -> None:
        tables = [
            TableItem(
                table_id="table_001",
                source_id="tbl1",
                label="Table 1",
                title_markdown="Complete table",
                title_plain="Complete table",
                parts=[
                    TablePart(
                        part_id="tbl1-part-01",
                        rows=[
                            [
                                TableCell("Name", "Name", True),
                                TableCell("Value", "Value", True),
                            ],
                            [
                                TableCell("alpha", "alpha", False),
                                TableCell("1", "1", False),
                            ],
                        ],
                    )
                ],
                footnotes_markdown=[],
                footnotes_plain=[],
                source_path="html/main.html",
                source_locator="//table[1]",
                source_kind="html",
            ),
            TableItem(
                table_id="table_002",
                source_id="tbl2",
                label="Table 2",
                title_markdown="Ragged table",
                title_plain="Ragged table",
                parts=[
                    TablePart(
                        part_id="tbl2-part-01",
                        rows=[
                            [
                                TableCell("Name", "Name", True),
                                TableCell("Value", "Value", True),
                            ],
                            [TableCell("beta", "beta", False)],
                        ],
                    )
                ],
                footnotes_markdown=[],
                footnotes_plain=[],
                source_path="html/main.html",
                source_locator="//table[2]",
                source_kind="html",
            ),
        ]

        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_table_derivatives(tables, root)

            for table in tables:
                with self.subTest(table_id=table.table_id):
                    json_path = root / "tables" / "main" / f"{table.table_id}.json"
                    self.assertTrue(json_path.is_file())
                    payload = json.loads(json_path.read_text(encoding="utf-8"))
                    self.assertEqual(payload["table_id"], table.table_id)
                    self.assertEqual(payload["source_kind"], "html")
                    self.assertNotIn("source", payload)
            self.assertEqual(list(root.rglob("*.csv")), [])
            self.assertFalse(
                any(
                    path.is_dir() and path.name.endswith("_cells")
                    for path in root.rglob("*")
                )
            )

    def test_table_image_requirements_apply_only_to_pdf_and_image_sources(self) -> None:
        def make_table(number: int, source_kind: str) -> TableItem:
            return TableItem(
                table_id=f"table_{number:03d}",
                source_id=f"tbl{number}",
                label=f"Table {number}",
                title_markdown=f"Table {number}",
                title_plain=f"Table {number}",
                parts=[],
                footnotes_markdown=[],
                footnotes_plain=[],
                source_path=f"{source_kind}/table-{number}",
                source_locator=f"table {number}",
                source_kind=source_kind,
            )

        html_table = make_table(1, "html")
        pdf_table = make_table(2, "pdf")
        image_table = make_table(3, "image")
        article = SimpleNamespace(
            figures=[],
            tables=[html_table, pdf_table, image_table],
            warnings=[],
        )

        _attach_assets(article, [], [])
        _record_missing_asset_warnings(article, [])

        missing_table_warnings = [
            warning
            for warning in article.warnings
            if warning["code"] == "main_table_source_image_missing"
        ]
        self.assertEqual(
            [warning["source_path"] for warning in missing_table_warnings],
            [pdf_table.source_path, image_table.source_path],
        )
        self.assertNotIn(
            html_table.source_path,
            {warning["source_path"] for warning in missing_table_warnings},
        )

        article.warnings.clear()
        _attach_assets(
            article,
            [],
            [
                {
                    "asset_id": pdf_table.table_id,
                    "output_path": "tables/main/table_002.png",
                },
                {
                    "asset_id": image_table.table_id,
                    "output_path": "tables/main/table_003.png",
                },
            ],
        )
        _record_missing_asset_warnings(article, [])

        self.assertIsNone(html_table.image_path)
        self.assertEqual(pdf_table.image_path, "tables/main/table_002.png")
        self.assertEqual(image_table.image_path, "tables/main/table_003.png")
        self.assertEqual(article.warnings, [])

        with TemporaryDirectory() as directory:
            root = Path(directory)
            for table in (pdf_table, image_table):
                image_path = root / str(table.image_path)
                image_path.parent.mkdir(parents=True, exist_ok=True)
                image_path.write_bytes(b"synthetic full-table image")

            write_table_derivatives(article.tables, root)

            self.assertFalse((root / "tables/main/table_001.png").exists())
            for table in article.tables:
                payload = json.loads(
                    (root / f"tables/main/{table.table_id}.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(payload["source_kind"], table.source_kind)
                self.assertNotIn("source", payload)
                if table.requires_source_image:
                    self.assertTrue((root / str(table.image_path)).is_file())
            self.assertEqual(list(root.rglob("*.csv")), [])


class TableSchemaContractTests(unittest.TestCase):
    @staticmethod
    def valid_payload(source_kind: str = "html") -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "1.0",
            "asset_path_base": "extraction_root",
            "table_id": "table_001",
            "label": "Table 1",
            "title": "Faithful synthetic table",
            "parts": [
                {
                    "part_id": "part-01",
                    "rows": [
                        [
                            {
                                "text": "α",
                                "html": "<em>α</em><sup>2</sup>",
                                "header": True,
                                "rowspan": 2,
                                "colspan": 3,
                            }
                        ],
                        [],
                    ],
                },
                {"part_id": "part-02", "rows": []},
            ],
            "footnotes": ["[e] Authors' wording; retained verbatim."],
            "footnotes_typed": [
                {
                    "label": "e",
                    "scope": "table",
                    "text": "Authors' wording; retained verbatim.",
                }
            ],
            "structure_assets": {},
            "machine_records": [],
            "source_kind": source_kind,
        }
        if source_kind in {"pdf", "image"}:
            payload["source_image"] = "tables/main/table_001.png"
        return payload

    def test_schema_accepts_faithful_multipart_spans_html_and_ragged_rows(self) -> None:
        payload = self.valid_payload()
        before = deepcopy(payload)

        validate_table_payload(payload)

        self.assertEqual(payload, before)
        self.assertEqual(payload["parts"][0]["rows"][1], [])  # type: ignore[index]
        self.assertEqual(
            payload["parts"][0]["rows"][0][0]["html"],  # type: ignore[index]
            "<em>α</em><sup>2</sup>",
        )
        self.assertEqual(
            payload["footnotes"], ["[e] Authors' wording; retained verbatim."]
        )

    def test_schema_rejects_invalid_container_without_rewriting_it(self) -> None:
        cases: dict[str, object] = {}

        missing_title = self.valid_payload()
        del missing_title["title"]
        cases["missing required field"] = missing_title

        wrong_version = self.valid_payload()
        wrong_version["schema_version"] = "2.0"
        cases["unsupported schema version"] = wrong_version

        wrong_path_base = self.valid_payload()
        wrong_path_base["asset_path_base"] = "table_directory"
        cases["ambiguous asset path base"] = wrong_path_base

        invalid_span = self.valid_payload()
        invalid_span["parts"][0]["rows"][0][0]["rowspan"] = 0  # type: ignore[index]
        cases["invalid cell span"] = invalid_span

        unexpected_field = self.valid_payload()
        unexpected_field["publisher_layout"] = "normalized"
        cases["undefined container field"] = unexpected_field

        cases["non-object root"] = []

        for description, payload in cases.items():
            with self.subTest(description=description):
                before = deepcopy(payload)
                with self.assertRaises(TableSchemaError) as raised:
                    validate_table_payload(payload)
                self.assertTrue(raised.exception.violations)
                self.assertEqual(payload, before)

    def test_source_image_is_required_only_for_pdf_or_image_sources(self) -> None:
        validate_table_payload(self.valid_payload("html"))

        html_with_image = self.valid_payload("html")
        html_with_image["source_image"] = "tables/main/table_001.png"
        with self.assertRaises(TableSchemaError):
            validate_table_payload(html_with_image)

        for source_kind in ("pdf", "image"):
            with self.subTest(source_kind=source_kind):
                payload = self.valid_payload(source_kind)
                validate_table_payload(payload)
                del payload["source_image"]
                with self.assertRaises(TableSchemaError):
                    validate_table_payload(payload)


class IndependentValidatorTests(unittest.TestCase):
    def write_json(self, path: Path, value: object) -> None:
        path.write_text(canonical_json(value), encoding="utf-8")

    def test_html_table_requires_structured_json_link_but_not_source_image(self) -> None:
        record_text = """# Synthetic article

## Tables

### Table 1

Assets: [structured data](tables/main/table_001.json)

<table><tr><th>Value</th></tr><tr><td>1</td></tr></table>
"""

        self.assertEqual(_required_asset_findings(record_text), [])

        findings_without_json = _required_asset_findings(
            record_text.replace(
                "Assets: [structured data](tables/main/table_001.json)\n\n", ""
            )
        )
        self.assertEqual(
            [finding.code for finding in findings_without_json],
            ["missing_consolidated_asset"],
        )
        self.assertIn("structured JSON", findings_without_json[0].message)

    def test_table_csv_is_an_unexpected_derivative(self) -> None:
        with TemporaryDirectory() as directory:
            extraction = Path(directory)
            table_root = extraction / "tables" / "main"
            table_root.mkdir(parents=True)
            self.write_json(
                table_root / "table_001.json",
                TableSchemaContractTests.valid_payload(),
            )
            csv_path = table_root / "table_001.csv"
            csv_path.write_text("name,value\nalpha,1\n", encoding="utf-8")

            findings = _table_derivative_findings(
                extraction,
                {Path("tables/main/table_001.json")},
            )

            csv_findings = [
                finding for finding in findings if finding.code == "unexpected_table_csv"
            ]
            self.assertEqual(len(csv_findings), 1)
            self.assertEqual(csv_findings[0].path, "tables/main/table_001.csv")

    def test_malformed_and_schema_invalid_table_json_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            extraction = Path(directory)
            table_root = extraction / "tables" / "main"
            table_root.mkdir(parents=True)

            malformed_path = table_root / "table_001.json"
            malformed_path.write_text('{"schema_version": "1.0",', encoding="utf-8")

            schema_invalid_path = table_root / "table_002.json"
            schema_invalid = TableSchemaContractTests.valid_payload()
            schema_invalid["table_id"] = "table_002"
            del schema_invalid["asset_path_base"]
            self.write_json(schema_invalid_path, schema_invalid)

            findings = _table_derivative_findings(
                extraction,
                {
                    Path("tables/main/table_001.json"),
                    Path("tables/main/table_002.json"),
                },
            )

            rejected = [
                (finding.code, finding.path, finding.severity)
                for finding in findings
                if finding.code in {"invalid_table_json", "invalid_table_schema"}
            ]
            self.assertEqual(
                rejected,
                [
                    (
                        "invalid_table_json",
                        "tables/main/table_001.json",
                        "critical",
                    ),
                    (
                        "invalid_table_schema",
                        "tables/main/table_002.json",
                        "structural",
                    ),
                ],
            )

    def test_reports_unsafe_links_placeholders_orphans_hashes_and_counts(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            extraction = root / "extraction"
            diagnostic = root / "extraction_diagnostic"
            assets = extraction / "assets"
            assets.mkdir(parents=True)
            diagnostic.mkdir()

            (extraction / "record.md").write_text(
                """# Synthetic validation article

[Unsafe](../outside.txt)
[Unsafe scheme](javascript:alert)
[Missing](assets/missing.bin)
[Linked](assets/linked.bin)

Unresolved publisher token: equation/tex2gif-sup-42.gif

## Figures

Figure 1. A wholly synthetic result.
""",
                encoding="utf-8",
            )
            linked = assets / "linked.bin"
            linked.write_bytes(b"linked synthetic asset")
            (extraction / "orphan.bin").write_bytes(b"unlinked synthetic asset")

            self.write_json(
                diagnostic / "manifest.json",
                {
                    "schema_version": "1.0",
                    "files": [
                        {
                            "output_path": "assets/linked.bin",
                            "sha256": "0" * 64,
                        },
                        {
                            "output_path": "../escaped.bin",
                            "sha256": "1" * 64,
                        },
                    ],
                },
            )
            self.write_json(diagnostic / "sources.json", {"sources": []})
            (diagnostic / "coverage.jsonl").write_text("", encoding="utf-8")
            self.write_json(diagnostic / "quality.json", {"schema_version": "1.0"})
            self.write_json(
                diagnostic / "confidence.json",
                {
                    "schema_version": "1.0",
                    "categories": {
                        "synthetic": {"level": "high", "basis": "synthetic fixture"}
                    },
                },
            )
            (diagnostic / "warnings.jsonl").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "code": "main_table_source_image_missing",
                        "severity": "scientific",
                        "message": "A PDF-sourced table has no full-table image.",
                        "source_path": "pdf/main.pdf",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            expected_counts = {"main_figures": 2}
            first = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic validation article",
                expected_counts=expected_counts,
            )
            second = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic validation article",
                expected_counts=expected_counts,
            )

            codes = {finding.code for finding in first.findings}
            self.assertTrue(
                {
                    "unsafe_local_link",
                    "unsafe_external_scheme",
                    "missing_local_link",
                    "publisher_equation_placeholder",
                    "orphan_output_file",
                    "output_hash_mismatch",
                    "invalid_manifest_path",
                    "content_count_mismatch",
                    "diagnostic_warning_main_table_source_image_missing",
                }.issubset(codes)
            )
            self.assertEqual(first.status, "fail")
            self.assertEqual(first.counts["main_figures"], 0)
            self.assertEqual(first.as_dict(), second.as_dict())

    def test_valid_candidate_with_matching_hash_and_count_passes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            extraction = root / "extraction"
            diagnostic = root / "extraction_diagnostic"
            assets = extraction / "figures"
            assets.mkdir(parents=True)
            diagnostic.mkdir()

            (extraction / "record.md").write_text(
                """# Synthetic passing article

## Figure and Scheme Captions

### Figure 1

Asset: [Figure 1](figures/figure_001.png)

Synthetic caption.
""",
                encoding="utf-8",
            )
            image = assets / "figure_001.png"
            image.write_bytes(b"synthetic image bytes")

            self.write_json(
                diagnostic / "manifest.json",
                {
                    "files": [
                        {
                            "path": "figures/figure_001.png",
                            "bytes": image.stat().st_size,
                            "sha256": sha256_file(image),
                        },
                        {
                            "path": "record.md",
                            "bytes": (extraction / "record.md").stat().st_size,
                            "sha256": sha256_file(extraction / "record.md"),
                        },
                    ]
                },
            )
            self.write_json(diagnostic / "sources.json", {"sources": []})
            (diagnostic / "coverage.jsonl").write_text(
                json.dumps(
                    {
                        "coverage_id": "synthetic-record",
                        "content_kind": "article",
                        "status": "included",
                        "source_path": "generated",
                        "source_locator": "synthetic validation fixture",
                        "output_path": "record.md",
                        "output_locator": {"start_line": 1, "end_line": 9},
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            self.write_json(diagnostic / "quality.json", {"schema_version": "1.0"})
            self.write_json(
                diagnostic / "confidence.json",
                {
                    "schema_version": "1.0",
                    "categories": {
                        "synthetic": {"level": "high", "basis": "synthetic fixture"}
                    },
                },
            )

            report = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic passing article",
                expected_counts={"main_figures": 1},
            )

            self.assertEqual(report.findings, ())
            self.assertTrue(report.passed)

            (extraction / "record.md").write_text(
                """# Synthetic passing article

## Local Assets

- [Figure 1](figures/figure_001.png)
""",
                encoding="utf-8",
            )
            truncated = validate_candidate(
                extraction,
                diagnostic,
                expected_title="Synthetic passing article",
                expected_counts={"main_figures": 1},
            )
            self.assertIn(
                "content_count_mismatch",
                {finding.code for finding in truncated.findings},
            )


if __name__ == "__main__":
    unittest.main()
