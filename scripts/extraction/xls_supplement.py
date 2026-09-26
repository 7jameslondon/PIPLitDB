"""Read legacy BIFF workbooks without recalculation, conversion, or saving.

Large sheets use the same linked-workbook inventory contract as OOXML sheets.
The original file remains the authority for formulas and presentation objects.
"""
from __future__ import annotations

from pathlib import Path

from .models import ContentBlock, SourceFile
from .xlsx_supplement import INLINE_CELL_LIMIT, _column_label, _format_numeric_value, _markup, _table

XLS_MEDIA_TYPE = "application/vnd.ms-excel"


def is_xls_supplement(source: SourceFile) -> bool:
    """Authenticate a legacy OLE workbook; Excel MIME is also used for CSV."""
    if source.detected_format != XLS_MEDIA_TYPE or source.path.suffix.casefold() != ".xls":
        return False
    with source.path.open("rb") as stream:
        return stream.read(8) == bytes.fromhex("D0CF11E0A1B11AE1")


def extract_xls_supplement(source: SourceFile, supplement_id: str, *, xls_path: Path):
    import xlrd

    book = xlrd.open_workbook(str(xls_path), formatting_info=True, on_demand=True)
    blocks, tables, summaries = [], [], []
    try:
        for index, sheet in enumerate(book.sheets(), 1):
            state = {0: "visible", 1: "hidden", 2: "veryHidden"}.get(sheet.visibility, "hidden")
            cells, merged, covered = {}, {}, set()
            for row in range(sheet.nrows):
                for col in range(sheet.ncols):
                    cell = sheet.cell(row, col)
                    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                        continue
                    xf = book.xf_list[cell.xf_index]
                    fmt = book.format_map[xf.format_key].format_str
                    stored = str(cell.value)
                    if cell.ctype in (xlrd.XL_CELL_NUMBER, xlrd.XL_CELL_DATE):
                        stored = repr(cell.value)
                        # BIFF stores numeric values as doubles; retain that exact
                        # recoverable value in machine records when display differs.
                        text = _format_numeric_value(stored, fmt, date_1904=bool(book.datemode))
                        if fmt == "General" and cell.value.is_integer():
                            text = str(int(cell.value))
                    elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        text = "TRUE" if cell.value else "FALSE"
                    elif cell.ctype == xlrd.XL_CELL_ERROR:
                        text = xlrd.error_text_from_code.get(cell.value, f"#ERROR({cell.value})")
                    else:
                        text = stored
                    coordinate = f"{_column_label(col + 1)}{row + 1}"
                    cells[row + 1, col + 1] = {
                        "text": text, "stored_value": stored, "number_format": fmt,
                        "coordinate": coordinate, "header": bool(book.font_list[xf.font_index].bold),
                    }
            for r0, r1, c0, c1 in sheet.merged_cells:
                merged[r0 + 1, c0 + 1] = (r1 - r0, c1 - c0)
                covered.update((r + 1, c + 1) for r in range(r0, r1) for c in range(c0, c1)
                               if (r, c) != (r0, c0))
            nrows = max([sheet.nrows, *(r + span[0] - 1 for (r, _), span in merged.items())])
            ncols = max([sheet.ncols, *(c + span[1] - 1 for (_, c), span in merged.items())])
            label = f"A1:{_column_label(max(ncols, 1))}{max(nrows, 1)}"
            summaries.append(f"{sheet.name} [{state}; {label}; {nrows:,} rows × {ncols:,} columns]")
            locator = f"BIFF;sheet={sheet.name};state={state};range={label}"
            if nrows and ncols and nrows * ncols <= INLINE_CELL_LIMIT:
                table = _table(source, supplement_id, index, sheet.name, state, "Workbook",
                               (1, 1, nrows, ncols, label), cells, merged, covered)
                table.source_locator = locator + ";complete-inline;cached-cell-values"
                tables.append(table)
            else:
                headers = " | ".join(cells.get((1, c), {}).get("text", "") for c in range(1, ncols + 1))
                visible = (f'Worksheet “{sheet.name}” ({state}) uses range {label}: '
                           f'{nrows:,} rows × {ncols:,} columns, with {len(cells):,} content-bearing cells. '
                           f'First-row labels: {headers or "(none)"}. '
                           f'The complete cell data remain in the linked original workbook {source.path.name}.')
                blocks.append(ContentBlock(block_id=f"{supplement_id}-workbook-sheet-{index:03d}",
                                           kind="workbook_sheet", markdown=_markup(visible), plain_text=visible,
                                           source_path=source.relative_path, source_locator=locator + ";linked-large-sheet"))
        summary = f"Workbook {source.path.name} contains {book.nsheets} worksheets: " + "; ".join(summaries) + "."
        blocks.insert(0, ContentBlock(block_id=f"{supplement_id}-workbook-summary", kind="workbook_summary",
                                     markdown=_markup(summary), plain_text=summary,
                                     source_path=source.relative_path, source_locator="BIFF;worksheet-inventory"))
    finally:
        book.release_resources()
    return blocks, tables, []
