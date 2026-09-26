"""Non-destructive OOXML spreadsheet supplement extraction.

Small worksheets are represented completely as semantic tables.  Very large
worksheets remain in their byte-identical linked workbook and receive an inline
machine-readable sheet/range/header inventory so ``record.json`` stays usable
in ordinary JSON tooling and the shared viewer.
"""

from __future__ import annotations

import html
import posixpath
import re
import zipfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from .models import ContentBlock, SourceFile, TableCell, TableItem, TablePart


XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "spreadsheetml.sheet"
)
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS, "pr": PACKAGE_REL_NS}
INLINE_CELL_LIMIT = 10_000
BUILTIN_NUMBER_FORMATS = {
    0: "General",
    1: "0",
    2: "0.00",
    9: "0%",
    10: "0.00%",
    11: "0.00E+00",
    14: "mm-dd-yy",
    15: "d-mmm-yy",
    16: "d-mmm",
    17: "mmm-yy",
    18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM",
    20: "h:mm",
    21: "h:mm:ss",
    22: "m/d/yy h:mm",
}
CELL_REFERENCE = re.compile(r"^([A-Z]{1,3})([1-9]\d*)$")
RANGE_REFERENCE = re.compile(
    r"^(?:'[^']+'!)?\$?([A-Z]{1,3})\$?([1-9]\d*)"
    r"(?::\$?([A-Z]{1,3})\$?([1-9]\d*))?$"
)

# Legacy Excel workbooks sometimes encode Greek letters as ASCII characters
# rendered with the exact Microsoft/Adobe ``Symbol`` font.  OOXML preserves
# that run font rather than the displayed Unicode glyph.  Translate only the
# alphabetic portion of the documented Symbol encoding and only for a rich
# text run whose font name is exactly ``Symbol``; other fonts stay literal.
SYMBOL_FONT_GREEK = str.maketrans(
    {
        "A": "Α", "B": "Β", "C": "Χ", "D": "Δ", "E": "Ε", "F": "Φ",
        "G": "Γ", "H": "Η", "I": "Ι", "J": "ϑ", "K": "Κ", "L": "Λ",
        "M": "Μ", "N": "Ν", "O": "Ο", "P": "Π", "Q": "Θ", "R": "Ρ",
        "S": "Σ", "T": "Τ", "U": "Υ", "V": "ς", "W": "Ω", "X": "Ξ",
        "Y": "Ψ", "Z": "Ζ", "a": "α", "b": "β", "c": "χ", "d": "δ",
        "e": "ε", "f": "φ", "g": "γ", "h": "η", "i": "ι", "j": "ϕ",
        "k": "κ", "l": "λ", "m": "μ", "n": "ν", "o": "ο", "p": "π",
        "q": "θ", "r": "ρ", "s": "σ", "t": "τ", "u": "υ", "v": "ϖ",
        "w": "ω", "x": "ξ", "y": "ψ", "z": "ζ",
    }
)


def _warning(
    code: str,
    message: str,
    source: SourceFile,
    supplement_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "code": code,
        "severity": "review",
        "message": message,
        "source_path": source.relative_path,
        "supplement_id": supplement_id,
    }


