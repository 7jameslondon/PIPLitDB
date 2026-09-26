from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.extraction.models import SourceFile
from scripts.extraction.supplements import extract_supplements


TABLE_ONLY_HTML = """
<html><body><div xmlns="http://www.w3.org/1999/xhtml">
  <p>&#160;</p>
  <p><b>Table 2. Synthetic supporting measurements</b></p>
  <table>
    <tr><td><b>Analyte<br/>name</b></td><td><i>P</i> value</td></tr>
    <tr><td>Example A</td><td>1.2E-06</td></tr>
    <tr><td>Example B</td><td>0.004</td></tr>
  </table>
  <p>Measurements with a <i>P</i> value &lt; 0.01.</p>
</div></body></html>
"""


class HtmlSupplementTests(unittest.TestCase):
    def test_legacy_single_table_html_is_structured_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "table2.html"
            payload = TABLE_ONLY_HTML.encode("utf-8")
            path.write_bytes(payload)
            source = SourceFile(
                role="supplement",
                path=path,
                relative_path="papers (private)/00001/supplementary/table2.html",
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                detected_format="text/html",
            )

            supplement = extract_supplements([source], root / "extraction")[0]

        self.assertEqual(supplement.warnings, [])
        self.assertEqual(len(supplement.tables), 1)
        table = supplement.tables[0]
        self.assertEqual(table.label, "Table 2")
        self.assertEqual(table.title_plain, "Table 2. Synthetic supporting measurements")
        self.assertEqual(len(table.parts[0].rows), 3)
        self.assertTrue(all(cell.header for cell in table.parts[0].rows[0]))
        self.assertEqual(table.parts[0].rows[0][0].text, "Analyte name")
        self.assertEqual(table.parts[0].rows[1][1].text, "1.2E-06")
        self.assertEqual(
            table.footnotes_plain,
            ["Measurements with a P value < 0.01."],
        )

    def test_unrecognized_html_remains_preserved_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "notes.html"
            payload = b"<html><body><p>Standalone prose.</p></body></html>"
            path.write_bytes(payload)
            source = SourceFile(
                role="supplement",
                path=path,
                relative_path="papers (private)/00001/supplementary/notes.html",
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                detected_format="text/html",
            )

            supplement = extract_supplements([source], root / "extraction")[0]

        self.assertEqual(supplement.tables, [])
        self.assertEqual(
            [warning["code"] for warning in supplement.warnings],
            ["unsupported_supplement_text_extraction"],
        )


if __name__ == "__main__":
    unittest.main()
