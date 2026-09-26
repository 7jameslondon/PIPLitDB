"""Deterministic, non-destructive extraction of small CSV supplements."""

from __future__ import annotations

import csv
import html
import io
from pathlib import Path
from typing import Any

from .models import ContentBlock, SourceFile, TableCell, TableItem, TablePart


CSV_SUFFIX = ".csv"
CSV_MEDIA_TYPES = frozenset(
    {
        "application/csv",
        "application/vnd.ms-excel",
        "text/csv",
        "text/plain",
    }
)
INLINE_CELL_LIMIT = 10_000
_SCIENTIFIC_GLYPHS = frozenset(
    "°±×−≤≥≪≫μΔαβγδδεζηθικλνξοπρστυφχψω"
    "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"
)


def is_csv_supplement(source: SourceFile) -> bool:
    """Recognize CSVs without misrouting legacy binary Excel workbooks."""

    return (
        source.path.suffix.casefold() == CSV_SUFFIX
        and source.detected_format.casefold() in CSV_MEDIA_TYPES
    )


def _decode_csv(payload: bytes) -> tuple[str, str]:
    """Decode one CSV losslessly using strict, deterministic candidates.

    Legacy scientific supplements in the corpus include both Shift-JIS and
    GB18030 files without byte-order marks. Some scientific-symbol byte pairs
    are legal under both codecs, so prefer the strict decoding that preserves
    more recognizable scientific glyphs; keep Shift-JIS as the stable tie
    break for backward compatibility.
    """

    if payload.startswith(b"\xef\xbb\xbf"):
        return payload.decode("utf-8-sig", errors="strict"), "utf-8-sig"
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16", errors="strict"), "utf-16"
    try:
        return payload.decode("utf-8", errors="strict"), "utf-8"
    except UnicodeDecodeError:
        candidates: list[tuple[str, str]] = []
        for encoding in ("shift_jis", "gb18030"):
            try:
                candidates.append(
                    (payload.decode(encoding, errors="strict"), encoding)
                )
            except UnicodeDecodeError:
                continue
        if candidates:
            return max(
                candidates,
                key=lambda candidate: sum(
                    character in _SCIENTIFIC_GLYPHS
                    for character in candidate[0]
                ),
            )
        raise ValueError(
            "CSV is neither strict UTF-8, Shift-JIS, nor GB18030"
        )


def _rows(text: str) -> list[list[str]]:
    if "\x00" in text:
        raise ValueError("CSV contains a NUL character")
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise ValueError(f"malformed CSV: {exc}") from exc
    if not rows:
        raise ValueError("CSV contains no rows")
    width = len(rows[0])
    if width < 1:
        raise ValueError("CSV header row is empty")
    for row_number, row in enumerate(rows, start=1):
        if len(row) != width:
            raise ValueError(
                f"CSV row {row_number} has {len(row)} columns; expected {width}"
            )
    return rows


def _markup(value: str) -> str:
    escaped = html.escape(value, quote=False)
    # CSV cells are literal data, not authoring markup.  Neutralize the small
    # Markdown subset understood by ``rich_text`` before it can reinterpret
    # scientific strings such as the SMILES token ``[N+](C)`` as a link.
    return escaped.translate(
        str.maketrans(
            {
                "\\": "&#92;",
                "*": "&#42;",
                "_": "&#95;",
                "[": "&#91;",
                "]": "&#93;",
                "(": "&#40;",
                ")": "&#41;",
            }
        )
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


def extract_csv_supplement(
    source: SourceFile,
    supplement_id: str,
    *,
    csv_path: Path,
) -> tuple[list[ContentBlock], list[TableItem], list[dict[str, Any]]]:
    """Represent a small CSV completely as one semantic table.

    The already verified candidate copy is read in place. No rewritten CSV,
    table image, or other derivative is created.
    """

    text, encoding = _decode_csv(csv_path.read_bytes())
    values = _rows(text)
    row_count = len(values)
    column_count = len(values[0])
    locator = (
        f"csv;encoding={encoding};delimiter=comma;header-row=1;"
        f"rows={row_count};columns={column_count}"
    )
    if row_count * column_count > INLINE_CELL_LIMIT:
        visible = (
            f"CSV {source.path.name} contains {row_count:,} rows × "
            f"{column_count:,} columns. The complete data remain in the linked "
            "original CSV."
        )
        return (
            [
                ContentBlock(
                    block_id=f"{supplement_id}-csv-summary",
                    kind="csv_summary",
                    markdown=_markup(visible),
                    plain_text=visible,
                    source_path=source.relative_path,
                    source_locator=locator + ";linked-large-table",
                )
            ],
            [],
            [
                _warning(
                    "csv_table_too_large_to_inline",
                    (
                        f"CSV has {row_count * column_count:,} cells; the byte-identical "
                        "linked original is authoritative"
                    ),
                    source,
                    supplement_id,
                )
            ],
        )

    rows = [
        [
            TableCell(
                text=value,
                markdown=_markup(value),
                header=row_number == 1,
            )
            for value in row
        ]
        for row_number, row in enumerate(values, start=1)
    ]
    table_id = f"{supplement_id}_csv_table_001"
    title = f"Supplementary CSV data from {source.path.name}"
    table = TableItem(
        table_id=table_id,
        source_id=source.path.name,
        label=f"CSV data: {source.path.name}",
        title_markdown=_markup(title),
        title_plain=title,
        parts=[TablePart(part_id=f"{table_id}_part_001", rows=rows)],
        footnotes_markdown=[],
        footnotes_plain=[],
        source_path=source.relative_path,
        source_locator=locator + ";complete-inline",
        source_kind="spreadsheet",
    )
    return [], [table], []


__all__ = [
    "CSV_MEDIA_TYPES",
    "CSV_SUFFIX",
    "INLINE_CELL_LIMIT",
    "extract_csv_supplement",
    "is_csv_supplement",
]