def _safe_entries(package: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries: dict[str, zipfile.ZipInfo] = {}
    folded: set[str] = set()
    for info in package.infolist():
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if (
            info.flag_bits & 0x1
            or path.is_absolute()
            or ".." in path.parts
            or any(":" in part for part in path.parts)
            or name.endswith("/")
        ):
            raise ValueError(f"unsafe XLSX package member: {info.filename!r}")
        key = name.casefold()
        if name in entries or key in folded:
            raise ValueError(f"duplicate XLSX package member: {info.filename!r}")
        entries[name] = info
        folded.add(key)
    required = {"[Content_Types].xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
    missing = required - entries.keys()
    if missing:
        raise ValueError(f"XLSX package is missing required members: {sorted(missing)}")
    return entries


def _xml(package: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo], name: str) -> ET.Element:
    if name not in entries:
        raise ValueError(f"XLSX relationship references a missing member: {name}")
    data = package.read(entries[name])
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError(f"XLSX XML contains a prohibited declaration: {name}")
    return ET.fromstring(data)


def _column_number(value: str) -> int:
    result = 0
    for character in value:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _column_label(value: int) -> str:
    result = ""
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _coordinate(value: str) -> tuple[int, int]:
    match = CELL_REFERENCE.fullmatch(value.upper())
    if match is None:
        raise ValueError(f"invalid XLSX cell reference: {value!r}")
    return int(match.group(2)), _column_number(match.group(1))


def _dimension(
    value: str,
    cells: dict[tuple[int, int], dict[str, Any]],
    merged: dict[tuple[int, int], tuple[int, int]],
) -> tuple[int, int, int, int, str]:
    """Return the authored content bounds rather than stale format extents.

    Excel's worksheet ``dimension`` is only a cached used-range hint. Publisher
    workbooks often retain formatted empty rows or even a styled cell at XFD1,
    which can expand that hint far beyond the cells carrying authored content.
    Bound the semantic representation by nonempty stored cells and extend an
    authored merged anchor through its declared span. The linked workbook still
    preserves every style and empty cell byte-for-byte.
    """

    match = RANGE_REFERENCE.fullmatch(value or "")
    declared: tuple[int, int, int, int] | None = None
    if match is not None:
        candidate = (
            int(match.group(2)),
            _column_number(match.group(1)),
            int(match.group(4) or match.group(2)),
            _column_number(match.group(3) or match.group(1)),
        )
        if candidate[0] <= candidate[2] and candidate[1] <= candidate[3]:
            declared = candidate

    content_coordinates = {
        coordinate
        for coordinate, cell in cells.items()
        if str(cell.get("stored_value", "")) or str(cell.get("text", ""))
    }
    if not content_coordinates:
        return 1, 1, 1, 1, "A1"

    for anchor, (rowspan, colspan) in merged.items():
        if anchor in content_coordinates:
            content_coordinates.add(
                (anchor[0] + rowspan - 1, anchor[1] + colspan - 1)
            )
    rows = [coordinate[0] for coordinate in content_coordinates]
    columns = [coordinate[1] for coordinate in content_coordinates]
    first_row, last_row = min(rows), max(rows)
    first_column, last_column = min(columns), max(columns)
    bounds = (first_row, first_column, last_row, last_column)
    range_label = (
        value
        if declared == bounds
        else (
            f"{_column_label(first_column)}{first_row}:"
            f"{_column_label(last_column)}{last_row}"
        )
    )
    return first_row, first_column, last_row, last_column, range_label


def _rich_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    parts: list[str] = []
    for child in node:
        if child.tag == f"{{{MAIN_NS}}}t":
            parts.append(child.text or "")
            continue
        if child.tag != f"{{{MAIN_NS}}}r":
            continue
        value = "".join(
            text.text or "" for text in child.findall(".//m:t", NS)
        )
        run_font = child.find("m:rPr/m:rFont", NS)
        if (
            run_font is not None
            and (run_font.get("val") or "").strip().casefold() == "symbol"
        ):
            value = value.translate(SYMBOL_FONT_GREEK)
        parts.append(value)
    return "".join(parts)


def _shared_strings(
    package: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo]
) -> list[str]:
    name = "xl/sharedStrings.xml"
    if name not in entries:
        return []
    root = _xml(package, entries, name)
    return [_rich_text(item) for item in root.findall("m:si", NS)]


def _cell_styles(
    package: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo]
) -> list[dict[str, Any]]:
    name = "xl/styles.xml"
    if name not in entries:
        return [{"bold": False, "number_format": "General"}]
    root = _xml(package, entries, name)
    fonts = root.find("m:fonts", NS)
    font_bold = [
        font.find("m:b", NS) is not None
        for font in ([] if fonts is None else list(fonts))
    ]
    cell_xfs = root.find("m:cellXfs", NS)
    custom_formats: dict[int, str] = {}
    for number_format in root.findall("m:numFmts/m:numFmt", NS):
        try:
            format_id = int(number_format.get("numFmtId", ""))
        except ValueError:
            continue
        format_code = number_format.get("formatCode") or ""
        if format_code:
            custom_formats[format_id] = format_code
    result: list[dict[str, Any]] = []
    for cell_format in [] if cell_xfs is None else list(cell_xfs):
        try:
            font_id = int(cell_format.get("fontId", "0"))
        except ValueError:
            font_id = 0
        try:
            number_format_id = int(cell_format.get("numFmtId", "0"))
        except ValueError:
            number_format_id = 0
        result.append(
            {
                "bold": 0 <= font_id < len(font_bold) and font_bold[font_id],
                "number_format": custom_formats.get(
                    number_format_id,
                    BUILTIN_NUMBER_FORMATS.get(number_format_id, f"builtin-{number_format_id}"),
                ),
            }
        )
    return result or [{"bold": False, "number_format": "General"}]


