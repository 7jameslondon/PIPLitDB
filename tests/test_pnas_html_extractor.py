from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.extraction.html_extractor import extract_html


PNAS_HTML = """
<article vocab="http://schema.org/" typeof="ScholarlyArticle">
  <header>
    <h1>PNAS semantic snapshot</h1>
    <div><span property="datePublished">January 2, 2024</span></div>
    <div><span property="volumeNumber">121</span>
      (<span property="issueNumber">1</span>)
      <span property="pagination"><span property="pageStart">10</span>-<span property="pageEnd">15</span></span>
      <a href="https://doi.org/10.1073/pnas.1234567890">https://doi.org/10.1073/pnas.1234567890</a>
    </div>
    <div role="paragraph">Contributed by A. Editor, December 1, 2023</div>
  </header>
  <div id="abstracts"><div>
    <section id="abstract" property="abstract" role="doc-abstract">
      <h2>Abstract</h2><div role="paragraph">The complete abstract.</div>
    </section>
    <div><div><div><h3>Sign up for PNAS alerts.</h3><p>Get alerts for new articles, or get an alert when an article is cited.</p></div><div><a>Learn More</a></div></div></div>
  </div></div>
  <section id="bodymatter" property="articleBody" typeof="Text"><div>
    <div role="paragraph">An unheaded introduction.</div>
    <section id="sec-1"><h2>Results</h2><div role="paragraph">A result.</div>
      <div><header><div>Fig. 1.</div></header>
        <figure id="fig01"><img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=" aria-labelledby="fig01">
          <figcaption>A complete PNAS caption with panel <i>A</i> and panel <i>B</i>.</figcaption>
        </figure>
      </div>
      <div><header><div>Table 1.</div></header>
        <div id="t01"><figcaption>Inhibition concentration (<i>K</i><sub>i</sub>, nM)</figcaption>
          <div><table><thead><tr><th>Condition</th><th>Value</th></tr></thead><tbody><tr><td>TS</td><td>10</td></tr></tbody></table></div>
        </div>
      </div>
      <figure id="sfig01"><figcaption>Figure S1. Embedded SI.</figcaption></figure>
      <div id="st01"><table><tr><td>SI value</td></tr></table></div>
    </section>
    <section id="sec-2"><h2>SI Materials and Methods</h2><div role="paragraph">SI prose.</div></section>
  </div></section>
  <section id="backmatter"><div>
    <section id="acknowledgments" role="doc-acknowledgments"><h2>Acknowledgments</h2><div role="paragraph">Thanks.</div></section>
    <section id="supplementary-materials"><h2>Supporting Information</h2><div>
      <div><div>Supporting Information (PDF)</div><div>Supporting Information</div></div>
      <div><ul><li><a href="/doi/suppl/10.1073/pnas.1234567890/suppl_file/article.si.pdf" download="article.si.pdf">Download</a></li><li>1.20 MB</li></ul></div>
    </div></section>
    <section id="bibliography" role="doc-bibliography"><h2>References</h2>
      <div id="bibliography-collapsible-text" role="list">
        <div role="listitem"><div>1</div><div id="r1"><div><div>A. Author, First citation.</div><div><div><a href="https://doi.org/10.1000/one">View</a></div><div><a href="https://scholar.google.com/one">Google Scholar</a></div></div></div></div></div>
        <div role="listitem"><div>2</div><div id="r2"><div><div>B. Author, Second citation.</div><div><div><a href="/servlet/linkout?suffix=e_1_3_3_2_2&amp;dbid=4&amp;doi=10.1073%2Fpnas.1234567890&amp;key=10.1073%2Fpnas.9876543210&amp;site=pnas-site">Go to reference</a></div><div><a href="https://scholar.google.com/two">Google Scholar</a></div></div></div></div></div>
      </div>
    </section>
  </div></section>
  <div><section id="tab-metrics-inner"><h3>Metrics</h3><p>Interface data.</p></section></div>
</article>
"""


