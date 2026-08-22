from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from scripts.extraction.pdf_text_extractor import (
    PdfTextExtractionError,
    extract_pdf_text,
)


class PdfTextExtractorTests(unittest.TestCase):
    def test_type1_cnn_decoding_diagnostics_and_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "special-fonts.pdf"
            _write_pdf(
                path,
                fonts={
                    "FNormal": ("AdvPS_TTR", ["C109"]),
                    "FPi": ("AdvPi1", ["C39", "C56"]),
                    "FGreekU": ("AdvPS_GRTU", ["C109"]),
                    "FGreekI": ("AdvPS_GRTI", ["C103", "C100", "C101"]),
                    "FP4": ("AdvP4C4E51", ["C136", "C135", "C60"]),
                    "FPrintable": (
                        "AdvPS_TTRX",
                        32,
                        ["C83", "C65", "C66", "C67", "C68", "C69", "C70", "C119", "C112"],
                    ),
                },
                operations=[
                    ("FNormal", 12, 40, 260, bytes([1])),
                    ("FPi", 12, 40, 230, bytes([1, 2])),
                    ("FGreekU", 12, 40, 200, bytes([1])),
                    ("FGreekI", 12, 40, 170, bytes([1, 2, 3])),
                    ("FP4", 12, 40, 140, bytes([1, 2, 3])),
                    ("FPrintable", 12, 40, 110, bytes([32, 33, 39, 40])),
                ],
            )

            document = extract_pdf_text(path, "pdf/special-fonts.pdf")

            self.assertEqual(
                [line.plain_text for line in document.lines],
                ["m", "′°", "μ", "γδε", "=+�", "SAwp"],
            )
            self.assertTrue(document.all_pages_classified)
            self.assertEqual(document.pages[0].classification, "native_text")
            self.assertEqual(
                {warning["code"] for warning in document.warnings},
                {"unresolved_glyph"},
            )
            unresolved = [
                row
                for row in document.diagnostic_rows
                if row.get("status") == "unresolved"
            ]
            self.assertEqual(len(unresolved), 1)
            self.assertEqual(unresolved[0]["font"], "AdvP4C4E51")
            self.assertEqual(unresolved[0]["glyph_name"], "C60")
            self.assertEqual(unresolved[0]["semantic_code"], 60)
            self.assertIn("=+�", unresolved[0]["context"])
            self.assertRegex(unresolved[0]["source_locator"], r"^page=1;bbox=")

            overridden = extract_pdf_text(
                path,
                "pdf/special-fonts.pdf",
                glyph_overrides={"AdvP4C4E51": {60: "≤"}},
            )
            self.assertEqual(overridden.lines[-2].plain_text, "=+≤")
            self.assertFalse(overridden.warnings)
            override_rows = [
                row
                for row in overridden.diagnostic_rows
                if row.get("method") == "glyph_override"
            ]
            self.assertEqual(len(override_rows), 1)
            self.assertEqual(override_rows[0]["replacement"], "≤")

    def test_geometry_markdown_styles_and_conservative_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "styles.pdf"
            _write_pdf(
                path,
                fonts={
                    "FRegular": ("AdvPS_TTR", ["H", "two", "O"]),
                    "FBold": ("AdvPS_TTB", ["B"]),
                    "FItalic": ("AdvPS_TTI", ["i"]),
                    "FBoldItalic": ("AdvPS_TTBI", ["X"]),
                },
                operations=[
                    ("FBold", 12, 40, 250, bytes([1])),
                    ("FItalic", 12, 47, 250, bytes([1])),
                    ("FBoldItalic", 12, 51, 250, bytes([1])),
                    ("FRegular", 12, 40, 210, bytes([1])),
                    # The smaller, raised '2' is grouped with H and O and is
                    # conservatively classified as a superscript.
                    ("FRegular", 7, 47, 215, bytes([2])),
                    ("FRegular", 12, 52, 210, bytes([3])),
                ],
            )

            document = extract_pdf_text(path, "pdf/styles.pdf")

            self.assertEqual(len(document.pages), 1)
            self.assertEqual(document.pages[0].page, 1)
            self.assertEqual(document.pages[0].width, 300.0)
            self.assertEqual(document.pages[0].height, 300.0)
            self.assertTrue(all(len(line.bbox) == 4 for line in document.lines))
            style_line = next(line for line in document.lines if line.plain_text == "BiX")
            self.assertEqual(style_line.markdown, "**B***i****X***")
            script_line = next(line for line in document.lines if line.plain_text == "H2O")
            self.assertEqual(script_line.markdown, "H<sup>2</sup>O")

    def test_word_gap_keeps_matching_emphasis_as_one_phrase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "italic-phrase.pdf"
            _write_pdf(
                path,
                fonts={
                    "FItalic": (
                        "AdvPS_TTI",
                        ["C116", "C119", "C111", "C114", "C100", "C115"],
                    )
                },
                operations=[
                    ("FItalic", 12, 40, 250, bytes([1, 2, 3])),
                    ("FItalic", 12, 70, 250, bytes([2, 3, 4, 5, 6])),
                ],
            )

            document = extract_pdf_text(path, "pdf/italic-phrase.pdf")

            self.assertEqual(document.lines[0].plain_text, "two words")
            self.assertEqual(document.lines[0].markdown, "*two words*")

    def test_visual_formula_order_places_subscript_before_charge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formula-order.pdf"
            _write_pdf(
                path,
                fonts={
                    "FRegular": ("AdvPS_TTR", ["C", "O", "plus", "three", "N"])
                },
                operations=[
                    ("FRegular", 12, 34, 210, bytes([1])),
                    ("FRegular", 12, 40, 210, bytes([2])),
                    # The PDF paints the raised charge first, followed by the
                    # lowered atom count, while the visual formula is O₃⁺.
                    ("FRegular", 7, 47, 213, bytes([3])),
                    ("FRegular", 7, 52, 207, bytes([4])),
                    ("FRegular", 12, 58, 210, bytes([5])),
                ],
            )

            document = extract_pdf_text(path, "pdf/formula-order.pdf")

            self.assertEqual(document.lines[0].plain_text, "CO3+N")
            self.assertEqual(
                document.lines[0].markdown,
                "CO<sub>3</sub><sup>+</sup>N",
            )

    def test_semantic_override_does_not_match_the_same_raw_byte(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "override-namespace.pdf"
            _write_pdf(
                path,
                fonts={
                    # Raw byte 29 means semantic C49 ('1'), while raw byte 30
                    # means semantic C29.  A semantic override for 29 must only
                    # affect the latter glyph.
                    "FRegular": ("AdvPS_TTRY", 29, ["C49", "C29"]),
                },
                operations=[("FRegular", 12, 40, 250, bytes([29, 30]))],
            )

            document = extract_pdf_text(
                path,
                "pdf/override-namespace.pdf",
                glyph_overrides={"AdvPS_TTRY": {29: "”"}},
            )

            self.assertEqual(document.lines[0].plain_text, "1”")
            override_rows = [
                row
                for row in document.diagnostic_rows
                if row.get("method") == "glyph_override"
            ]
            self.assertEqual(len(override_rows), 1)
            self.assertEqual(override_rows[0]["encoded_code"], 30)
            self.assertEqual(override_rows[0]["semantic_code"], 29)

            explicit_raw = extract_pdf_text(
                path,
                "pdf/override-namespace.pdf",
                glyph_overrides={"AdvPS_TTRY": {"RAW:29": "X", "C29": "”"}},
            )
            self.assertEqual(explicit_raw.lines[0].plain_text, "X”")

    def test_rotated_text_is_marked_but_not_warned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rotated.pdf"
            _write_pdf(
                path,
                fonts={"FRegular": ("AdvPS_TTR", ["R", "o", "t"])},
                operations=[("FRegular", 10, 285, 30, bytes([1, 2, 3]), 90)],
            )

            document = extract_pdf_text(path, "pdf/rotated.pdf")

            self.assertEqual(document.pages[0].classification, "rotated_text_only")
            self.assertTrue(document.lines[0].rotated)
            self.assertFalse(document.warnings)
            self.assertTrue(
                any(row.get("kind") == "rotated_text" for row in document.diagnostic_rows)
            )

    def test_image_only_and_blank_pages_are_classified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blank.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=100)
            with path.open("wb") as output:
                writer.write(output)

            document = extract_pdf_text(path, "pdf/blank.pdf")

            self.assertEqual(document.pages[0].classification, "blank")
            self.assertTrue(document.all_pages_classified)
            self.assertFalse(document.lines)

    def test_rejects_unsafe_paths_and_bad_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blank.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=100)
            with path.open("wb") as output:
                writer.write(output)

            with self.assertRaisesRegex(PdfTextExtractionError, "safe relative"):
                extract_pdf_text(path, "../main.pdf")
            with self.assertRaisesRegex(PdfTextExtractionError, "non-empty string"):
                extract_pdf_text(
                    path,
                    "pdf/main.pdf",
                    glyph_overrides={"AdvP4": {60: ""}},
                )