def _format_numeric_value(value: str, number_format: str, *, date_1904: bool) -> str:
    """Render the small, explicit Excel number-format subset used by sources.

    Unsupported formats deliberately retain the stored OOXML numeric string;
    the exact format code is still exposed in the table's machine records.
    """

    try:
        numeric = Decimal(value)
    except InvalidOperation:
        return value
    if number_format == "General":
        mantissa = value.split("E", 1)[0].split("e", 1)[0]
        significant = re.sub(r"[^0-9]", "", mantissa).lstrip("0") or "0"
        fraction = mantissa.split(".", 1)[1] if "." in mantissa else ""
        binary_tail = re.search(r"(?:0{6,}|9{6,})\d{0,6}$", fraction) is not None
        if len(significant) > 15 or binary_tail:
            # Excel numeric cells have 15 significant decimal digits. OOXML
            # writers commonly serialize the underlying binary double with 17
            # digits, exposing tails that Excel's General view never shows.
            # Retain the exact stored string in machine_records below.
            precision = 12 if binary_tail else 15
            return format(float(numeric), f".{precision}g").replace("e", "E")
        return value
    if number_format == "0":
        return format(numeric, ".0f")
    if number_format == "0.00":
        return format(numeric, ".2f")
    if number_format == "0.0000":
        return format(numeric, ".4f")
    if number_format == "0.00E+00":
        return format(numeric, ".2E")
    if number_format in {"0%", "0.00%"}:
        places = 0 if number_format == "0%" else 2
        return f"{format(numeric * 100, f'.{places}f')}%"
    if number_format in {"d-mmm", "d-mmm-yy", "mm-dd-yy", "mmm-yy"}:
        epoch = datetime(1904, 1, 1) if date_1904 else datetime(1899, 12, 30)
        date = epoch + timedelta(days=float(numeric))
        if number_format == "d-mmm":
            return f"{date.day}-{date.strftime('%b')}"
        if number_format == "d-mmm-yy":
            return f"{date.day}-{date.strftime('%b-%y')}"
        if number_format == "mm-dd-yy":
            return date.strftime("%m-%d-%y")
        return date.strftime("%b-%y")
    return value


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.get("t") or "n"
    formula = cell.find("m:f", NS)
    value = cell.findtext("m:v", default="", namespaces=NS)
    if cell_type == "s":
        try:
            visible = shared[int(value)]
        except (ValueError, IndexError):
            raise ValueError(f"invalid shared-string index in cell {cell.get('r')!r}")
    elif cell_type == "inlineStr":
        visible = _rich_text(cell.find("m:is", NS))
    elif cell_type == "b":
        visible = "TRUE" if value == "1" else "FALSE" if value == "0" else value
    else:
        visible = value
    if formula is not None:
        expression = formula.text or ""
        return f"={expression}" + (f" [cached: {visible}]" if visible else "")
    return visible


def _sheet_cells(
    sheet: ET.Element,
    shared: list[str],
    cell_styles: list[dict[str, Any]],
    *,
    date_1904: bool,
) -> tuple[dict[tuple[int, int], dict[str, Any]], dict[tuple[int, int], tuple[int, int]], set[tuple[int, int]]]:
    cells: dict[tuple[int, int], dict[str, Any]] = {}
    for cell in sheet.findall(".//m:sheetData/m:row/m:c", NS):
        reference = cell.get("r") or ""
        coordinate = _coordinate(reference)
        try:
            style_id = int(cell.get("s", "0"))
        except ValueError:
            style_id = 0
        style = (
            cell_styles[style_id]
            if 0 <= style_id < len(cell_styles)
            else {"bold": False, "number_format": "General"}
        )
        stored_value = _cell_value(cell, shared)
        number_format = str(style["number_format"])
        cell_type = cell.get("t") or "n"
        formula = cell.find("m:f", NS)
        text = (
            _format_numeric_value(
                stored_value, number_format, date_1904=date_1904
            )
            if cell_type == "n" and formula is None and stored_value
            else stored_value
        )
        cells[coordinate] = {
            "text": text,
            "stored_value": stored_value,
            "number_format": number_format,
            "coordinate": reference.upper(),
            "header": bool(style["bold"]),
        }

    merged: dict[tuple[int, int], tuple[int, int]] = {}
    covered: set[tuple[int, int]] = set()
    for merge in sheet.findall(".//m:mergeCells/m:mergeCell", NS):
        value = merge.get("ref") or ""
        match = RANGE_REFERENCE.fullmatch(value)
        if match is None or match.group(3) is None or match.group(4) is None:
            raise ValueError(f"invalid XLSX merged range: {value!r}")
        first = (int(match.group(2)), _column_number(match.group(1)))
        last = (int(match.group(4)), _column_number(match.group(3)))
        if first[0] > last[0] or first[1] > last[1]:
            raise ValueError(f"reversed XLSX merged range: {value!r}")
        merged[first] = (last[0] - first[0] + 1, last[1] - first[1] + 1)
        for row in range(first[0], last[0] + 1):
            for column in range(first[1], last[1] + 1):
                if (row, column) != first:
                    covered.add((row, column))
    return cells, merged, covered


