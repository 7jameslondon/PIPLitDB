import unittest
from types import SimpleNamespace

from scripts.extraction.pdf_ocr_text import (
    _coalesced_region_lines,
    _escape_ocr_markdown,
)
from scripts.extraction.rich_text import inline_markup_to_safe_html


class PdfOcrMarkdownTests(unittest.TestCase):
    def test_literal_ocr_characters_do_not_become_markup_or_entities(self) -> None:
        source = "R&D Systems used <5% and * or _ literally"

        escaped = _escape_ocr_markdown(source)

        self.assertEqual(
            inline_markup_to_safe_html(escaped),
            "R&amp;D Systems used &lt;5% and * or _ literally",
        )


class PdfOcrCoalescingTests(unittest.TestCase):
    @staticmethod
    def observation(text, box, confidence=0.99):
        left, top, right, bottom = box
        return SimpleNamespace(
            text=text,
            confidence=confidence,
            polygon_pdf_points=(
                (left, top), (right, top), (right, bottom), (left, bottom)
            ),
        )

    def test_adjacent_words_with_overlapping_boxes_keep_boundary_letter(self):
        first = (
            329.7201365187713, 641.1581632653061,
            376.52901023890786, 650.5102040816327,
        )
        second = (
            373.88850967007966, 640.1989795918367,
            413.25597269624575, 651.469387755102,
        )
        region = SimpleNamespace(page=1, observations=[
            self.observation("promoter", first),
            self.observation("region", second, 0.91),
        ])

        lines = _coalesced_region_lines(region)

        self.assertEqual([line["text"] for line in lines], ["promoter region"])
        self.assertEqual(lines[0]["confidence"], 0.91)
        self.assertEqual(lines[0]["constituent_geometry"], ((1, first), (1, second)))

    def test_distinct_words_values_and_repetitions_keep_all_characters(self):
        for first, second, expected in [
            ("DNA", "alkylation", "DNA alkylation"),
            ("100", "0.5", "100 0.5"),
            ("the", "the", "the the"),
            ("region", "region", "region region"),
        ]:
            with self.subTest(first=first, second=second):
                region = SimpleNamespace(page=1, observations=[
                    self.observation(first, (10, 20, 40, 30)),
                    self.observation(second, (38, 20, 80, 30)),
                ])
                self.assertEqual(_coalesced_region_lines(region)[0]["text"], expected)

    def test_identical_whole_observations_deduplicate_but_keep_evidence(self):
        box = (10, 20, 40, 30)
        region = SimpleNamespace(page=2, observations=[
            self.observation("signal", box, 0.98),
            self.observation(" signal ", box, 0.83),
            self.observation("signal", (50, 20, 80, 30), 0.95),
        ])

        lines = _coalesced_region_lines(region)

        self.assertEqual(lines[0]["text"], "signal signal")
        self.assertEqual(lines[0]["confidence"], 0.83)
        self.assertEqual(lines[0]["bbox"], (10, 20, 80, 30))
        self.assertEqual(
            lines[0]["constituent_geometry"],
            ((2, box), (2, box), (2, (50, 20, 80, 30))),
        )

    def test_case_distinct_symbols_at_identical_geometry_are_preserved(self):
        region = SimpleNamespace(page=1, observations=[
            self.observation("A", (10, 20, 40, 30)),
            self.observation("a", (10, 20, 40, 30)),
        ])
        self.assertEqual(_coalesced_region_lines(region)[0]["text"], "A a")

    def test_punctuation_and_line_order_are_preserved(self):
        region = SimpleNamespace(page=3, observations=[
            self.observation("next", (10, 60, 40, 70)),
            self.observation(";", (160, 20, 170, 30)),
            self.observation(")", (140, 20, 150, 30)),
            self.observation("value", (80, 20, 130, 30)),
            self.observation("(", (60, 20, 70, 30)),
            self.observation("unit", (10, 20, 50, 30)),
        ])
        self.assertEqual(
            [line["text"] for line in _coalesced_region_lines(region)],
            ["unit (value);", "next"],
        )


if __name__ == "__main__":
    unittest.main()
