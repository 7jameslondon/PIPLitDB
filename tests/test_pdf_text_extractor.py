from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)
from reportlab.pdfgen import canvas

from scripts.extraction.pdf_text_extractor import (
    _DecodedChar,
    PdfTextExtractionError,
    _FontInfo,
    _decode_char,
    _canonicalize_degree_celsius,
    _canonicalize_scientific_multiplication_dots,
    _group_upright_chars,
    _make_character_lines,
    _infer_scripts,
    _normalize_overrides,
    _render_markdown,
    extract_pdf_text,
)
from scripts.extraction.pdf_extractor import (
    PdfExtractionError,
    _validate_box,
    render_pdf_crop,
)
from scripts.extraction.rich_text import inline_markup_to_safe_html


class PdfTextExtractorTests(unittest.TestCase):
    def test_reviewed_word_gap_recovers_close_set_words_without_changing_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'narrow-gap.pdf'
            document = canvas.Canvas(str(path), pagesize=(300, 300))
            document.setFont('Helvetica', 10)
            document.drawString(40, 240, 'A')
            document.drawString(47.5, 240, 'B')
            document.save()
            default = extract_pdf_text(path, 'pdf/narrow-gap.pdf')
            reviewed = extract_pdf_text(path, 'pdf/narrow-gap.pdf', word_gap_points=0.6)
            self.assertEqual(default.lines[0].plain_text, 'AB')
            self.assertEqual(reviewed.lines[0].plain_text, 'A B')
            for invalid in (0, -1, 3.1, True, '0.6'):
                with self.subTest(invalid=invalid), self.assertRaises(PdfTextExtractionError):
                    extract_pdf_text(path, 'pdf/narrow-gap.pdf', word_gap_points=invalid)

    def test_mathematical_pi_exact_aliases_decode_and_unknowns_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "math-aliases.pdf"
            _write_pdf(
                path,
                fonts={
                    "FPi": ("ABCDEF+MathematicalPi-One", [
                        "H11001", "H11002", "H11032", "H11034", "H11999"
                    ]),
                    "FOther": ("UnrelatedMath", ["H11001"]),
                },
                operations=[
                    ("FPi", 12, 40, 240, bytes([1, 2, 3, 4, 5])),
                    ("FOther", 12, 40, 210, bytes([1])),
                ],
            )
            result = extract_pdf_text(path, "pdf/math-aliases.pdf")
            self.assertEqual([line.plain_text for line in result.lines], ["+−′°�", "�"])
            resolved = [row for row in result.diagnostic_rows
                        if row.get("method") == "mathematicalpi_one_alias"]
            self.assertEqual([row["replacement"] for row in resolved], list("+−′°"))
            self.assertEqual(len(result.warnings), 2)
            overridden = extract_pdf_text(
                path, "pdf/math-aliases.pdf",
                glyph_overrides={"MathematicalPi-One": {"H11002": "x"}},
            )
            self.assertEqual(overridden.lines[0].plain_text, "+x′°�")

    def test_adobe_small_caps_use_explicit_font_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "small-caps.pdf"
            _write_pdf(
                path,
                fonts={"FExpert": ("ExampleExpert", ["Dsmall", "Lsmall", "Aacutesmall"])},
                operations=[("FExpert", 12, 40, 240, bytes([1, 2, 3]))],
            )
            result = extract_pdf_text(path, "pdf/small-caps.pdf")
            self.assertEqual(result.lines[0].plain_text, "DLÁ")
            self.assertFalse(result.warnings)
            self.assertEqual(
                {row["glyph_name"] for row in result.diagnostic_rows
                 if row.get("method") == "adobe_small_cap"},
                {"Dsmall", "Lsmall", "Aacutesmall"},
            )
        font = _FontInfo("Unrelated", "Unrelated", {}, False, False, False)
        char = {"text": "\uf764", "fontname": "Unrelated",
                "x0": 0, "x1": 5, "top": 10, "bottom": 20}
        self.assertEqual(_decode_char(char, 1, {"Unrelated": font}, {}).text, "\uf764")

    def test_mixed_font_raised_minus_uses_text_origin_not_font_box(self) -> None:
        fonts = {
            "Text": _FontInfo("Text", "Text", {}, False, False, False, -200),
            "MathematicalPi-One": _FontInfo(
                "MathematicalPi-One", "MathematicalPi-One", {3: "H11002"},
                False, False, False, 0,
            ),
        }
        def char(text, x, baseline, size=10, font="Text", matrix_y=None):
            descent = fonts[font].descent
            y0 = baseline + descent * size / 1000
            return {"text": text, "fontname": font, "size": size,
                    "x0": x, "x1": x + size * .5, "y0": y0,
                    "bottom": 200 - y0, "top": 200 - y0 - size,
                    "matrix": (1, 0, 0, 1, x, baseline if matrix_y is None else matrix_y)}
        chars = [char("7", 40, 90.5), char("8", 45, 90.5),
                 char("c", 40, 80), char("m", 45, 80),
                 char("(cid:3)", 50, 83.6, 6, "MathematicalPi-One"),
                 char("1", 53, 83.6, 6)]
        lines = _make_character_lines(chars, 1, "pdf/example.pdf", fonts, {}, [], [])
        self.assertEqual([line.plain_text for line in lines], ["78", "cm−1"])
        self.assertEqual(lines[1].markdown, "cm<sup>−1</sup>")
        # Synthetic italic shear elsewhere in the line preserves its baseline.
        sheared = [dict(c, matrix=(1, 0, .3, 1, c["x0"], c["matrix"][5]))
                   if i == 2 else c for i, c in enumerate(chars)]
        lines = _make_character_lines(sheared, 1, "pdf/shear.pdf", fonts, {}, [], [])
        self.assertEqual([line.plain_text for line in lines], ["78", "cm−1"])
        self.assertEqual(lines[1].markdown, "cm<sup>−1</sup>")
        # PDF Ts can raise the glyph without changing the matrix origin.
        risen = [dict(c, matrix=(1, 0, 0, 1, c["x0"], 80)) if i >= 4 else c
                 for i, c in enumerate(chars)]
        lines = _make_character_lines(risen, 1, "pdf/rise.pdf", fonts, {}, [], [])
        self.assertEqual([line.plain_text for line in lines], ["78", "cm−1"])
        self.assertEqual(lines[1].markdown, "cm<sup>−1</sup>")

    def test_standard_font_explicit_text_rise_preserves_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "standard-rise.pdf"
            document = canvas.Canvas(str(path), pagesize=(300, 300))
            text = document.beginText(40, 240)
            text.setFont("Helvetica", 12)
            text.textOut("H")
            text.setFont("Helvetica", 7)
            text.setRise(-3)
            text.textOut("2")
            text.setRise(0)
            text.setFont("Helvetica", 12)
            text.textOut("O")
            text.setFont("Helvetica", 7)
            text.setRise(4)
            text.textOut("+")
            document.drawText(text)
            document.save()
            result = extract_pdf_text(path, "pdf/standard-rise.pdf")
            self.assertEqual(result.lines[0].markdown, "H<sub>2</sub>O<sup>+</sup>")

    def test_same_origin_small_symbol_is_not_a_false_script(self) -> None:
        def item(text, x, size, bottom, baseline):
            return _DecodedChar(text, {"size": size, "top": bottom-size,
                "bottom": bottom, "matrix": (1, 0, 0, 1, x, baseline)}, False, False)
        chars = [item("A", 10, 10, 102, 100),
                 item("°", 15, 6, 100, 100),
                 item("′", 18, 6, 100, 100),
                 item("μ", 21, 6, 100, 100),
                 item("B", 24, 10, 102, 100)]
        _infer_scripts(chars)
        self.assertEqual(_render_markdown(chars), "A°′μB")
        for baseline, tag in [(104, "sup"), (97, "sub")]:
            with self.subTest(baseline=baseline):
                shifted = [item("A", 10, 10, 102, 100),
                           item("2", 15, 6, 102-(baseline-100), baseline),
                           item("B", 18, 10, 102, 100)]
                _infer_scripts(shifted)
                self.assertEqual(_render_markdown(shifted), f"A<{tag}>2</{tag}>B")

    def test_compact_numeric_subscript_with_small_origin_shift(self) -> None:
        def item(text, x, size, origin):
            return _DecodedChar(text, {"size": size, "top": 100-origin-size,
                "bottom": 100-origin, "matrix": (1, 0, 0, 1, x, origin)}, False, False)
        for digit_size, shift, text, expected in [
            (7.92, 1.44, "2", "MgCl<sub>2</sub>"),
            (7.92, 0, "2", "MgCl2"),
            (7.92, 1.0, "2", "MgCl2"),
            (9.6, 1.44, "2", "MgCl2"),
            (7.92, 1.44, "x", "MgClx"),
        ]:
            with self.subTest(digit_size=digit_size, shift=shift, text=text):
                chars = [item(c, i*6, 12, 100) for i, c in enumerate("MgCl")]
                chars.append(item(text, 24, digit_size, 100-shift))
                _infer_scripts(chars)
                self.assertEqual(_render_markdown(chars), expected)

    def test_letter_subscript_at_rounded_thirteen_percent_origin_shift(self) -> None:
        def item(text: str, x: float, size: float, origin: float) -> _DecodedChar:
            return _DecodedChar(
                text,
                {
                    "size": size,
                    "top": 500 - origin - size,
                    "bottom": 500 - origin,
                    "matrix": (1, 0, 0, 1, x, origin),
                },
                False,
                False,
            )

        chars = [
            item(character, index * 6, 11.9999952, 495.679801728)
            for index, character in enumerate("valueλ")
        ]
        chars.extend(
            item(character, 36 + index * 4, 8.039996784, 494.119802352)
            for index, character in enumerate("max")
        )

        _infer_scripts(chars)

        self.assertEqual(_render_markdown(chars), "valueλ<sub>max</sub>")

    def test_multi_character_whitespace_token_is_layout_whitespace(self) -> None:
        font = _FontInfo(
            full_name="ABCDEF+MS-Mincho",
            name="MS-Mincho",
            differences={},
            cnn_encoded=False,
            bold=False,
            italic=False,
        )
        decoded = _decode_char(
            {
                "text": "\t\r ",
                "fontname": "ABCDEF+MS-Mincho",
                "x0": 10,
                "x1": 16,
                "top": 20,
                "bottom": 32,
            },
            1,
            {"ABCDEF+MS-Mincho": font, "MS-Mincho": font},
            {},
        )

        self.assertEqual(decoded.text, " ")
        self.assertIsNone(decoded.diagnostic)

    def test_symbolssk_literal_latin_token_is_visible_greek(self) -> None:
        font = _FontInfo(
            full_name="EFGPEJ+SymbolSSK",
            name="SymbolSSK",
            differences={},
            cnn_encoded=False,
            bold=False,
            italic=False,
        )
        decoded = _decode_char(
            {
                "text": "b",
                "fontname": "EFGPEJ+SymbolSSK",
                "x0": 10,
                "x1": 16,
                "top": 20,
                "bottom": 30,
            },
            1,
            {"EFGPEJ+SymbolSSK": font, "SymbolSSK": font},
            {},
        )

        self.assertEqual(decoded.text, "β")
        self.assertEqual(
            decoded.diagnostic["method"], "symbolssk_greek_transliteration"
        )

    def test_overprinted_bold_simulation_is_not_repeated_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overprinted-heading.pdf"
            document = canvas.Canvas(str(path), pagesize=(300, 300))
            for x, y in (
                (40.0, 250.0),
                (40.24, 250.0),
                (40.0, 250.24),
                (40.24, 250.24),
            ):
                document.drawString(x, y, "Cell treatment")
            document.save()

            extracted = extract_pdf_text(path, "pdf/overprinted-heading.pdf")

            self.assertEqual(
                [line.plain_text for line in extracted.lines],
                ["Cell treatment"],
            )

    def test_raised_period_before_angstrom_is_a_multiplication_dot(self) -> None:
        chars = [
            _DecodedChar("l", {}, bold=False, italic=False),
            _DecodedChar(".", {}, bold=False, italic=False, script="sup"),
            _DecodedChar(" ", {}, bold=False, italic=False),
            _DecodedChar("Å", {}, bold=False, italic=False),
            _DecodedChar("-", {}, bold=False, italic=False, script="sup"),
            _DecodedChar("2", {}, bold=False, italic=False, script="sup"),
        ]

        _canonicalize_scientific_multiplication_dots(chars)

        self.assertEqual("".join(char.text for char in chars), "l·Å-2")
        self.assertEqual(_render_markdown(chars), "l·Å<sup>-2</sup>")

    def test_raised_lowercase_o_before_c_is_degree_celsius(self) -> None:
        chars = [
            _DecodedChar("9", {}, bold=False, italic=False),
            _DecodedChar("5", {}, bold=False, italic=False),
            _DecodedChar(" ", {}, bold=False, italic=False),
            _DecodedChar("o", {}, bold=False, italic=False, script="sup"),
            _DecodedChar("C", {}, bold=False, italic=False),
        ]

        _canonicalize_degree_celsius(chars)

        self.assertEqual("".join(char.text for char in chars), "95 °C")
        self.assertEqual(_render_markdown(chars), "95 °C")

    def test_multi_page_pdf_crop_is_one_deterministic_vertical_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "sources"
            source_root.mkdir()
            pdf_path = source_root / "two-pages.pdf"
            document = canvas.Canvas(str(pdf_path), pagesize=(72, 72))
            document.setFillColorRGB(1, 0, 0)
            document.rect(0, 0, 72, 72, stroke=0, fill=1)
            document.showPage()
            document.setFillColorRGB(0, 0, 1)
            document.rect(0, 0, 72, 72, stroke=0, fill=1)
            document.save()

            result = render_pdf_crop(
                source_root,
                root / "output",
                {
                    "source_path": "two-pages.pdf",
                    "asset_id": "figure-s1",
                    "parts": [
                        {"page": 1, "box": [0, 0, 72, 72]},
                        {"page": 2, "box": [0, 0, 36, 72]},
                    ],
                    "output_path": "figures/figure-s1.png",
                },
                dpi=72,
            )

            self.assertEqual(result["dimensions_pixels"], {"width": 72, "height": 144})
            self.assertEqual(result["composition"]["layout"], "vertical")
            self.assertEqual(result["parts"][1]["placement_pixels"]["left"], 18)
            self.assertFalse(result["ocr_performed"])
            with Image.open(root / "output/figures/figure-s1.png") as image:
                self.assertEqual(image.getpixel((1, 1)), (255, 0, 0))
                self.assertEqual(image.getpixel((1, 100)), (255, 255, 255))
                self.assertEqual(image.getpixel((20, 100)), (0, 0, 255))

    def test_multi_page_pdf_crop_rejects_ambiguous_or_single_part_specs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "sources"
            source_root.mkdir()
            pdf_path = source_root / "one-page.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            with pdf_path.open("wb") as stream:
                writer.write(stream)

            common = {
                "source_path": "one-page.pdf",
                "asset_id": "figure-s1",
                "output_path": "figure-s1.png",
            }
            with self.assertRaisesRegex(PdfExtractionError, "cannot combine"):
                render_pdf_crop(
                    source_root,
                    root / "ambiguous",
                    {
                        **common,
                        "page": 1,
                        "box": [0, 0, 72, 72],
                        "parts": [
                            {"page": 1, "box": [0, 0, 72, 72]},
                            {"page": 1, "box": [0, 0, 72, 72]},
                        ],
                    },
                    dpi=72,
                )
            with self.assertRaisesRegex(PdfExtractionError, "at least two"):
                render_pdf_crop(
                    source_root,
                    root / "single",
                    {**common, "parts": [{"page": 1, "box": [0, 0, 72, 72]}]},
                    dpi=72,
                )

    def test_pdf_crop_can_rotate_a_reviewed_sideways_visual_clockwise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "sources"
            source_root.mkdir()
            pdf_path = source_root / "sideways.pdf"
            document = canvas.Canvas(str(pdf_path), pagesize=(72, 36))
            document.setFillColorRGB(1, 0, 0)
            document.rect(0, 0, 36, 36, stroke=0, fill=1)
            document.setFillColorRGB(0, 0, 1)
            document.rect(36, 0, 36, 36, stroke=0, fill=1)
            document.save()

            result = render_pdf_crop(
                source_root,
                root / "output",
                {
                    "source_path": "sideways.pdf",
                    "asset_id": "spectrum-1",
                    "page": 1,
                    "box": [0, 0, 72, 36],
                    "rotate_clockwise": 90,
                    "output_path": "figures/spectrum-1.png",
                },
                dpi=72,
            )

            self.assertEqual(result["dimensions_pixels"], {"width": 36, "height": 72})
            self.assertEqual(result["render_rotation_degrees_clockwise"], 90)
            with Image.open(root / "output/figures/spectrum-1.png") as image:
                self.assertEqual(image.getpixel((18, 8)), (255, 0, 0))
                self.assertEqual(image.getpixel((18, 60)), (0, 0, 255))

    def test_pdf_crop_can_add_deterministic_white_padding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "sources"
            source_root.mkdir()
            pdf_path = source_root / "edge-artwork.pdf"
            document = canvas.Canvas(str(pdf_path), pagesize=(72, 72))
            document.setFillColorRGB(1, 0, 0)
            document.rect(0, 0, 72, 72, stroke=0, fill=1)
            document.save()

            result = render_pdf_crop(
                source_root,
                root / "output",
                {
                    "source_path": "edge-artwork.pdf",
                    "asset_id": "figure-edge",
                    "page": 1,
                    "box": [0, 0, 72, 72],
                    "padding_points": 5,
                    "output_path": "figures/figure-edge.png",
                },
                dpi=72,
            )

            self.assertEqual(result["dimensions_pixels"], {"width": 82, "height": 82})
            self.assertEqual(result["padding_points"], 5)
            self.assertEqual(result["padding_pixels"], 5)
            with Image.open(root / "output/figures/figure-edge.png") as image:
                self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
                self.assertEqual(image.getpixel((5, 5)), (255, 0, 0))

    def test_pdf_crop_page_edge_tolerates_only_decimal_rounding_noise(self) -> None:
        self.assertEqual(
            _validate_box(
                [0.0, 0.0, 595.32, 841.92],
                595.320007,
                841.919983,
                asset_id="full-page",
            ),
            (0.0, 0.0, 595.32, 841.919983),
        )
        with self.assertRaisesRegex(PdfExtractionError, "exceeds the PDF page"):
            _validate_box(
                [0.0, 0.0, 595.32, 841.921],
                595.320007,
                841.919983,
                asset_id="out-of-bounds",
            )

    def test_exact_font_unicode_codepoint_override_repairs_private_use_glyph(self) -> None:
        char = {
            "fontname": "FAAADI+SymbolMT",
            "text": "\uf044",
            "x0": 10,
            "top": 20,
            "x1": 16,
            "bottom": 30,
            "size": 10,
        }
        font = _FontInfo(
            full_name="FAAADI+SymbolMT",
            name="SymbolMT",
            differences={},
            cnn_encoded=False,
            bold=False,
            italic=False,
        )
        decoded = _decode_char(
            char,
            15,
            {"FAAADI+SymbolMT": font, "SymbolMT": font},
            _normalize_overrides({"FAAADI+SymbolMT": {"U+F044": "Δ"}}),
        )

        self.assertEqual(decoded.text, "Δ")
        self.assertEqual(decoded.diagnostic["method"], "unicode_glyph_override")
        self.assertEqual(decoded.diagnostic["unicode_codepoint"], "U+F044")

        unmatched = _decode_char(
            char,
            15,
            {"FAAADI+SymbolMT": font, "SymbolMT": font},
            _normalize_overrides({"OTHER+SymbolMT": {"U+F044": "X"}}),
        )
        self.assertEqual(unmatched.text, "\uf044")

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
            self.assertEqual(
                style_line.markdown,
                "**B**<em>i</em><strong><em>X</em></strong>",
            )
            self.assertEqual(
                inline_markup_to_safe_html(style_line.markdown),
                "<strong>B</strong><em>i</em><strong><em>X</em></strong>",
            )
            script_line = next(line for line in document.lines if line.plain_text == "H2O")
            self.assertEqual(script_line.markdown, "H<sup>2</sup>O")

    def test_raised_small_glyph_just_over_half_a_line_stays_attached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "high-superscript.pdf"
            _write_pdf(
                path,
                fonts={"FRegular": ("AdvPS_TTR", ["P", "three", "two", "hyphen", "A"])},
                operations=[
                    ("FRegular", 12, 40, 210, bytes([1])),
                    # This isotope is deliberately raised farther than the
                    # prior half-line clustering tolerance but remains close
                    # enough to be visually part of the baseline text.
                    ("FRegular", 7, 47, 216, bytes([2, 3])),
                    ("FRegular", 12, 55, 210, bytes([4, 5])),
                    # An ordinary next line remains spatially separate.
                    ("FRegular", 12, 40, 194, bytes([5])),
                ],
            )

            document = extract_pdf_text(path, "pdf/high-superscript.pdf")

            self.assertEqual(
                len(document.lines),
                2,
                [(line.plain_text, line.bbox) for line in document.lines],
            )
            self.assertEqual(document.lines[0].plain_text, "P32-A")
            self.assertEqual(document.lines[0].markdown, "P<sup>32</sup>-A")
            self.assertEqual(document.lines[1].plain_text, "A")

    def test_legacy_small_superscript_at_two_thirds_line_height_stays_attached(self) -> None:
        body = {
            "text": "H",
            "x0": 55.3,
            "x1": 61.0,
            "top": 41.98,
            "bottom": 52.35,
            "size": 9.84,
        }
        superscript = {
            "text": "2",
            "x0": 61.0,
            "x1": 64.5,
            "top": 39.39,
            "bottom": 45.87,
            "size": 6.48,
        }
        next_line = {
            "text": "N",
            "x0": 55.3,
            "x1": 61.0,
            "top": 59.98,
            "bottom": 70.35,
            "size": 9.84,
        }

        groups = _group_upright_chars([body, superscript, next_line])

        self.assertEqual(len(groups), 2)
        self.assertEqual([item["text"] for item in groups[0]], ["H", "2"])
        self.assertEqual([item["text"] for item in groups[1]], ["N"])

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

    def test_same_row_reference_marker_precedes_slightly_higher_citation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference-row-order.pdf"
            document = canvas.Canvas(str(path), pagesize=(300, 300))
            document.setFont("Times-Roman", 10)
            document.drawString(40, 230, "[ii]")
            document.drawString(100, 230, "Reference two.")
            # A tiny baseline displacement changes the citation bbox top but
            # still renders it on the marker's visual row.
            document.drawString(40, 210, "[iii]")
            document.drawString(100, 210.2, "Reference three.")
            document.save()

            extracted = extract_pdf_text(path, "pdf/reference-row-order.pdf")

            self.assertEqual(
                [line.plain_text for line in extracted.lines],
                ["[ii]", "Reference two.", "[iii]", "Reference three."],
            )

    def test_reviewed_native_reading_regions_split_columns_before_line_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native-columns.pdf"
            document = canvas.Canvas(str(path), pagesize=(400, 300))
            document.setFont("Times-Roman", 10)
            document.drawString(40, 230, "Left column reaches its margin.")
            document.drawString(205, 230, "Right column starts here.")
            document.drawString(40, 210, "Left column")
            document.drawString(110, 210, "second line.")
            document.drawString(205, 210, "Right column second line.")
            document.save()

            extracted = extract_pdf_text(
                path,
                "pdf/native-columns.pdf",
                reading_regions=[
                    {"region_id": "left", "page": 1, "box": [0, 0, 203, 300]},
                    {"region_id": "right", "page": 1, "box": [204, 0, 400, 300]},
                ],
            )

            self.assertEqual(
                [line.plain_text for line in extracted.lines],
                [
                    "Left column reaches its margin.",
                    "Left column second line.",
                    "Right column starts here.",
                    "Right column second line.",
                ],
            )
            self.assertEqual(
                [line.source_region_id for line in extracted.lines],
                ["left", "left", "right", "right"],
            )
            region_rows = [
                row
                for row in extracted.diagnostic_rows
                if row.get("kind") == "configured_native_reading_region"
            ]
            self.assertEqual([row["region_id"] for row in region_rows], ["left", "right"])

    def test_line_geometry_excludes_leading_space_glyphs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "leading-spaces.pdf"
            document = canvas.Canvas(str(path), pagesize=(300, 300))
            document.setFont("Times-Roman", 10)
            document.drawString(40, 230, "   Indented paragraph")
            document.save()

            extracted = extract_pdf_text(path, "pdf/leading-spaces.pdf")

            self.assertEqual(extracted.lines[0].plain_text, "Indented paragraph")
            self.assertGreater(extracted.lines[0].bbox[0], 45.0)

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

    def test_type1_formula_digit_with_near_baseline_bottom_is_subscript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "type1-subscript.pdf"
            _write_pdf(
                path,
                fonts={"FRegular": ("AdvPS_TTR", ["C", "seven", "H"])},
                operations=[
                    ("FRegular", 12, 40, 210, bytes([1])),
                    # A 6.96-point digit placed 6.18 points below the main
                    # glyph top has a bottom only 1.14 points lower.  This is
                    # the geometry used by legacy molecular formulas.
                    ("FRegular", 6.96, 48, 203.82, bytes([2, 2])),
                    ("FRegular", 12, 55.2, 210, bytes([3])),
                ],
            )

            document = extract_pdf_text(path, "pdf/type1-subscript.pdf")

            self.assertEqual(document.lines[0].plain_text, "C77H")
            self.assertEqual(document.lines[0].markdown, "C<sub>77</sub>H")

    def test_compact_arial_formula_digit_with_shared_bottom_is_subscript(self) -> None:
        def decoded(text: str, size: float, top: float, bottom: float) -> _DecodedChar:
            return _DecodedChar(
                text,
                {"size": size, "top": top, "bottom": bottom},
                bold=False,
                italic=False,
            )

        chars = [
            decoded("C", 10.56, 100.0, 110.56),
            # Geometry from the reviewed ChemBioChem supplement: the
            # 6.96-point formula digit begins 3.32 points lower while its
            # bottom is only 0.28 points above the body baseline.
            decoded("4", 6.96, 103.32, 110.28),
            decoded("4", 6.96, 103.32, 110.28),
            decoded("H", 10.56, 100.0, 110.56),
        ]

        _infer_scripts(chars)

        self.assertEqual(_render_markdown(chars), "C<sub>44</sub>H")

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

    def test_full_subset_override_precedes_same_base_font_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "subset-overrides.pdf"
            _write_pdf(
                path,
                fonts={
                    "FFirst": ("AAAAAA+Symbol", ["C60"]),
                    "FSecond": ("BBBBBB+Symbol", ["C60"]),
                },
                operations=[
                    ("FFirst", 12, 40, 250, bytes([1])),
                    ("FSecond", 12, 40, 220, bytes([1])),
                ],
            )

            document = extract_pdf_text(
                path,
                "pdf/subset-overrides.pdf",
                glyph_overrides={
                    "Symbol": {60: "base"},
                    "AAAAAA+Symbol": {60: "α"},
                    "BBBBBB+Symbol": {60: "β"},
                },
            )

            self.assertEqual([line.plain_text for line in document.lines], ["α", "β"])

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