def _markup(value: str) -> str:
    return html.escape(value, quote=False).replace("\n", "<br>")


def _table(
    source: SourceFile,
    supplement_id: str,
    sheet_index: int,
    sheet_name: str,
    state: str,
    worksheet_part: str,
    dimension: tuple[int, int, int, int, str],
    cells: dict[tuple[int, int], dict[str, Any]],
    merged: dict[tuple[int, int], tuple[int, int]],
    covered: set[tuple[int, int]],
) -> TableItem:
    first_row, first_column, last_row, last_column, range_label = dimension
    rows: list[list[TableCell]] = []
    machine_records: list[dict[str, Any]] = []
    for row_number in range(first_row, last_row + 1):
        row: list[TableCell] = []
        for column_number in range(first_column, last_column + 1):
            coordinate = (row_number, column_number)
            if coordinate in covered:
                continue
            raw = cells.get(coordinate, {"text": "", "header": False})
            rowspan, colspan = merged.get(coordinate, (1, 1))
            text = str(raw["text"])
            number_format = str(raw.get("number_format", "General"))
            stored_value = str(raw.get("stored_value", text))
            if stored_value and (
                number_format != "General" or stored_value != text
            ):
                machine_records.append(
                    {
                        "record_type": "spreadsheet_cell_value",
                        "sheet": sheet_name,
                        "coordinate": str(raw.get("coordinate", "")),
                        "stored_value": stored_value,
                        "display_text": text,
                        "number_format": number_format,
                    }
                )
            row.append(
                TableCell(
                    text=text,
                    markdown=_markup(text),
                    header=bool(raw["header"]),
                    rowspan=rowspan,
                    colspan=colspan,
                )
            )
        rows.append(row)
    identifier = f"{supplement_id}_sheet_{sheet_index:03d}"
    state_note = "" if state == "visible" else f" ({state} worksheet)"
    title = f'Worksheet “{sheet_name}”{state_note} from {source.path.name}'
    return TableItem(
        table_id=identifier,
        source_id=f"{source.path.name}#{sheet_name}",
        label=f"Worksheet {sheet_name}",
        title_markdown=_markup(title),
        title_plain=title,
        parts=[TablePart(part_id=f"{identifier}_part_001", rows=rows)],
        footnotes_markdown=[],
        footnotes_plain=[],
        source_path=source.relative_path,
        source_locator=(
            f"workbook-part=xl/workbook.xml;worksheet-part={worksheet_part};"
            f"sheet={sheet_name};state={state};range={range_label};complete-inline"
        ),
        source_kind="spreadsheet",
        machine_records=machine_records,
    )


