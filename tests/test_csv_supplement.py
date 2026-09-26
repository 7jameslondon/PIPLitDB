from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.extraction.csv_supplement import extract_csv_supplement
from scripts.extraction.models import SourceFile
from scripts.extraction.rich_text import (
    inline_markup_to_safe_html,
    rich_text_matches_plain,
)
from scripts.extraction.supplements import extract_supplements


def source_for(
    path: Path,
    payload: bytes,
    *,
    detected_format: str = "application/vnd.ms-excel",
) -> SourceFile:
    return SourceFile(
        role="supplement",
        path=path,
        relative_path=f"papers (private)/00001/supplementary/{path.name}",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        detected_format=detected_format,
    )


class CsvSupplementTests(unittest.TestCase):
    def test_smiles_bracket_atom_is_literal_not_a_markdown_link(self) -> None:
        payload = b"Compound_ID,SMILES\nPolyamide 1,C[N+](C)C\n"
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "structures.csv"
            path.write_bytes(payload)

            _blocks, tables, warnings = extract_csv_supplement(
                source_for(path, payload),
                "supplement_001",
                csv_path=path,
            )

            self.assertEqual(warnings, [])
            cell = tables[0].parts[0].rows[1][1]
            rendered = inline_markup_to_safe_html(cell.markdown)
            self.assertEqual(cell.text, "C[N+](C)C")
            self.assertNotIn("<a ", rendered)
            self.assertTrue(rich_text_matches_plain(cell.text, rendered))

    def test_shift_jis_csv_is_a_complete_semantic_table_without_derivatives(self) -> None:
        text = (
            "Compound #,SMILES,ΔTm (Target),Viability (3 μM)\r\n"
            '1,"C(=O)N,CC",1.2 ± 0.1,75.0 ± 2.0\r\n'
            "2,CNCC,0.4 ± 0.2,91.0 ± 1.5\r\n"
        )
        payload = text.encode("shift_jis")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "values.csv"
            path.write_bytes(payload)
            extraction_root = root / "extraction"

            result = extract_supplements(
                [source_for(path, payload)], extraction_root
            )[0]

            copied = extraction_root / result.copied_path
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(copied.read_bytes(), payload)
            self.assertEqual(result.warnings, [])
            self.assertEqual(result.blocks, [])
            self.assertEqual(result.assets, [])
            self.assertEqual(len(result.tables), 1)
            table = result.tables[0]
            self.assertEqual(table.source_kind, "spreadsheet")
            self.assertFalse(table.requires_source_image)
            self.assertIsNone(table.image_path)
            self.assertIn("encoding=shift_jis", table.source_locator)
            self.assertEqual(
                [[cell.text for cell in row] for row in table.parts[0].rows],
                [
                    ["Compound #", "SMILES", "ΔTm (Target)", "Viability (3 μM)"],
                    ["1", "C(=O)N,CC", "1.2 ± 0.1", "75.0 ± 2.0"],
                    ["2", "CNCC", "0.4 ± 0.2", "91.0 ± 1.5"],
                ],
            )
            self.assertTrue(all(cell.header for cell in table.parts[0].rows[0]))
            self.assertTrue(
                all(
                    not cell.header
                    for row in table.parts[0].rows[1:]
                    for cell in row
                )
            )
            self.assertEqual(list(extraction_root.rglob("*.png")), [])
            self.assertEqual(list(extraction_root.rglob("*.csv")), [copied])

    def test_gb18030_scientific_csv_is_not_misdecoded_as_shift_jis(self) -> None:
        text = "IC50 (μM),KD [M]\r\n23.23±0.98,8.28(±0.56) ?10-9\r\n"
        payload = text.encode("gb18030")
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "values.csv"
            path.write_bytes(payload)

            _blocks, tables, warnings = extract_csv_supplement(
                source_for(path, payload),
                "supplement_001",
                csv_path=path,
            )

            self.assertEqual(warnings, [])
            table = tables[0]
            self.assertIn("encoding=gb18030", table.source_locator)
            self.assertEqual(
                [[cell.text for cell in row] for row in table.parts[0].rows],
                [
                    ["IC50 (μM)", "KD [M]"],
                    ["23.23±0.98", "8.28(±0.56) ?10-9"],
                ],
            )
            self.assertNotIn("ｦﾌ", table.parts[0].rows[0][0].text)

    def test_excel_media_type_without_csv_suffix_is_not_misrouted(self) -> None:
        payload = b"not,a,binary,workbook\n"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "legacy.xls"
            path.write_bytes(payload)
            result = extract_supplements(
                [source_for(path, payload)], root / "extraction"
            )[0]

            self.assertEqual(result.tables, [])
            self.assertEqual(
                [warning["code"] for warning in result.warnings],
                ["unsupported_supplement_text_extraction"],
            )

    def test_inconsistent_csv_rows_fail_closed(self) -> None:
        payload = b"a,b\n1,2,3\n"
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "invalid.csv"
            path.write_bytes(payload)
            with self.assertRaisesRegex(ValueError, "row 2 has 3 columns"):
                extract_csv_supplement(
                    source_for(path, payload),
                    "supplement_001",
                    csv_path=path,
                )


if __name__ == "__main__":
    unittest.main()
