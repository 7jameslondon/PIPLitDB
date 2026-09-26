from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.extraction.discovery import detect_format
from scripts.extraction.models import (
    ContentBlock,
    FigureItem,
    SourceFile,
    SupplementExtraction,
)
from scripts.extraction.supplements import (
    _PdfLine,
    _PdfParagraph,
    _apply_supplement_reviewed_page_blocks,
    _apply_supplement_page_continuations,
    _apply_supplement_line_exclusions,
    _begins_bold_lead_paragraph,
    _consolidate_cross_file_caption_figures,
    _consolidate_supplement_figure_captions,
    _combine_reviewed_pdf_figures,
    _exclude_reviewed_pdf_table_lines,
    _extract_page_lines,
    _heading_markup,
    _is_email_correspondence_page,
    _is_pdf_heading,
    _is_unreviewed_graphic_only_paragraph,
    _is_supplement_page_number,
    _is_standalone_bold_line,
    _is_standalone_heading_line,
    _is_standalone_italic_heading_line,
    _is_standalone_italic_line,
    _is_standalone_uppercase_heading,
    _supplement_affiliation_line_numbers,
    _natural_path_key,
    _normalize_space,
    _paragraphs,
    _supplement_caption_overrides,
    _supplement_duplicate_file_only_exclusion,
    _supplement_equation_overrides,
    _supplement_line_exclusions,
    _supplement_needs_decoded_text,
    _supplement_page_continuations,
    _unresolved_glyph_warning_is_covered,
    _supplement_pdf_text_config,
    _supplement_paragraph_overrides,
    _supplement_reviewed_page_blocks,
    _supplement_table_overrides,
    _supplement_table_text_regions,
    _supplement_unboxed_table_pages,
    _reviewed_pdf_crop_regions,
    _reviewed_pdf_graphic_regions,
    _reviewed_pdf_crop_figures,
    _resolved_fully_reviewed_pdf_native_text_warning,
    _resolved_reviewed_pdf_warnings,
    _resolved_reviewed_page_text_warnings,
    _reviewed_visual_pdf_pages,
    _should_warn_supplement_page_native_text_empty,
    _pdf_page_events,
    extract_supplements,
)
from scripts.extraction.caption_patterns import (
    CAPTION_PATTERN,
    is_figure_caption,
    normalize_caption_number,
)
from scripts.extraction.rich_text import (
    inline_markup_to_safe_html,
    rich_text_matches_plain,
)