def extract_xlsx_supplement(
    source: SourceFile,
    supplement_id: str,
    *,
    xlsx_path: Path,
) -> tuple[list[ContentBlock], list[TableItem], list[dict[str, Any]]]:
    """Extract worksheet structure without saving or rewriting the workbook."""

    blocks: list[ContentBlock] = []
    tables: list[TableItem] = []
    warnings: list[dict[str, Any]] = []
    with zipfile.ZipFile(xlsx_path) as package:
        entries = _safe_entries(package)
        workbook = _xml(package, entries, "xl/workbook.xml")
        relationships = _xml(package, entries, "xl/_rels/workbook.xml.rels")
        relationship_targets = {
            relationship.get("Id", ""): relationship.get("Target", "")
            for relationship in relationships.findall("pr:Relationship", NS)
            if (relationship.get("Type") or "").endswith("/worksheet")
        }
        shared = _shared_strings(package, entries)
        cell_styles = _cell_styles(package, entries)
        workbook_properties = workbook.find("m:workbookPr", NS)
        date_1904 = bool(
            workbook_properties is not None
            and str(workbook_properties.get("date1904", "")).casefold()
            in {"1", "true"}
        )
        sheet_nodes = workbook.findall("m:sheets/m:sheet", NS)
        if not sheet_nodes:
            raise ValueError("XLSX workbook contains no worksheets")

        summary_items: list[str] = []
        for sheet_index, sheet_node in enumerate(sheet_nodes, 1):
            name = sheet_node.get("name") or f"Sheet{sheet_index}"
            state = sheet_node.get("state") or "visible"
            relationship_id = sheet_node.get(f"{{{REL_NS}}}id") or ""
            target = relationship_targets.get(relationship_id)
            if not target:
                raise ValueError(f"XLSX worksheet {name!r} has no internal relationship")
            worksheet_part = posixpath.normpath(posixpath.join("xl", target))
            if worksheet_part.startswith("../") or worksheet_part.startswith("/"):
                raise ValueError(f"unsafe XLSX worksheet relationship: {target!r}")
            worksheet = _xml(package, entries, worksheet_part)
            cells, merged, covered = _sheet_cells(
                worksheet, shared, cell_styles, date_1904=date_1904
            )
            dimension_node = worksheet.find("m:dimension", NS)
            dimension = _dimension(
                dimension_node.get("ref", "") if dimension_node is not None else "",
                cells,
                merged,
            )
            first_row, first_column, last_row, last_column, range_label = dimension
            rectangle_cells = (last_row - first_row + 1) * (last_column - first_column + 1)
            summary_items.append(
                f"{name} [{state}; {range_label}; {last_row - first_row + 1:,} rows × "
                f"{last_column - first_column + 1:,} columns]"
            )
            if rectangle_cells <= INLINE_CELL_LIMIT:
                tables.append(
                    _table(
                        source,
                        supplement_id,
                        sheet_index,
                        name,
                        state,
                        worksheet_part,
                        dimension,
                        cells,
                        merged,
                        covered,
                    )
                )
                continue

            header_values = [
                str(cells.get((first_row, column), {}).get("text", ""))
                for column in range(first_column, last_column + 1)
            ]
            header_values = [value for value in header_values if value]
            header_text = " | ".join(header_values) if header_values else "(no nonempty first-row labels)"
            content_cell_count = sum(
                1
                for cell in cells.values()
                if str(cell.get("stored_value", "")) or str(cell.get("text", ""))
            )
            visible = (
                f'Worksheet “{name}” ({state}) uses range {range_label}: '
                f"{last_row - first_row + 1:,} rows × {last_column - first_column + 1:,} columns, "
                f"with {content_cell_count:,} content-bearing cells. "
                f"First-row labels: {header_text}. "
                f"The complete cell data remain in the linked original workbook {source.path.name}."
            )
            blocks.append(
                ContentBlock(
                    block_id=f"{supplement_id}-workbook-sheet-{sheet_index:03d}",
                    kind="workbook_sheet",
                    markdown=_markup(visible),
                    plain_text=visible,
                    source_path=source.relative_path,
                    source_locator=(
                        f"workbook-part=xl/workbook.xml;worksheet-part={worksheet_part};"
                        f"sheet={name};state={state};range={range_label};linked-large-sheet"
                    ),
                )
            )

        summary = (
            f"Workbook {source.path.name} contains {len(sheet_nodes)} worksheet"
            f"{'s' if len(sheet_nodes) != 1 else ''}: " + "; ".join(summary_items) + "."
        )
        blocks.insert(
            0,
            ContentBlock(
                block_id=f"{supplement_id}-workbook-summary",
                kind="workbook_summary",
                markdown=_markup(summary),
                plain_text=summary,
                source_path=source.relative_path,
                source_locator="workbook-part=xl/workbook.xml;worksheet-inventory",
            ),
        )

        external_relationships = [
            relationship
            for relationship in relationships.findall("pr:Relationship", NS)
            if (relationship.get("TargetMode") or "").casefold() == "external"
        ]
        if external_relationships:
            warnings.append(
                _warning(
                    "spreadsheet_external_relationships_not_followed",
                    f"Workbook contains {len(external_relationships)} external relationships; they were not followed",
                    source,
                    supplement_id,
                )
            )
    return blocks, tables, warnings


__all__ = ["INLINE_CELL_LIMIT", "XLSX_MEDIA_TYPE", "extract_xlsx_supplement"]