class PnasHtmlExtractorTests(unittest.TestCase):
    def test_semantic_silverchair_snapshot_recovers_article_not_ui_or_embedded_si(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(PNAS_HTML, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertEqual(
            [section.heading for section in article.sections],
            ["Abstract", "Introduction", "Results", "Acknowledgments"],
        )
        self.assertEqual(
            [block.plain_text for section in article.sections for block in section.blocks],
            ["The complete abstract.", "An unheaded introduction.", "A result.", "Thanks."],
        )
        self.assertEqual(len(article.figures), 1)
        self.assertEqual(article.figures[0].label, "Figure 1")
        self.assertEqual(
            article.figures[0].caption_plain,
            "A complete PNAS caption with panel A and panel B.",
        )
        self.assertEqual(len(article.tables), 1)
        self.assertEqual(
            article.tables[0].title_plain,
            "Table 1. Inhibition concentration (K_{i}, nM)",
        )
        self.assertEqual(
            [block.plain_text for block in article.supporting_information],
            ["Supporting Information (PDF)"],
        )
        self.assertEqual(len(article.references), 2)
        self.assertEqual(
            article.references[0].plain_text,
            "1. A. Author, First citation. DOI: https://doi.org/10.1000/one",
        )
        self.assertNotIn("Google Scholar", article.references[0].plain_text)
        self.assertEqual(
            article.references[1].plain_text,
            "2. B. Author, Second citation. DOI: https://doi.org/10.1073/pnas.9876543210",
        )
        self.assertEqual(
            article.bibliographic,
            {
                "volume": "121",
                "issue": "1",
                "date": "January 2, 2024",
                "pages": "10-15",
            },
        )
        self.assertEqual(
            article.front_matter[0].plain_text,
            "Editorial history: Contributed by A. Editor, December 1, 2023",
        )

    def test_noncontiguous_reference_ids_do_not_trigger_pnas_normalization(self) -> None:
        malformed = PNAS_HTML.replace('id="r2"', 'id="r3"')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(malformed, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertFalse(any(section.heading == "Introduction" for section in article.sections))
        self.assertEqual(article.references, [])

    def test_legacy_uppercase_b_reference_ids_are_authenticated(self) -> None:
        legacy = PNAS_HTML.replace('id="r1"', 'id="B1"').replace(
            'id="r2"', 'id="B2"'
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(legacy, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertEqual(
            [section.heading for section in article.sections],
            ["Abstract", "Introduction", "Results", "Acknowledgments"],
        )
        self.assertEqual(len(article.references), 2)
        self.assertEqual(article.references[1].plain_text[:2], "2.")

    def test_legacy_ref_and_opaque_reference_ids_are_authenticated(self) -> None:
        legacy = PNAS_HTML.replace('id="r1"', 'id="ref1"').replace(
            'id="r2"', 'id="N0x123abc-0x456def"'
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(legacy, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertEqual(
            [section.heading for section in article.sections],
            ["Abstract", "Introduction", "Results", "Acknowledgments"],
        )
        self.assertEqual(len(article.references), 2)

    def test_legacy_tbl_wrapper_spacer_and_table_note_are_recovered(self) -> None:
        legacy = (
            PNAS_HTML.replace('<div id="t01">', '<figure id="tbl1">')
            .replace('<div><table><thead>', '<div><div><table><thead>')
            .replace(
                '</table></div>\n        </div>\n      </div>\n      <figure id="sfig01">',
                '</table></div></div><div></div>'
                '<div><div role="doc-footnote">A legacy authored note.</div></div>'
                '</figure>\n      </div>\n      <figure id="sfig01">',
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(legacy, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertEqual(len(article.tables), 1)
        self.assertEqual(
            article.tables[0].title_plain,
            "Table 1. Inhibition concentration (K_{i}, nM)",
        )
        self.assertEqual(
            article.tables[0].footnotes_plain,
            ["A legacy authored note."],
        )

    def test_failed_legacy_inline_supplement_import_is_removed(self) -> None:
        legacy = PNAS_HTML.replace(
            '<section id="sec-2"><h2>SI Materials and Methods</h2>',
            '<section id="sec-4"><h2>Supplementary Material</h2><div>'
            '<div>Supporting Information</div>'
            '<span>Media (pnas_101_1_example.gif) is missing or otherwise invalid.</span>'
            '</div></section>'
            '<section id="sec-2"><h2>SI Materials and Methods</h2>',
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(legacy, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertNotIn(
            "Supplementary Material",
            [section.heading for section in article.sections],
        )
        self.assertNotIn(
            "missing or otherwise invalid",
            " ".join(
                block.plain_text
                for section in article.sections
                for block in section.blocks
            ),
        )

    def test_nested_download_card_retains_authored_label_without_ui(self) -> None:
        nested = PNAS_HTML.replace(
            '<section id="supplementary-materials"><h2>Supporting Information</h2><div>',
            '<section id="supplementary-materials"><h2>Supporting Information</h2>'
            '<section id="sec-5-1"><h3>Materials/Methods, Supplementary Text, '
            'Tables, Figures, and/or References</h3><div>',
        ).replace(
            '</div></section>\n    <section id="bibliography"',
            '</div></section></section>\n    <section id="bibliography"',
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(nested, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertNotIn(
            "Materials/Methods, Supplementary Text, Tables, Figures, and/or References",
            [section.heading for section in article.sections],
        )
        self.assertEqual(
            [block.plain_text for block in article.supporting_information],
            ["Materials/Methods, Supplementary Text, Tables, Figures, and/or References"],
        )
        self.assertNotIn(
            "Download",
            " ".join(
                block.plain_text
                for section in article.sections
                for block in section.blocks
            ),
        )

    def test_roleless_legacy_bibliography_is_authenticated(self) -> None:
        legacy = (
            PNAS_HTML.replace('id="r1"', 'id="B1"')
            .replace('id="r2"', 'id="B2"')
            .replace(' id="bibliography-collapsible-text" role="list"',
                     ' id="bibliography-collapsible-text"')
            .replace('<div role="listitem">', '<div>')
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(legacy, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertEqual(
            [section.heading for section in article.sections],
            ["Abstract", "Introduction", "Results", "Acknowledgments"],
        )
        self.assertEqual(len(article.figures), 1)
        self.assertEqual(
            article.figures[0].caption_plain,
            "A complete PNAS caption with panel A and panel B.",
        )
        self.assertEqual(len(article.references), 2)
        self.assertEqual(article.references[1].plain_text[:2], "2.")

    def test_legacy_uppercase_figure_and_table_containers_are_authenticated(self) -> None:
        legacy = (
            PNAS_HTML.replace('id="r1"', 'id="B1"')
            .replace('id="r2"', 'id="B2"')
            .replace('id="fig01"', 'id="F1"')
            .replace('aria-labelledby="fig01"', 'aria-labelledby="F1"')
            .replace('<div id="t01">', '<figure id="T1">')
            .replace('<div><table><thead>', '<div><div><table><thead>')
            .replace('</table></div>\n        </div>', '</table></div></div><div/></figure>')
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(legacy, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertEqual(len(article.figures), 1)
        self.assertEqual(article.figures[0].label, "Figure 1")
        self.assertEqual(
            article.figures[0].caption_plain,
            "A complete PNAS caption with panel A and panel B.",
        )
        self.assertEqual(len(article.tables), 1)
        self.assertEqual(
            article.tables[0].title_plain,
            "Table 1. Inhibition concentration (K_{i}, nM)",
        )

    def test_legacy_table_notes_and_wrapped_glossary_pairs_are_recovered(self) -> None:
        legacy = PNAS_HTML.replace('<div id="t01">', '<figure id="T1">').replace(
            '</table></div>\n        </div>\n      </div>\n      <figure id="sfig01">',
            '</table></div><div>'
            '<div role="doc-footnote">An unmarked authored table note.</div>'
            '<div role="doc-footnote"><div id="TF1-1" role="paragraph">'
            '*A marked authored table note.</div></div>'
            '</div></figure>\n      </div>\n'
            '<section id="glossary" role="doc-glossary"><h2>Abbreviations</h2>'
            '<dl><div><dt>FRDA</dt><dd>Friedreich\'s ataxia</dd></div>'
            '<div><dt>qRT-PCR</dt><dd>quantitative RT-PCR</dd></div></dl></section>\n'
            '      <figure id="sfig01">',
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(legacy, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertEqual(len(article.tables), 1)
        self.assertEqual(
            article.tables[0].title_plain,
            "Table 1. Inhibition concentration (K_{i}, nM)",
        )
        self.assertEqual(
            article.tables[0].footnotes_plain,
            [
                "An unmarked authored table note.",
                "*A marked authored table note.",
            ],
        )
        glossary = next(
            section for section in article.sections if section.heading == "Abbreviations"
        )
        self.assertEqual(
            [block.plain_text for block in glossary.blocks],
            ["- FRDA: Friedreich's ataxia\n- qRT-PCR: quantitative RT-PCR"],
        )

    def test_legacy_scheme_wrapper_is_not_mislabeled_graphical_abstract(self) -> None:
        legacy = (
            PNAS_HTML.replace("Fig. 1.", "Scheme. 1.")
            .replace('id="fig01"', 'id="S1"')
            .replace('aria-labelledby="fig01"', 'aria-labelledby="S1"')
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(legacy, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertEqual(len(article.figures), 1)
        self.assertEqual(article.figures[0].figure_id, "scheme_001")
        self.assertEqual(article.figures[0].label, "Scheme 1")
        self.assertEqual(article.figures[0].kind, "scheme")
        self.assertNotEqual(article.figures[0].label, "Graphical Abstract")

    def test_google_scholar_only_reference_control_preserves_bibliography(self) -> None:
        scholar_only = PNAS_HTML.replace(
            '<div><a href="/servlet/linkout?suffix=e_1_3_3_2_2&amp;dbid=4&amp;doi=10.1073%2Fpnas.1234567890&amp;key=10.1073%2Fpnas.9876543210&amp;site=pnas-site">Go to reference</a></div>',
            "",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(scholar_only, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertEqual(len(article.references), 2)
        self.assertEqual(
            article.references[1].plain_text,
            "2. B. Author, Second citation.",
        )

    def test_data_availability_and_multiple_affiliations_are_recovered(self) -> None:
        enriched = PNAS_HTML.replace(
            '<section id="acknowledgments"',
            '<section id="data-availability"><h2>Data Availability</h2>'
            '<div role="paragraph">Deposited under accession GSE1.</div></section>'
            '<section id="acknowledgments"',
        ).replace(
            "</article>",
            '<section id="tab-contributors"><section><h4>Affiliations</h4>'
            '<div id="con1" property="author" typeof="Person"><div><h5>'
            '<span property="givenName">A.</span> <span property="familyName">Author</span>'
            '</h5></div><div><div><div property="affiliation" typeof="Organization">'
            '<span property="name">Institute One;</span></div>'
            '<div property="affiliation" typeof="Organization">'
            '<span property="name">Institute Two; and</span></div>'
            '</div></div></div></section></section></article>',
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(enriched, encoding="utf-8")
            article = extract_html(path, "article.html")

        data = next(section for section in article.sections if section.heading == "Data Availability")
        self.assertEqual([block.plain_text for block in data.blocks], ["Deposited under accession GSE1."])
        self.assertIn(
            "Affiliation — A. Author: Institute One; Institute Two",
            [block.plain_text for block in article.front_matter],
        )

    def test_article_footnotes_and_notes_are_recovered(self) -> None:
        enriched = PNAS_HTML.replace(
            "</article>",
            '<section id="tab-information"><section><div>Notes</div>'
            '<div role="doc-footnote"><div>*</div>'
            '<div id="FN1" role="paragraph">An authored explanatory note.</div></div>'
            '<div role="doc-footnote">This article contains supporting information online.</div>'
            '</section></section></article>',
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(enriched, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertIn(
            "Article footnote *: An authored explanatory note.",
            [block.plain_text for block in article.front_matter],
        )
        self.assertIn(
            "Article note: This article contains supporting information online.",
            [block.plain_text for block in article.front_matter],
        )

    def test_unknown_reference_control_does_not_trigger_pnas_reference_recovery(self) -> None:
        malformed = PNAS_HTML.replace(">Go to reference</a>", ">Open reference</a>")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(malformed, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertEqual(article.references, [])

    def test_pubmed_only_reference_control_is_an_authenticated_alternative(self) -> None:
        pubmed_only = PNAS_HTML.replace(">Go to reference</a>", ">PubMed</a>")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.html"
            path.write_text(pubmed_only, encoding="utf-8")
            article = extract_html(path, "article.html")

        self.assertEqual(len(article.references), 2)
        self.assertEqual(
            article.references[1].plain_text,
            "2. B. Author, Second citation. DOI: https://doi.org/10.1073/pnas.9876543210",
        )


if __name__ == "__main__":
    unittest.main()