def _write_pdf(
    path: Path,
    *,
    fonts: dict[
        str,
        tuple[str, list[str]] | tuple[str, int, list[str]],
    ],
    operations: list[tuple[str, int, int, int, bytes] | tuple[str, int, int, int, bytes, int]],
) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font_resources = DictionaryObject()
    for resource_name, font_spec in fonts.items():
        if len(font_spec) == 2:
            base_font, glyph_names = font_spec
            first_code = 1
        else:
            base_font, first_code, glyph_names = font_spec
        differences = ArrayObject([NumberObject(first_code)])
        differences.extend(NameObject("/" + name) for name in glyph_names)
        descriptor = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/FontDescriptor"),
                NameObject("/FontName"): NameObject("/" + base_font),
                NameObject("/Flags"): NumberObject(32),
                NameObject("/FontBBox"): ArrayObject(
                    [NumberObject(0), NumberObject(-200), NumberObject(1000), NumberObject(900)]
                ),
                NameObject("/ItalicAngle"): NumberObject(0),
                NameObject("/Ascent"): NumberObject(800),
                NameObject("/Descent"): NumberObject(-200),
                NameObject("/CapHeight"): NumberObject(700),
                NameObject("/StemV"): NumberObject(80),
            }
        )
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/" + base_font),
                NameObject("/FirstChar"): NumberObject(first_code),
                NameObject("/LastChar"): NumberObject(first_code + len(glyph_names) - 1),
                NameObject("/Widths"): ArrayObject(
                    NumberObject(600) for _ in glyph_names
                ),
                NameObject("/Encoding"): DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Encoding"),
                        NameObject("/Differences"): differences,
                    }
                ),
                NameObject("/FontDescriptor"): writer._add_object(descriptor),
            }
        )
        font_resources[NameObject("/" + resource_name)] = writer._add_object(font)

    resources = DictionaryObject(
        {NameObject("/Font"): font_resources}
    )
    page[NameObject("/Resources")] = resources
    content_parts: list[bytes] = []
    for operation in operations:
        font_name, size, x, y, encoded = operation[:5]
        rotation = operation[5] if len(operation) == 6 else 0
        if rotation == 90:
            matrix = f"0 1 -1 0 {x} {y} Tm"
        else:
            matrix = f"1 0 0 1 {x} {y} Tm"
        content_parts.append(
            (
                f"BT /{font_name} {size} Tf {matrix} <{encoded.hex()}> Tj ET\n"
            ).encode("ascii")
        )
    content = DecodedStreamObject()
    content.set_data(b"".join(content_parts))
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as output:
        writer.write(output)


if __name__ == "__main__":
    unittest.main()
