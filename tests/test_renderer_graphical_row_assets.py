from __future__ import annotations

import unittest

from scripts.extraction.models import TableCell, TableItem, TablePart
from scripts.extraction.renderer import machine_table_records


class GraphicalRowAssetRecordTests(unittest.TestCase):
    def test_ka_records_link_single_graphical_first_cell_by_coordinates(self) -> None:
        table = TableItem(
            table_id="table_001",
            source_id="tbl1",
            label="Table 1",
            title_markdown="K<sub>a</sub>",
            title_plain="K_{a} [M^{−1}] measurements",
            parts=[
                TablePart(
                    part_id="tbl1-part-01",
                    rows=[
                        [
                            TableCell("Oligomer", "Oligomer", True),
                            TableCell("5′-aTACGt-3′", "5′-aTACGt-3′", True),
                        ],
                        [
                            TableCell("", "", False),
                            TableCell("5.1(±2.0) × 10^{9}", "5.1(±2.0) × 10<sup>9</sup>", False),
                        ],
                    ],
                )
            ],
            footnotes_markdown=[],
            footnotes_plain=[],
            source_path="html/main.html",
            source_locator="//table[1]",
            structure_assets={
                "part_01_row_002_column_001": (
                    "tables/main/table_001_cells/part_01_row_002_column_001.gif"
                )
            },
        )

        record = machine_table_records(table)[0]

        self.assertEqual(record["compound_id"], "part_01_row_002_column_001")
        self.assertEqual(
            record["structure_asset"],
            "tables/main/table_001_cells/part_01_row_002_column_001.gif",
        )


if __name__ == "__main__":
    unittest.main()