class SupplementDiscoveryTests(unittest.TestCase):
    def test_reviewed_exclusion_can_remove_duplicate_figure_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "overview.pdf"
            source_path.write_bytes(b"%PDF-1.4\nreviewed-test")
            source_relative = (
                "papers (private)/00001/supplementary/overview.pdf"
            )
            source = SourceFile(
                role="supplement",
                path=source_path,
                relative_path=source_relative,
                size=source_path.stat().st_size,
                sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                detected_format="application/pdf",
                page_count=1,
            )
            retained_block = ContentBlock(
                "supplement_001_block_001",
                "text",
                "Supplementary discussion.",
                "Supplementary discussion.",
                source_relative,
                "page=1;native-lines=1-1",
            )
            duplicate_figure = FigureItem(
                figure_id="supplement_001_figure_s1",
                source_id="Supplementary Figure S1",
                label="Supplementary Figure S1",
                kind="figure",
                caption_markdown="Supplementary Figure S1. Contents listing.",
                caption_plain="Supplementary Figure S1. Contents listing.",
                source_path=source_relative,
                source_locator="page=1;native-lines=2-2",
            )
            exclusion = {
                "source_path": source_relative,
                "pattern": r"^Supplementary Figure S1\. Contents listing\.$",
                "reason": "The overview page lists a separately supplied figure.",
                "evidence": "Reviewed overview page 1 and standalone figure PDF.",
            }

            with patch(
                "scripts.extraction.supplements._pdf_blocks",
                return_value=([retained_block], [duplicate_figure], []),
            ):
                supplements = extract_supplements(
                    [source],
                    Path(temp_dir) / "extraction",
                    exclusion_specs=[exclusion],
                )

            self.assertEqual(supplements[0].blocks, [retained_block])
            self.assertEqual(supplements[0].figures, [])
            self.assertEqual(len(supplements[0].exclusions), 1)
            self.assertEqual(
                supplements[0].exclusions[0]["content_kind"], "figure_caption"
            )

    def test_reviewed_visual_region_resolves_only_contained_glyph_warning(self) -> None:
        warning = {
            "code": "unresolved_glyph",
            "page": 2,
            "bbox": [10.0, 20.0, 14.0, 28.0],
        }
        self.assertTrue(
            _unresolved_glyph_warning_is_covered(
                warning, {2: [(5.0, 5.0, 20.0, 30.0)]}
            )
        )
        self.assertFalse(
            _unresolved_glyph_warning_is_covered(
                warning, {2: [(12.0, 5.0, 20.0, 30.0)]}
            )
        )
        self.assertFalse(
            _unresolved_glyph_warning_is_covered(
                {**warning, "code": "different_warning"},
                {2: [(5.0, 5.0, 20.0, 30.0)]},
            )
        )

    def test_hash_pinned_duplicate_file_only_requires_complete_page_map(self) -> None:
        main = SourceFile(
            role="main_pdf",
            path=Path("main.pdf"),
            relative_path="papers (private)/00001/pdf/main.pdf",
            size=1,
            sha256="a" * 64,
            detected_format="application/pdf",
            page_count=2,
        )
        si = SourceFile(
            role="supplement",
            path=Path("si.pdf"),
            relative_path="papers (private)/00001/supplementary/si.pdf",
            size=1,
            sha256="b" * 64,
            detected_format="application/pdf",
            page_count=1,
        )
        combined = SourceFile(
            role="supplement",
            path=Path("combined.pdf"),
            relative_path="papers (private)/00001/supplementary/combined.pdf",
            size=1,
            sha256="c" * 64,
            detected_format="application/pdf",
            page_count=3,
        )
        item = {
            "source_path": combined.relative_path,
            "source_sha256": combined.sha256,
            "reviewed_content_disposition": "duplicate_file_only",
            "reason": "Publisher convenience download duplicates known PDFs.",
            "evidence": "Reviewed all three rendered pages.",
            "duplicate_of": [
                {
                    "source_path": main.relative_path,
                    "source_sha256": main.sha256,
                    "source_pages": [1, 2],
                    "duplicate_pages": [1, 2],
                },
                {
                    "source_path": si.relative_path,
                    "source_sha256": si.sha256,
                    "source_pages": [1, 1],
                    "duplicate_pages": [3, 3],
                },
            ],
        }
        config = _supplement_pdf_text_config({"supplements": [item]}, combined)
        exclusion = _supplement_duplicate_file_only_exclusion(
            config, combined, "supplement_002", [main, si, combined]
        )

        self.assertIsNotNone(exclusion)
        assert exclusion is not None
        self.assertEqual(exclusion["status"], "duplicate")
        self.assertIn("pages=1-2->", exclusion["source_locator"])

        stale = dict(item)
        stale["source_sha256"] = "d" * 64
        with self.assertRaisesRegex(ValueError, "hash does not match"):
            _supplement_pdf_text_config({"supplements": [stale]}, combined)

        incomplete = {**item, "duplicate_of": item["duplicate_of"][:1]}
        with self.assertRaisesRegex(ValueError, "cover every duplicate page"):
            _supplement_duplicate_file_only_exclusion(
                incomplete, combined, "supplement_002", [main, si, combined]
            )

        stale_reference = {
            **item,
            "duplicate_of": [
                {**item["duplicate_of"][0], "source_sha256": "e" * 64},
                item["duplicate_of"][1],
            ],
        }
        with self.assertRaisesRegex(ValueError, "item 1 is incomplete"):
            _supplement_duplicate_file_only_exclusion(
                stale_reference, combined, "supplement_002", [main, si, combined]
            )

    def test_legacy_pdf_review_keeps_decoded_text_unless_explicitly_literal(self) -> None:
        caption_only = {
            "source_path": "papers (private)/00020/supplementary/supplementary.pdf",
            "source_sha256": "a" * 64,
            "native_text_engine": "pdfplumber",
            "caption_overrides": [{"kind": "figure"}],
            "table_overrides": [],
            "line_exclusions": [],
            "reviewed_figure_policy": "merge",
        }

        self.assertTrue(_supplement_needs_decoded_text(caption_only))
        self.assertFalse(
            _supplement_needs_decoded_text(
                {
                    **caption_only,
                    "text_stream": "pdfplumber",
                    "paragraph_overrides": [{"page": 1}],
                }
            )
        )
        self.assertTrue(
            _supplement_needs_decoded_text(
                {**caption_only, "text_stream": "decoded"}
            )
        )
        self.assertTrue(
            _supplement_needs_decoded_text(
                {**caption_only, "glyph_overrides": []}
            )
        )
        self.assertTrue(
            _supplement_needs_decoded_text(
                {**caption_only, "unexpected_text_anchor": []}
            )
        )
        with self.assertRaisesRegex(ValueError, "cannot use custom glyph"):
            _supplement_needs_decoded_text(
                {
                    **caption_only,
                    "text_stream": "pdfplumber",
                    "glyph_overrides": [],
                }
            )
        with self.assertRaisesRegex(ValueError, "text_stream must be"):
            _supplement_needs_decoded_text(
                {**caption_only, "text_stream": "near-miss"}
            )

    def test_reviewed_pdf_page_continuations_merge_exact_adjacent_text(self) -> None:
        specs = _supplement_page_continuations(
            {
                "page_continuations": [
                    {
                        "from_locator": "page=1;native-lines=8-44",
                        "to_locator": "page=2;native-lines=1-17",
                        "reason": "The sentence crosses the page boundary.",
                        "evidence": "Reviewed pages 1-2 against the PDF.",
                    },
                    {
                        "from_locator": "page=2;native-lines=1-17",
                        "to_locator": "page=3;native-lines=1-7",
                        "reason": "The same paragraph continues again.",
                        "evidence": "Reviewed pages 2-3 against the PDF.",
                    },
                ]
            }
        )
        blocks = [
            ContentBlock(
                "block-1",
                "text",
                "The sample was precipitated in the",
                "The sample was precipitated in the",
                "support.pdf",
                "page=1;native-lines=8-44",
            ),
            ContentBlock(
                "block-2",
                "text",
                "resulting powder was dried in vacuo.",
                "resulting powder was dried in vacuo.",
                "support.pdf",
                "page=2;native-lines=1-17",
            ),
            ContentBlock(
                "block-3",
                "text",
                "The next sentence continues.",
                "The next sentence continues.",
                "support.pdf",
                "page=3;native-lines=1-7",
            ),
        ]

        retained = _apply_supplement_page_continuations(blocks, specs)

        self.assertEqual(len(retained), 1)
        self.assertEqual(
            retained[0].plain_text,
            "The sample was precipitated in the resulting powder was dried in vacuo. The next sentence continues.",
        )
        self.assertIn("reviewed-page-continuation", retained[0].source_locator)

    def test_full_bold_pdf_lines_form_independent_scientific_headings(self) -> None:
        heading = _PdfLine(
            "Melting temperature Tm assay",
            "**Melting temperature** <strong><em>T</em></strong>**m assay**",
            1,
            80,
            300,
            100,
            112,
        )
        prose = _PdfLine(
            "DNA oligomers were purchased.",
            "DNA oligomers were purchased.",
            2,
            80,
            300,
            118,
            130,
        )
        inline_bold = _PdfLine(
            "The result was significant.",
            "The result was **significant**.",
            3,
            80,
            300,
            136,
            148,
        )

        paragraphs = _paragraphs([heading, prose, inline_bold])

        self.assertTrue(_is_standalone_bold_line(heading.markdown))
        self.assertFalse(_is_standalone_bold_line(inline_bold.markdown))
        self.assertEqual([item.text for item in paragraphs], [heading.text, f"{prose.text} {inline_bold.text}"])
        self.assertEqual(
            _heading_markup(heading.markdown),
            "Melting temperature <em>T</em>m assay",
        )

    def test_bold_lead_starts_a_new_authored_pdf_paragraph(self) -> None:
        preceding = _PdfLine(
            "The preceding paragraph ends here.",
            "The preceding paragraph ends here.",
            1,
            55,
            560,
            40,
            52,
        )
        lead = _PdfLine(
            "Methods. Samples were prepared.",
            "<strong>Methods</strong>. Samples were prepared.",
            2,
            55,
            560,
            58,
            70,
        )

        self.assertTrue(_begins_bold_lead_paragraph(lead.markdown))
        self.assertFalse(
            _begins_bold_lead_paragraph(
                "<strong>n-ODN</strong>. After each addition spectra were acquired."
            )
        )
        compound_lead = _PdfLine(
            "2,2,2-trichloroethyl compound (19): Under nitrogen, reagent was added.",
            (
                "<strong>2,2,2-trichloroethyl compound (19):</strong> "
                "Under nitrogen, reagent was added."
            ),
            3,
            55,
            560,
            76,
            88,
        )
        self.assertTrue(_begins_bold_lead_paragraph(compound_lead.markdown))
        self.assertEqual(
            [
                paragraph.text
                for paragraph in _paragraphs([preceding, lead, compound_lead])
            ],
            [preceding.text, lead.text, compound_lead.text],
        )
        caption = _PdfLine(
            "Scheme S1: Complete authored caption.",
            "**Scheme S1:** Complete authored caption.",
            4,
            55,
            560,
            100,
            112,
        )
        self.assertEqual(
            [
                paragraph.text
                for paragraph in _paragraphs([caption, compound_lead])
            ],
            [caption.text, compound_lead.text],
        )
        unbolded_prose = _PdfLine(
            "Im-Py-COOH was dissolved in DMF.",
            "Im-Py-COOH was dissolved in DMF.",
            5,
            55,
            560,
            132,
            144,
        )
        self.assertEqual(
            [
                paragraph.text
                for paragraph in _paragraphs([caption, unbolded_prose])
            ],
            [caption.text, unbolded_prose.text],
        )

    def test_peer_review_labels_and_numbered_points_start_pdf_blocks(self) -> None:
        lines = [
            _PdfLine(
                "The response paragraph ends here.",
                "The response paragraph ends here.",
                1,
                55,
                500,
                40,
                52,
            ),
            _PdfLine(
                "Major concern:", "Major concern:", 2, 55, 200, 58, 70
            ),
            _PdfLine(
                "Are these direct targets?",
                "Are these direct targets?",
                3,
                55,
                300,
                76,
                88,
            ),
            _PdfLine(
                "1. Figure 1 should be enlarged.",
                "1. Figure 1 should be enlarged.",
                4,
                55,
                400,
                94,
                106,
            ),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            [
                "The response paragraph ends here.",
                "Major concern: Are these direct targets?",
                "1. Figure 1 should be enlarged.",
            ],
        )

    def test_email_page_bold_signatures_are_not_scientific_headings(self) -> None:
        lines = [
            _PdfLine(
                "Subject: Re: author list",
                "<strong>Subject:</strong> Re: author list",
                1,
                55,
                400,
                40,
                52,
            ),
            _PdfLine(
                "From: Author <author@example.org>",
                "<strong>From:</strong> Author &lt;author@example.org&gt;",
                2,
                55,
                400,
                58,
                70,
            ),
            _PdfLine(
                "AUTHOR NAME",
                "<strong>AUTHOR NAME</strong>",
                3,
                55,
                240,
                100,
                112,
            ),
        ]

        self.assertTrue(_is_email_correspondence_page(lines))
        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines, recognize_headings=False)],
            ["Subject: Re: author list", "From: Author <author@example.org>", "AUTHOR NAME"],
        )
        self.assertFalse(
            _is_email_correspondence_page(
                [
                    _PdfLine(
                        "Subject: biochemical pathway",
                        "Subject: biochemical pathway",
                        1,
                        55,
                        400,
                        40,
                        52,
                    ),
                    _PdfLine(
                        "Methods",
                        "<strong>Methods</strong>",
                        2,
                        55,
                        200,
                        60,
                        72,
                    ),
                ]
            )
        )

    def test_adjacent_full_bold_pdf_lines_form_one_wrapped_heading(self) -> None:
        first = _PdfLine(
            "Novel strategy using pyrrole-",
            "**Novel strategy using pyrrole-**",
            1,
            80,
            500,
            100,
            112,
        )
        second = _PdfLine(
            "imidazole polyamide",
            "**imidazole polyamide**",
            2,
            80,
            300,
            120,
            132,
        )
        prose = _PdfLine(
            "Authors and affiliations.",
            "Authors and affiliations.",
            3,
            80,
            300,
            160,
            172,
        )

        paragraphs = _paragraphs([first, second, prose])

        self.assertEqual(
            [paragraph.text for paragraph in paragraphs],
            ["Novel strategy using pyrrole-imidazole polyamide", prose.text],
        )
        self.assertTrue(_is_standalone_bold_line(paragraphs[0].markdown))

        wrapped_without_hyphen = [
            _PdfLine(
                "Substitution to hydrophobic linker and formation of host–guest",
                "**Substitution to hydrophobic linker and formation of host–guest**",
                1,
                80,
                510,
                100,
                112,
            ),
            _PdfLine(
                "complex enhanced the effect of synthetic transcription factor",
                "**complex enhanced the effect of synthetic transcription factor**",
                2,
                90,
                500,
                118,
                130,
            ),
            _PdfLine(
                "made of pyrrole-imidazole polyamide",
                "**made of pyrrole-imidazole polyamide**",
                3,
                170,
                420,
                136,
                148,
            ),
            _PdfLine(
                "Authors and affiliations.",
                "Authors and affiliations.",
                4,
                80,
                350,
                166,
                178,
            ),
        ]
        paragraphs = _paragraphs(wrapped_without_hyphen)
        self.assertEqual(
            [paragraph.text for paragraph in paragraphs],
            [
                "Substitution to hydrophobic linker and formation of host–guest "
                "complex enhanced the effect of synthetic transcription factor "
                "made of pyrrole-imidazole polyamide",
                "Authors and affiliations.",
            ],
        )

        numbered_heading = _PdfLine(
            "2. Results",
            "**2. Results**",
            5,
            80,
            250,
            154,
            166,
        )
        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs([first, numbered_heading])],
            [first.text, numbered_heading.text],
        )

    def test_hyphenated_italic_compound_name_uses_observed_line_leading(self) -> None:
        lines = [
            _PdfLine(
                "N-(3-aminopropyl)-1-",
                "*N-(3-aminopropyl)-1-*",
                1,
                72,
                535,
                100,
                112,
            ),
            _PdfLine(
                "methylimidazole-2-carboxamide 2. To a flask",
                "*methylimidazole-2-carboxamide* **2**. To a flask",
                2,
                72,
                540,
                127.6,
                139.6,
            ),
            _PdfLine(
                "was added the reagent.",
                "was added the reagent.",
                3,
                72,
                300,
                155.2,
                167.2,
            ),
        ]

        paragraphs = _paragraphs(lines)

        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(
            paragraphs[0].text,
            "N-(3-aminopropyl)-1-methylimidazole-2-carboxamide 2. "
            "To a flask was added the reagent.",
        )
        self.assertEqual(
            paragraphs[0].markdown,
            "*N-(3-aminopropyl)-1-methylimidazole-2-carboxamide* "
            "**2**. To a flask was added the reagent.",
        )

    def test_positioned_rich_fragments_form_one_compound_paragraph(self) -> None:
        lines = [
            _PdfLine(
                "methyl",
                "**methyl**",
                1,
                169.4,
                204.6,
                198.0,
                209.0,
            ),
            _PdfLine(
                "1-methyl-imidazole-2-",
                "**1-methyl-imidazole-2-**",
                2,
                249.8,
                541.3,
                198.0,
                209.0,
            ),
            _PdfLine(
                "carboxylate (22): To a solution of substrate",
                "**carboxylate (22):** To a solution of substrate",
                3,
                169.4,
                544.0,
                211.7,
                222.7,
            ),
            _PdfLine(
                "the reagent was added.",
                "the reagent was added.",
                4,
                169.4,
                400.0,
                225.4,
                236.4,
            ),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            [
                "methyl 1-methyl-imidazole-2-carboxylate (22): "
                "To a solution of substrate the reagent was added."
            ],
        )

    def test_bold_scientific_label_after_hyphen_remains_in_paragraph(self) -> None:
        lines = [
            _PdfLine(
                "The sample was characterized by 13C-",
                "The sample was characterized by <sup>**13**</sup>**C-**",
                1,
                70.8,
                541.3,
                180.5,
                193.0,
            ),
            _PdfLine(
                "NMR (75 MHz) and FT-",
                "**NMR** (75 MHz) and **FT-**",
                2,
                70.8,
                541.3,
                195.6,
                206.7,
            ),
            _PdfLine(
                "IR spectroscopy.",
                "**IR** spectroscopy.",
                3,
                70.8,
                300.0,
                209.3,
                220.4,
            ),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            ["The sample was characterized by 13C-NMR (75 MHz) and FT-IR spectroscopy."],
        )

        byline = _PdfLine(
            "Junnosuke Hatanaka, Kaori Hashiya",
            "Junnosuke Hatanaka, Kaori Hashiya",
            6,
            150,
            445,
            180,
            192,
        )
        affiliation = _PdfLine(
            "a Department of Chemistry, Kyoto University",
            "a Department of Chemistry, Kyoto University",
            7,
            120,
            475,
            218,
            230,
        )
        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs([byline, affiliation])],
            [byline.text, affiliation.text],
        )

    def test_supporting_information_front_matter_keeps_authorship_separate(self) -> None:
        lines = [
            _PdfLine(
                "Supporting Information",
                "Supporting Information",
                1,
                85,
                230,
                103,
                117,
            ),
            _PdfLine(
                "A wrapped scientific title",
                "A wrapped scientific title",
                2,
                85,
                500,
                160,
                174,
            ),
            _PdfLine("continues here", "continues here", 3, 85, 300, 184, 198),
            _PdfLine(
                "First Author, Second Author",
                "First Author, Second Author",
                4,
                85,
                470,
                221,
                233,
            ),
            _PdfLine(
                "School of Pharmacy", "School of Pharmacy", 5, 85, 300, 254, 266
            ),
            _PdfLine(
                "Correspondence: author@example.org",
                "Correspondence: author@example.org",
                6,
                85,
                420,
                287,
                299,
            ),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            [
                "Supporting Information",
                "A wrapped scientific title continues here",
                "First Author, Second Author",
                "School of Pharmacy",
                "Correspondence: author@example.org",
            ],
        )

    def test_full_italic_pdf_line_is_an_independent_methods_heading(self) -> None:
        heading = _PdfLine(
            "Coupling with terminal Im-CCl3",
            "<em>Coupling with terminal Im-CCl</em><sub><em>3</em></sub>",
            1,
            55,
            300,
            40,
            52,
        )
        prose = _PdfLine(
            "The building block was activated.",
            "The building block was activated.",
            2,
            55,
            560,
            58,
            70,
        )

        self.assertTrue(_is_standalone_italic_line(heading.markdown))
        self.assertTrue(_is_standalone_italic_heading_line(heading.markdown))
        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs([heading, prose])],
            [heading.text, prose.text],
        )
        self.assertEqual(
            _heading_markup(heading.markdown),
            "Coupling with terminal Im-CCl<sub>3</sub>",
        )

        authors = (
            "<em>Sophie Kendall-Price,</em><sup><em>1</em></sup> "
            "<em>Ryan Nichol,</em><sup><em>2</em></sup>"
        )
        self.assertTrue(_is_standalone_italic_line(authors))
        self.assertFalse(_is_standalone_italic_heading_line(authors))
        self.assertFalse(_is_standalone_heading_line(authors))

        gene_symbol = "<em>Htt</em>"
        self.assertTrue(_is_standalone_italic_line(gene_symbol))
        self.assertFalse(_is_standalone_italic_heading_line(gene_symbol))
        self.assertFalse(_is_standalone_heading_line(gene_symbol))

    def test_wrapped_italic_si_affiliations_keep_style_and_are_not_headings(self) -> None:
        values = [
            ('SUPPORTING INFORMATION', 'SUPPORTING INFORMATION', 20, 32),
            ('Alice Author 1, Bob Author 2', 'Alice Author<sup>1</sup>, Bob Author<sup>2</sup>', 60, 70),
            ('1Department of Chemistry, Example University,', '<sup>*1*</sup>*Department of Chemistry, Example University,*', 72, 82),
            ('School, Example City, 12345, Country.', '*School, Example City, 12345, Country.*', 84, 94),
            ('2Department of Biology, Another University,', '<sup>*2*</sup>*Department of Biology, Another University,*', 96, 106),
            ('Another City, 54321, Country.', '*Another City, 54321, Country.*', 108, 118),
            ('Experimental procedure', '*Experimental procedure*', 155, 167),
            ('The sample was prepared.', 'The sample was prepared.', 170, 182),
        ]
        lines = [_PdfLine(text, markup, number, 50, 500, top, bottom)
                 for number, (text, markup, top, bottom) in enumerate(values, 1)]
        protected = _supplement_affiliation_line_numbers(lines)
        self.assertEqual(protected, {3, 4, 5, 6})
        paragraphs = _paragraphs(lines)
        affiliation = next(p for p in paragraphs if p.first_line <= 3 <= p.last_line)
        self.assertIn('School, Example City', affiliation.text)
        self.assertIn('*School, Example City', affiliation.markdown)
        heading = next(p for p in paragraphs if p.text == 'Experimental procedure')
        self.assertTrue(_is_pdf_heading(heading.markdown, heading.text))
        self.assertFalse(any(line.line_number in protected for line in heading.lines))
        self.assertEqual(_supplement_affiliation_line_numbers(lines[1:]), set())

    def test_short_multiword_uppercase_pdf_heading_is_structural(self) -> None:
        heading = _PdfLine(
            "AUTHOR INFORMATION",
            "AUTHOR INFORMATION",
            1,
            55,
            300,
            40,
            52,
        )
        prose = _PdfLine(
            "Corresponding author details follow.",
            "Corresponding author details follow.",
            2,
            55,
            560,
            58,
            70,
        )

        self.assertTrue(_is_standalone_uppercase_heading(heading.text))
        self.assertFalse(_is_standalone_uppercase_heading("NMR"))
        self.assertFalse(_is_standalone_uppercase_heading("DNA SAMPLE."))
        self.assertFalse(
            _is_standalone_uppercase_heading("TEL +81-76-264-6755 FAX +81-76")
        )
        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs([heading, prose])],
            [heading.text, prose.text],
        )

    def test_numbered_bold_pdf_headings_remain_separate(self) -> None:
        section = _PdfLine(
            "1. Supplementary methods",
            "1. **Supplementary methods**",
            1,
            55,
            300,
            40,
            52,
        )
        subsection = _PdfLine(
            "1.1. Synthetic general procedures",
            "1.1. **Synthetic general procedures**",
            2,
            55,
            300,
            76,
            88,
        )

        self.assertTrue(_is_standalone_heading_line(section.markdown))
        self.assertTrue(_is_standalone_heading_line(subsection.markdown))
        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs([section, subsection])],
            [section.text, subsection.text],
        )

    def test_bold_caption_continuation_is_not_misclassified_as_a_heading(self) -> None:
        first = _PdfLine(
            "Supplementary Figure 4. Inhibition of transcription and pathogenic RNA",
            "**Supplementary Figure 4. Inhibition of transcription and pathogenic RNA**",
            2,
            80,
            520,
            100,
            112,
        )
        continuation = _PdfLine(
            "in disease models by treatment.",
            "**in disease models by treatment.**",
            4,
            80,
            360,
            118,
            130,
        )
        detail = _PdfLine(
            "(A) Quantification of mRNA levels.",
            "**(A)** Quantification of mRNA levels.",
            6,
            80,
            360,
            136,
            148,
        )

        paragraphs = _paragraphs([first, continuation, detail])

        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0].first_line, 2)
        self.assertEqual(paragraphs[0].last_line, 6)
        self.assertFalse(
            _is_pdf_heading(paragraphs[0].markdown, paragraphs[0].text)
        )
        self.assertTrue(_is_pdf_heading("**Methods**", "Methods"))

    def test_caption_cross_reference_label_is_not_a_second_figure(self) -> None:
        first = _PdfLine(
            "Supplementary Figure S8. Flow cytometry was performed as shown in",
            "**Supplementary Figure S8.** Flow cytometry was performed as shown in",
            1,
            72,
            500,
            540,
            552,
        )
        cross_reference = _PdfLine(
            "Supplementary Figure S1.",
            "Supplementary Figure S1.",
            2,
            72,
            205,
            555,
            567,
        )

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs([first, cross_reference])],
            [
                "Supplementary Figure S8. Flow cytometry was performed as "
                "shown in Supplementary Figure S1."
            ],
        )

    def test_structured_supplement_figure_owns_its_caption(self) -> None:
        source_path = "papers (private)/00001/supplementary/support.pdf"
        blocks = [
            ContentBlock(
                block_id="supplement_001_block_001",
                kind="text",
                markdown="Methods text.",
                plain_text="Methods text.",
                source_path=source_path,
                source_locator="page=1;lines=1-2",
            ),
            ContentBlock(
                block_id="supplement_001_block_002",
                kind="figure_caption",
                markdown="**Figure S1.** Reviewed caption.",
                plain_text="Figure S1. Reviewed caption.",
                source_path=source_path,
                source_locator="page=2;lines=3-8",
            ),
            ContentBlock(
                block_id="supplement_001_block_003",
                kind="figure_caption",
                markdown="**Figure S2.** Unrelated caption.",
                plain_text="Figure S2. Unrelated caption.",
                source_path=source_path,
                source_locator="page=3;lines=1-4",
            ),
        ]
        figures = [
            FigureItem(
                figure_id="figure_s1",
                source_id="page=2;lines=3-8",
                label="Figure S1",
                kind="figure",
                caption_markdown="**Figure S1.** Reviewed caption.",
                caption_plain="Figure S1. Reviewed caption.",
                source_path=source_path,
                source_locator="page=2;lines=3-8",
                output_path="figures/s1.png",
            )
        ]

        retained = _consolidate_supplement_figure_captions(blocks, figures)

        self.assertEqual(
            [block.block_id for block in retained],
            ["supplement_001_block_001", "supplement_001_block_003"],
        )

        reviewed_page_figure = FigureItem(
            figure_id="figure_s2_reviewed",
            source_id="Figure S2",
            label="Figure S2",
            kind="figure",
            caption_markdown="Complete reviewed Figure S2 caption.",
            caption_plain="Complete reviewed Figure S2 caption.",
            source_path=source_path,
            source_locator="support.pdf;page=3;reviewed-complete-caption",
            output_path="figures/s2.png",
        )
        self.assertEqual(
            [block.block_id for block in _consolidate_supplement_figure_captions(
                blocks, [figures[0], reviewed_page_figure]
            )],
            ["supplement_001_block_001"],
        )

        reviewed_other_page = FigureItem(
            figure_id="figure_s2_other_page",
            source_id="Figure S2",
            label="Figure S2",
            kind="figure",
            caption_markdown="Another page.",
            caption_plain="Another page.",
            source_path=source_path,
            source_locator="support.pdf;page=4;reviewed-complete-caption",
            output_path="figures/s2-other.png",
        )
        self.assertIn(
            "supplement_001_block_003",
            [block.block_id for block in _consolidate_supplement_figure_captions(
                blocks, [figures[0], reviewed_other_page]
            )],
        )

    def test_exact_caption_only_docx_figure_yields_to_separate_pdf_pixels(self) -> None:
        docx_source = SourceFile(
            role="supplement",
            path=Path("captions.docx"),
            relative_path="papers (private)/00001/supplementary/captions.docx",
            size=3,
            sha256="1" * 64,
            detected_format=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )
        pdf_source = SourceFile(
            role="supplement",
            path=Path("figures.pdf"),
            relative_path="papers (private)/00001/supplementary/figures.pdf",
            size=3,
            sha256="2" * 64,
            detected_format="application/pdf",
        )
        caption = "Viability was measured at 72 hours."
        caption_only = FigureItem(
            figure_id="supplement_001_figure_s1",
            source_id="Supplementary Fig. 1",
            label="Supplementary Fig. 1",
            kind="figure",
            caption_markdown=f"Supplementary Fig. 1<br>{caption}",
            caption_plain=f"Supplementary Fig. 1\n{caption}",
            source_path=docx_source.relative_path,
            source_locator="docx-part=word/document.xml;body-children=1-3",
        )
        pixels = FigureItem(
            figure_id="supplement_002_figure_1",
            source_id="Supplementary Figure 1",
            label="Supplementary Figure 1",
            kind="figure",
            caption_markdown=caption,
            caption_plain=caption,
            source_path=pdf_source.relative_path,
            source_locator="page=1;reviewed-complete-caption",
            output_path="supplementary/supplement_002/figures/figure_001.png",
        )
        supplements = [
            SupplementExtraction(
                "supplement_001",
                docx_source,
                "supplementary/supplement_001/captions.docx",
                [],
                [caption_only],
                [],
            ),
            SupplementExtraction(
                "supplement_002",
                pdf_source,
                "supplementary/supplement_002/figures.pdf",
                [],
                [pixels],
                [],
            ),
        ]

        _consolidate_cross_file_caption_figures(supplements)

        self.assertEqual(supplements[0].figures, [])
        self.assertEqual(supplements[1].figures, [pixels])
        self.assertEqual(pixels.caption_plain, caption_only.caption_plain)
        self.assertEqual(pixels.caption_markdown, caption_only.caption_markdown)
        self.assertEqual(len(supplements[0].exclusions), 1)
        self.assertEqual(
            supplements[0].exclusions[0]["content_kind"], "figure_caption"
        )

        # A caption mismatch is not silently deduplicated.
        caption_only.caption_plain = "Supplementary Fig. 1\nDifferent result."
        supplements[0].figures = [caption_only]
        supplements[0].exclusions = []
        _consolidate_cross_file_caption_figures(supplements)
        self.assertEqual(supplements[0].figures, [caption_only])
        self.assertEqual(supplements[0].exclusions, [])

    def test_hash_pinned_line_furniture_exclusion_is_geometry_and_pattern_bound(self) -> None:
        exclusions = _supplement_line_exclusions(
            {
                "line_exclusions": [
                    {
                        "pages": [1, 2],
                        "box": [45, 90, 75, 715],
                        "pattern": r"\d{1,3}",
                        "reason": "Authored manuscript line numbers are not content.",
                        "evidence": "Reviewed left gutter on pages 1-2.",
                    }
                ]
            }
        )
        lines = [
            _PdfLine("17", "17", 1, 52, 69, 100, 110),
            _PdfLine("17", "17", 2, 300, 313, 735, 745),
            _PdfLine("Content", "Content", 3, 85, 200, 100, 112),
        ]

        retained = _apply_supplement_line_exclusions(lines, 1, exclusions)

        self.assertEqual([line.text for line in retained], ["17", "Content"])
        self.assertEqual(exclusions[0]["hits"], 1)

    def test_reviewed_table_line_exclusion_counts_before_bbox_filter(self) -> None:
        exclusions = _supplement_line_exclusions(
            {
                "line_exclusions": [
                    {
                        "pages": [2],
                        "box": [0, 0, 600, 842],
                        "pattern": r"Hairpin polyamide",
                        "reason": "This table cell has an exact semantic transcription.",
                        "evidence": "Reviewed Table S1 on page 2.",
                    }
                ]
            }
        )
        lines = [
            _PdfLine("Hairpin polyamide", "Hairpin polyamide", 1, 70, 180, 190, 202),
            _PdfLine("ATAT", "ATAT", 2, 210, 250, 210, 222),
            _PdfLine("Methods", "Methods", 3, 70, 140, 700, 712),
        ]

        after_source_review = _apply_supplement_line_exclusions(lines, 2, exclusions)
        retained = _exclude_reviewed_pdf_table_lines(
            after_source_review, [(65, 185, 530, 605)]
        )

        self.assertEqual(exclusions[0]["hits"], 1)
        self.assertEqual([line.text for line in retained], ["Methods"])

    def test_captioned_pdf_crop_becomes_an_exact_semantic_figure(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("support.pdf"),
            relative_path="papers (private)/00001/supplementary/support.pdf",
            size=3,
            sha256="0" * 64,
            detected_format="application/pdf",
        )

        figures = _reviewed_pdf_crop_figures(
            source,
            [
                {
                    "source_path": source.relative_path,
                    "asset_id": "supplement_001_figure_1",
                    "category": "supplement_figure",
                    "label": "Supplemental Figure 1",
                    "caption_plain": "Reviewed caption with γH2AX.",
                    "caption_markdown": "Reviewed caption with γH2AX.",
                    "caption_source_locator": "page=1;caption-bbox=10,300,400,350",
                }
            ],
        )

        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0].figure_id, "supplement_001_figure_1")
        self.assertEqual(figures[0].caption_plain, "Reviewed caption with γH2AX.")
        self.assertEqual(figures[0].kind, "figure")

    def test_reviewed_unnumbered_pdf_visual_requires_explicit_semantic_kind(
        self,
    ) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("support.pdf"),
            relative_path="papers (private)/00001/supplementary/support.pdf",
            size=3,
            sha256="0" * 64,
            detected_format="application/pdf",
        )

        figures = _reviewed_pdf_crop_figures(
            source,
            [
                {
                    "source_path": source.relative_path,
                    "asset_id": "supplement_001_hplc_profile",
                    "category": "supplement_figure",
                    "label": "Analytical HPLC profile",
                    "semantic_kind": "figure",
                    "caption_plain": "Analytical HPLC profile of compound 1.",
                    "caption_markdown": "Analytical HPLC profile of compound 1.",
                    "caption_source_locator": "page=2;native-line=5",
                }
            ],
        )

        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0].label, "Analytical HPLC profile")
        self.assertEqual(figures[0].kind, "figure")

        with self.assertRaisesRegex(ValueError, "invalid semantic_kind"):
            _reviewed_pdf_crop_figures(
                source,
                [
                    {
                        "source_path": source.relative_path,
                        "asset_id": "supplement_001_hplc_profile",
                        "category": "supplement_figure",
                        "label": "Analytical HPLC profile",
                        "semantic_kind": "chart",
                        "caption_plain": "Analytical HPLC profile of compound 1.",
                        "caption_markdown": "Analytical HPLC profile of compound 1.",
                        "caption_source_locator": "page=2;native-line=5",
                    }
                ],
            )

    def test_supplemental_native_pdf_caption_is_recognized(self) -> None:
        caption = "Supplemental Figure 3. Experimental protocol."

        self.assertIsNotNone(CAPTION_PATTERN.match(caption))
        self.assertTrue(is_figure_caption(caption))

    def test_reviewed_supplement_crop_regions_cover_all_composite_pages(self) -> None:
        self.assertEqual(
            _reviewed_pdf_crop_regions(
                {
                    "parts": [
                        {"page": 6, "box": [50, 40, 550, 760]},
                        {"page": 7, "box": [50, 40, 550, 750]},
                        {"page": 8, "box": [50, 40, 550, 740]},
                    ]
                }
            ),
            [
                (6, (50.0, 40.0, 550.0, 760.0)),
                (7, (50.0, 40.0, 550.0, 750.0)),
                (8, (50.0, 40.0, 550.0, 740.0)),
            ],
        )
        with self.assertRaisesRegex(ValueError, "cannot combine"):
            _reviewed_pdf_crop_regions(
                {
                    "page": 6,
                    "box": [50, 40, 550, 760],
                    "parts": [
                        {"page": 6, "box": [50, 40, 550, 760]},
                        {"page": 7, "box": [50, 40, 550, 750]},
                    ],
                }
            )

    def test_complete_reviewed_pdf_figure_map_explicitly_replaces_auto_noise(self) -> None:
        def figure(identifier: str) -> FigureItem:
            return FigureItem(
                figure_id=identifier,
                source_id=identifier,
                label="Figure S1",
                kind="figure",
                caption_markdown="Reviewed caption.",
                caption_plain="Reviewed caption.",
                source_path="support.pdf",
                source_locator="page=1",
            )

        automatic = [figure("figure_s1"), figure("figure_s1_fragment")]
        reviewed = [figure("figure_s1")]

        self.assertEqual(
            [item.figure_id for item in _combine_reviewed_pdf_figures(
                automatic, reviewed, policy="merge"
            )],
            ["figure_s1", "figure_s1_fragment"],
        )
        automatic_in_source_order = [
            figure("figure_s1"),
            figure("figure_s2"),
            figure("figure_s3"),
        ]
        reviewed_late_figure = [figure("figure_s3")]
        self.assertEqual(
            [
                item.figure_id
                for item in _combine_reviewed_pdf_figures(
                    automatic_in_source_order,
                    reviewed_late_figure,
                    policy="merge",
                )
            ],
            ["figure_s1", "figure_s2", "figure_s3"],
        )
        self.assertEqual(
            [item.figure_id for item in _combine_reviewed_pdf_figures(
                automatic, reviewed, policy="replace"
            )],
            ["figure_s1"],
        )
        with self.assertRaisesRegex(ValueError, "requires reviewed figures"):
            _combine_reviewed_pdf_figures(
                automatic, [], policy="replace"
            )

        warnings = [
            {"code": "image_only_page", "page": 1},
            {"code": "supplement_pdf_page_native_text_empty", "page": 1},
            {"code": "supplement_pdf_page_native_text_empty", "page": 2},
        ]
        self.assertEqual(
            _resolved_reviewed_pdf_warnings(
                warnings, [1], policy="replace"
            ),
            [{"code": "supplement_pdf_page_native_text_empty", "page": 2}],
        )
        self.assertEqual(
            _resolved_reviewed_pdf_warnings(
                warnings, [1], policy="merge"
            ),
            warnings,
        )

        visual_warnings = [
            {"code": "image_only_page", "page": 3},
            {"code": "supplement_pdf_page_native_text_empty", "page": 3},
            {"code": "supplement_pdf_page_native_text_empty", "page": 4},
        ]
        self.assertEqual(
            _resolved_reviewed_pdf_warnings(
                visual_warnings,
                [],
                policy="merge",
                reviewed_visual_pages=[3],
            ),
            [
                {"code": "image_only_page", "page": 3},
                {"code": "supplement_pdf_page_native_text_empty", "page": 4},
            ],
        )

        source = SourceFile(
            role="supplement",
            path=Path("support.pdf"),
            relative_path="papers (private)/00001/supplementary/support.pdf",
            size=3,
            sha256="0" * 64,
            detected_format="application/pdf",
        )
        self.assertEqual(
            _reviewed_visual_pdf_pages(
                source,
                [
                    {
                        "source_path": source.relative_path,
                        "category": "supplement_page_render",
                        "label": "NMR source page 3",
                        "page": 3,
                    },
                    {
                        "source_path": source.relative_path,
                        "category": "supplement_image",
                        "label": "Figure S1",
                        "page": 4,
                    },
                    {
                        "source_path": source.relative_path,
                        "category": "supplement_figure",
                        "label": "Figure S2",
                        "page": 6,
                    },
                    {
                        "source_path": source.relative_path,
                        "category": "supplement_image",
                        "label": "S-15",
                        "semantic_kind": "figure",
                        "page": 8,
                    },
                    {
                        "source_path": source.relative_path,
                        "category": "supplement_table",
                        "label": "Table S1",
                        "page": 5,
                    },
                    {
                        "source_path": source.relative_path,
                        "category": "supplement_table",
                        "label": "Unlabeled crop",
                        "page": 7,
                    },
                    {
                        "source_path": source.relative_path,
                        "category": "supplement_table",
                        "label": "Table S2",
                        "parts": [
                            {"page": 9, "box": [0, 0, 10, 10]},
                            {"page": 10, "box": [0, 0, 10, 10]},
                        ],
                    },
                    {
                        "source_path": "different.pdf",
                        "category": "supplement_page_render",
                        "label": "Wrong source",
                        "page": 6,
                    },
                ],
            ),
            {3, 4, 5, 6, 8, 9, 10},
        )

    def test_source_empty_warning_requires_complete_semantic_and_visual_review(
        self,
    ) -> None:
        warnings = [
            {"code": "supplement_pdf_native_text_empty"},
            {"code": "supplement_pdf_page_native_text_empty", "page": 1},
            {"code": "image_only_page", "page": 2},
            {"code": "another_warning"},
        ]

        self.assertEqual(
            _resolved_fully_reviewed_pdf_native_text_warning(
                warnings,
                page_count=3,
                reviewed_semantic_locators=["page=1", "pages=2-3"],
                reviewed_visual_pages=[1, 2, 3],
            ),
            [{"code": "another_warning"}],
        )
        self.assertEqual(
            _resolved_fully_reviewed_pdf_native_text_warning(
                warnings,
                page_count=3,
                reviewed_semantic_locators=["page=1", "page=2"],
                reviewed_visual_pages=[1, 2, 3],
            ),
            warnings,
        )
        self.assertEqual(
            _resolved_fully_reviewed_pdf_native_text_warning(
                warnings,
                page_count=3,
                reviewed_semantic_locators=["page=1", "pages=2-3"],
                reviewed_visual_pages=[1, 2],
            ),
            warnings,
        )

    def test_asset_only_pdf_crop_is_not_promoted_to_semantic_figure(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("support.pdf"),
            relative_path="papers (private)/00001/supplementary/support.pdf",
            size=3,
            sha256="0" * 64,
            detected_format="application/pdf",
        )

        self.assertEqual(
            _reviewed_pdf_crop_figures(
                source,
                [
                    {
                        "source_path": source.relative_path,
                        "asset_id": "supplement_001_figure_1",
                        "category": "supplement_figure",
                        "label": "Supplementary Figure 1",
                    }
                ],
            ),
            [],
        )

    def test_partially_captioned_pdf_crop_requires_reviewed_locator(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("support.pdf"),
            relative_path="papers (private)/00001/supplementary/support.pdf",
            size=3,
            sha256="0" * 64,
            detected_format="application/pdf",
        )

        with self.assertRaisesRegex(ValueError, "incomplete"):
            _reviewed_pdf_crop_figures(
                source,
                [
                    {
                        "source_path": source.relative_path,
                        "asset_id": "supplement_001_figure_1",
                        "category": "supplement_figure",
                        "label": "Supplementary Figure 1",
                        "caption_plain": "Reviewed caption without a locator.",
                    }
                ],
            )

    def test_caption_prefix_spacing_is_normalized_without_loose_matching(self) -> None:
        match = CAPTION_PATTERN.match("Figure S 9. Reviewed caption.")

        self.assertIsNotNone(match)
        self.assertEqual(normalize_caption_number(match.group("number")), "S9")
        self.assertIsNone(CAPTION_PATTERN.match("Figure supplemental nine. Caption."))

    def test_caption_pattern_accepts_exact_native_pdf_intra_word_break(self) -> None:
        match = CAPTION_PATTERN.match("Fi gure S13. Reviewed caption.")

        self.assertIsNotNone(match)
        self.assertTrue(is_figure_caption("Fi gure S13. Reviewed caption."))
        self.assertFalse(is_figure_caption("Fi gure result without a number."))

    def test_caption_pattern_accepts_compact_fig_dot_label(self) -> None:
        match = CAPTION_PATTERN.match("Fig.S1. Reviewed caption.")

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group("number"), "S1")

    def test_hash_pinned_equation_override_orders_and_replaces_native_lines(self) -> None:
        config = {
            "equation_overrides": [
                {
                    "page": 3,
                    "after_line": 1,
                    "replace_lines": [2, 2],
                    "plain_text": "ΔAbs(x) = ΔAbs(0) · exp(−|x|/ξ) (1)",
                    "markdown": "ΔAbs(<em>x</em>) = ΔAbs(0) · exp(−|<em>x</em>|/ξ) (1)",
                    "source_locator": "page=3;equation=1",
                    "reason": "Native geometry interleaves the fraction.",
                    "evidence": "Reviewed page 3.",
                    "box": [10, 20, 100, 40],
                }
            ]
        }
        equations = _supplement_equation_overrides(config)
        lines = [
            _PdfLine("Before", "Before", 1, 0, 50, 0, 10),
            _PdfLine("broken", "broken", 2, 0, 50, 12, 22),
            _PdfLine("After", "After", 3, 0, 50, 40, 50),
        ]

        events = _pdf_page_events(lines, equations[3])

        self.assertEqual([kind for kind, _ in events], ["paragraph", "equation", "paragraph"])
        self.assertEqual(events[0][1].text, "Before")
        self.assertEqual(events[1][1]["plain_text"], config["equation_overrides"][0]["plain_text"])
        self.assertEqual(events[2][1].text, "After")

    def test_equation_override_fails_when_line_range_drifts_into_prose(self) -> None:
        config = {
            "equation_overrides": [
                {
                    "page": 1,
                    "after_line": 4,
                    "replace_lines": [2, 4],
                    "plain_text": "Q = A/B (1)",
                    "markdown": "<em>Q</em> = <em>A</em>/<em>B</em> (1)",
                    "source_locator": "page=1;equation=1;native-lines=2-4",
                    "reason": "Native geometry interleaves the fraction.",
                    "evidence": "Reviewed page 1.",
                    "box": [100, 20, 300, 42],
                }
            ]
        }
        lines = [
            _PdfLine("Before", "Before", 1, 50, 350, 0, 12),
            _PdfLine("fraction", "fraction", 2, 120, 220, 20, 32),
            _PdfLine("(1)", "(1)", 3, 260, 290, 24, 36),
            _PdfLine("Definition follows.", "Definition follows.", 4, 50, 350, 48, 60),
        ]

        equations = _supplement_equation_overrides(config)
        with self.assertRaisesRegex(
            ValueError,
            "outside the reviewed equation box: 4",
        ):
            _pdf_page_events(lines, equations[1])

        config["equation_overrides"][0].update(
            {
                "after_line": 3,
                "replace_lines": [2, 3],
                "source_locator": "page=1;equation=1;native-lines=2-3",
            }
        )
        events = _pdf_page_events(
            lines,
            _supplement_equation_overrides(config)[1],
        )

        self.assertEqual(
            [kind for kind, _ in events],
            ["paragraph", "equation", "paragraph"],
        )
        self.assertEqual(events[2][1].text, "Definition follows.")

    def test_paragraph_override_is_bound_to_exact_page_and_native_lines(self) -> None:
        overrides = _supplement_paragraph_overrides(
            {
                "paragraph_overrides": [
                    {
                        "page": 25,
                        "first_line": 26,
                        "last_line": 27,
                        "plain_text": "Equation S1. Propagation length ξ.",
                        "markdown": "**Equation S1.** Propagation length ξ.",
                        "source_locator": "page=25;native-lines=26-27;reviewed",
                        "reason": "Mathematical alphabet normalization.",
                        "evidence": "Reviewed PDF page 25.",
                    }
                ]
            }
        )

        self.assertEqual(
            overrides[(25, 26, 27)]["plain_text"],
            "Equation S1. Propagation length ξ.",
        )

    def test_reviewed_paragraph_range_can_span_automatic_boundaries(self) -> None:
        lines = [
            _PdfLine("First", "First", 2, 55, 560, 40, 52),
            _PdfLine("continued", "continued", 3, 55, 560, 85, 97),
            _PdfLine("After", "After", 4, 55, 560, 130, 142),
        ]

        events = _pdf_page_events(lines, [], [(2, 3)])

        self.assertEqual([kind for kind, _ in events], ["paragraph", "paragraph"])
        self.assertEqual(events[0][1].text, "First continued")
        self.assertEqual(events[0][1].first_line, 2)
        self.assertEqual(events[0][1].last_line, 3)
        self.assertEqual(events[1][1].text, "After")

        with self.assertRaisesRegex(ValueError, "are absent"):
            _pdf_page_events(lines, [], [(1, 3)])

        with self.assertRaisesRegex(ValueError, "overlap"):
            _pdf_page_events(lines, [], [(2, 3), (3, 4)])

    def test_reviewed_paragraph_range_can_name_hash_pinned_excluded_lines(self) -> None:
        retained = [
            _PdfLine("First", "First", 10, 55, 560, 40, 52),
            _PdfLine("continued", "continued", 11, 55, 560, 55, 67),
            _PdfLine("after drawing", "after drawing", 13, 55, 560, 85, 97),
            _PdfLine("finished", "finished", 14, 55, 560, 100, 112),
        ]

        events = _pdf_page_events(retained, [], [(10, 14, (12,))])

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1].text, "First continued after drawing finished")

        with self.assertRaisesRegex(ValueError, "still present"):
            _pdf_page_events(
                retained
                + [_PdfLine("drawing", "drawing", 12, 500, 550, 70, 82)],
                [],
                [(10, 14, (12,))],
            )

    def test_source_mode_paragraph_range_keeps_decoded_rich_text(self) -> None:
        item = {
            "page": 1,
            "first_line": 1,
            "last_line": 2,
            "mode": "source",
            "block_kind": "subsection_heading",
            "source_locator": "page=1;native-lines=1-2",
            "reason": "Join one visually reviewed paragraph.",
            "evidence": "Exact native lines 1-2.",
        }
        parsed = _supplement_paragraph_overrides(
            {"paragraph_overrides": [item]}
        )
        self.assertTrue(parsed[(1, 1, 2)]["use_source_text"])
        self.assertEqual(parsed[(1, 1, 2)]["plain_text"], "")
        self.assertEqual(
            parsed[(1, 1, 2)]["block_kind"], "subsection_heading"
        )

        malformed = dict(item)
        malformed["plain_text"] = "unexpected"
        with self.assertRaisesRegex(ValueError, "incomplete"):
            _supplement_paragraph_overrides(
                {"paragraph_overrides": [malformed]}
            )

        invalid_kind = dict(item)
        invalid_kind["block_kind"] = "figure_caption"
        with self.assertRaisesRegex(ValueError, "incomplete"):
            _supplement_paragraph_overrides(
                {"paragraph_overrides": [invalid_kind]}
            )

    def test_source_mode_paragraph_range_can_preserve_an_authored_list(self) -> None:
        parsed = _supplement_paragraph_overrides(
            {
                "paragraph_overrides": [
                    {
                        "page": 1,
                        "first_line": 8,
                        "last_line": 9,
                        "mode": "source",
                        "block_kind": "list",
                        "source_locator": "page=1;native-lines=8-9",
                        "reason": "The source labels these lines as a contents item.",
                        "evidence": "Reviewed PDF page 1.",
                    }
                ]
            }
        )

        self.assertEqual(parsed[(1, 8, 9)]["block_kind"], "list")
        self.assertTrue(parsed[(1, 8, 9)]["use_source_text"])

    def test_reviewed_exclusions_do_not_masquerade_as_an_empty_text_layer(self) -> None:
        self.assertTrue(
            _should_warn_supplement_page_native_text_empty(
                had_native_content_lines=False,
                retained_lines=[],
                has_graphic_content=True,
            )
        )
        self.assertFalse(
            _should_warn_supplement_page_native_text_empty(
                had_native_content_lines=True,
                retained_lines=[],
                has_graphic_content=True,
            )
        )
        self.assertFalse(
            _should_warn_supplement_page_native_text_empty(
                had_native_content_lines=False,
                retained_lines=[],
                has_graphic_content=False,
            )
        )

    def test_source_review_can_reclassify_caption_looking_prose_as_text(self) -> None:
        item = {
            "page": 10,
            "first_line": 2,
            "last_line": 7,
            "mode": "source",
            "block_kind": "text",
            "source_locator": "page=10;native-lines=2-7",
            "reason": "Peer-review prose refers to a supplementary figure.",
            "evidence": "Reviewed native lines 2-7 on PDF page 10.",
        }

        parsed = _supplement_paragraph_overrides(
            {"paragraph_overrides": [item]}
        )

        self.assertTrue(parsed[(10, 2, 7)]["use_source_text"])
        self.assertEqual(parsed[(10, 2, 7)]["block_kind"], "text")

        near_miss = dict(item)
        near_miss["block_kind"] = "figure_caption"
        with self.assertRaisesRegex(ValueError, "incomplete"):
            _supplement_paragraph_overrides(
                {"paragraph_overrides": [near_miss]}
            )

    def test_reviewed_page_blocks_replace_only_the_source_pinned_page(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("reporting-summary.pdf"),
            relative_path="papers (private)/00001/supplementary/reporting-summary.pdf",
            size=3,
            sha256="a" * 64,
            detected_format="application/pdf",
            page_count=2,
        )
        config = {
            "reviewed_page_blocks": [
                {
                    "page": 1,
                    "block_kind": "subsection_heading",
                    "plain_text": "Reporting summary",
                    "markdown": "Reporting summary",
                    "source_locator": "page=1;reviewed-document-text",
                    "reason": "The flattened form has no usable native text.",
                    "evidence": "Offline OCR was visually reviewed against page 1.",
                },
                {
                    "page": 1,
                    "plain_text": "Data analysis: Nothing",
                    "source_locator": "page=1;reviewed-document-text;field=data-analysis",
                    "reason": "The flattened form has no usable native text.",
                    "evidence": "Offline OCR was visually reviewed against page 1.",
                },
            ]
        }
        reviewed = _supplement_reviewed_page_blocks(
            config, source, "supplement_001"
        )
        native = [
            ContentBlock(
                "native-1",
                "text",
                "residue",
                "residue",
                source.relative_path,
                "page=1;native-lines=1-1",
            ),
            ContentBlock(
                "native-2",
                "text",
                "retained",
                "retained",
                source.relative_path,
                "page=2;native-lines=1-1",
            ),
        ]

        applied = _apply_supplement_reviewed_page_blocks(native, reviewed)

        self.assertEqual(
            [block.plain_text for block in applied],
            ["Reporting summary", "Data analysis: Nothing", "retained"],
        )
        self.assertEqual(
            _resolved_reviewed_page_text_warnings(
                [
                    {"code": "image_only_page", "page": 1},
                    {
                        "code": "supplement_pdf_page_native_text_empty",
                        "page": 1,
                    },
                    {
                        "code": "supplement_pdf_page_native_text_empty",
                        "page": 2,
                    },
                ],
                reviewed,
                page_count=2,
            ),
            [{"code": "supplement_pdf_page_native_text_empty", "page": 2}],
        )

        near_miss = dict(config["reviewed_page_blocks"][0])
        near_miss["source_locator"] = "page=2;reviewed-document-text"
        with self.assertRaisesRegex(ValueError, "incomplete"):
            _supplement_reviewed_page_blocks(
                {"reviewed_page_blocks": [near_miss]},
                source,
                "supplement_001",
            )

    def test_reviewed_paragraph_survives_graphic_proximity_filter(self) -> None:
        paragraph = _PdfParagraph(
            lines=[
                _PdfLine(
                    "2.2. HPLC and MS profiles",
                    "**2.2. HPLC and MS profiles**",
                    1,
                    85,
                    320,
                    104,
                    115,
                    overlaps_figure_graphic=True,
                )
            ]
        )

        self.assertTrue(
            _is_unreviewed_graphic_only_paragraph(
                paragraph,
                is_caption=False,
                is_footnote=False,
                has_reviewed_override=False,
            )
        )
        self.assertFalse(
            _is_unreviewed_graphic_only_paragraph(
                paragraph,
                is_caption=False,
                is_footnote=False,
                has_reviewed_override=True,
            )
        )

    def test_reviewed_pdf_image_crop_is_a_graphic_text_region(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("support.pdf"),
            relative_path="papers (private)/00005/supplementary/support.pdf",
            size=1,
            sha256="a" * 64,
            detected_format="application/pdf",
            page_count=2,
        )
        common = {
            "source_path": source.relative_path,
            "page": 1,
            "box": [10, 20, 100, 200],
        }

        regions = _reviewed_pdf_graphic_regions(
            source,
            [
                {**common, "category": "supplement_image"},
                {**common, "page": 2, "category": "supplement_table"},
            ],
        )

        self.assertEqual(regions, {1: [(10.0, 20.0, 100.0, 200.0)]})

    def test_caption_still_survives_graphic_proximity_filter(self) -> None:
        paragraph = _PdfParagraph(
            lines=[
                _PdfLine(
                    "Figure S1. Trace.",
                    "Figure S1. Trace.",
                    3,
                    85,
                    320,
                    330,
                    342,
                    overlaps_figure_graphic=True,
                )
            ]
        )

        self.assertFalse(
            _is_unreviewed_graphic_only_paragraph(
                paragraph,
                is_caption=True,
                is_footnote=False,
                has_reviewed_override=False,
            )
        )

    def test_caption_without_period_requires_uppercase_caption_text(self) -> None:
        match = CAPTION_PATTERN.match("Figure S1 Analytical HPLC trace.")
        self.assertIsNotNone(match)
        self.assertEqual(normalize_caption_number(match.group("number")), "S1")
        self.assertIsNone(CAPTION_PATTERN.match("Figure S1 shows the result."))

    def test_unstyled_pdf_text_escapes_literal_markup_controls(self) -> None:
        class Page:
            images = []
            lines = []
            curves = []
            rects = []

            @staticmethod
            def extract_text_lines(**_kwargs):
                return [
                    {
                        "text": (
                            "Burley,2,* Hunt1,* and A_B < C & D; "
                            "[191PtCl(H2O)](2−n)−"
                        ),
                        "x0": 10,
                        "x1": 200,
                        "top": 20,
                        "bottom": 30,
                    }
                ]

        [line] = _extract_page_lines(Page())

        self.assertEqual(
            line.markdown,
            (
                r"Burley,2,\* Hunt1,\* and A\_B &lt; C &amp; D; "
                r"\[191PtCl\(H2O\)\]\(2−n\)−"
            ),
        )
        self.assertNotIn("<a ", inline_markup_to_safe_html(line.markdown))
        self.assertTrue(
            rich_text_matches_plain(
                line.text,
                inline_markup_to_safe_html(line.markdown),
            )
        )
        self.assertEqual(
            inline_markup_to_safe_html("[source](https://example.test/item)"),
            '<a href="https://example.test/item">source</a>',
        )

    def test_pdf_graphic_filter_does_not_consume_adjacent_prose(self) -> None:
        class Page:
            images = [{"x0": 100, "x1": 500, "top": 100, "bottom": 400}]
            lines = []
            # The first path duplicates the prose glyph geometry. The second
            # is close enough to connect that path to the raster image under
            # the vector-assembly tolerance.
            curves = [
                {"x0": 50, "x1": 550, "top": 82, "bottom": 94},
                {"x0": 100, "x1": 500, "top": 95, "bottom": 100},
            ]
            rects = []

            @staticmethod
            def extract_text_lines(**_kwargs):
                return [
                    {
                        "text": "Authored prose immediately above the figure.",
                        "x0": 50,
                        "x1": 550,
                        "top": 82,
                        "bottom": 94,
                    },
                    {
                        "text": "Native plot label",
                        "x0": 190,
                        "x1": 280,
                        "top": 105,
                        "bottom": 117,
                    },
                ]

        prose, plot_label = _extract_page_lines(Page())

        self.assertFalse(prose.overlaps_figure_graphic)
        self.assertTrue(plot_label.overlaps_figure_graphic)

    def test_styled_pdf_corresponding_author_star_stays_inside_strong_run(self) -> None:
        markup = r"**Fuyuhiko Tamanoi** <sup>**1,**</sup>**\***"
        safe_html = inline_markup_to_safe_html(markup)

        self.assertEqual(
            safe_html,
            "<strong>Fuyuhiko Tamanoi</strong> "
            "<sup><strong>1,</strong></sup><strong>*</strong>",
        )
        self.assertTrue(
            rich_text_matches_plain("Fuyuhiko Tamanoi ^{1,}*", safe_html)
        )

    def test_ooxml_presentation_is_not_mislabeled_as_generic_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "supplement.pptx"
            with zipfile.ZipFile(path, "w") as package:
                package.writestr(
                    "[Content_Types].xml",
                    (
                        '<Types xmlns="http://schemas.openxmlformats.org/package/'
                        '2006/content-types"><Override PartName="/ppt/presentation.xml" '
                        'ContentType="application/vnd.openxmlformats-officedocument.'
                        'presentationml.presentation.main+xml"/></Types>'
                    ),
                )
            self.assertEqual(
                detect_format(path),
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation",
            )

    def test_numbered_supplements_sort_naturally(self) -> None:
        names = [
            "supplementary_10.pptx",
            "supplementary_2.pptx",
            "supplementary.pptx",
            "supplementary_17.xml",
        ]
        self.assertEqual(
            sorted(names, key=_natural_path_key),
            [
                "supplementary.pptx",
                "supplementary_2.pptx",
                "supplementary_10.pptx",
                "supplementary_17.xml",
            ],
        )

    def test_reviewed_split_pdf_caption_override_is_normalized(self) -> None:
        result = _supplement_caption_overrides(
            {
                "caption_overrides": [
                    {
                        "kind": "Figure",
                        "number": "3",
                        "plain_text": "Supporting Figure 3.  Complete caption.",
                        "markdown": "Supporting Figure 3. Complete *caption*.",
                        "source_locator": "pages=6-7;reviewed-caption",
                    }
                ]
            }
        )

        self.assertEqual(
            result[("figure", "3")],
            {
                "plain_text": "Supporting Figure 3. Complete caption.",
                "markdown": "Supporting Figure 3. Complete *caption*.",
                "source_locator": "pages=6-7;reviewed-caption",
            },
        )

    def test_scientific_pdf_symbols_and_hyphen_continuation_are_normalized(self) -> None:
        self.assertEqual(
            _normalize_space(
                "DNA 5’-triphosphate and 3´ end; 3’. at 37 ºC; "
                "triﬂuoroacetic (complex) ."
            ),
            "DNA 5′-triphosphate and 3′ end; 3′. at 37 °C; trifluoroacetic (complex).",
        )
        self.assertEqual(
            _normalize_space(
                "AB-C<sup>2´</sup>H, AB-C<sup>3´</sup>; DNA <strong>5’</strong>-end; D1‘"
            ),
            "AB-C<sup>2′</sup>H, AB-C<sup>3′</sup>; DNA <strong>5′</strong>-end; D1′",
        )
        self.assertEqual(
            _normalize_space("Significance: * P < .05 vs day 0 ."),
            "Significance: * P < .05 vs day 0.",
        )
        lines = [
            _PdfLine("time-", "time-", 1, 0, 40, 0, 10),
            _PdfLine("of-flight analysis", "of-flight *analysis*", 2, 0, 80, 10, 20),
        ]
        paragraph = _PdfParagraph(lines)
        self.assertEqual(paragraph.text, "time-of-flight analysis")
        self.assertEqual(paragraph.markdown, "time-of-flight *analysis*")

        italic_hyphenated_lines = [
            _PdfLine(
                "N-(3-aminopropyl)-1-",
                "*N-(3-aminopropyl)-1-*",
                1,
                0,
                100,
                0,
                10,
            ),
            _PdfLine(
                "methylimidazole-2-carboxamide",
                "*methylimidazole-2-carboxamide*",
                2,
                0,
                150,
                10,
                20,
            ),
        ]
        italic_hyphenated_paragraph = _PdfParagraph(italic_hyphenated_lines)
        self.assertEqual(
            italic_hyphenated_paragraph.text,
            "N-(3-aminopropyl)-1-methylimidazole-2-carboxamide",
        )
        self.assertEqual(
            italic_hyphenated_paragraph.markdown,
            "*N-(3-aminopropyl)-1-methylimidazole-2-carboxamide*",
        )

        tagged_lines = [
            _PdfLine("pyrrole-", "<strong>pyrrole-</strong>", 1, 0, 40, 0, 10),
            _PdfLine("imidazole", "<strong>imidazole</strong>", 2, 0, 60, 10, 20),
        ]
        self.assertEqual(
            _PdfParagraph(tagged_lines).markdown,
            "<strong>pyrrole-</strong><strong>imidazole</strong>",
        )

        dna_lines = [
            _PdfLine("GATCGGAGCAAGAAGAAGT", "GATCGGAGCAAGAAGAAGT", 1, 0, 100, 0, 10),
            _PdfLine("GCGGAGGCAAGA", "GCGGAGGCAAGA", 2, 0, 80, 10, 20),
        ]
        self.assertEqual(
            _PdfParagraph(dna_lines).text,
            "GATCGGAGCAAGAAGAAGTGCGGAGGCAAGA",
        )

        terminal_dna_lines = [
            _PdfLine("GATCGGAGCAAGAAGAAGT", "GATCGGAGCAAGAAGAAGT", 1, 0, 100, 0, 10),
            _PdfLine("TCGC-3’.", "TCGC-3’.", 2, 0, 60, 10, 20),
        ]
        self.assertEqual(
            _PdfParagraph(terminal_dna_lines).text,
            "GATCGGAGCAAGAAGAAGTTCGC-3′.",
        )

    def test_supplement_page_number_requires_bare_footer_geometry(self) -> None:
        footer = _PdfLine("S7", "S7", 1, 540, 560, 744, 756)
        spaced_footer = _PdfLine("S 7", "S 7", 1, 540, 560, 744, 756)
        hyphenated_footer = _PdfLine("S-7", "S-7", 1, 540, 560, 744, 756)
        threshold_crossing_footer = _PdfLine(
            "S8", "S8", 2, 290, 305, 711, 722
        )
        bare_footer = _PdfLine("7", "7", 2, 540, 560, 744, 756)
        body = _PdfLine("7", "7", 3, 50, 70, 120, 132)
        prose_footer = _PdfLine("Figure S7", "Figure S7", 4, 50, 110, 744, 756)

        self.assertTrue(_is_supplement_page_number(footer, 792))
        self.assertTrue(_is_supplement_page_number(spaced_footer, 792))
        self.assertTrue(_is_supplement_page_number(hyphenated_footer, 792))
        self.assertTrue(_is_supplement_page_number(threshold_crossing_footer, 800))
        self.assertTrue(_is_supplement_page_number(bare_footer, 792))
        self.assertFalse(_is_supplement_page_number(body, 792))
        self.assertFalse(_is_supplement_page_number(prose_footer, 792))

    def test_double_spaced_pdf_lines_remain_one_authored_paragraph(self) -> None:
        lines = [
            _PdfLine("First line", "First line", 1, 55, 560, 40, 52),
            _PdfLine("second line", "second line", 2, 55, 560, 67.6, 79.6),
            _PdfLine("New paragraph", "New paragraph", 3, 64, 560, 122.7, 134.7),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            ["First line second line", "New paragraph"],
        )

    def test_single_spaced_pdf_paragraph_gap_remains_a_boundary(self) -> None:
        lines = [
            _PdfLine("First line", "First line", 1, 55, 560, 40, 52),
            _PdfLine("second line", "second line", 2, 55, 560, 54, 66),
            _PdfLine("New paragraph", "New paragraph", 3, 55, 560, 77, 89),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            ["First line second line", "New paragraph"],
        )

    def test_sparse_pdf_lowercase_wrapped_line_is_not_a_false_paragraph(self) -> None:
        lines = [
            _PdfLine(
                "titration reached the imino proton region and PA",
                "titration reached the imino proton region and PA",
                1,
                72,
                523,
                73.8,
                85.8,
            ),
            _PdfLine(
                "aromatic region of the data.",
                "aromatic region of the data.",
                2,
                72,
                492,
                101.4,
                113.4,
            ),
            _PdfLine(
                "Figure S1. Reviewed caption line",
                "**Figure S1.** Reviewed caption line",
                3,
                72,
                523,
                590,
                602,
            ),
            _PdfLine(
                "caption continuation.",
                "caption continuation.",
                4,
                72,
                523,
                603.3,
                615.3,
            ),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            [
                "titration reached the imino proton region and PA aromatic region of the data.",
                "Figure S1. Reviewed caption line caption continuation.",
            ],
        )

    def test_lowercase_in_text_figure_reference_is_not_a_caption_boundary(self) -> None:
        lines = [
            _PdfLine(
                "Purity was checked by HPLC under 254 nm,",
                "Purity was checked by HPLC under 254 nm,",
                1,
                55,
                560,
                40,
                52,
            ),
            _PdfLine(
                "figure S1 A,B) and MS(figure S1 C,D); all procedures were followed.",
                "figure S1 A,B) and MS(figure S1 C,D); all procedures were followed.",
                2,
                55,
                560,
                54,
                66,
            ),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            [
                "Purity was checked by HPLC under 254 nm, figure S1 A,B) and "
                "MS(figure S1 C,D); all procedures were followed."
            ],
        )

    def test_compact_pdf_table_leading_does_not_fragment_double_spaced_prose(self) -> None:
        lines = [
            _PdfLine("Table value A", "Table value A", 1, 250, 350, 40, 52),
            _PdfLine("Table value B", "Table value B", 2, 250, 350, 56, 68),
            _PdfLine("Table value C", "Table value C", 3, 250, 350, 72, 84),
            _PdfLine("First prose line", "First prose line", 4, 55, 560, 120, 132),
            _PdfLine("continues here", "continues here", 5, 55, 560, 147.6, 159.6),
            _PdfLine("and still continues", "and still continues", 6, 55, 560, 175.2, 187.2),
            _PdfLine("New paragraph", "New paragraph", 7, 64, 560, 230.3, 242.3),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            [
                "Table value A Table value B Table value C",
                "First prose line continues here and still continues",
                "New paragraph",
            ],
        )

    def test_pdf_table_caption_stops_before_numbered_bold_heading(self) -> None:
        lines = [
            _PdfLine(
                "Table S1. DNA duplexes used in the paper",
                "**Table S1.** DNA duplexes used in the paper",
                1,
                55,
                400,
                40,
                52,
            ),
            _PdfLine(
                "1.5. Formation of complexes",
                "1.5. **Formation of complexes**",
                2,
                70,
                400,
                80,
                92,
            ),
            _PdfLine(
                "Samples were prepared as described.",
                "Samples were prepared as described.",
                3,
                55,
                560,
                116,
                128,
            ),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            [
                "Table S1. DNA duplexes used in the paper",
                "1.5. Formation of complexes",
                "Samples were prepared as described.",
            ],
        )

    def test_complete_pdf_figure_caption_stops_before_bold_references_heading(self) -> None:
        lines = [
            _PdfLine(
                "Supporting Figure 4. Sequence logos.",
                "**Supporting Figure 4.** Sequence logos.",
                1,
                55,
                560,
                40,
                52,
            ),
            _PdfLine(
                "References.",
                "**References.**",
                2,
                55,
                160,
                70,
                82,
            ),
            _PdfLine(
                "(1) Reviewed citation.",
                "(1) Reviewed citation.",
                3,
                55,
                560,
                88,
                100,
            ),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            [
                "Supporting Figure 4. Sequence logos.",
                "References.",
                "(1) Reviewed citation.",
            ],
        )

    def test_pdf_paragraph_stops_at_figure_graphic_boundary(self) -> None:
        lines = [
            _PdfLine(
                "Caption continuation outside the crop.",
                "Caption continuation outside the crop.",
                1,
                55,
                560,
                40,
                52,
            ),
            _PdfLine(
                "Cy10..0035AT (+)ATA",
                "Cy10..0035AT (+)ATA",
                2,
                190,
                420,
                58,
                70,
                overlaps_figure_graphic=True,
            ),
            _PdfLine(
                "more native plot text",
                "more native plot text",
                3,
                190,
                420,
                74,
                86,
                overlaps_figure_graphic=True,
            ),
        ]

        paragraphs = _paragraphs(lines)

        self.assertEqual(
            [paragraph.text for paragraph in paragraphs],
            [
                "Caption continuation outside the crop.",
                "Cy10..0035AT (+)ATA more native plot text",
            ],
        )
        self.assertFalse(paragraphs[0].lines[0].overlaps_figure_graphic)
        self.assertTrue(
            all(line.overlaps_figure_graphic for line in paragraphs[1].lines)
        )

    def test_pdf_caption_lines_inside_graphic_region_stay_together(self) -> None:
        lines = [
            _PdfLine(
                "Figure S1. First caption line",
                "Figure S1. First caption line",
                1,
                55,
                560,
                40,
                52,
            ),
            _PdfLine(
                "and its continuation.",
                "and its continuation.",
                2,
                55,
                560,
                58,
                70,
                overlaps_figure_graphic=True,
            ),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            ["Figure S1. First caption line and its continuation."],
        )

    def test_pdf_caption_continues_out_of_conservative_figure_crop(self) -> None:
        lines = [
            _PdfLine(
                "Figure S1. Caption title.",
                "**Figure S1. Caption title.**",
                1,
                55,
                560,
                400,
                412,
                overlaps_figure_graphic=True,
            ),
            _PdfLine(
                "Authored continuation outside the crop.",
                "Authored continuation outside the crop.",
                2,
                55,
                560,
                420,
                432,
            ),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            ["Figure S1. Caption title. Authored continuation outside the crop."],
        )

    def test_pdf_caption_above_figure_stops_when_graphic_begins(self) -> None:
        lines = [
            _PdfLine(
                "Figure S1. Complete caption.",
                "**Figure S1. Complete caption.**",
                1,
                55,
                560,
                40,
                52,
            ),
            _PdfLine(
                "native plot label",
                "native plot label",
                2,
                190,
                420,
                60,
                72,
                overlaps_figure_graphic=True,
            ),
        ]

        paragraphs = _paragraphs(lines)

        self.assertEqual(
            [paragraph.text for paragraph in paragraphs],
            ["Figure S1. Complete caption.", "native plot label"],
        )

    def test_standalone_italic_heading_after_caption_is_not_caption_text(self) -> None:
        lines = [
            _PdfLine(
                "Scheme S1. Synthesis of PA1.",
                "**Scheme S1.** Synthesis of **PA1**.",
                1,
                55,
                300,
                40,
                52,
            ),
            _PdfLine(
                "Resin preparation",
                "*Resin preparation*",
                2,
                55,
                200,
                64,
                76,
            ),
        ]

        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines)],
            ["Scheme S1. Synthesis of PA1.", "Resin preparation"],
        )

    def test_hash_pinned_raster_pdf_table_override_is_semantic(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("supplement.pdf"),
            relative_path="papers (private)/00001/supplementary/supplement.pdf",
            size=1,
            sha256="a" * 64,
            detected_format="application/pdf",
            page_count=1,
        )
        tables, pages = _supplement_table_overrides(
            {
                "table_overrides": [
                    {
                        "number": "S1",
                        "label": "Supporting Table S1",
                        "page": 1,
                        "title_plain": "Table S1. Measurements.",
                        "title_markdown": "Table S1. Measurements.",
                        "source_locator": "page=1;raster-table=Im1",
                        "source_kind": "image",
                        "reason": "The table body is a raster XObject.",
                        "evidence": "Reviewed PDF page 1.",
                        "parts": [
                            {
                                "rows": [
                                    [
                                        {"text": "Agent", "header": True},
                                        {"text": "K_{a}", "markdown": "K<sub>a</sub>", "header": True},
                                    ],
                                    [
                                        {"text": "1"},
                                        {"text": "2 × 10^{9}", "markdown": "2 × 10<sup>9</sup>"},
                                    ],
                                ]
                            }
                        ],
                        "footnotes": [
                            {"plain_text": "[a] Reviewed note."}
                        ],
                    }
                ]
            },
            source,
            "supplement_001",
        )

        self.assertEqual(pages, {1})
        self.assertEqual(tables[0].table_id, "supplement_001_table_s1")
        self.assertEqual(tables[0].label, "Supporting Table S1")
        self.assertEqual(tables[0].source_id, "Supporting Table S1")
        self.assertEqual(tables[0].source_kind, "image")
        self.assertTrue(tables[0].requires_source_image)
        self.assertEqual(
            [[cell.text for cell in row] for row in tables[0].parts[0].rows],
            [["Agent", "K_{a}"], ["1", "2 × 10^{9}"]],
        )

        document_tables, _ = _supplement_table_overrides(
            {
                "table_overrides": [
                    {
                        "number": "S2",
                        "page": 2,
                        "title_plain": "Table S2. Native PDF table.",
                        "title_markdown": "Table S2. Native PDF table.",
                        "source_locator": (
                            "supplementary.pdf;page=2;"
                            "native-vector-pdf-table"
                        ),
                        "source_kind": "document",
                        "reason": "The authoritative table is native PDF structure.",
                        "evidence": "Reviewed native text and vector rules on PDF page 2.",
                        "parts": [
                            {
                                "rows": [
                                    [{"text": "Agent", "header": True}],
                                    [{"text": "1"}],
                                ]
                            }
                        ],
                        "footnotes": [],
                    }
                ]
            },
            source,
            "supplement_001",
        )
        self.assertEqual(document_tables[0].source_kind, "document")
        self.assertFalse(document_tables[0].requires_source_image)

        pdf_config = {
            "table_overrides": [
                {
                    "number": "S3",
                    "page": 3,
                    "title_plain": "Table S3. Visual PDF table.",
                    "title_markdown": "Table S3. Visual PDF table.",
                    "source_locator": "supplementary.pdf;page=3;visual-table",
                    "source_kind": "pdf",
                    "reason": "The authoritative table is visual PDF content.",
                    "evidence": "Reviewed PDF page 3.",
                    "parts": [
                        {
                            "rows": [
                                [{"text": "Agent", "header": True}],
                                [{"text": "1"}],
                            ]
                        }
                    ],
                    "footnotes": [],
                }
            ]
        }
        pdf_tables, _ = _supplement_table_overrides(
            pdf_config, source, "supplement_001"
        )
        self.assertTrue(pdf_tables[0].requires_source_image)

    def test_reviewed_pdf_table_default_markdown_escapes_literal_controls(self) -> None:
        source = SourceFile(
            role="supplement",
            path=Path("supplement.pdf"),
            relative_path="papers (private)/00001/supplementary/supplement.pdf",
            size=1,
            sha256="a" * 64,
            detected_format="application/pdf",
            page_count=1,
        )
        tables, _ = _supplement_table_overrides(
            {
                "table_overrides": [
                    {
                        "number": "1",
                        "page": 1,
                        "title_plain": "Table 1. Literal controls.",
                        "source_locator": "page=1;native-pdf-table",
                        "source_kind": "document",
                        "reason": "The table is native PDF structure.",
                        "evidence": "Reviewed PDF page 1.",
                        "parts": [
                            {
                                "rows": [
                                    [{"text": "Probe_name*↑", "header": True}],
                                    [{"text": "#N/A"}],
                                ]
                            }
                        ],
                    }
                ]
            },
            source,
            "supplement_001",
        )

        self.assertEqual(
            tables[0].parts[0].rows[0][0].markdown,
            r"Probe\_name\*↑",
        )
        self.assertEqual(tables[0].parts[0].rows[1][0].markdown, "#N/A")

    def test_reviewed_pdf_table_bbox_removes_only_native_table_lines(self) -> None:
        config = {
            "table_overrides": [
                {
                    "page": 8,
                    "source_locator": (
                        "supplementary.pdf;page=8;"
                        "bbox=162.975,171.044,432.075,316.356;native-table"
                    ),
                }
            ]
        }
        regions = _supplement_table_text_regions(config)
        lines = [
            _PdfLine("Prose above", "Prose above", 1, 72, 520, 140, 152),
            _PdfLine("8-ODN", "**8-ODN**", 2, 180, 223, 230, 242),
            _PdfLine(
                "Table S1. Caption below",
                "**Table S1.** Caption below",
                3,
                72,
                280,
                318.4,
                330.4,
            ),
        ]

        retained = _exclude_reviewed_pdf_table_lines(lines, regions[8])

        self.assertEqual([line.text for line in retained], ["Prose above", "Table S1. Caption below"])

    def test_reviewed_pdf_table_bbox_requires_matching_locator_page(self) -> None:
        with self.assertRaisesRegex(ValueError, "bbox page is inconsistent"):
            _supplement_table_text_regions(
                {
                    "table_overrides": [
                        {
                            "page": 8,
                            "source_locator": "page=9;table-bbox=[1,2,3,4]",
                        }
                    ]
                }
            )

        with self.assertRaisesRegex(ValueError, "bbox is invalid"):
            _supplement_table_text_regions(
                {
                    "table_overrides": [
                        {
                            "page": 8,
                            "source_locator": "page=8;bbox=[1,2,not-a-number,4]",
                        }
                    ]
                }
            )

    def test_unboxed_reviewed_table_page_does_not_promote_cell_headings(self) -> None:
        config = {
            "table_overrides": [
                {"page": 5, "source_locator": "page=5;native-pdf-table"},
                {"page": 6, "source_locator": "page=6;table-bbox=[1,2,3,4]"},
            ]
        }
        boxed = _supplement_table_text_regions(config)
        self.assertEqual(_supplement_unboxed_table_pages(config, boxed), {5})
        lines = [
            _PdfLine("Marker", "**Marker**", 1, 55, 200, 40, 52),
            _PdfLine("ASCL1", "**ASCL1**", 2, 55, 200, 58, 70),
        ]

        self.assertEqual(len(_paragraphs(lines)), 2)
        self.assertEqual(
            [paragraph.text for paragraph in _paragraphs(lines, recognize_headings=False)],
            ["Marker ASCL1"],
        )


if __name__ == "__main__":
    unittest.main()
