from __future__ import annotations

import base64
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

try:
    from lxml import html as lxml_html
except ModuleNotFoundError:
    lxml_html = None

if lxml_html is not None:
    from scripts.extraction.html_extractor import extract_html
else:  # pragma: no cover - documents the optional dependency boundary
    extract_html = None


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0publisher-image\xff\xd9"


@unittest.skipUnless(lxml_html is not None, "lxml is required for extraction tests")
class ScienceDirectHtmlExtractionTests(unittest.TestCase):
    def extract(self, source: str, repair_specs=()):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "article.html"
            path.write_text(source, encoding="utf-8")
            return extract_html(path, "html/main.html", repair_specs)

    def test_current_unheaded_body_after_aep_keywords_gets_main_text_scope(self) -> None:
        result = self.extract(
            """<body><article>
            <h1>Current unheaded ScienceDirect article</h1>
            <a href="https://doi.org/10.1016/j.synthetic.2026.1">DOI</a>
            <div><div id="aep-keywords-id28"><h2>Keywords</h2>
              <div id="k0005"><span>Alpha</span></div>
            </div></div>
            <div id="body"><div><div id="p0010">Authored body paragraph.</div></div></div>
            <section><h2>References and notes</h2><ol><li>
              <a id="ref-id-b0005">1</a><span>Synthetic reference.</span>
            </li></ol></section>
            </article></body>"""
        )

        headings = [
            section.heading
            if isinstance(section.heading, str)
            else section.heading.plain_text
            for section in result.sections
        ]
        self.assertEqual(headings, ["Keywords", "Main text"])
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Alpha"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Authored body paragraph."],
        )

    def test_current_aep_nested_keyword_cards_remain_separate(self) -> None:
        result = self.extract(
            """<body><article>
            <h1>Current nested-keyword ScienceDirect article</h1>
            <a href="https://doi.org/10.1016/j.synthetic.2026.12">DOI</a>
            <div><div id="aep-keywords-id28"><h2>Keywords</h2>
              <div><span>Alpha</span></div>
              <div><span>Sequence specificity</span>
                <div><span>Footprinting</span></div>
                <div><span>SPR</span></div>
                <div><span>Gene control</span></div>
              </div>
            </div></div>
            <div id="body"><div><div id="p0010">Authored body paragraph.</div></div></div>
            <section><h2>References and notes</h2><ol><li>
              <a id="ref-id-b0005">1</a><span>Synthetic reference.</span>
            </li></ol></section>
            </article></body>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Alpha", "Sequence specificity", "Footprinting", "SPR", "Gene control"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Authored body paragraph."],
        )

    def test_current_anonymous_aep_body_excludes_embedded_assets_from_prose(self) -> None:
        image = base64.b64encode(PNG_BYTES).decode("ascii")
        result = self.extract(
            f"""<body><article>
            <h1>Current anonymous ScienceDirect article</h1>
            <a href="https://doi.org/10.1016/j.synthetic.2026.11">DOI</a>
            <div><div id="aep-keywords-id28"><h2>Keywords</h2>
              <div id="k0005"><span>Alpha</span></div>
            </div></div>
            <div id="body"><div>
              <div>First anonymous authored paragraph.</div>
              <div>Second paragraph before its figure.
                <figure id="fig1"><img src="data:image/png;base64,{image}">
                  <figcaption>Figure 1. Main authored caption.</figcaption>
                </figure>
              </div>
              <div>Third paragraph before its table.<div id="tbl1">
                <p>Table 1. Authored values.</p><table><tbody>
                  <tr><td>A</td><td>1</td></tr>
                </tbody></table></div></div>
              <div>Fourth anonymous authored paragraph.</div>
            </div><div><section id="app1"><h2>Supplementary data</h2>
              <div><figure id="aep-figure-id16">
                <img src="data:image/png;base64,{image}">
                <figcaption>Supplementary Fig. S1. First supporting result.</figcaption>
              </figure></div>
              <div><figure id="aep-figure-id18">
                <img src="data:image/png;base64,{image}">
                <figcaption>Supplementary Fig. S2. Second supporting result.</figcaption>
              </figure></div>
            </section></div></div>
            <section><h2>References and notes</h2><ol><li>
              <a id="ref-id-b0005">1</a><span>Synthetic reference.</span>
            </li></ol></section>
            </article></body>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Keywords", "Main text"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            [
                "First anonymous authored paragraph.",
                "Second paragraph before its figure.",
                "Third paragraph before its table.",
                "Fourth anonymous authored paragraph.",
            ],
        )
        body_text = " ".join(
            block.plain_text for block in result.sections[1].blocks
        )
        self.assertNotIn("Main authored caption", body_text)
        self.assertNotIn("Authored values", body_text)
        self.assertEqual(
            [(figure.figure_id, figure.label) for figure in result.figures],
            [
                ("figure_001", "Figure 1"),
                ("html_supplement_figure_s1", "Supplementary Fig. S1"),
                ("html_supplement_figure_s2", "Supplementary Fig. S2"),
            ],
        )
        self.assertEqual(len(result.tables), 1)

    def test_current_aep_label_only_supplement_figure_is_not_graphical_abstract(self) -> None:
        image = base64.b64encode(PNG_BYTES).decode("ascii")
        result = self.extract(
            f"""<body><article>
            <h1>Current label-only supplementary figure</h1>
            <a href="https://doi.org/10.1016/j.synthetic.2026.12">DOI</a>
            <div id="body"><div><div>Authored body paragraph.</div></div>
              <div><section id="app1"><h2>Supplementary data</h2><div>
                <figure id="aep-figure-id19"><span>
                  <img src="data:image/png;base64,{image}" alt="">
                  <a href="https://example.invalid/mmc1.png" download="">Download</a>
                </span><span><span><p><span>Supplementary Fig. 1</span>. </p></span></span>
                </figure>
              </div></section></div>
            </div></article></body>"""
        )

        self.assertEqual(len(result.figures), 1)
        figure = result.figures[0]
        self.assertEqual(figure.figure_id, "html_supplement_figure_1")
        self.assertEqual(figure.label, "Supplementary Fig. 1")
        self.assertEqual(figure.kind, "figure")
        self.assertEqual(figure.caption_markdown, "")
        self.assertEqual(figure.caption_plain, "")
        self.assertEqual(result.embedded_assets[0].asset_id, figure.figure_id)
        self.assertEqual(result.embedded_assets[0].data, PNG_BYTES)

    def test_legacy_sectioned_anonymous_divs_and_schema_labels_are_preserved(self) -> None:
        image = base64.b64encode(PNG_BYTES).decode("ascii")
        result = self.extract(
            f"""<body><main id="main"><div>
            <h1 id="screen-reader-main-title">Legacy sectioned article</h1>
            <a href="https://doi.org/10.1016/j.synthetic.2005.1">DOI</a>
            <div id="aep-abstract-id1"><h2>Abstract</h2>
              <div id="aep-abstract-sec-id2"><div>Authored abstract.
                <span><figure id="aep-figure-id3"><span><img
                  src="data:image/png;base64,{image}" alt=""></span></figure></span>
              </div></div>
            </div>
            <div id="body"><div>
              <section id="sec1"><h2>1. Introduction</h2><div>
                <div>First authored paragraph <a href="#bib1" name="bbib1">[1]</a>.</div>
                <div>Second authored paragraph <a href="#bib2" name="bbib2">[2]</a>.</div>
                <div id="tbl1"><span><span><p><span>Table 1</span>. Exact legacy
                  table title with log<sub>10</sub><em>GI</em><sub>50</sub>.</p>
                </span></span><div><table><thead><tr><th>Value</th></tr></thead>
                  <tbody><tr><td>1</td></tr></tbody></table></div></div>
              </div></section>
              <section id="sec2"><h2>2. Results</h2>
                <section id="sec2.1"><h3>2.1. Synthesis</h3><div>
                  <div>Paragraph before the scheme.</div>
                  <figure id="sc1"><span><img src="data:image/png;base64,{image}"
                    alt=""></span><span><span><p><span>Schema 1</span>. </p>
                    </span></span></figure>
                </div></section>
              </section>
              <section id="aep-acknowledgment-id4"><h2>Acknowledgements</h2>
                <div>Authored thanks.</div></section>
            </div></div>
            <section id="aep-bibliography-id5"><h2>References</h2><ol>
              <li id="bib1">First reference.</li><li id="bib2">Second reference.</li>
            </ol></section></div></main></body>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            [
                "Abstract",
                "1. Introduction",
                "2. Results",
                "2.1. Synthesis",
                "Acknowledgements",
            ],
        )
        self.assertEqual(
            [[block.plain_text for block in section.blocks] for section in result.sections],
            [
                ["Authored abstract."],
                [
                    "First authored paragraph ^{[1]}.",
                    "Second authored paragraph ^{[2]}.",
                ],
                [],
                ["Paragraph before the scheme."],
                ["Authored thanks."],
            ],
        )
        self.assertNotIn(
            "Download",
            " ".join(
                block.plain_text
                for section in result.sections
                for block in section.blocks
            ),
        )
        self.assertEqual(
            [(figure.figure_id, figure.kind, figure.label) for figure in result.figures],
            [
                ("graphical_abstract", "graphical_abstract", "Graphical Abstract"),
                ("scheme_001", "scheme", "Schema 1"),
            ],
        )
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(
            result.tables[0].title_plain,
            "Table 1. Exact legacy table title with log_{10}GI_{50}.",
        )

    def test_direct_legacy_article_preserves_anonymous_section_prose(self) -> None:
        image = base64.b64encode(PNG_BYTES).decode("ascii")
        result = self.extract(
            f"""<body><article lang="en">
            <div id="publication"><h2>Tetrahedron</h2></div>
            <h1 id="screen-reader-main-title">Direct legacy article</h1>
            <div id="article-identifier-links"><a
              href="https://doi.org/10.1016/j.synthetic.2007.1">DOI</a></div>
            <div id="body"><div>
              <section id="sec1"><h2>1. Introduction</h2>
                <div>First authored paragraph <a href="#bib1" name="bbib1">1</a>.</div>
                <div>Second authored paragraph before the figure.
                  <figure id="fig1"><img src="data:image/png;base64,{image}">
                    <figcaption>Figure 1. Authored figure.</figcaption>
                  </figure>
                </div>
              </section>
              <section id="sec2"><h2>2. Results</h2>
                <div>Third authored paragraph <a href="#bib2" name="bbib2">2</a>.</div>
              </section>
            </div></div>
            <section id="aep-bibliography-id5"><h2>References</h2><ol>
              <li id="bib1">First reference.</li><li id="bib2">Second reference.</li>
            </ol></section></article></body>"""
        )

        self.assertEqual(
            [[block.plain_text for block in section.blocks] for section in result.sections],
            [
                ["First authored paragraph ^{1}.", "Second authored paragraph before the figure."],
                ["Third authored paragraph ^{2}."],
            ],
        )
        self.assertEqual(
            [(figure.figure_id, figure.label) for figure in result.figures],
            [("figure_001", "Figure 1")],
        )

    def test_current_aep_abbreviations_and_keywords_remain_separate(self) -> None:
        result = self.extract(
            """<body><article>
            <h1>Current ScienceDirect abbreviation article</h1>
            <a href="https://doi.org/10.1016/j.synthetic.2026.2">DOI</a>
            <div>
              <div id="aep-keywords-id15"><h2>Abbreviations</h2>
                <div><span>Py–Im</span><div><span>pyrrole–imidazole</span></div></div>
                <div><span>Chl</span><div><span>chlorambucil</span></div></div>
              </div>
              <div id="aep-keywords-id16"><h2>Keywords</h2>
                <div><span>Chromatin</span></div>
              </div>
            </div>
            <div id="body"><div><div id="p0010">Authored body paragraph.</div></div></div>
            <section><h2>References and notes</h2><ol><li>
              <a id="ref-id-b0005">1</a><span>Synthetic reference.</span>
            </li></ol></section>
            </article></body>"""
        )

        headings = [
            section.heading
            if isinstance(section.heading, str)
            else section.heading.plain_text
            for section in result.sections
        ]
        self.assertEqual(headings, ["Abbreviations", "Keywords", "Main text"])
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["- Py–Im: pyrrole–imidazole\n- Chl: chlorambucil"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Chromatin"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[2].blocks],
            ["Authored body paragraph."],
        )

    def test_current_aep_nomenclature_definition_list_is_preserved(self) -> None:
        result = self.extract(
            """<body><article>
            <h1>AEP nomenclature article</h1>
            <a href="https://doi.org/10.1016/j.synthetic.1999.1">DOI</a>
            <div id="body"><section id="aep-nomenclature-id12">
              <h2>Nomenclature</h2>
              <ul id="aep-def-list-id13"><h3><span>Abbreviations</span></h3>
                <dt>Bp</dt><dd><div>base pair</div></dd>
                <dt><strong>Py</strong></dt><dd><div><em>N</em>-methylpyrrole</div></dd>
              </ul>
            </section><section id="aep-section-id14"><h2>Introduction</h2>
              <div>Authored body paragraph.</div>
            </section></div>
            </article></body>"""
        )

        sections = {section.heading: section for section in result.sections}
        self.assertEqual(
            [block.plain_text for block in sections["Abbreviations"].blocks],
            ["- Bp: base pair\n- Py: N-methylpyrrole"],
        )
        self.assertEqual(
            [block.markdown for block in sections["Abbreviations"].blocks],
            [
                "- <strong>Bp</strong>: base pair\n"
                "- <strong>Py</strong>: <em>N</em>-methylpyrrole"
            ],
        )

    def test_current_anonymous_abstract_and_abbreviations_used_are_preserved(self) -> None:
        result = self.extract(
            """<body><article>
            <h1 id="screen-reader-main-title">Anonymous abstract article</h1>
            <div id="article-identifier-links"><a
              href="https://doi.org/10.1016/j.synthetic.2005.2">DOI</a></div>
            <div id="abstracts"><div id="aep-abstract-id13" lang="en">
              <div id="aep-abstract-sec-id14"><div>Exact unheaded abstract.</div>
            </div></div></div>
            <div>
              <div id="aep-keywords-id15"><h2>Keywords</h2>
                <div><span>Chromatin</span></div></div>
              <div id="aep-keywords-id16"><h2>Abbreviations used</h2>
                <div><span>NCP</span><div><span>nucleosome core particle</span></div></div>
              </div>
            </div>
            <section><h2>Results</h2><div>Authored result.</div></section>
            </article></body>"""
        )

        sections = {section.heading: section for section in result.sections}
        self.assertEqual(
            [block.plain_text for block in sections["Abstract"].blocks],
            ["Exact unheaded abstract."],
        )
        self.assertEqual(
            [block.plain_text for block in sections["Abbreviations used"].blocks],
            ["- NCP: nucleosome core particle"],
        )

    def test_current_aep_mixed_unnumbered_table_and_duplicate_graphical_summary(self) -> None:
        image = base64.b64encode(PNG_BYTES).decode("ascii")
        result = self.extract(
            f"""<body><article><h1>Current AEP mixed table</h1>
            <div id="aep-abstract-id1"><h2>Abstract</h2>
              <div id="aep-abstract-sec-id2"><div>Exact authored summary.</div></div>
            </div>
            <div id="aep-abstract-id3"><h2>Graphical abstract</h2>
              <div id="aep-abstract-sec-id4"><div>Exact authored summary.</div></div>
              <figure id="aep-figure-id5"><img
                src="data:image/png;base64,{image}" alt=""></figure>
            </div>
            <div id="body">
              <section id="aep-section-id10"><h2>Results</h2>
                <div id="tbl1"><span><span><p><span>Table 1</span>.
                  Authored multipart title</p></span></span><div>
                  <table><tbody><tr><td>A</td><td>1</td></tr>
                    <tr><td colspan="2"><br></td></tr></tbody></table>
                  <table><tbody><tr><td>B</td><td>2</td></tr></tbody></table>
                </div></div>
              </section>
              <section id="aep-section-id34"><h3>9.6. Assay</h3><div>
                First authored sentence with <a href="https://example.test">link</a>.
                The primer sequences used in qRT-PCR are listed below.
                <span><div id="aep-table-id35"><div><table>
                  <thead><tr><th colspan="2">GAPDH</th></tr></thead>
                  <tbody><tr><td>Forward primer</td><td>GAGT</td></tr>
                  <tr><td>Reverse primer</td><td>ACTC</td></tr></tbody>
                </table></div></div></span>
                Second authored sentence after the table.
              </div></section>
            </div></article></body>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Results", "9.6. Assay"],
        )
        assay = result.sections[-1]
        self.assertEqual(
            [block.plain_text for block in assay.blocks],
            [
                (
                    "First authored sentence with link. "
                    "The primer sequences used in qRT-PCR are listed below."
                ),
                "Second authored sentence after the table.",
            ],
        )
        self.assertNotIn("GAGT", " ".join(block.plain_text for block in assay.blocks))
        self.assertEqual(len(result.tables), 2)
        self.assertEqual(result.tables[0].title_plain, "Table 1. Authored multipart title")
        self.assertEqual(len(result.tables[0].parts), 2)
        self.assertEqual(len(result.tables[0].parts[0].rows), 1)
        self.assertEqual(result.tables[1].table_id, "table_002")
        self.assertEqual(result.tables[1].label, "Unnumbered table")
        self.assertEqual(
            result.tables[1].title_plain,
            "The primer sequences used in qRT-PCR are listed below.",
        )
        self.assertEqual(
            [[cell.text for cell in row] for row in result.tables[1].parts[0].rows],
            [["GAPDH"], ["Forward primer", "GAGT"], ["Reverse primer", "ACTC"]],
        )
        self.assertEqual(
            [(figure.figure_id, figure.kind) for figure in result.figures],
            [("graphical_abstract", "graphical_abstract")],
        )

    def test_source_pinned_text_repair_enforces_exact_match_count(self) -> None:
        source = (
            "<body><h1>Exact repair</h1><section><h2>Body</h2>"
            "<p>Value<sup>1</sup>.</p></section></body>"
        )
        repaired = self.extract(
            source,
            [{
                "pattern": r"Value<sup>1</sup>",
                "replacement": "Value<sup>2</sup>",
                "expected_matches": 1,
            }],
        )
        self.assertEqual(repaired.sections[0].blocks[0].plain_text, "Value^{2}.")

        with self.assertRaisesRegex(ValueError, "matched 0 times; expected 1"):
            self.extract(
                source,
                [{
                    "pattern": r"near-miss-does-not-exist",
                    "replacement": "replacement",
                    "expected_matches": 1,
                }],
            )

    def test_multi_image_semantic_figure_preserves_every_part_in_one_asset(self) -> None:
        def pixel(width: int, height: int, color: tuple[int, int, int]) -> str:
            output = io.BytesIO()
            Image.new("RGB", (width, height), color).save(output, format="PNG")
            return base64.b64encode(output.getvalue()).decode("ascii")

        result = self.extract(
            f"""<body><article><h1>Multipart figure</h1><section><h2>Results</h2>
            <figure id="fig1"><img src="data:image/png;base64,{pixel(2, 1, (255, 0, 0))}">
            <img src="data:image/png;base64,{pixel(1, 2, (0, 0, 255))}">
            <figcaption>Figure 1. Complete multipart figure.</figcaption></figure>
            </section></article></body>"""
        )

        self.assertEqual(len(result.embedded_assets), 1)
        asset = result.embedded_assets[0]
        self.assertEqual(asset.output_path, "figures/main/figure_001.png")
        self.assertEqual(
            asset.source_locator,
            (
                "/html/body/article/section/figure/img[1]|"
                "/html/body/article/section/figure/img[2]"
            ),
        )
        with Image.open(io.BytesIO(asset.data)) as composed:
            self.assertEqual(composed.size, (2, 3))
            self.assertEqual(composed.convert("RGBA").getpixel((0, 0)), (255, 0, 0, 255))
            self.assertEqual(composed.convert("RGBA").getpixel((0, 1)), (0, 0, 255, 255))
            self.assertEqual(composed.convert("RGBA").getpixel((1, 1))[3], 0)
        self.assertNotIn(
            "multiple_embedded_images_for_figure",
            {warning["code"] for warning in result.warnings},
        )

    def test_legacy_inline_equation_figures_are_not_article_assets(self) -> None:
        def pixel(width: int, height: int, color: tuple[int, int, int]) -> str:
            output = io.BytesIO()
            Image.new("RGB", (width, height), color).save(output, format="PNG")
            return base64.b64encode(output.getvalue()).decode("ascii")

        main = pixel(4, 3, (20, 40, 60))
        marker = pixel(2, 1, (255, 0, 0))
        result = self.extract(
            f"""<body><article><h1>Legacy inline equation wrappers</h1>
            <section><h2>Results</h2>
              <div>Authored prose before <figure id="equation-1"><img
                src="data:image/png;base64,{marker}" alt=""></figure> after.</div>
              <figure id="fig1"><span><img
                src="data:image/png;base64,{main}" alt=""></span><span><p>
                <span>Figure 1</span>. Caption with marker
                <figure id="equation-1"></figure>.</p></span></figure>
            </section></article></body>"""
        )

        self.assertEqual(
            [(item.figure_id, item.kind) for item in result.figures],
            [("figure_001", "figure")],
        )
        self.assertEqual([asset.asset_id for asset in result.embedded_assets], ["figure_001"])
        asset = result.embedded_assets[0]
        with Image.open(io.BytesIO(asset.data)) as extracted:
            self.assertEqual(extracted.size, (4, 3))
            self.assertEqual(extracted.convert("RGB").getpixel((0, 0)), (20, 40, 60))

    def test_structured_supplement_card_retains_lead_and_removes_download_ui(self) -> None:
        source = """<body><article><h1>Supplement-card article</h1>
        <section><h2>Appendix A. Supplementary data</h2><div id="p0130">The
        following is the Supplementary data to this article:<span><span id="mmc1">
        <span><a href="https://example.invalid/mmc1.pdf" download=""
        title="Download Acrobat PDF file (4MB)">Download: Download Acrobat PDF
        file (4MB)</a></span><span><span><p><span>Multimedia component 1</span>.
        </p></span></span></span></span></div></section></article></body>"""

        result = self.extract(source)
        appendix = next(
            section
            for section in result.sections
            if section.heading == "Appendix A. Supplementary data"
        )
        self.assertEqual(
            [block.plain_text for block in appendix.blocks],
            [
                "The following is the Supplementary data to this article: "
                "Multimedia component 1."
            ],
        )
        self.assertNotIn("Download", appendix.blocks[0].plain_text)

        near_miss = self.extract(source.replace(' download=""', "", 1))
        near_appendix = next(
            section
            for section in near_miss.sections
            if section.heading == "Appendix A. Supplementary data"
        )
        self.assertEqual(
            [block.plain_text for block in near_appendix.blocks],
            ["Multimedia component 1."],
        )

    def test_legacy_special_issue_note_is_not_part_of_title(self) -> None:
        result = self.extract(
            """<body><article><h1 id="screen-reader-main-title">
              <span>Authored <em>p</em>-anisyl title</span>
              <a href="#aep-article-footnote-id1"
                 name="baep-article-footnote-id1"><span><span>☆</span></span></a>
            </h1><section><h2>Body</h2><p>Text.</p></section>
            <div><dl><dt><a href="#baep-article-footnote-id1"><sup>☆</sup></a></dt>
              <dd><div>Part of a special issue.</div></dd></dl></div>
            </article></body>"""
        )

        self.assertEqual(result.title, "Authored p-anisyl title")

        near_miss = self.extract(
            """<body><article><h1 id="screen-reader-main-title">Authored title
              <a href="#aep-article-footnote-id1"
                 name="baep-article-footnote-id1">☆</a></h1>
            <section><h2>Body</h2><p>Text.</p></section></article></body>"""
        )
        self.assertEqual(near_miss.title, "Authored title ☆")

    def test_jbc_linked_author_note_marker_is_not_part_of_title(self) -> None:
        result = self.extract(
            """<body><article><header>
            <h1 property="name">Scientific <em>GAA·TTC</em> title
              <a id="cecrefs10" href="#FN1" role="doc-noteref">*</a></h1>
            <details id="core-affiliations-notes"><div role="doc-footnote">
              <div>*</div><div id="FN1"><div role="paragraph">Funding statement.</div></div>
            </div></details></header>
            <section><h2>Body</h2><p>Text.</p></section></article></body>"""
        )

        self.assertEqual(result.title, "Scientific GAA·TTC title")

        near_miss = self.extract(
            """<body><article><h1 property="name">Authored title
              <a href="#FN1" role="doc-noteref">*</a></h1>
            <div id="FN1" role="paragraph">Ordinary linked prose.</div>
            <section><h2>Body</h2><p>Text.</p></section></article></body>"""
        )
        self.assertEqual(near_miss.title, "Authored title *")

    def test_jbc_decadic_sbref_bibliography_is_structured(self) -> None:
        result = self.extract(
            """<body><article><h1>JBC references</h1>
            <section><h2>Results</h2>
              <p>Prior studies <a id="body-ref-sbref20-1">1–2</a>.</p>
              <p>One study (<span><a id="body-ref-sbref10" href="#" href-manipulated="true"
                aria-controls="sbref10">1</a><div><div><div>1.</div>
                <div>Hidden duplicate reference title</div>
                <a href="https://doi.org/10.1000/first" target="_blank">Crossref</a>
                </div></div></span>) supports this.</p>
            </section>
            <section id="references"><h2>REFERENCES</h2><div id="bibliography"><div>
              <div id="bib1"><div id="sbref10"><div>
                <div><a href="#body-ref-sbref20-1">1.</a></div>
                <div>A. Author</div><div><strong>First title</strong></div>
                <div><em>Journal</em> 2012; <strong>1</strong>:1-2</div>
              </div><div><a href="https://doi.org/10.1000/first" target="_blank">Crossref</a></div></div></div>
              <div id="bib2"><div id="sbref20"><div>
                <div><a href="#body-ref-sbref20-1">2.</a></div>
                <div>B. Author</div><div><strong>Second title</strong></div>
                <div><em>Journal</em> 2012; <strong>2</strong>:3-4</div>
              </div><div><a href="https://doi.org/10.1000/second" target="_blank">Crossref</a></div></div></div>
            </div></div></section></article></body>"""
        )

        self.assertEqual(len(result.references), 2)
        body_text = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertIn("One study (1) supports this.", body_text)
        self.assertNotIn("Hidden duplicate reference title", body_text)
        self.assertNotIn("Crossref", body_text)
        self.assertEqual(
            [reference.plain_text for reference in result.references],
            [
                "1. A. Author First title Journal 2012; 1:1-2 DOI: https://doi.org/10.1000/first",
                "2. B. Author Second title Journal 2012; 2:3-4 DOI: https://doi.org/10.1000/second",
            ],
        )

    def test_jbc_compact_figure_caption_restores_panel_boundary(self) -> None:
        result = self.extract(
            """<body><article><header>
            <h1 property="name">JBC figure</h1>
            <div property="author" typeof="Person" role="listitem">
              <span><a><span property="givenName">A.</span>
              <span property="familyName">Author</span></a></span>
              <sup><a role="doc-noteref" href="#FN1">‡</a></sup>
            </div>
            <div id="core-affiliations-notes"><div id="FN1" role="doc-footnote"><p>Affiliation.</p></div></div>
            <div id="core-content-info"><a property="sameAs" href="https://doi.org/10.1074/example">DOI</a></div>
            </header><section><h2>Results</h2>
            <figure id="fig1"><a><img src="data:image/png;base64,iVBORw0KGgo="/></a>
              <figcaption><div id="fig1-title"><span>FIGURE 1</span>
                <span><b>Complete legend title.</b><i>A</i>, first panel. <i>B</i>, second panel.</span>
              </div></figcaption>
            </figure></section></article></body>"""
        )

        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].label, "Figure 1")
        self.assertEqual(
            result.figures[0].caption_plain,
            "Complete legend title. A, first panel. B, second panel.",
        )
        self.assertEqual(
            result.figures[0].caption_markdown,
            "<strong>Complete legend title.</strong> <em>A</em>, first panel. "
            "<em>B</em>, second panel.",
        )

    def test_legacy_headed_structured_abstract_keeps_each_part_text(self) -> None:
        result = self.extract(
            """<body><article><h1>Structured abstract</h1>
            <div id="abstracts"><div id="ab0005"><h2>Abstract</h2>
              <div id="as0005"><h3 id="st0010">Background</h3>
                <div id="sp0050">Background text with <em>topo IIα</em>.</div>
              </div>
              <div id="as0010"><h3 id="st0015">Methods</h3>
                <div id="sp0055">Methods text with 2 μM treatment.</div>
              </div>
            </div></div>
            <section><h2>Introduction</h2><p>Body.</p></section>
            </article></body>"""
        )

        sections = {section.heading: section for section in result.sections}
        self.assertEqual(
            [block.plain_text for block in sections["Background"].blocks],
            ["Background text with topo IIα."],
        )
        self.assertEqual(
            [block.plain_text for block in sections["Methods"].blocks],
            ["Methods text with 2 μM treatment."],
        )

    def test_legacy_par_and_spar_divs_preserve_article_prose(self) -> None:
        result = self.extract(
            """<body><div id="abstracts"><div id="abs0010"><h2>Abstract</h2>
            <div id="abst0010"><div id="spar0010">Abstract TGF-β1 text.</div>
            </div></div></div>
            <section id="sec0005"><h2>1. Introduction</h2>
            <div id="par0020">Body text with 5 μg/ml and CO<sub>2</sub>.</div>
            <div id="par0025">Second paragraph.</div></section></body>"""
        )

        sections = {section.heading: section for section in result.sections}
        self.assertEqual(
            [block.plain_text for block in sections["Abstract"].blocks],
            ["Abstract TGF-β1 text."],
        )
        self.assertEqual(
            [block.plain_text for block in sections["1. Introduction"].blocks],
            ["Body text with 5 μg/ml and CO_{2}.", "Second paragraph."],
        )

    def test_current_aep_leaf_divs_preserve_prose_and_display_equations(self) -> None:
        result = self.extract(
            """<body><div><h1>Current AEP article</h1>
            <div id="aep-abstract-id20"><h2>Abstract</h2>
              <div id="aep-abstract-sec-id21"><div>Abstract text.</div></div>
            </div>
            <section id="aep-section-id22"><h2>1. Introduction</h2>
              <div>First paragraph with CO<sub>2</sub>.</div>
              <div><div>Second paragraph.</div><figure><img
                src="data:image/png;base64,iVBORw0KGgo="></figure></div>
            </section>
            <section id="aep-section-id23"><h2>2. Methods</h2>
              <div>Before equation. <span id="fd1"><span>(1)</span><span>
                <math><mi>r</mi><mo>=</mo><msub><mi>K</mi><mn>1</mn></msub>
                </math></span></span> After equation.</div>
            </section>
            <section id="aep-acknowledgment-id24"><h2>Acknowledgments</h2>
              <div>Funding statement.</div>
            </section></div></body>"""
        )

        sections = {section.heading: section for section in result.sections}
        self.assertEqual(
            [block.plain_text for block in sections["Abstract"].blocks],
            ["Abstract text."],
        )
        self.assertEqual(
            [block.plain_text for block in sections["1. Introduction"].blocks],
            ["First paragraph with CO_{2}.", "Second paragraph."],
        )
        self.assertEqual(
            [(block.kind, block.plain_text) for block in sections["2. Methods"].blocks],
            [
                ("paragraph", "Before equation."),
                ("equation", "(1) r = K_{1}"),
                ("paragraph", "After equation."),
            ],
        )
        self.assertEqual(
            [block.plain_text for block in sections["Acknowledgments"].blocks],
            ["Funding statement."],
        )

    def test_current_aep_multiline_mathjax_frame_is_a_display_equation(self) -> None:
        result = self.extract(
            """<body><div><h1 id="screen-reader-main-title">Current AEP equation</h1>
            <section id="aep-section-id22"><h2>Methods</h2>
              <div>Before equation.<span><span><span>
                <span id="MathJax-Element-1-Frame" role="presentation">
                  <svg height="9.932ex" role="img" aria-hidden="true"></svg>
                  <span role="presentation"><math><mi>r</mi><mo>=</mo>
                    <msub><mi>K</mi><mn>1</mn></msub></math></span>
                </span>
              </span></span></span>After equation.</div>
              <div>Inline value <span id="MathJax-Element-2-Frame" role="presentation">
                <svg height="2.2ex" role="img" aria-hidden="true"></svg>
                <span role="presentation"><math><msub><mi>K</mi><mn>d</mn></msub>
                </math></span></span> remains prose.</div>
            </section></div></body>"""
        )

        methods = result.sections[0]
        self.assertEqual(
            [(block.kind, block.plain_text) for block in methods.blocks],
            [
                ("paragraph", "Before equation."),
                ("equation", "r = K_{1}"),
                ("paragraph", "After equation."),
                ("paragraph", "Inline value K_{d} remains prose."),
            ],
        )

    def test_current_aep_bib_note_reference_is_retained(self) -> None:
        result = self.extract(
            """<body><h1>Current AEP references</h1>
            <section><h2>References and notes</h2><ol><li>
              <span><a href="#bbib1" id="ref-id-bib1">1</a></span>
              <div><div>Authored experimental note with Na<sub>2</sub>SO<sub>4</sub>.</div></div>
            </li></ol></section></body>"""
        )

        self.assertEqual(len(result.references), 1)
        self.assertEqual(
            result.references[0].plain_text,
            "1. Authored experimental note with Na_{2}SO_{4}.",
        )

    def test_current_aep_bib_note_internal_id_can_differ_from_visible_number(self) -> None:
        result = self.extract(
            """<body><h1>Current AEP internal reference serials</h1>
            <section><h2>References</h2><ol>
            <li><span><a href="#bBIB10" id="ref-id-BIB10">4.</a></span>
              <span><div><div>A. Author</div></div><div>J. Test, 1 (2020), 1–2</div>
                <div lang="en"><a href="https://scopus.invalid/record">View in Scopus</a></div>
              </span></li>
            <li><span><a href="#bBIB11" id="ref-id-BIB11">5.</a></span>
              <div><div>Authored experimental note with Na<sub>2</sub>SO<sub>4</sub>.</div></div>
            </li>
            <li><span><a href="#bBIB14" id="ref-id-BIB14">8.</a></span>
              <span id="BIB15"><span>(a)</span><div><div>A. One</div></div>
                <div>J. One, 1 (2020), 1–2</div><div lang="en">
                <a href="https://scopus.invalid/one">View in Scopus</a></div></span>
              <span id="BIB16"><span>(b)</span><div><div>B. Two</div></div>
                <div>J. Two, 2 (2021), 3–4</div><div lang="en">
                <a href="https://scopus.invalid/two">View in Scopus</a></div></span>
            </li></ol></section></body>"""
        )

        self.assertEqual(len(result.references), 3)
        self.assertEqual(
            result.references[0].plain_text,
            "4. A. Author J. Test, 1 (2020), 1–2",
        )
        self.assertNotIn("Scopus", result.references[0].plain_text)
        self.assertEqual(
            result.references[1].plain_text,
            "5. Authored experimental note with Na_{2}SO_{4}.",
        )
        self.assertEqual(
            result.references[2].plain_text,
            "8. (a) A. One J. One, 1 (2020), 1–2 (b) B. Two J. Two, 2 (2021), 3–4",
        )
        self.assertNotIn("Scopus", result.references[2].plain_text)

    def test_current_aep_bracketed_reference_excludes_linkout_controls(self) -> None:
        result = self.extract(
            """<body><h1>Current AEP bracketed references</h1>
            <section id="references"><h2>References</h2><ol><li>
              <span><a href="#bbib1" id="ref-id-bib1"><span>[1]</span></a></span>
              <span><div><div>A. Author, B. Writer</div></div>
                <div><em>J. Test</em>, 1 (2020), pp. 1–2</div>
                <div lang="en"><a href="/reference.pdf">View PDF</a>
                  <a href="/reference">View article</a>
                  <a href="https://scopus.invalid/record">View in Scopus</a></div>
              </span>
            </li></ol></section></body>"""
        )

        self.assertEqual(len(result.references), 1)
        self.assertEqual(
            result.references[0].plain_text,
            "[1]. A. Author, B. Writer J. Test, 1 (2020), pp. 1–2",
        )
        self.assertNotIn("View PDF", result.references[0].plain_text)
        self.assertNotIn("View article", result.references[0].plain_text)
        self.assertNotIn("Scopus", result.references[0].plain_text)

    def test_current_aep_full_article_reference_href_excludes_linkout_controls(self) -> None:
        result = self.extract(
            """<body><h1>Current AEP full reference link</h1>
            <section><h2>References</h2><ol><li>
              <span><a href="https://www.sciencedirect.com/science/article/pii/S0123456789?via%3Dihub#bbib1"
                id="ref-id-bib1"><span>[1]</span></a></span>
              <span id="sref1"><div><div>A. Author</div>
                <div id="ref-id-sref1">Source-backed title</div></div>
                <div><em>J. Test</em>, 1 (2020), pp. 1–2</div>
                <div lang="en"><a href="/reference.pdf">View PDF</a>
                  <a href="https://scholar.invalid/record">Google Scholar</a></div>
              </span>
            </li></ol></section></body>"""
        )

        self.assertEqual(len(result.references), 1)
        self.assertEqual(
            result.references[0].plain_text,
            "[1]. A. Author Source-backed title J. Test, 1 (2020), pp. 1–2",
        )
        self.assertNotIn("View PDF", result.references[0].plain_text)
        self.assertNotIn("Google Scholar", result.references[0].plain_text)

    def test_screen_reader_title_excludes_allowlisted_article_type_badges(self) -> None:
        research = self.extract(
            """<body><h1 id="screen-reader-main-title">
            <div><span>Research paper</span></div>
            <span>Thermodynamics <em>and</em> binding</span>
            </h1><section><h2>Body</h2><p>Text.</p></section></body>"""
        )
        review = self.extract(
            """<body><h1 id="screen-reader-main-title">
            <div><span>Review article</span></div>
            <span>Nested <strong>review</strong> title</span>
            </h1><section><h2>Body</h2><p>Text.</p></section></body>"""
        )

        self.assertEqual(research.title, "Thermodynamics and binding")
        self.assertEqual(review.title, "Nested review title")

    def test_plain_and_rich_text_remove_source_space_before_punctuation(self) -> None:
        result = self.extract(
            """<body><h1>Whitespace normalization</h1>
            <section><h2>Acknowledgements</h2><p>Supported by
            <span>Example Foundation, United States</span> , at Example Lab.
            Value 3. 14 remains source text.</p></section></body>"""
        )

        block = result.sections[0].blocks[0]
        expected = (
            "Supported by Example Foundation, United States, at Example Lab. "
            "Value 3. 14 remains source text."
        )
        self.assertEqual(block.plain_text, expected)
        self.assertEqual(block.markdown, expected)

    def test_styled_identifier_closing_quote_is_not_a_nucleotide_prime(self) -> None:
        result = self.extract(
            "<body><h1>Quote and prime disambiguation</h1>"
            "<section><h2>Body</h2><p>Complex ‘Duplex 1: "
            "<strong><em>F1</em></strong>’ at 260 nm; DNA "
            "<strong>5</strong>’ end.</p>"
            "</section></body>"
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.plain_text,
            "Complex ‘Duplex 1: F1’ at 260 nm; DNA 5′ end.",
        )
        self.assertEqual(
            block.markdown,
            "Complex ‘Duplex 1: <strong><em>F1</em></strong>’ at 260 nm; "
            "DNA <strong>5</strong>′ end.",
        )

    def test_component_bibliography_links_restore_visible_superscripts(self) -> None:
        result = self.extract(
            """<body><h1>Component citations</h1><section><h2>Body</h2>
            <p>Same study.<a href="#h0075" name="bh0075"><span><span>
            6(b)</span></span></a>, <a href="#h0080" name="bh0080">
            <span><span>6(c)</span></span></a> and text.</p></section></body>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(
            block.markdown,
            "Same study.<sup>6(b)</sup>, <sup>6(c)</sup> and text.",
        )
        self.assertEqual(
            block.plain_text,
            "Same study.^{6(b)}, ^{6(c)} and text.",
        )

        near_miss = self.extract(
            """<body><h1>Ordinary local link</h1><section><h2>Body</h2>
            <p>See <a href="#h0075" name="ordinary">6(b)</a>.</p>
            </section></body>"""
        )
        self.assertEqual(near_miss.sections[0].blocks[0].plain_text, "See 6(b).")

        legacy_bib = self.extract(
            """<body><h1>Legacy Elsevier citations</h1><section><h2>Body</h2>
            <p>Prior work<a href="#bib1c" name="bbib1c"><span><span>
            1(c)</span></span></a>, <a href="#bib2" name="bbib2">
            <span><span>2</span></span></a>.</p></section></body>"""
        )
        self.assertEqual(
            legacy_bib.sections[0].blocks[0].markdown,
            "Prior work<sup>1(c)</sup>, <sup>2</sup>.",
        )
        self.assertEqual(
            legacy_bib.sections[0].blocks[0].plain_text,
            "Prior work^{1(c)}, ^{2}.",
        )

    def test_title_badge_filter_preserves_unknown_and_ordinary_nested_markup(self) -> None:
        ordinary_nested = self.extract(
            """<body><h1><span>Ordinary <em>in vivo</em> title</span></h1>
            <section><h2>Body</h2><p>Text.</p></section></body>"""
        )
        unknown_screen_reader = self.extract(
            """<body><h1 id="screen-reader-main-title"><div>Study subtitle</div>
            <span>Primary <em>title</em></span></h1>
            <section><h2>Body</h2><p>Text.</p></section></body>"""
        )
        ordinary_with_badge_text = self.extract(
            """<body><h1><div>Research paper</div>
            <span>Ordinary <em>title</em></span></h1>
            <section><h2>Body</h2><p>Text.</p></section></body>"""
        )

        self.assertEqual(ordinary_nested.title, "Ordinary in vivo title")
        self.assertEqual(unknown_screen_reader.title, "Study subtitle Primary title")
        self.assertEqual(ordinary_with_badge_text.title, "Research paper Ordinary title")

    def test_exact_table_of_contents_navigation_is_removed_without_losing_content(self) -> None:
        result = self.extract(
            """<body><h1>Navigation boundary</h1>
            <div aria-label="Table of contents" role="navigation">
              <h2>Outline</h2><ul><li>Highlights</li><li>Figures (1)</li>
              <li>Tables (1)</li></ul>
            </div>
            <section><h2>Highlights</h2><ul><li>Primary finding.</li></ul></section>
            <section><h2>Body</h2><p>Authored body text.</p>
              <figure id="f0005"><img src="https://example.invalid/f1.jpg">
                <figcaption>Fig. 1. Authored asset.</figcaption></figure>
              <div id="t0005"><p>Table 1. Authored table.</p>
                <table><tr><th>Measure</th><th>Value</th></tr>
                <tr><td>Binding</td><td>1</td></tr></table></div>
            </section>
            <section><h2>Outline</h2><p>Genuine authored outline.</p></section>
            <div aria-label="Table of contents" role="region">
              <h2>Near-miss navigation</h2><p>Must remain.</p>
            </div></body>"""
        )

        headings = [section.heading for section in result.sections]
        all_text = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertNotIn("Figures (1)", all_text)
        self.assertNotIn("Tables (1)", all_text)
        self.assertEqual(headings.count("Outline"), 1)
        self.assertIn("Highlights", headings)
        self.assertIn("Body", headings)
        self.assertIn("Near-miss navigation", headings)
        self.assertIn("Primary finding.", all_text)
        self.assertIn("Authored body text.", all_text)
        self.assertIn("Genuine authored outline.", all_text)
        self.assertIn("Must remain.", all_text)
        self.assertEqual([item.label for item in result.figures], ["Figure 1"])
        self.assertEqual(len(result.tables), 1)

    def test_current_summary_multiaffiliations_and_short_copyright(self) -> None:
        result = self.extract(
            """<body>
            <div><span>Date: </span>5 November 2026</div>
            <div><span>Article: </span>119089</div>
            <div><span>Volume: </span><a>Volume 317</a></div>
            <h1 id="screen-reader-main-title"><div>Review article</div>
              <span>Current ScienceDirect article</span></h1>
            <div id="author-group">
              <span><span>Alice Example</span><span id="baff1"><sup>a</sup></span>
                <span id="baff2"><sup>b</sup></span></span>
              <span><span>Bob Example</span><span id="baff2"><sup>b</sup></span></span>
            </div>
            <section><h2>Body</h2><p>Text.</p></section>
            <div>© 2026 Example Publisher. All rights reserved.</div>
            </body>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "article_number": "119089",
                "date": "5 November 2026",
                "volume": "317",
            },
        )
        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Article category: Review article",
                "Affiliation assignments: Alice Example (a,b); Bob Example (b)",
                "Copyright: © 2026 Example Publisher. All rights reserved.",
            ],
        )

    def test_current_summary_singular_page_range_is_bibliographic(self) -> None:
        result = self.extract(
            """<body>
            <div><span>Date:</span>September 2001</div>
            <div><span>Page</span>2215-2235</div>
            <div><span>Volume:</span><a>Volume 9, Issue 9</a></div>
            <h1 id="screen-reader-main-title">Legacy review</h1>
            <section><h2>Body</h2><p>Text.</p></section>
            </body>"""
        )

        self.assertEqual(
            result.bibliographic,
            {"issue": "9", "pages": "2215-2235", "volume": "9"},
        )

    def test_publication_banner_retains_month_year_issue_date(self) -> None:
        result = self.extract(
            """<body><div id="publication"><h2>Methods</h2>
            <div><a>Volume 225</a>, May 2024, Pages 20-27</div></div>
            <h1 id="screen-reader-main-title">Month-year article</h1>
            <section><h2>Body</h2><p>Text.</p></section></body>"""
        )

        self.assertEqual(
            result.bibliographic,
            {"date": "May 2024", "pages": "20-27", "volume": "225"},
        )

    def test_concatenated_journal_title_is_not_misread_as_issue_date(self) -> None:
        result = self.extract(
            """<body><div id="publication"><h2>Gene Regulatory Mechanisms</h2>
            <div><a>Volume 1860, Issue 5</a>, May 2017, Pages 617-629</div></div>
            <h1 id="screen-reader-main-title">Issue article</h1>
            <section><h2>Body</h2><p>Text.</p></section></body>"""
        )

        self.assertEqual(
            result.bibliographic,
            {
                "date": "May 2017",
                "issue": "5",
                "pages": "617-629",
                "volume": "1860",
            },
        )

    def test_definition_list_abbreviations_keep_terms_and_definitions(self) -> None:
        result = self.extract(
            """<body><h1>Abbreviation pairs</h1>
            <section><h2>List of abbreviations</h2>
              <ul id="dlist0010">
                <dt>NF-κB</dt><dd><div id="p0010">nuclear factor kappa B</div></dd>
                <dt>MGBs</dt><dd><div id="p0015">minor groove binders</div></dd>
                <dt>AML:</dt><dd><div id="p0020">acute myeloid leukemia</div></dd>
              </ul>
            </section><section><h2>Body</h2><p>Text.</p></section></body>"""
        )

        section = result.sections[0]
        self.assertEqual(len(section.blocks), 1)
        self.assertEqual(section.blocks[0].kind, "list")
        self.assertEqual(
            section.blocks[0].plain_text,
            "- NF-κB: nuclear factor kappa B\n"
            "- MGBs: minor groove binders\n"
            "- AML: acute myeloid leukemia",
        )
        self.assertEqual(
            section.blocks[0].markdown,
            "- <strong>NF-κB</strong>: nuclear factor kappa B\n"
            "- <strong>MGBs</strong>: minor groove binders\n"
            "- <strong>AML:</strong> acute myeloid leukemia",
        )

    def test_table_cell_lists_retain_readable_item_boundaries(self) -> None:
        result = self.extract(
            """<body><h1>Table list cells</h1><section><h2>Results</h2>
            <div id="tbl1"><div id="cap0010"><p id="tspara0005">Table 1.
            Outcomes.</p></div><div><table>
              <thead><tr><th>Agent</th><th>Outcomes</th></tr></thead>
              <tbody><tr><td>Compound A</td><td><ul>
                <li><span>•</span><span><div>First outcome</div></span></li>
                <li><span>•</span><span><div>Second outcome</div></span></li>
              </ul></td></tr></tbody>
            </table></div><div><div id="tspara0010">Abbreviations: ATP,
            adenosine triphosphate.</div></div></div></section></body>"""
        )

        cell = result.tables[0].parts[0].rows[1][1]
        self.assertEqual(cell.text, "• First outcome\n• Second outcome")
        self.assertEqual(cell.markdown, "• First outcome<br>• Second outcome")
        self.assertEqual(
            result.tables[0].footnotes_plain,
            ["Abbreviations: ATP, adenosine triphosphate."],
        )

    def test_legacy_sp_table_note_is_not_confused_with_sp_caption(self) -> None:
        result = self.extract(
            """<body><h1>Legacy ScienceDirect table note</h1>
            <section><h2>Results</h2><div id="t0005">
              <span><span id="cn0085"><p id="sp0040">Table 1. Results of
              <i>T</i><sub>m</sub> analyses</p></span></span>
              <div><table><thead><tr><th>Compound</th><th>Value</th></tr></thead>
              <tbody><tr><td>1</td><td>27.8</td></tr></tbody></table></div>
              <div><div id="sp0045">ΔΔ<i>T</i><sub>m</sub> =
              Δ<i>T</i><sub>m</sub>(match) − Δ<i>T</i><sub>m</sub>(mismatch).
              </div></div>
            </div></section></body>"""
        )

        table = result.tables[0]
        self.assertEqual(table.title_plain, "Table 1. Results of T_{m} analyses")
        self.assertEqual(
            table.footnotes_plain,
            ["ΔΔT_{m} = ΔT_{m}(match) − ΔT_{m}(mismatch)."],
        )

    def test_legacy_aep_unmarked_condition_before_definition_notes_is_retained(self) -> None:
        result = self.extract(
            """<body><h1>Legacy AEP table condition</h1>
            <section><h2>Results</h2><div id="tbl1">
              <span><p><span>Table 1</span>. Photophysical data</p></span>
              <div><table><thead><tr><th>Compound</th><th>Value</th></tr></thead>
              <tbody><tr><td>1</td><td>27.8</td></tr></tbody></table></div>
              <div><div>[<strong>1</strong>] = 100 μM in DMF.</div></div>
              <dl><dt id="tblfn1">a</dt><dd><div>Longest wavelength.</div></dd></dl>
            </div></section></body>"""
        )

        table = result.tables[0]
        self.assertEqual(
            table.footnotes_plain,
            ["[1] = 100 μM in DMF.", "[a] Longest wavelength."],
        )

    def test_table_scoped_figures_remain_table_assets_not_article_figures(self) -> None:
        png = base64.b64encode(PNG_BYTES).decode("ascii")
        jpeg = base64.b64encode(JPEG_BYTES).decode("ascii")
        result = self.extract(
            f"""<body><h1>Table graphics</h1><section><h2>Results</h2>
            <figure id="f0005"><img src="data:image/jpeg;base64,{jpeg}">
              <figcaption>Fig. 1. Authored article figure.</figcaption></figure>
            <div id="t0005"><span id="cn0005"><p>Table 1. Values.
              <span><figure id="f0005"><img src="data:image/png;base64,{png}">
              <a download="" title="Download high-res image (2KB)">Download:
              Download high-res image (2KB)</a></figure></span></p></span>
              <table><tr><td rowspan="2"><span>Empty Cell</span></td>
                <th>Structure</th><th>Value</th></tr>
                <tr><td><figure id="equation-1"><img
                src="data:image/png;base64,{png}"></figure></td><td>2</td></tr>
                <tr><td>Empty Cell</td><td>3</td></tr>
              </table>
              <div><figure id="equation-2"><img
              src="data:image/png;base64,{png}"></figure></div>
            </div></section></body>"""
        )

        self.assertEqual(
            [(figure.figure_id, figure.label) for figure in result.figures],
            [("figure_001", "Figure 1")],
        )
        table = result.tables[0]
        self.assertEqual(table.title_plain, "Table 1. Values.")
        self.assertNotIn("Download", table.title_plain)
        self.assertEqual(table.parts[0].rows[0][0].text, "")
        self.assertEqual(table.parts[0].rows[0][0].markdown, "")
        self.assertEqual(table.parts[0].rows[2][0].text, "Empty Cell")
        self.assertEqual(
            [asset.asset_id for asset in result.embedded_assets],
            [
                "figure_001",
                "table_001_part_01_row_002_column_001",
                "table_001_context_001",
                "table_001_context_002",
            ],
        )
        self.assertTrue(
            all(
                asset.parent_table_id == "table_001"
                for asset in result.embedded_assets[1:]
            )
        )

    def test_non_spanning_empty_cell_corner_placeholder_is_not_authored_text(self) -> None:
        result = self.extract(
            """<body><h1>Table corner placeholder</h1><section><h2>Results</h2>
            <div id="t0005"><span id="ca0005"><p><span>Table 1</span>.
            Values.</p></span><table><thead><tr>
              <td><span>Empty Cell</span></td><td><span>Empty Cell</span></td>
              <th>Target A</th><th>Target B</th>
            </tr></thead><tbody><tr><th>Compound 1</th><td>2</td><td>3</td>
            </tr><tr><td>Empty Cell</td><td>4</td><td>5</td></tr></tbody>
            </table></div></section></body>"""
        )

        table = result.tables[0]
        self.assertEqual(table.parts[0].rows[0][0].text, "")
        self.assertEqual(table.parts[0].rows[0][0].markdown, "")
        self.assertEqual(table.parts[0].rows[0][1].text, "")
        self.assertEqual(table.parts[0].rows[0][1].markdown, "")
        self.assertEqual(table.parts[0].rows[2][0].text, "Empty Cell")

    def test_legacy_ca_table_caption_controls_number_not_internal_id(self) -> None:
        result = self.extract(
            """<body><article><h1>Legacy ScienceDirect table</h1>
            <section><h2>Results</h2><div id="t0005"><span><span id="ca0045">
              <p id="sp0045"><span>Table 1</span>. Thermal values.</p>
            </span></span><table><tr><th>Agent</th><th>Value</th></tr>
              <tr><td>A</td><td>3</td></tr></table>
              <dl><dt id="tf0005">a</dt><dd>Previously reported.</dd></dl>
            </div></section></article></body>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual((table.table_id, table.label), ("table_001", "Table 1"))
        self.assertEqual(table.title_plain, "Table 1. Thermal values.")
        self.assertEqual(table.footnotes_plain, ["[a] Previously reported."])

    def test_current_t_table_uses_figcaption_number_and_file_card_is_not_figure(self) -> None:
        result = self.extract(
            """<body><article><h1>Current ScienceDirect table</h1>
            <section><h2>Results</h2><figure id="t0010"><div><table>
              <thead><tr><th>Agent</th><th>Value</th></tr></thead>
              <tbody><tr><td>Compound 2</td><td>3.2</td></tr></tbody>
            </table></div><figcaption>
              <div><span>Table 1</span><div id="sp0045" role="paragraph">
                Authored values for <b>2</b></div></div>
              <div><div id="sp0050" role="paragraph">Reviewed table note.</div></div>
              <div><ul><li><a href="/action/showFullTableHTML?isHtml=true&amp;tableId=t0010&amp;pii=S0000">
                Open table in a new tab</a></li></ul></div>
            </figcaption></figure></section>
            <section id="app-1"><h2>Supplementary material</h2>
              <figure id="mmc1"><div><a
                href="/cms/10.1000/example/asset/123/main.assets/mmc1.doc"
                download="mmc1.doc">Download application (doc, 86 KB)</a></div>
                <figcaption><span>Supplementary Tables S1–S3</span></figcaption>
              </figure></section></article></body>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(
            (table.table_id, table.source_id, table.label),
            ("table_001", "t0010", "Table 1"),
        )
        self.assertEqual(table.title_plain, "Table 1. Authored values for 2")
        self.assertEqual(table.footnotes_plain, ["Reviewed table note."])
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [["Agent", "Value"], ["Compound 2", "3.2"]],
        )
        self.assertEqual(result.figures, [])

    def test_legacy_direct_sp_table_caption_controls_internal_id(self) -> None:
        result = self.extract(
            """<body><article><h1>Direct SP ScienceDirect table</h1>
            <section><h2>Results</h2><div id="t0010"><span><span>
              <p id="sp0040"><span>Table 1</span>. Binding values for
              <strong>1</strong> and <strong>2</strong></p>
            </span></span><div><table><thead><tr><th>Agent</th><th>Value</th>
            </tr></thead><tbody><tr><td>1</td><td>3</td></tr></tbody></table>
            </div><dl><dt id="tblfn1">a</dt><dd>Reviewed note.</dd></dl>
            </div></section></article></body>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(
            (table.table_id, table.source_id, table.label),
            ("table_001", "t0010", "Table 1"),
        )
        self.assertEqual(
            table.title_plain, "Table 1. Binding values for 1 and 2"
        )
        self.assertEqual(
            table.title_markdown,
            "Table 1. Binding values for <strong>1</strong> and <strong>2</strong>",
        )
        self.assertEqual(table.footnotes_plain, ["[a] Reviewed note."])

    def test_current_aep_identifier_light_table_caption_is_complete(self) -> None:
        result = self.extract(
            """<body><h1>Current AEP table</h1>
            <section id="aep-section-id20"><h2>Results</h2>
              <div id="tbl1"><span><span><p><span>Table 1</span>. Binding
                values for compound <strong>2</strong></p></span></span>
                <div><table><thead><tr><th>DNA</th><th>Value</th></tr></thead>
                <tbody><tr><td>ATCGAT</td><td>5 × 10<sup>5</sup></td></tr>
                </tbody></table></div>
                <div><div>Mean values are from three experiments.</div></div>
              </div>
            </section></body>"""
        )

        self.assertEqual(len(result.tables), 1)
        self.assertEqual(
            result.tables[0].title_plain,
            "Table 1. Binding values for compound 2",
        )
        self.assertEqual(
            result.tables[0].title_markdown,
            "Table 1. Binding values for compound <strong>2</strong>",
        )
        self.assertEqual(
            result.tables[0].footnotes_plain,
            ["Mean values are from three experiments."],
        )

    def test_current_aep_unheaded_article_body_table_caption_is_complete(self) -> None:
        result = self.extract(
            """<body><article><h1>Current AEP unheaded table</h1>
            <div id="body"><div id="tbl1"><span><span><p><span>Table 1</span>.
              Melting temperatures<sup>a</sup> (<i>T</i><sub>m</sub> in °C)
            </p></span></span><div><table><thead><tr><th>DNA</th><th>Value</th>
            </tr></thead><tbody><tr><td>ATCGAT</td><td>65</td></tr></tbody>
            </table></div></div></div></article></body>"""
        )

        self.assertEqual(len(result.tables), 1)
        self.assertEqual(
            result.tables[0].title_plain,
            "Table 1. Melting temperatures^{a} (T_{m} in °C)",
        )
        self.assertEqual(
            result.tables[0].title_markdown,
            "Table 1. Melting temperatures<sup>a</sup> (<em>T</em><sub>m</sub> in °C)",
        )

    def test_authenticated_unheaded_aep_table_keeps_note_and_mmultiscripts(self) -> None:
        source = """<body><article><h1 id="screen-reader-main-title">AEP table</h1>
          <div id="article-identifier-links"><a
            href="https://doi.org/10.1016/j.synthetic.2026.13">DOI</a></div>
          <div id="body"><div><div id="tbl1">
            <span><span><p><span>Table 1</span>. Charged structures</p></span></span>
            <div><table><thead><tr><th>Group</th><th>Value</th></tr></thead>
              <tbody><tr><td><span id="MathJax-Element-1-Frame" role="presentation">
                <svg role="img" aria-hidden="true"></svg><span role="presentation"><math>
                <mmultiscripts><mrow><mtext>NH</mtext></mrow><mrow><mn>3</mn></mrow>
                <none/><none/><mrow><mo>+</mo></mrow></mmultiscripts>
                </math></span></span></td><td>1</td></tr></tbody></table></div>
            <div><div>Mean values are from three experiments.</div></div>
          </div></div></div>
          <section id="aep-bibliography-id43"><h2>References and notes</h2></section>
        </article></body>"""
        result = self.extract(source)

        table = result.tables[0]
        self.assertEqual(table.parts[0].rows[1][0].text, "NH_{3}^{+}")
        self.assertEqual(
            table.parts[0].rows[1][0].markdown,
            "NH<sub>3</sub><sup>+</sup>",
        )
        self.assertEqual(
            table.footnotes_plain,
            ["Mean values are from three experiments."],
        )

        near_miss = self.extract(
            source.replace(
                '<div id="article-identifier-links"><a\n            href="https://doi.org/10.1016/j.synthetic.2026.13">DOI</a></div>',
                "",
            )
        )
        self.assertEqual(near_miss.tables[0].footnotes_plain, [])

    def test_prefixed_legacy_table_container_is_extracted(self) -> None:
        result = self.extract(
            """<body><article><h1>Prefixed ScienceDirect table</h1>
            <section><h2>Results</h2>
            <div id="feb4s2211546314000278-t0005">
              <header><span>Table 1. </span>Down-regulated genes</header>
              <div><table><thead><tr><th>FDR (%)</th><th>Gene symbol</th>
              </tr></thead><tbody><tr><td>0.40</td><td>MALAT1</td></tr>
              </tbody></table></div>
            </div></section></article></body>"""
        )

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(
            (table.table_id, table.source_id, table.label),
            ("table_001", "feb4s2211546314000278-t0005", "Table 1"),
        )
        self.assertEqual(table.title_plain, "Table 1. Down-regulated genes")
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [["FDR (%)", "Gene symbol"], ["0.40", "MALAT1"]],
        )

    def test_legacy_reference_line_wraps_do_not_split_panel_or_parenthesis(self) -> None:
        result = self.extract(
            """<body><article><h1>ScienceDirect reference wraps</h1>
            <section><h2>Results</h2><p>Shown in
            <a href="#feb4s2211546314000278-fig-0005">Fig. 1</a>
            A and B, and summarized in
            (<a href="#feb4s2211546314000278-t0005">Table 1</a>
            ). A separate <a href="https://example.invalid">link</a>
            A representative sentence remains spaced.</p></section>
            <figure id="feb4s2211546314000278-fig-0005"><img
            src="https://example.invalid/f1.jpg"><figcaption>Fig. 1. Panels.</figcaption>
            </figure><div id="feb4s2211546314000278-t0005"><header>Table 1.
            Values.</header><table><tr><th>Value</th></tr><tr><td>1</td></tr>
            </table></div></article></body>"""
        )

        text = result.sections[0].blocks[0].plain_text
        self.assertIn("Fig. 1A and B", text)
        self.assertIn("(Table 1).", text)
        self.assertIn("link A representative", text)

    def test_source_rendered_display_equations_keep_order_and_notation(self) -> None:
        result = self.extract(
            """<body><h1>Equations</h1><section><h2>Methods</h2>
            <div id="p0001">Before equations.
              <span id="ufd1"><span>D<sub>f</sub> = P<sub>f</sub><sup>nˆH</sup> + (1</span></span>
              <span id="ufd2"><span>Fraction bound = ((1/K<sub>d,app</sub>)*(P<sub>f</sub><sup>nˆH</sup>))/(1 + x)</span></span>
              After equations.</div>
            <div id="p0002">Inline A<sub>x</sub><sup>2</sup> remains prose;
              <span id="ufdx">not a display equation</span>.
              <span id="ufd3"></span><div id="ufd4">not a span</div></div>
            </section></body>"""
        )

        methods = result.sections[0]
        self.assertEqual(
            [(block.kind, block.plain_text) for block in methods.blocks],
            [
                ("paragraph", "Before equations."),
                ("equation", "D_{f} = P_{f}^{nˆH} + (1"),
                (
                    "equation",
                    "Fraction bound = ((1/K_{d,app})*(P_{f}^{nˆH}))/(1 + x)",
                ),
                ("paragraph", "After equations."),
                (
                    "paragraph",
                    "Inline A_{x}^{2} remains prose; not a display equation. not a span",
                ),
            ],
        )
        self.assertEqual(
            methods.blocks[1].markdown,
            "D<sub>f</sub> = P<sub>f</sub><sup>nˆH</sup> + (1",
        )
        self.assertEqual(
            methods.blocks[2].markdown,
            "Fraction bound = ((1/K<sub>d,app</sub>)\\*(P<sub>f</sub><sup>nˆH</sup>))/(1 + x)",
        )

    def test_mathml_spacing_and_reaction_arrow_limits_remain_explicit(self) -> None:
        result = self.extract(
            """<body><h1>Reaction kinetics</h1><section><h2>Methods</h2>
            <div id="p0001">A reaction follows.
              <span><span id="e0005"><span><math><mrow>
                <mtext>A</mtext><mo>+</mo><mtext>B</mtext><mspace width="0.35em"/>
                <munderover><mo>⇆</mo>
                  <msub><mi>k</mi><mrow><mi>d</mi><mn>1</mn></mrow></msub>
                  <msub><mi>k</mi><mrow><mi>a</mi><mn>1</mn></mrow></msub>
                </munderover><mspace width="0.35em"/><mtext>AB</mtext>
                <mspace width="1em"/><mtext>A</mtext><mo>+</mo><mtext>AB</mtext>
              </mrow></math></span></span></span>
            </div></section></body>"""
        )

        equation = next(
            block
            for block in result.sections[0].blocks
            if block.kind == "equation"
        )
        self.assertEqual(
            equation.plain_text,
            "A+B ⇆_{k_{d1}}^{k_{a1}} AB A+AB",
        )
        self.assertEqual(
            equation.markdown,
            "A+B ⇆<sub>k<sub>d1</sub></sub><sup>k<sub>a1</sub></sup> AB A+AB",
        )

    def test_rendered_dom_is_extracted_as_structured_primary_content(self) -> None:
        png = base64.b64encode(PNG_BYTES).decode("ascii")
        jpeg = base64.b64encode(JPEG_BYTES).decode("ascii")
        result = self.extract(
            f"""<body>
<h2>Publisher banner</h2>
<div><span>Date: </span>15 May 2018</div>
<div><span>Pages: </span>2337-2344</div>
<div><a>Volume 26, Issue 9</a></div>
<h1>Hydrophobic <em>in vivo</em> study</h1>
<div id="abstracts">
  <div><h2>Highlights</h2><ul><li><span>•</span><span>Primary finding.</span></li></ul></div>
  <div><h2>Abstract</h2><div id="as001"><div id="sp001">
    Compound <strong>1</strong> retained <em>in vivo</em>.
  </div></div></div>
  <div><h2>Graphical abstract</h2><figure id="f0030">
    <img src="data:image/png;base64,{png}" alt="">
  </figure></div>
  <div><h2>Keywords</h2><div id="k0005">Polyamide</div><span>; </span>
    <div id="k0010">Hydrophobicity</div></div>
</div>
<ul><li><a href="/science/article/pii/previous">Previous article in this issue</a></li>
    <li><a href="/science/article/pii/next">Next article in this issue</a></li></ul>
<div id="body">
  <section><h2>1. Introduction</h2><div id="p0020">
    Prior work<a href="#b0010"><span><span>2</span></span></a> and
    another source<a href="#b0015"><span><span><sup>3</sup></span></span></a>.
    Fluorescence quantum yield (<em>ϕ<sub>f</sub></em>) was calculated as
    <math><mrow>
      <msub><mo>∅</mo><mi>f</mi></msub><mo>=</mo>
      <msub><mo>∅</mo><mi>s</mi></msub><mo>×</mo>
      <mfenced><mfrac>
        <mrow><msub><mi>F</mi><mi>x</mi></msub><mo>/</mo><msub><mi>A</mi><mi>x</mi></msub></mrow>
        <mrow><msub><mi>F</mi><mi>s</mi></msub><mo>/</mo><msub><mi>A</mi><mi>s</mi></msub></mrow>
      </mfrac></mfenced>
    </mrow></math>.
  </div><div id="p0021">A terminal display equation follows.
    <span><span id="e0005"><span><math><mrow>
      <msub><mi>F</mi><mi>x</mi></msub><mo>=</mo><mn>2</mn>
    </mrow></math></span></span>
    <span id="e0010"><span>(2) </span><span id="e0015"><math><mrow>
      <mi>x</mi><mo>&lt;</mo><mn>3</mn>
    </mrow></math></span></span> after equations.</span>
  </div></section>
  <section><h2>2. Results</h2>
    <section><h3>2.1. <em>In vivo</em> measurements</h3><div id="p0025">β-form was observed.</div>
      <section><h4>2.1.1. Controls</h4><div id="p0030">Control text.</div></section>
    </section>
    <div id="p0031">Parent text after the nested section.</div>
    <figure id="f0005"><img src="data:image/jpeg;base64,{jpeg}" alt="">
      <span><span id="cn0005"><p><span>Fig. 1</span>. Response <em>in vivo</em>
        (<sup>**</sup><em>p</em> &lt; 0.01).</p></span></span>
    </figure>
    <figure id="f0010"><img src="https://example.invalid/scheme.jpg" alt="">
      <span><span id="cn0010"><p><span>Scheme 2</span>. Remote scheme.</p></span></span>
    </figure>
    <div id="t0005"><span><span id="cn0030"><p><span>Table 1</span>.
      Partition coefficients (log<em>P<sub>OW</sub></em>).</p></span></span>
      <table><thead><tr><th>Compound</th><th>log<em>P<sub>OW</sub></em></th></tr></thead>
      <tbody><tr><td><strong>1</strong></td><td>1.5</td></tr></tbody></table>
    </div>
  </section>
  <section><h2>A. Supplementary data</h2><div id="p0900">
    <span><a href="https://example.invalid/mmc1.pptx">Download Powerpoint document</a>
      <span><p>Supplementary Fig. SI1.</p></span></span>
  </div></section>
  <section id="article-references"><h2>References</h2><ol><li>
    <span><a href="#bb0005" id="ref-id-b0005"><span>1</span></a></span>
    <span id="h0005"><div><div>A. Author, B. Writer</div></div>
      <div id="ref-id-h0005"><em>J Test</em>, 1 (2020), pp. 1-2</div>
      <div lang="en"><a href="https://doi.org/10.1234/example">Crossref</a>
        <a href="https://scopus.invalid/record">View in Scopus</a></div>
    </span>
  </li></ol></section>
</div></body>"""
        )

        self.assertEqual(result.title, "Hydrophobic in vivo study")
        self.assertEqual(
            result.bibliographic,
            {
                "date": "15 May 2018",
                "issue": "9",
                "pages": "2337-2344",
                "volume": "26",
            },
        )
        self.assertEqual(
            [(section.heading, section.level) for section in result.sections],
            [
                ("Highlights", 2),
                ("Abstract", 2),
                ("Keywords", 2),
                ("1. Introduction", 2),
                ("2. Results", 2),
                ("2.1. <em>In vivo</em> measurements", 3),
                ("2.1.1. Controls", 4),
            ],
        )
        self.assertEqual(result.sections[0].blocks[0].markdown, "- Primary finding.")
        self.assertEqual(result.sections[0].blocks[0].plain_text, "- Primary finding.")
        self.assertIn("<strong>1</strong>", result.sections[1].blocks[0].markdown)
        self.assertEqual(
            [block.plain_text for block in result.sections[2].blocks],
            ["Polyamide", "Hydrophobicity"],
        )
        all_body_text = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertNotIn("Previous article", all_body_text)
        introduction = next(
            section for section in result.sections if section.heading == "1. Introduction"
        )
        self.assertIn(
            "∅<sub>f</sub> = ∅<sub>s</sub>", introduction.blocks[0].markdown
        )
        self.assertIn("F<sub>x</sub>", introduction.blocks[0].markdown)
        self.assertIn("work<sup>2</sup>", introduction.blocks[0].markdown)
        self.assertIn("source<sup>3</sup>", introduction.blocks[0].markdown)
        self.assertIn("work^{2}", introduction.blocks[0].plain_text)
        self.assertIn("source^{3}", introduction.blocks[0].plain_text)
        self.assertEqual(
            [(block.block_id, block.kind) for block in introduction.blocks[1:]],
            [
                ("main-paragraph-0006", "paragraph"),
                ("equation-001", "equation"),
                ("equation-002", "equation"),
                ("main-paragraph-0007", "paragraph"),
            ],
        )
        self.assertEqual(introduction.blocks[2].plain_text, "F_{x} = 2")
        self.assertEqual(introduction.blocks[2].markdown, "F<sub>x</sub> = 2")
        self.assertEqual(introduction.blocks[3].plain_text, "(2) x < 3")
        self.assertEqual(introduction.blocks[3].markdown, "(2) x &lt; 3")
        self.assertEqual(introduction.blocks[4].plain_text, "after equations.")

        results = next(
            section for section in result.sections if section.heading == "2. Results"
        )
        controls = next(
            section for section in result.sections if section.heading == "2.1.1. Controls"
        )
        self.assertEqual(
            [block.plain_text for block in results.blocks],
            ["Parent text after the nested section."],
        )
        self.assertEqual(
            [block.plain_text for block in controls.blocks],
            ["Control text."],
        )

        self.assertEqual(
            [(item.figure_id, item.kind, item.label) for item in result.figures],
            [
                ("graphical_abstract", "graphical_abstract", "Graphical Abstract"),
                ("figure_001", "figure", "Figure 1"),
                ("scheme_002", "scheme", "Scheme 2"),
            ],
        )
        self.assertIn("Response <em>in vivo</em>", result.figures[1].caption_markdown)
        self.assertIn(r"<sup>\*\*</sup>", result.figures[1].caption_markdown)
        self.assertEqual(
            [asset.asset_id for asset in result.embedded_assets],
            ["graphical_abstract", "figure_001"],
        )
        self.assertEqual(result.embedded_assets[0].data, PNG_BYTES)
        self.assertEqual(
            result.embedded_assets[0].output_path,
            "figures/main/graphical_abstract.png",
        )
        self.assertEqual(result.embedded_assets[1].data, JPEG_BYTES)
        self.assertEqual(result.embedded_assets[1].output_path, "figures/main/figure_001.jpg")
        self.assertFalse(result.embedded_assets[1].ocr_performed)

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual((table.table_id, table.source_id), ("table_001", "t0005"))
        self.assertIn("<em>P<sub>OW</sub></em>", table.title_markdown)
        self.assertEqual(
            [[cell.text for cell in row] for row in table.parts[0].rows],
            [["Compound", "logP_{OW}"], ["1", "1.5"]],
        )
        self.assertEqual(
            [block.plain_text for block in result.supporting_information],
            ["Supplementary Fig. SI1."],
        )
        self.assertNotIn("Download", result.supporting_information[0].plain_text)

        self.assertEqual(len(result.references), 1)
        reference = result.references[0]
        self.assertIn("A. Author, B. Writer", reference.plain_text)
        self.assertIn("https://doi.org/10.1234/example", reference.markdown)
        self.assertNotIn("Crossref", reference.markdown)
        self.assertNotIn("Scopus", reference.markdown)
        warning = next(
            warning
            for warning in result.warnings
            if warning["code"] == "possible_publisher_math_symbol_substitution"
        )
        self.assertIn("retained unchanged", warning["message"])

    def test_graphical_abstract_image_controls_are_not_authored_prose(self) -> None:
        png = base64.b64encode(PNG_BYTES).decode("ascii")
        graphical_abstract = f"""<div id="ab005" lang="en">
          <h2>Graphical abstract</h2><div id="as005"><div id="sp0005">
          <span><figure id="f0040"><span><img
            src="data:image/png;base64,{png}" alt="">
          <ol><li><a href="https://ars.els-cdn.com/content/image/article-fx1_lrg.jpg"
            target="_blank" download="" title="Download high-res image (169KB)">
            <span>Download: <span>Download high-res image (169KB)</span></span></a></li>
          <li><a href="https://ars.els-cdn.com/content/image/article-fx1.jpg"
            target="_blank" download="" title="Download full-size image">
            <span>Download: <span>Download full-size image</span></span></a></li></ol>
          </span></figure></span></div></div></div>"""
        source = f"""<body><article><h1>Graphical abstract controls</h1>
        <div id="abstracts"><div id="ab010"><h2>Abstract</h2>
          <div id="as010"><div id="sp0010">Authored abstract.</div></div></div>
          {graphical_abstract}</div>
        <section><h2>Results</h2><p>Authored result.</p></section>
        </article></body>"""

        result = self.extract(source)

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Results"],
        )
        self.assertEqual(
            [block.block_id for section in result.sections for block in section.blocks],
            ["main-paragraph-0001", "main-paragraph-0002"],
        )
        self.assertEqual(
            [(figure.figure_id, figure.kind) for figure in result.figures],
            [("graphical_abstract", "graphical_abstract")],
        )

        authored = self.extract(
            source.replace(
                '<div id="sp0005">',
                '<div id="sp0005">Distinct authored graphical summary. ',
                1,
            )
        )
        graphical_section = next(
            section
            for section in authored.sections
            if section.heading == "Graphical abstract"
        )
        self.assertEqual(
            [block.plain_text for block in graphical_section.blocks],
            ["Distinct authored graphical summary."],
        )
        self.assertNotIn("Download", graphical_section.blocks[0].plain_text)

    def test_legacy_image_controls_without_download_attribute_are_not_prose(self) -> None:
        png = base64.b64encode(PNG_BYTES).decode("ascii")
        result = self.extract(
            f"""<body><article><h1>Legacy graphical abstract controls</h1>
            <div id="abstracts"><div id="ab005"><h2>Graphical abstract</h2>
            <div id="as005"><div id="sp0005"><figure id="f0040"><span>
              <img src="data:image/png;base64,{png}" alt="">
              <ol><li><a href="https://ars.els-cdn.com/content/image/article-fx1_lrg.jpg"
                title="Download high-res image (169KB)"><span>Download:
                <span>Download high-res image (169KB)</span></span></a></li>
              <li><a href="https://ars.els-cdn.com/content/image/article-fx1.jpg"
                title="Download full-size image"><span>Download:
                <span>Download full-size image</span></span></a></li></ol>
            </span></figure></div></div></div></div>
            <section><h2>Results</h2><p>Authored result.</p></section>
            </article></body>"""
        )

        self.assertEqual(
            [block.plain_text for section in result.sections for block in section.blocks],
            ["Authored result."],
        )
        self.assertEqual(
            [(figure.figure_id, figure.kind) for figure in result.figures],
            [("graphical_abstract", "graphical_abstract")],
        )

    def test_legacy_split_caption_and_nested_glyphs_stay_with_outer_figure(self) -> None:
        png = base64.b64encode(PNG_BYTES).decode("ascii")
        result = self.extract(
            f"""<body><article><h1>Legacy split caption</h1>
            <section><h2>Results</h2><p>Body.</p>
            <figure id="f0030"><span>
              <img src="data:image/png;base64,{png}" alt="Fig. 6">
              <ol><li><a href="https://ars.els-cdn.com/content/image/article-gr6_lrg.jpg"
                title="Download high-res image (605KB)"><span>Download:
                <span>Download high-res image (605KB)</span></span></a></li>
              <li><a href="https://ars.els-cdn.com/content/image/article-gr6.jpg"
                title="Download full-size image"><span>Download:
                <span>Download full-size image</span></span></a></li></ol>
            </span><span><span id="ca0030">
              <p id="sp0030"><span>Fig. 6</span>. First panel with
                <figure><img src="data:image/png;base64,{png}" alt="legend glyph"></figure>.</p>
              <div id="sp0035"><strong>(b)</strong> Complete second panel.</div>
            </span></span></figure></section></article></body>"""
        )

        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].figure_id, "figure_006")
        self.assertEqual(
            result.figures[0].caption_plain,
            "First panel with. (b) Complete second panel.",
        )

    def test_legacy_bb_reference_keeps_fields_and_removes_linkout_controls(self) -> None:
        result = self.extract(
            """<body><article><h1>Legacy references</h1>
            <a href="https://doi.org/10.1016/j.example.2019.1">DOI</a>
            <section><h2>References</h2><ol><li>
              <span><a href="https://www.sciencedirect.com/science/article/pii/S0123456789?via%3Dihub#bbb0005"
                id="ref-id-bb0005"><span><span>[1]</span></span></a></span>
              <span id="rf4785"><div><div>A. Author, B. Writer</div>
                <div id="ref-id-rf4785">Complete title</div></div>
                <div>Journal, 1 (2019), pp. 1-2</div><div lang="en">
                  <a href="https://doi.org/10.1234/example"><span>Crossref</span></a>
                  <a href="https://www.scopus.com/example"><span>View in Scopus</span></a>
                  <a href="https://scholar.google.com/example"><span>Google Scholar</span></a>
                </div></span>
            </li></ol></section></article></body>"""
        )

        self.assertEqual(len(result.references), 1)
        reference = result.references[0]
        self.assertEqual(
            reference.plain_text,
            "[1]. A. Author, B. Writer Complete title Journal, 1 (2019), pp. 1-2 "
            "DOI: https://doi.org/10.1234/example",
        )
        self.assertNotIn("Crossref", reference.markdown)
        self.assertNotIn("View in Scopus", reference.markdown)
        self.assertNotIn("Google Scholar", reference.markdown)

    def test_invalid_and_remote_figure_sources_are_not_materialized(self) -> None:
        mismatched = base64.b64encode(JPEG_BYTES).decode("ascii")
        result = self.extract(
            f"""<body><h1>Asset safety</h1><section><h2>Results</h2>
<figure id="f0005"><img src="data:image/png;base64,{mismatched}">
  <span id="cn0005"><p>Fig. 1. Invalid declaration.</p></span></figure>
<figure id="f0010"><img src="https://example.invalid/figure.jpg">
  <span id="cn0010"><p>Fig. 2. Remote source.</p></span></figure>
<figure id="f0015"><img src="data:text/plain;base64,SGVsbG8=">
  <span id="cn0015"><p>Fig. 3. Non-image data.</p></span></figure>
</section></body>"""
        )

        self.assertEqual(result.embedded_assets, [])
        invalid = [
            warning
            for warning in result.warnings
            if warning["code"] == "invalid_embedded_figure_image"
        ]
        self.assertEqual(len(invalid), 1)
        self.assertIn("do not match declared media type", invalid[0]["message"])

    def test_ordered_list_keeps_nested_number_and_spaced_ellipsis(self) -> None:
        result = self.extract(
            """<body><h1>Lists</h1><section><h2>Outline</h2><ol>
            <li>1. Introduction</li><li>These ...</li>
            </ol></section></body>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(block.kind, "list")
        self.assertEqual(
            block.markdown, "1. 1. Introduction\n1. These ..."
        )
        self.assertEqual(
            block.plain_text, "1. 1. Introduction\n1. These ..."
        )

    def test_cell_press_header_summary_and_generated_ai_widget(self) -> None:
        result = self.extract(
            """<body><header><div>
<h1 property="name">Cell Press article</h1>
<div><span role="list">
  <span property="author" typeof="Person" role="listitem">
    <span><a><span property="givenName">Alice</span> <span property="familyName">Example</span></a></span>
    <sup><a property="affiliation" role="doc-noteref">1</a></sup>
  </span>
  <span property="author" typeof="Person" role="listitem">
    <span><a><span property="givenName">Bob</span> <span property="familyName">Writer</span></a></span>
    <sup><a property="affiliation" role="doc-noteref">2</a><a role="doc-noteref">3</a></sup>
    <a property="email" href="mailto:bob@example.org">Send email</a>
  </span>
</span></div>
<details id="core-affiliations-notes"><summary>Affiliations &amp; Notes</summary>
  <div><div property="affiliation"><span>1</span><span property="name">Institute A</span></div>
  <div property="affiliation"><span>2</span><span property="name">Institute B</span></div></div>
  <div><div role="doc-footnote"><div>3</div><div><div id="ntpara0010">Lead contact</div></div></div>
  <div role="doc-footnote"><div></div><div><div id="ntpara0020">Supplement statement.</div></div></div></div>
</details>
<details id="core-content-info"><summary>Article Info</summary>
  <div><div>Publication History:</div><div><span>Received January 1, 2021</span><span>Accepted February 2, 2021</span></div></div>
  <div><div>Footnotes:</div><div><div role="doc-footnote"><div id="np0010" role="paragraph">The authors state no conflict of interest.</div></div></div></div>
  <div><a property="sameAs" href="https://doi.org/10.1000/cell">10.1000/cell</a></div>
  <div role="paragraph">Copyright: © 2021 Publisher.</div>
  <div><a href="http://www.elsevier.com/open-access/userlicense/1.0/">Elsevier user license</a></div>
  <div>published online 9 March 2021</div>
</details></div></header>
<div role="navigation" aria-label="Article navigation"><a>Download PDF</a>
  <nav id="article_sections_menu"><div>Outline</div><ul><li>Results</li></ul></nav>
</div>
<nav id="article_sections_menu-desktop"><ul><li>Results</li></ul></nav>
<div id="abstracts"><section id="author-abstract" property="abstract">
  <h2>Summary</h2><div id="spara0010">An <i>in vitro</i> β-cell summary.</div>
</section></div>
<div role="region" aria-labelledby="gen-ai-header"><div><div>
  <h3 id="gen-ai-header">Reading Assistant</h3><div id="chat-status" aria-live="polite"></div>
  <div>AI-generated content may vary in quality.</div>
  <div><h3>Actions you could take:</h3><p>Summarize this article</p></div>
</div></div></div>
<section><h2>Results</h2><div id="p0010">Authored result.</div></section>
<section id="core-collateral-figures"><h2>Figures (6)</h2></section>
<section id="core-collateral-metrics"><h2>Article metrics</h2></section>
<section id="core-collateral-supplementary"><h2>Supplementary materials (1)</h2></section>
<section id="core-collateral-relatedArticles"><h2>Related Articles</h2></section>
</body>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Author assignments: Alice Example (1); Bob Writer (2,3)",
                "Correspondence: Bob Writer (bob@example.org)",
                "Affiliation 1: Institute A",
                "Affiliation 2: Institute B",
                "Author note 3: Lead contact",
                "Author note: Supplement statement.",
                "Article history: Received January 1, 2021; Accepted February 2, 2021",
                "DOI: 10.1000/cell",
                "Copyright: © 2021 Publisher.",
                "License: Elsevier user license: http://www.elsevier.com/open-access/userlicense/1.0/",
                "Conflict of interest: The authors state no conflict of interest.",
                "Publication date: Published online 9 March 2021",
            ],
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Summary", "Results"],
        )
        self.assertEqual(
            result.sections[0].blocks[0].markdown,
            "An <em>in vitro</em> β-cell summary.",
        )
        visible = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertNotIn("Reading Assistant", visible)
        self.assertNotIn("Summarize this article", visible)
        self.assertNotIn("Download PDF", visible)
        self.assertNotIn("Article metrics", visible)
        self.assertNotIn("Supplementary materials", visible)
        self.assertNotIn("Related Articles", visible)

    def test_current_author_names_exclude_affiliation_and_note_markers(self) -> None:
        result = self.extract(
            """<body><h1>Current ScienceDirect author markers</h1>
            <div id="author-group">
              <span type="button"><span><span><span>C.</span> <span>James Chou</span>
              </span><span id="baff1"><sup>a</sup></span>
              <span id="bfn1"><sup>†</sup></span></span></span>
            </div>
            <dl><dt><a href="#bfn1"><sup>†</sup></a></dt>
              <dd>Present address: Synthetic University.</dd></dl>
            <section><h2>Body</h2><p>Text.</p></section></body>"""
        )

        self.assertEqual(
            [block.plain_text for block in result.front_matter],
            [
                "Affiliation assignments: C. James Chou (a)",
                "Author note assignments: C. James Chou (†)",
                "Author note †: Present address: Synthetic University.",
            ],
        )

    def test_legacy_cell_press_summary_div_is_preserved(self) -> None:
        result = self.extract(
            """<body><h1>Legacy Cell Press article</h1>
<div id="abstracts"><div id="abs0010"><h2>Summary</h2>
  <div id="abssec0010"><div id="abspara0010">
    Synthetic <i>p</i>-anisyl <b>summary</b> text.
  </div></div>
</div></div>
<section><h2>Results</h2><div id="p0010">Authored result.</div></section>
</body>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Summary", "Results"],
        )
        self.assertEqual(
            result.sections[0].blocks[0].markdown,
            "Synthetic <em>p</em>-anisyl <strong>summary</strong> text.",
        )

    def test_cell_press_semantic_header_preserves_level_five_and_six_sections(self) -> None:
        result = self.extract(
            """<body><header>
<h1 property="name">Deep Cell Press methods</h1>
<span property="author" typeof="Person" role="listitem">
  <span><a><span property="givenName">Alice</span> <span property="familyName">Example</span></a></span>
  <sup><a role="doc-noteref">1</a></sup>
</span>
<details id="core-affiliations-notes"><div property="affiliation"><span>1</span><span property="name">Institute A</span></div></details>
<details id="core-content-info"><a property="sameAs" href="https://doi.org/10.1000/deep">10.1000/deep</a></details>
</header>
<section><h2>Methods</h2><div id="p0010">Parent prose.</div>
  <section><h4>Assay details</h4><div id="p0020">Assay prose.</div>
    <section><h5>Cloning step</h5><div id="p0030">Cloning prose.</div>
      <section><h6>Readout</h6><div id="p0040">Readout prose.</div></section>
      <div id="p0050">Cloning continuation.</div>
    </section>
  </section>
</section></body>"""
        )

        self.assertEqual(
            [(section.heading, section.level) for section in result.sections],
            [
                ("Methods", 2),
                ("Assay details", 4),
                ("Cloning step", 5),
                ("Readout", 6),
            ],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[2].blocks],
            ["Cloning prose.", "Cloning continuation."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[3].blocks],
            ["Readout prose."],
        )

    def test_non_cell_press_level_five_heading_remains_a_near_miss(self) -> None:
        result = self.extract(
            """<body><h1>Generic archive</h1>
<section><h2>Methods</h2><section><h5>Unrecognized deep heading</h5>
<p>Prose remains in the recognized parent section.</p></section></section>
</body>"""
        )

        self.assertEqual(
            [(section.heading, section.level) for section in result.sections],
            [("Methods", 2)],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Prose remains in the recognized parent section."],
        )

    def test_cell_press_inline_preview_and_div_bibliography(self) -> None:
        result = self.extract(
            """<body><h1>Cell bibliography</h1>
<section><h2>Results</h2><div id="p0010">Prior work
<span><a id="body-ref-sref1" href="#" href-manipulated="true" aria-controls="sref1">Author et al., 2020</a><div>
  <div><div><div>1.</div><div>Hidden authors</div><div><strong>Hidden title</strong></div></div>
  <div><a href="https://doi.org/10.1000/hidden" target="_blank">Crossref</a></div></div>
</div></span> established the result.</div></section>
<section id="references"><h2>References</h2><div id="bibliography"><div><div>
  <div id="bib1"><div id="sref1"><div><div><a href="#body-ref-sref1">1.</a></div><div>A. Author</div><div><strong>First β study</strong></div><div><em>J. Test</em> 2020; <strong>1</strong>:1-2</div></div><div><div><a href="https://doi.org/10.1000/one" target="_blank">Crossref</a></div></div></div></div>
  <div id="bib3"><div id="sref3"><div><div><a href="#body-ref-sref3-1">2.</a></div><div>B. Writer</div><div><strong>Second study</strong></div><div><em>J. Test</em> 2021; <strong>2</strong>:3-4</div></div><div><div><a href="https://scholar.example/" target="_blank">Google Scholar</a></div></div></div></div>
</div></div></div></section></body>"""
        )

        paragraph = result.sections[0].blocks[0]
        self.assertEqual(
            paragraph.plain_text,
            "Prior work Author et al., 2020 established the result.",
        )
        self.assertNotIn("Hidden title", paragraph.plain_text)
        self.assertEqual(len(result.references), 2)
        self.assertEqual(
            result.references[0].plain_text,
            "1. A. Author First β study J. Test 2020; 1:1-2 DOI: https://doi.org/10.1000/one",
        )
        self.assertIn("<strong>First β study</strong>", result.references[0].markdown)
        self.assertEqual(
            result.references[1].plain_text,
            "2. B. Writer Second study J. Test 2021; 2:3-4",
        )
        self.assertNotIn("Google Scholar", result.references[1].plain_text)

    def test_cell_press_rf_bibliography_and_unheaded_abbreviations(self) -> None:
        result = self.extract(
            """<body><header><div>
<h1 property="name">Legacy JID article</h1>
<span property="author" typeof="Person" role="listitem"><span><a>
  <span property="givenName">Alice</span> <span property="familyName">Example</span>
</a></span><sup><a role="doc-noteref">1</a></sup></span>
<details id="core-affiliations-notes"><div><div property="affiliation"><span>1</span><span property="name">Institute A</span></div></div><div></div></details>
<details id="core-content-info"><div><a property="sameAs" href="https://doi.org/10.1000/jid">10.1000/jid</a></div></details>
</div></header>
<section id="bodymatter" property="articleBody" typeof="Text"><div>Abbreviations
<dl id="dl0010"><dt id="dt0010">PI</dt><dd id="dd0010"><div id="p0010" role="paragraph">pyrrole–imidazole</div></dd>
<dt id="dt0015">TGF-β</dt><dd id="dd0015"><div id="p0015" role="paragraph">transforming growth factor-β</div></dd></dl>
<section id="sec-1"><h2>Introduction</h2><div id="p0020">Prior work
<span><a id="body-ref-rf0010" href="#" href-manipulated="true" aria-controls="rf0010">Author et al., 2020</a><div>
  <div><div><div>1.</div><div>Hidden authors</div><div><strong>Hidden title</strong></div></div>
  <div><a href="https://doi.org/10.1000/hidden" target="_blank">Crossref</a></div></div>
</div></span> established the result.</div></section></div></section>
<section id="references"><h2>References</h2><div id="bibliography"><div>
  <div id="bb0010"><div id="rf0010"><div><div><a href="#body-ref-rf0010">1.</a></div><div>A. Author ...</div><div><strong>First β study</strong></div><div><em>J. Test</em> 2020; <strong>1</strong>:1-2</div></div><div><div><a href="https://doi.org/10.1000/one" target="_blank">Crossref</a></div></div></div></div>
  <div id="bb0015"><div id="rf0015"><div><div><a href="#body-ref-rf0015">2.</a></div><div>B. Writer</div><div><strong>Second study</strong></div><div><em>J. Test</em> 2021; <strong>2</strong>:3-4</div></div><div><div><a href="https://scholar.example/" target="_blank">Google Scholar</a></div></div></div></div>
</div></div></section></body>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abbreviations", "Introduction"],
        )
        self.assertEqual(
            result.sections[0].blocks[0].plain_text,
            "- PI: pyrrole–imidazole\n- TGF-β: transforming growth factor-β",
        )
        self.assertEqual(
            result.sections[1].blocks[0].plain_text,
            "Prior work Author et al., 2020 established the result.",
        )
        self.assertNotIn("Hidden title", result.sections[1].blocks[0].plain_text)
        self.assertEqual(len(result.references), 2)
        self.assertEqual(
            result.references[0].plain_text,
            "1. A. Author et al. First β study J. Test 2020; 1:1-2 DOI: https://doi.org/10.1000/one",
        )
        self.assertIn("<em>et al.</em>", result.references[0].markdown)
        self.assertEqual(
            result.references[1].plain_text,
            "2. B. Writer Second study J. Test 2021; 2:3-4",
        )

    def test_cell_press_rf_bibliography_accepts_authenticated_grouped_targets(self) -> None:
        result = self.extract(
            """<body><h1>Grouped citations</h1><section><h2>Results</h2>
<p>Grouped result <a id="body-ref-rf0020" href="#" href-manipulated="true"
aria-controls="rf0020"><sup>2,3</sup></a>.</p></section>
<section id="references"><h2>References</h2><div id="bibliography"><div>
  <div id="bb0010"><div id="rf0010"><div><div><a href="#body-ref-rf0010">1.</a></div><div>A. Author</div><div><strong>First study</strong></div><div><em>J. Test</em> 2020; <strong>1</strong>:1-2</div></div><div><div><a href="https://scholar.example/1" target="_blank">Google Scholar</a></div></div></div></div>
  <div id="bb0015"><div id="rf0015"><div><div><a href="#body-ref-rf0020">2.</a></div><div>B. Author</div><div><strong>Second study</strong></div><div><em>J. Test</em> 2021; <strong>2</strong>:3-4</div></div><div><div><a href="https://scholar.example/2" target="_blank">Google Scholar</a></div></div></div></div>
  <div id="bb0020"><div id="rf0020"><div><div><a href="#body-ref-rf0020">3.</a></div><div>C. Author</div><div><strong>Third study</strong></div><div><em>J. Test</em> 2022; <strong>3</strong>:5-6</div></div><div><div><a href="https://scholar.example/3" target="_blank">Google Scholar</a></div></div></div></div>
</div></div></section></body>"""
        )

        self.assertEqual(
            [reference.plain_text for reference in result.references],
            [
                "1. A. Author First study J. Test 2020; 1:1-2",
                "2. B. Author Second study J. Test 2021; 2:3-4",
                "3. C. Author Third study J. Test 2022; 3:5-6",
            ],
        )

    def test_current_cell_press_unheaded_introduction_is_not_scoped_as_keywords(self) -> None:
        result = self.extract(
            """<body><header><div>
<h1 property="name">Unheaded introduction</h1>
<span property="author" typeof="Person" role="listitem"><span><a>
  <span property="givenName">Alice</span> <span property="familyName">Example</span>
</a></span><sup><a role="doc-noteref">1</a></sup></span>
<details id="core-affiliations-notes"><div><div property="affiliation"><span>1</span><span property="name">Institute A</span></div></div><div></div></details>
<details id="core-content-info"><div><a property="sameAs" href="https://doi.org/10.1000/current">10.1000/current</a></div></details>
</div></header>
<section id="keywords" property="keywords"><h2>KEYWORDS</h2><ol><li>kidney</li></ol></section>
<section id="bodymatter" property="articleBody" typeof="Text"><div>
  <div id="p0010" role="paragraph">First introductory paragraph.</div>
  <div id="p0015" role="paragraph">Second introductory paragraph.</div>
  <section id="sec-1"><h2>RESULTS</h2><section id="sec-1-1"><h3>Finding</h3>
    <div id="p0020" role="paragraph">Result text.</div>
  </section></section>
</div></section></body>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["KEYWORDS", "Introduction", "RESULTS", "Finding"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["1. kidney"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["First introductory paragraph.", "Second introductory paragraph."],
        )

    def test_cell_press_rf_bibliography_rejects_inconsistent_group_target(self) -> None:
        result = self.extract(
            """<body><h1>Bad grouped citation</h1><section><h2>Results</h2>
<p><a id="body-ref-rf0020" href="#" href-manipulated="true"
aria-controls="rf0020"><sup>4,5</sup></a></p></section>
<section id="references"><h2>References</h2><div id="bibliography"><div>
  <div id="bb0010"><div id="rf0010"><div><div><a href="#body-ref-rf0020">1.</a></div><div>A. Author</div><div><strong>Study</strong></div></div><div><div><a href="https://scholar.example/" target="_blank">Google Scholar</a></div></div></div></div>
</div></div></section></body>"""
        )

        self.assertEqual(result.references, [])

    def test_cell_press_table_figures_are_semantic_tables(self) -> None:
        result = self.extract(
            """<body><h1>Cell tables</h1><section><h2>Results</h2>
<figure id="tbl1"><div><table><thead><tr><th>DNA</th><th>Value</th></tr></thead>
<tbody><tr><td>Target</td><td>9.97</td></tr></tbody></table></div>
<figcaption><div><span>Table 1</span><div id="tspara0010">Shift of <i>T</i><sub>m</sub> values</div></div>
<div><div role="doc-footnote"><div>a</div><div id="tblfn1"><div>The mean <i>T</i><sub>m</sub> was calculated.</div></div></div></div></figcaption></figure>
</section><section><h2>Key resources table</h2><div id="p0100" role="paragraph"><div><figure id="undtbl1"><div><table>
<thead><tr><th>RESOURCE</th><th>SOURCE</th><th>IDENTIFIER</th></tr></thead><tbody>
<tr><td>Compound</td><td><span><a id="body-ref-sref3-1" href="#" href-manipulated="true" aria-controls="sref3-1">Author, 1996</a><div><div><div><div>3.</div><div>Hidden citation</div></div><div><a href="https://doi.org/10.1000/hidden" target="_blank">Crossref</a></div></div></div></span></td><td>N/A</td></tr>
</tbody></table></div><figcaption><div><ul><li><a>Open table in a new tab</a></li></ul></div></figcaption></figure></div></section>
</body>"""
        )

        self.assertEqual(result.figures, [])
        self.assertEqual(result.embedded_assets, [])
        self.assertEqual([table.table_id for table in result.tables], ["table_001", "table_002"])
        self.assertEqual(
            result.tables[0].title_markdown,
            "Table 1. Shift of <em>T</em><sub>m</sub> values",
        )
        self.assertEqual(
            result.tables[0].footnotes_plain,
            ["[a] The mean T_{m} was calculated."],
        )
        self.assertEqual(result.tables[1].title_plain, "Key resources table")
        self.assertEqual(
            result.tables[1].parts[0].rows[1][1].text,
            "Author, 1996",
        )
        self.assertEqual(result.sections[1].blocks, [])

    def test_cell_press_table_wrapper_with_authored_prose_is_not_suppressed(self) -> None:
        result = self.extract(
            """<body><h1>Cell table context</h1><section><h2>Results</h2>
<div id="p0100" role="paragraph">Authored context before the table.<div>
<figure id="undtbl1"><div><table><tbody><tr><td>Value</td></tr></tbody></table></div>
<figcaption><div><a>Open table in a new tab</a></div></figcaption></figure>
</div></div></section></body>"""
        )

        self.assertEqual(len(result.tables), 1)
        self.assertIn(
            "Authored context before the table.",
            result.sections[0].blocks[0].plain_text,
        )

    def test_cell_press_supplement_cards_keep_file_boundaries(self) -> None:
        result = self.extract(
            """<body><h1>Cell supplements</h1><section><h2>Supplemental information (2)</h2>
<div id="p0270" role="paragraph"><div><div></div></div>
<div id="mmc1"><div><a href="/attachment/mmc1.pdf" download="mmc1.pdf"
aria-labelledby="mmc1-heading mmc1-link"><span id="mmc1-link">PDF (734.93 KB)</span></a></div>
<div><div id="mmc1-heading">Document S1. Figures S1–S4 and Table S1</div></div></div>
<div><div><a href="/attachment/mmc2.pdf" download="mmc2.pdf"
aria-labelledby="mmc2-heading mmc2-link"><span id="mmc2-link">PDF (2.30 MB)</span></a></div>
<div><div id="mmc2-heading">Document S2. Article plus supplemental information</div></div></div>
</div></section></body>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(block.kind, "list")
        self.assertEqual(
            block.plain_text,
            (
                "- PDF (734.93 KB): Document S1. Figures S1–S4 and Table S1\n"
                "- PDF (2.30 MB): Document S2. Article plus supplemental information"
            ),
        )
        self.assertEqual(block.markdown, block.plain_text)

    def test_legacy_cell_press_supplementary_material_card_is_a_list(self) -> None:
        result = self.extract(
            """<body><h1>Legacy JID supplements</h1>
<section><h2>SUPPLEMENTARY MATERIAL (1)</h2>
<div id="p0180" role="paragraph">Supplementary material is linked to the online version of the paper at</div>
<div id="para300a" role="paragraph"><div>
<div><a href="/attachment/mmc1.pdf" download="mmc1.pdf"
aria-labelledby="mmc1-heading mmc1-link"><span id="mmc1-link">PDF (62.30 KB)</span></a></div>
<div><div id="mmc1-heading">Supplementary Table 1</div></div>
</div></div></section></body>"""
        )

        self.assertEqual(len(result.sections[0].blocks), 2)
        self.assertEqual(result.sections[0].blocks[1].kind, "list")
        self.assertEqual(
            result.sections[0].blocks[1].plain_text,
            "- PDF (62.30 KB): Supplementary Table 1",
        )

    def test_cell_press_supplement_card_near_miss_is_not_restructured(self) -> None:
        result = self.extract(
            """<body><h1>Near miss supplements</h1><section><h2>Supplemental information (2)</h2>
<div id="p0270" role="paragraph"><div id="mmc1"><div>
<a href="/attachment/mmc1.pdf" download="mmc1.pdf"
aria-labelledby="wrong-heading mmc1-link"><span id="mmc1-link">PDF (10 KB)</span></a></div>
<div><div id="mmc1-heading">Document S1. Authored description</div></div>
<div>Authored card note.</div></div></div></section></body>"""
        )

        block = result.sections[0].blocks[0]
        self.assertEqual(block.kind, "paragraph")
        self.assertIn("Document S1. Authored description", block.plain_text)
        self.assertIn("Authored card note.", block.plain_text)

    def test_cell_press_figure_caption_keeps_title_panels_and_markup(self) -> None:
        result = self.extract(
            """<body><h1>Cell figure</h1><section><h2>Results</h2>
<figure id="fig1"><a href="figure.jpg"></a><figcaption>
  <div id="fig1-title"><span>Figure 1</span> <span>Designed <i>in vitro</i> assay</span></div>
  <div><div></div><div id="fig1-content"><div>
    <div id="fspara0015" role="paragraph">(A) First panel with <b>8950A-Chb(Cl/OH)</b>.</div>
    <div id="fspara0020" role="paragraph">(B) Second panel with T<sub>m</sub>.</div>
  </div></div></div>
</figcaption></figure></section></body>"""
        )

        self.assertEqual(len(result.figures), 1)
        figure = result.figures[0]
        self.assertEqual(figure.label, "Figure 1")
        self.assertEqual(
            figure.caption_plain,
            (
                "Designed in vitro assay (A) First panel with "
                "8950A-Chb(Cl/OH). (B) Second panel with T_{m}."
            ),
        )
        self.assertEqual(
            figure.caption_markdown,
            (
                "Designed <em>in vitro</em> assay\n\n"
                "(A) First panel with <strong>8950A-Chb(Cl/OH)</strong>.\n\n"
                "(B) Second panel with T<sub>m</sub>."
            ),
        )

    def test_multi_image_cell_press_figure_keeps_shared_described_caption(self) -> None:
        result = self.extract(
            """<body><h1>Split Cell figure</h1><section><h2>Results</h2>
<figure id="fig4">
  <span><img src="data:image/png;base64,iVBORw0KGgo=" aria-describedby="cap0025"></span>
  <span><img src="data:image/png;base64,iVBORw0KGgo=" aria-describedby="cap0025"></span>
  <span id="cap0025">
    <p><span>Figure 4</span>. Shared caption title</p>
    <div>(A) First source image.</div>
    <div>(B) Second source image with T<sub>m</sub>.</div>
  </span>
</figure></section></body>"""
        )

        self.assertEqual(len(result.figures), 1)
        figure = result.figures[0]
        self.assertEqual(figure.label, "Figure 4")
        self.assertEqual(
            figure.caption_plain,
            (
                "Shared caption title (A) First source image. "
                "(B) Second source image with T_{m}."
            ),
        )
        self.assertEqual(
            figure.caption_markdown,
            (
                "Shared caption title\n\n(A) First source image.\n\n"
                "(B) Second source image with T<sub>m</sub>."
            ),
        )

    def test_cell_press_near_miss_controls_remain_visible(self) -> None:
        result = self.extract(
            """<body><h1>Near miss controls</h1><section><h2>Results</h2>
<div id="p0010"><span><a id="body-ref-sref1" href="#" href-manipulated="true" aria-controls="different">Visible label</a>
<div><div><div><div>1.</div><div>Authored disclosure</div></div><div><a href="https://example.org" target="_blank">Details</a></div></div></div></span></div>
<div role="region" aria-labelledby="gen-ai-header"><h3 id="gen-ai-header">Reading Assistant</h3>
<div id="chat-status" aria-live="polite"></div><p>Authored near-miss region.</p></div>
</section></body>"""
        )

        visible = " ".join(
            block.plain_text
            for section in result.sections
            for block in section.blocks
        )
        self.assertIn("Authored disclosure", visible)
        self.assertIn("Authored near-miss region.", visible)

    def test_semantic_cell_press_role_paragraphs_equation_and_bibliography(self) -> None:
        result = self.extract(
            """<body><article typeof="ScholarlyArticle">
<div><header><div><h1 property="name">Semantic Cell Press article</h1>
<div><span property="author" typeof="Person" role="listitem">
  <span><a><span property="givenName">Test</span> <span property="familyName">Author</span></a></span>
  <sup><a role="doc-noteref">a</a></sup>
</span></div>
<div id="core-affiliations-notes"><div></div><div></div></div>
<div id="core-content-info"><div><div></div><div></div></div>
  <a property="sameAs" href="https://doi.org/10.1016/j.synthetic.2026.3">DOI</a>
</div></div></header></div>
<div><section id="author-abstract" property="abstract" typeof="Text" role="doc-abstract">
  <h2>Abstract</h2><div id="simple-para-0040" role="paragraph">Exact abstract.</div>
</section>
<section id="bodymatter" property="articleBody" typeof="Text"><div>
  <section id="sec-1"><h2>Introduction</h2>
    <div id="para-0010" role="paragraph">Prose before <span>
      <a href="#" id="body-ref-bib1" href-manipulated="true"
        aria-expanded="false" aria-controls="bib1">1</a><div><div><div>
        <div><div><div>1.</div><div>Test Author ...</div>
        <div><strong>Hidden preview title</strong></div></div>
        <div><a href="https://doi.org/10.1000/example" target="_blank">Crossref</a></div>
        </div></div></div></div></span>.
      <div id="fd1"><div role="math"><div><mjx-container jax="CHTML"
        aria-label="x equals StartFraction y Over z EndFraction">
        <mjx-math aria-hidden="true"><mjx-mrow><mjx-mi><mjx-c>x</mjx-c></mjx-mi>
        <mjx-mo><mjx-c>=</mjx-c></mjx-mo><mjx-mfrac><mjx-frac>
        <mjx-num><mjx-nstrut/><mjx-mi><mjx-c>y</mjx-c></mjx-mi></mjx-num>
        <mjx-dbox><mjx-dtable><mjx-line/><mjx-row><mjx-den><mjx-dstrut/>
        <mjx-mi><mjx-c>z</mjx-c></mjx-mi></mjx-den></mjx-row></mjx-dtable></mjx-dbox>
        </mjx-frac></mjx-mfrac></mjx-mrow></mjx-math></mjx-container></div></div>
        <div>(1)</div></div>
      Prose after.</div>
    <div id="para-0180" role="paragraph"><div id="ulist0015" role="list">
      <div id="u0030" role="listitem"><div>•</div><div>
        <div id="para-0185" role="paragraph">Dataset entry.</div>
      </div></div>
      <div id="u0035" role="listitem"><div>•</div><div>
        <div id="para-0190" role="paragraph">Code entry.</div>
      </div></div>
    </div></div>
  </section>
</div></section>
<section id="backmatter"><div><section id="references"><h2>References</h2>
  <div id="bibliography" role="doc-bibliography"><div><div><div id="bib1"><div>
    <div><div><a href="#body-ref-bib1" title="View in article">1.</a></div>
      <div>Author, A. ...</div><div><strong>Exact cited title</strong></div>
      <div><em>Journal.</em> 2026; <strong>1</strong>:1-2</div></div>
    <div><div><a href="https://doi.org/10.1000/example" target="_blank">Crossref</a></div></div>
  </div></div></div></div>
</section></div></section></div>
</article></body>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Introduction"],
        )
        self.assertEqual(
            [block.kind for block in result.sections[1].blocks],
            ["paragraph", "equation", "paragraph", "list"],
        )
        self.assertEqual(result.sections[1].blocks[0].plain_text, "Prose before 1.")
        self.assertNotIn("Hidden preview title", result.sections[1].blocks[0].plain_text)
        self.assertEqual(
            result.sections[1].blocks[1].plain_text,
            r"x=\frac{y}{z} (1)",
        )
        self.assertEqual(result.sections[1].blocks[2].plain_text, "Prose after.")
        self.assertEqual(
            sum(
                block.plain_text.count("Dataset entry.")
                for block in result.sections[1].blocks
            ),
            1,
        )
        self.assertEqual(
            result.sections[1].blocks[3].plain_text,
            "- Dataset entry.\n- Code entry.",
        )
        self.assertEqual(len(result.references), 1)
        self.assertEqual(
            result.references[0].plain_text,
            (
                "1. Author, A. et al. Exact cited title Journal. 2026; 1:1-2 "
                "DOI: https://doi.org/10.1000/example"
            ),
        )

    def test_partial_semantic_cell_press_bullet_wrapper_is_not_duplicated(self) -> None:
        result = self.extract(
            """<body><article typeof="ScholarlyArticle">
<div><header><div><h1 property="name">Partial Cell Press article</h1>
<div><span property="author" typeof="Person" role="listitem">
  <span><a><span property="givenName">Test</span> <span property="familyName">Author</span></a></span>
  <sup><a role="doc-noteref">a</a></sup>
</span></div>
<div id="core-affiliations-notes"><div></div><div></div></div>
<div id="core-content-info"><div><div></div><div></div></div>
  <a property="sameAs" href="https://doi.org/10.1016/j.synthetic.2026.5">DOI</a>
</div></div></header></div>
<div><section id="author-abstract" property="abstract" typeof="Text" role="doc-abstract">
  <h2>Abstract</h2><div id="simple-para-0040" role="paragraph">Exact abstract.</div>
</section>
<section id="bodymatter" property="articleBody" typeof="Text"><div>
  <section id="sec-1"><h2>Data availability</h2>
    <div id="p0180" role="paragraph"><div id="ulist0015" role="list">
      <div id="u0030" role="listitem"><div>•</div><div>
        <div id="p0185" role="paragraph">Dataset entry.</div>
      </div></div>
      <div id="u0035" role="listitem"><div>•</div><div>
        <div id="p0190" role="paragraph">Code entry.</div>
      </div></div>
    </div></div>
  </section>
</div></section></div>
</article></body>"""
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Data availability"],
        )
        self.assertEqual(
            [block.kind for block in result.sections[1].blocks],
            ["list"],
        )
        self.assertEqual(
            result.sections[1].blocks[0].plain_text,
            "- Dataset entry.\n- Code entry.",
        )

    def test_semantic_cell_press_idless_direct_section_paragraphs(self) -> None:
        self._check_semantic_cell_press_idless_direct_section_paragraphs(False)

    def test_semantic_cell_press_uppercase_bibliography_ids(self) -> None:
        self._check_semantic_cell_press_idless_direct_section_paragraphs(True)

    def _check_semantic_cell_press_idless_direct_section_paragraphs(self, uppercase: bool) -> None:
        result = self.extract(
            """<body><article typeof="ScholarlyArticle">
<div><header><div><h1 property="name">ID-less Cell Press article</h1>
<div><span property="author" typeof="Person" role="listitem">
  <span><a><span property="givenName">Test</span> <span property="familyName">Author</span></a></span>
  <sup><a role="doc-noteref">a</a></sup>
</span></div>
<div id="core-affiliations-notes"><div></div><div></div></div>
<div id="core-content-info"><a property="sameAs" href="https://doi.org/10.1016/j.synthetic.2026.4">DOI</a></div>
</div></header></div>
<div><section id="author-abstract" property="abstract" typeof="Text" role="doc-abstract">
  <h2>Abstract</h2><div role="paragraph">Exact ID-less abstract.</div>
</section>
<section id="bodymatter" property="articleBody" typeof="Text"><div>
  <section id="sec-1"><h2>Introduction</h2>
    <div role="paragraph">Exact ID-less body prose.</div>
    <figure id="fig1"><img src="data:image/png;base64,iVBORw0KGgo="/>
      <figcaption><div role="paragraph">Caption prose stays structural.</div></figcaption>
    </figure>
    <figure id="tbl1"><div><table><thead><tr><th>Value</th></tr></thead>
      <tbody><tr><td>1</td></tr></tbody></table></div><figcaption>
      <div><span>Table 1</span><div role="paragraph">Exact table title</div></div>
      <div><div role="paragraph">Exact unmarked table note.</div></div>
      <div><ul><li><a href="/action/showFullTableHTML?isHtml=true&amp;tableId=tbl1&amp;pii=Synthetic"
        target="_blank">Open table in a new tab</a></li></ul></div>
    </figcaption></figure>
  </section>
</div></section>
<section id="backmatter"><div><section id="references"><h2>References</h2>
  <div id="bibliography" role="doc-bibliography"><div><div><div id="bib1"><div>
    <div><div><a href="#body-ref-bib1" title="View in article">1.</a></div>
      <div>Author, A.</div><div><strong>Exact cited title</strong></div>
      <div><em>Journal.</em> 2026; <strong>1</strong>:1-2</div></div>
    <div><div><a href="https://doi.org/10.1000/example" target="_blank">Crossref</a></div></div>
  </div></div></div></div>
</section></div></section></div>
</article></body>""".replace('bib1', 'BIB1' if uppercase else 'bib1').replace(
    'href="#body-ref-BIB1" title="View in article"',
    'href="#fig1" title="View in article"',
)
        )

        self.assertEqual(
            [section.heading for section in result.sections],
            ["Abstract", "Introduction"],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[0].blocks],
            ["Exact ID-less abstract."],
        )
        self.assertEqual(
            [block.plain_text for block in result.sections[1].blocks],
            ["Exact ID-less body prose."],
        )
        self.assertEqual(len(result.figures), 1)
        self.assertEqual(result.figures[0].caption_plain, "Caption prose stays structural.")
        self.assertEqual(len(result.tables), 1)
        self.assertEqual(result.tables[0].title_plain, "Table 1. Exact table title")
        self.assertEqual(
            result.tables[0].footnotes_plain,
            ["Exact unmarked table note."],
        )
        self.assertEqual(len(result.references), 1)


if __name__ == "__main__":
    unittest.main()
