from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.extraction.discovery import detect_format
from scripts.extraction.supplements import _natural_path_key


class SupplementDiscoveryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
