"""Bounded, read-only extraction for PowerPoint supplementary files.

The parser reads OOXML parts directly from the ZIP package.  It does not invoke
PowerPoint/LibreOffice, run OCR, execute macros, or unpack the archive onto the
filesystem. Referenced media, chart workbooks, and the package thumbnail are
materialized only through an explicitly supplied extraction root.
"""

from __future__ import annotations

import hashlib
import html
import mimetypes
import os
import posixpath
import re
import stat
import tempfile
import unicodedata
import zipfile
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from .caption_patterns import is_figure_caption
from .models import ContentBlock, SourceFile, TableCell, TableItem, TablePart
from .paths import ensure_within, is_reparse_point, reject_reparse_chain


MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
MAX_ZIP_ENTRIES = 20_000
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200.0

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
RELATIONSHIP_ATTRIBUTE = f"{{{NS['r']}}}id"
EMBED_ATTRIBUTE = f"{{{NS['r']}}}embed"
LINK_ATTRIBUTE = f"{{{NS['r']}}}link"
XML_SPACE_ATTRIBUTE = "{http://www.w3.org/XML/1998/namespace}space"

_MEDIA_TYPE_BY_SUFFIX = {
    ".avi": "video/x-msvideo",
    ".bmp": "image/bmp",
    ".emf": "image/emf",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".m4a": "audio/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".wav": "audio/wav",
    ".wdp": "image/vnd.ms-photo",
    ".wmf": "image/wmf",
    ".wmv": "video/x-ms-wmv",
}
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_EXCLUDED_NOTES_PLACEHOLDERS = frozenset({"dt", "ftr", "hdr", "sldImg", "sldNum"})
_AUTHORING_PATH = re.compile(
    r"(?ix)(?:"
    r"^[a-z]:[\\/]"
    r"|^file:/+"
    r"|^\\\\"
    r"|^/(?:home|users|volumes)/"
    r"|[\\/](?:users|documents|desktop|downloads)[\\/]"
    r")"
)

# Legacy Office files can store Greek glyphs as Latin character codes plus the
# classic Symbol typeface. These are the Symbol-encoded characters observed in
# the publisher supplements; decoding the font semantics restores what is
# visibly printed without OCR or scientific inference.
_LEGACY_SYMBOL_TEXT = str.maketrans({"b": "β", "g": "γ", "m": "μ"})


def _tag(namespace: str, local: str) -> str:
    return f"{{{NS[namespace]}}}{local}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _truthy(value: str | None) -> bool:
    return str(value or "").casefold() in {"1", "true", "on", "yes"}


def _normalize_space(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _safe_id(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"[^A-Za-z0-9]+", "_", ascii_value).strip("_").casefold() or "item"


def _natural_part_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """Sort numbered OOXML parts in publisher/human order."""

    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
        if part
    )


def _warning(
    code: str,
    message: str,
    source: SourceFile,
    supplement_id: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "code": code,
        "severity": "review",
        "message": message,
        "source_path": source.relative_path,
        "supplement_id": supplement_id,
        **details,
    }


@dataclass(frozen=True)
class _Relationship:
    relationship_id: str
    relationship_type: str
    target: str
    target_mode: str
    resolved_target: str | None


@dataclass(frozen=True)
class _EmbeddedWorkbook:
    chart_part: str
    relationship_id: str
    workbook_part: str
    table_id: str


@dataclass(frozen=True)
class _Transform:
    scale_x: float = 1.0
    scale_y: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0

    def point(self, x: float, y: float) -> tuple[float, float]:
        return (
            self.offset_x + self.scale_x * x,
            self.offset_y + self.scale_y * y,
        )


@dataclass(frozen=True)
class _Shape:
    element: ET.Element
    x: float
    y: float
    width: float
    height: float
    xml_order: int

    @property
    def c_nv_pr(self) -> ET.Element | None:
        return self.element.find(".//p:cNvPr", NS)

    @property
    def shape_id(self) -> str:
        node = self.c_nv_pr
        return node.get("id", "") if node is not None else ""

    @property
    def name(self) -> str:
        node = self.c_nv_pr
        return node.get("name", "") if node is not None else ""

    @property
    def locator(self) -> str:
        return f"shape-id={self.shape_id or 'unknown'};shape-name={self.name or 'unnamed'}"


def _validate_member_name(name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise ValueError(f"unsafe OOXML ZIP member name: {name!r}")
    stripped = name[:-1] if name.endswith("/") else name
    pure = PurePosixPath(stripped)
    if (
        not stripped
        or stripped.startswith("/")
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or (pure.parts and ":" in pure.parts[0])
        or posixpath.normpath(stripped) != stripped
    ):
        raise ValueError(f"unsafe OOXML ZIP member name: {name!r}")
    return stripped


def _validate_package(path: Path, archive: zipfile.ZipFile) -> set[str]:
    if path.stat().st_size > MAX_PACKAGE_BYTES:
        raise ValueError("PowerPoint package exceeds the configured byte limit")
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ValueError("PowerPoint package contains too many ZIP entries")
    names: set[str] = set()
    folded_names: set[str] = set()
    total = 0
    for info in infos:
        name = _validate_member_name(info.filename)
        folded = name.casefold()
        if folded in folded_names:
            raise ValueError(f"PowerPoint package contains a duplicate ZIP member: {name}")
        folded_names.add(folded)
        names.add(name)
        if info.flag_bits & 0x1:
            raise ValueError(f"encrypted OOXML ZIP member is not supported: {name}")
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError(f"OOXML ZIP member exceeds the configured byte limit: {name}")
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ValueError("PowerPoint package exceeds the uncompressed byte limit")
        if info.file_size and not info.compress_size:
            raise ValueError(f"OOXML ZIP member has an invalid compressed size: {name}")
        if (
            info.file_size >= 1024 * 1024
            and info.file_size / max(1, info.compress_size) > MAX_COMPRESSION_RATIO
        ):
            raise ValueError(f"OOXML ZIP member has a suspicious compression ratio: {name}")
    return names


def _read_member(
    archive: zipfile.ZipFile,
    names: set[str],
    part: str,
    *,
    xml: bool = False,
) -> bytes:
    if part not in names:
        raise KeyError(part)
    info = archive.getinfo(part)
    limit = MAX_XML_BYTES if xml else MAX_MEMBER_BYTES
    if info.file_size > limit:
        kind = "XML part" if xml else "ZIP member"
        raise ValueError(f"{kind} exceeds the configured byte limit: {part}")
    data = archive.read(info)
    if len(data) != info.file_size:
        raise ValueError(f"OOXML ZIP member size changed while reading: {part}")
    return data


def _xml_root(archive: zipfile.ZipFile, names: set[str], part: str) -> ET.Element:
    data = _read_member(archive, names, part, xml=True)
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.IGNORECASE):
        raise ValueError(f"DTD/entity declarations are forbidden in OOXML XML: {part}")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"malformed OOXML XML part: {part}") from exc


def _relationship_part(source_part: str) -> str:
    if not source_part:
        return "_rels/.rels"
    pure = PurePosixPath(source_part)
    return str(pure.parent / "_rels" / f"{pure.name}.rels")


def _resolve_internal_target(source_part: str, target: str) -> str:
    if not target or "\x00" in target or "\\" in target:
        raise ValueError(f"unsafe OOXML relationship target: {target!r}")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query:
        raise ValueError(f"internal OOXML relationship uses a URI target: {target!r}")
    decoded = unquote(parsed.path)
    if "\x00" in decoded or "\\" in decoded:
        raise ValueError(f"unsafe OOXML relationship target: {target!r}")
    if decoded.startswith("/"):
        joined = decoded.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(source_part), decoded)
    normalized = posixpath.normpath(joined)
    if (
        not normalized
        or normalized == "."
        or normalized.startswith("../")
        or normalized == ".."
        or normalized.startswith("/")
        or ":" in normalized.split("/", 1)[0]
    ):
        raise ValueError(f"OOXML relationship escapes the package: {target!r}")
    return normalized


def _relationships(
    archive: zipfile.ZipFile,
    names: set[str],
    source_part: str,
) -> list[_Relationship]:
    part = _relationship_part(source_part)
    if part not in names:
        return []
    root = _xml_root(archive, names, part)
    result: list[_Relationship] = []
    seen: set[str] = set()
    for node in root.findall("pr:Relationship", NS):
        relationship_id = node.get("Id", "")
        target = node.get("Target", "")
        target_mode = node.get("TargetMode", "Internal")
        relationship_type = node.get("Type", "").rsplit("/", 1)[-1]
        if not relationship_id or relationship_id in seen:
            raise ValueError(f"duplicate or empty relationship ID in {part}")
        seen.add(relationship_id)
        resolved = None
        if target_mode.casefold() != "external":
            resolved = _resolve_internal_target(source_part, target)
        result.append(
            _Relationship(
                relationship_id=relationship_id,
                relationship_type=relationship_type,
                target=target,
                target_mode=target_mode,
                resolved_target=resolved,
            )
        )
    return result


def _content_types(
    archive: zipfile.ZipFile, names: set[str]
) -> tuple[dict[str, str], dict[str, str]]:
    root = _xml_root(archive, names, "[Content_Types].xml")
    defaults = {
        str(node.get("Extension", "")).casefold(): str(node.get("ContentType", ""))
        for node in root.findall("ct:Default", NS)
    }
    overrides = {
        str(node.get("PartName", "")).lstrip("/"): str(node.get("ContentType", ""))
        for node in root.findall("ct:Override", NS)
    }
    return defaults, overrides


def _media_type(
    part: str,
    defaults: dict[str, str],
    overrides: dict[str, str],
) -> str:
    if part in overrides and overrides[part]:
        return overrides[part]
    suffix = PurePosixPath(part).suffix.casefold()
    extension = suffix.lstrip(".")
    return (
        defaults.get(extension)
        or _MEDIA_TYPE_BY_SUFFIX.get(suffix)
        or mimetypes.guess_type(part)[0]
        or "application/octet-stream"
    )


def _number(node: ET.Element | None, attribute: str, default: float = 0.0) -> float:
    if node is None:
        return default
    try:
        return float(node.get(attribute, str(default)))
    except (TypeError, ValueError):
        return default


def _shape_xfrm(element: ET.Element) -> ET.Element | None:
    local = _local_name(element.tag)
    if local == "graphicFrame":
        return element.find("p:xfrm", NS)
    if local == "grpSp":
        return element.find("p:grpSpPr/a:xfrm", NS)
    return element.find("p:spPr/a:xfrm", NS)


def _shape_geometry(
    element: ET.Element,
    parent: _Transform,
) -> tuple[float, float, float, float]:
    xfrm = _shape_xfrm(element)
    if xfrm is None:
        return parent.point(0.0, 0.0) + (0.0, 0.0)
    off = xfrm.find("a:off", NS)
    ext = xfrm.find("a:ext", NS)
    local_x = _number(off, "x")
    local_y = _number(off, "y")
    x, y = parent.point(local_x, local_y)
    return (
        x,
        y,
        abs(parent.scale_x * _number(ext, "cx")),
        abs(parent.scale_y * _number(ext, "cy")),
    )


def _group_transform(element: ET.Element, parent: _Transform) -> _Transform:
    xfrm = _shape_xfrm(element)
    if xfrm is None:
        return parent
    off = xfrm.find("a:off", NS)
    ext = xfrm.find("a:ext", NS)
    child_off = xfrm.find("a:chOff", NS)
    child_ext = xfrm.find("a:chExt", NS)
    off_x = _number(off, "x")
    off_y = _number(off, "y")
    ext_x = _number(ext, "cx", 1.0)
    ext_y = _number(ext, "cy", 1.0)
    child_x = _number(child_off, "x")
    child_y = _number(child_off, "y")
    child_width = _number(child_ext, "cx", ext_x or 1.0) or 1.0
    child_height = _number(child_ext, "cy", ext_y or 1.0) or 1.0
    ratio_x = ext_x / child_width
    ratio_y = ext_y / child_height
    return _Transform(
        scale_x=parent.scale_x * ratio_x,
        scale_y=parent.scale_y * ratio_y,
        offset_x=parent.offset_x + parent.scale_x * (off_x - child_x * ratio_x),
        offset_y=parent.offset_y + parent.scale_y * (off_y - child_y * ratio_y),
    )


def _iter_shapes(tree: ET.Element) -> list[_Shape]:
    shapes: list[_Shape] = []
    order = 0

    def visit(parent: ET.Element, transform: _Transform) -> None:
        nonlocal order
        for child in list(parent):
            local = _local_name(child.tag)
            if local == "grpSp":
                visit(child, _group_transform(child, transform))
                continue
            if local not in {"sp", "pic", "graphicFrame", "cxnSp", "contentPart"}:
                continue
            order += 1
            x, y, width, height = _shape_geometry(child, transform)
            shapes.append(_Shape(child, x, y, width, height, order))

    visit(tree, _Transform())
    return sorted(shapes, key=lambda shape: (shape.y, shape.x, shape.xml_order))


def _escape_run_text(value: str) -> str:
    escaped = html.escape(value, quote=False)
    return escaped.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")


def _run_properties(
    run: ET.Element, defaults: ET.Element | None
) -> tuple[bool, bool, int, str]:
    props = run.find("a:rPr", NS)

    def attr(name: str) -> str | None:
        if props is not None and props.get(name) is not None:
            return props.get(name)
        return defaults.get(name) if defaults is not None else None

    try:
        baseline = int(attr("baseline") or "0")
    except ValueError:
        baseline = 0

    typeface = ""
    for value in (props, defaults):
        if value is None:
            continue
        latin = value.find("a:latin", NS)
        if latin is not None and latin.get("typeface"):
            typeface = str(latin.get("typeface"))
            break
    return _truthy(attr("b")), _truthy(attr("i")), baseline, typeface


def _decode_typeface_text(value: str, typeface: str) -> str:
    if typeface.strip().casefold() == "symbol":
        return value.translate(_LEGACY_SYMBOL_TEXT)
    return value


def _styled_run(value: str, *, bold: bool, italic: bool, baseline: int) -> tuple[str, str]:
    markdown = _escape_run_text(value)
    plain = value
    if baseline > 0:
        markdown = f"<sup>{markdown}</sup>"
        plain = f"^{{{plain}}}"
    elif baseline < 0:
        markdown = f"<sub>{markdown}</sub>"
        plain = f"_{{{plain}}}"
    if italic:
        markdown = f"<em>{markdown}</em>"
    if bold:
        markdown = f"<strong>{markdown}</strong>"
    return markdown, plain


def _paragraph_text(paragraph: ET.Element) -> tuple[str, str]:
    defaults = paragraph.find("a:pPr/a:defRPr", NS)
    markdown_parts: list[str] = []
    plain_parts: list[str] = []
    for child in list(paragraph):
        local = _local_name(child.tag)
        if local in {"r", "fld"}:
            text_node = child.find("a:t", NS)
            if text_node is None:
                continue
            bold, italic, baseline, typeface = _run_properties(child, defaults)
            markdown, plain = _styled_run(
                _decode_typeface_text(text_node.text or "", typeface),
                bold=bold,
                italic=italic,
                baseline=baseline,
            )
            markdown_parts.append(markdown)
            plain_parts.append(plain)
        elif local == "br":
            markdown_parts.append("<br>")
            plain_parts.append("\n")
    return "".join(markdown_parts).strip(), "".join(plain_parts).strip()


def _shape_paragraphs(shape: _Shape) -> list[tuple[str, str]]:
    body = shape.element.find("p:txBody", NS)
    if body is None:
        return []
    return [
        pair
        for paragraph in body.findall("a:p", NS)
        if any(pair := _paragraph_text(paragraph))
    ]


def _geometry(slide_number: int, shape: _Shape) -> list[dict[str, Any]]:
    return [
        {
            "slide": slide_number,
            "x": round(shape.x),
            "y": round(shape.y),
            "width": round(shape.width),
            "height": round(shape.height),
            "unit": "EMU",
        }
    ]


def _placeholder_type(shape: _Shape) -> str:
    node = shape.element.find(".//p:nvPr/p:ph", NS)
    return node.get("type", "body") if node is not None else ""


def _looks_like_authoring_path(value: str) -> bool:
    candidate = value.strip()
    return bool(
        candidate
        and (
            _AUTHORING_PATH.search(candidate)
            or ("\\" in candidate and re.search(r"\.[A-Za-z0-9]{2,5}$", candidate))
        )
    )


def _accessibility_descriptions(shape: _Shape) -> list[str]:
    node = shape.c_nv_pr
    if node is None:
        return []
    result: list[str] = []
    for attribute in ("title", "descr"):
        value = _normalize_space(node.get(attribute, ""))
        if value and value not in result:
            result.append(value)
    return result


def _block_kind(plain: str) -> str:
    return "figure_caption" if is_figure_caption(plain) else "text"


def _cell_text(cell: ET.Element) -> tuple[str, str]:
    paragraphs = [
        _paragraph_text(paragraph)
        for paragraph in cell.findall("a:txBody/a:p", NS)
    ]
    paragraphs = [pair for pair in paragraphs if any(pair)]
    return (
        "<br>".join(pair[0] for pair in paragraphs),
        "\n".join(pair[1] for pair in paragraphs),
    )


def _integer_attribute(node: ET.Element, name: str, default: int = 1) -> int:
    try:
        return max(1, int(node.get(name, str(default))))
    except ValueError:
        return default


def _presentation_table_item(**kwargs: Any) -> TableItem:
    return TableItem(source_kind="presentation", **kwargs)


def _native_table(
    source: SourceFile,
    supplement_id: str,
    slide_number: int,
    table_number: int,
    shape: _Shape,
) -> TableItem:
    table = shape.element.find(".//a:tbl", NS)
    if table is None:  # pragma: no cover - guarded by caller
        raise ValueError("graphic frame does not contain a native table")
    rows: list[list[TableCell]] = []
    for row_number, row in enumerate(table.findall("a:tr", NS)):
        cells: list[TableCell] = []
        for cell in row.findall("a:tc", NS):
            if _truthy(cell.get("hMerge")) or _truthy(cell.get("vMerge")):
                continue
            markdown, plain = _cell_text(cell)
            cells.append(
                TableCell(
                    text=plain,
                    markdown=markdown,
                    header=row_number == 0,
                    rowspan=_integer_attribute(cell, "rowSpan"),
                    colspan=_integer_attribute(cell, "gridSpan"),
                )
            )
        if cells:
            rows.append(cells)
    table_id = f"{supplement_id}_slide_{slide_number:03d}_table_{table_number:03d}"
    return _presentation_table_item(
        table_id=table_id,
        source_id=shape.name or f"slide-{slide_number}-table-{table_number}",
        label=f"Slide {slide_number} table {table_number}",
        title_markdown="",
        title_plain="",
        parts=[TablePart(part_id=f"{table_id}_part_001", rows=rows)],
        footnotes_markdown=[],
        footnotes_plain=[],
        source_path=source.relative_path,
        source_locator=f"slide={slide_number};{shape.locator};native-table={table_number}",
    )


def _point_map(container: ET.Element | None) -> tuple[dict[int, str], str]:
    if container is None:
        return {}, ""
    for cache_name in ("strCache", "numCache", "strLit", "numLit"):
        cache = container.find(f".//c:{cache_name}", NS)
        if cache is None:
            continue
        points: dict[int, str] = {}
        for point in cache.findall("c:pt", NS):
            try:
                index = int(point.get("idx", str(len(points))))
            except ValueError:
                index = len(points)
            value = point.find("c:v", NS)
            points[index] = value.text or "" if value is not None else ""
        format_node = cache.find("c:formatCode", NS)
        return points, (format_node.text or "" if format_node is not None else "")
    multi = container.find(".//c:multiLvlStrCache", NS)
    if multi is not None:
        levels: list[dict[int, str]] = []
        for level in multi.findall("c:lvl", NS):
            points: dict[int, str] = {}
            for point in level.findall("c:pt", NS):
                try:
                    index = int(point.get("idx", str(len(points))))
                except ValueError:
                    index = len(points)
                value = point.find("c:v", NS)
                points[index] = value.text or "" if value is not None else ""
            levels.append(points)
        indices = sorted({index for level in levels for index in level})
        return {
            index: " / ".join(level.get(index, "") for level in levels).strip(" / ")
            for index in indices
        }, ""
    return {}, ""


def _chart_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    rich = "".join(text.text or "" for text in node.findall(".//a:t", NS)).strip()
    if rich:
        return _normalize_space(rich)
    points, _ = _point_map(node)
    if points:
        return _normalize_space(points[min(points)])
    value = node.find(".//c:v", NS)
    return _normalize_space(value.text or "") if value is not None else ""


def _series_name(series: ET.Element, number: int) -> str:
    return _chart_text(series.find("c:tx", NS)) or f"Series {number}"


def _error_bars(
    series: ET.Element,
) -> tuple[list[dict[str, Any]], list[str]]:
    result: list[dict[str, Any]] = []
    notes: list[str] = []
    for number, bars in enumerate(series.findall("c:errBars", NS), 1):
        direction_node = bars.find("c:errDir", NS)
        type_node = bars.find("c:errBarType", NS)
        value_type_node = bars.find("c:errValType", NS)
        direction = direction_node.get("val", "y") if direction_node is not None else "y"
        bar_type = type_node.get("val", "both") if type_node is not None else "both"
        raw_value_type = value_type_node.get("val", "") if value_type_node is not None else ""
        value_type = {
            "cust": "custom",
            "fixedVal": "fixed value",
            "percentage": "percentage",
            "stdDev": "standard deviation",
            "stdErr": "standard error",
        }.get(raw_value_type, raw_value_type)
        plus, _ = _point_map(bars.find("c:plus", NS))
        minus, _ = _point_map(bars.find("c:minus", NS))
        scalar_node = bars.find("c:val", NS)
        scalar = scalar_node.get("val", "") if scalar_node is not None else ""
        result.append(
            {
                "number": number,
                "direction": direction,
                "bar_type": bar_type,
                "value_type": value_type,
                "plus": plus,
                "minus": minus,
                "scalar": scalar,
            }
        )
        details = [f"direction={direction}", f"type={bar_type}"]
        if value_type:
            details.append(f"value type={value_type}")
        if scalar:
            details.append(f"value={scalar}")
        notes.append("Error bars: " + ", ".join(details) + ".")
    return result, notes


def _chart_table(
    archive: zipfile.ZipFile,
    names: set[str],
    source: SourceFile,
    supplement_id: str,
    slide_number: int,
    chart_number: int,
    shape: _Shape,
    chart_part: str,
    warnings: list[dict[str, Any]],
) -> TableItem:
    table_id = f"{supplement_id}_slide_{slide_number:03d}_chart_{chart_number:03d}"
    root = _xml_root(archive, names, chart_part)
    chart_title = _chart_text(root.find("c:chart/c:title", NS))
    axis_notes: list[str] = []
    for axis in root.findall("c:chart/c:plotArea/*", NS):
        if _local_name(axis.tag) not in {"catAx", "dateAx", "serAx", "valAx"}:
            continue
        title = _chart_text(axis.find("c:title", NS))
        if title:
            axis_notes.append(f"{_local_name(axis.tag)} title: {title}")

    series_nodes: list[ET.Element] = []
    for plot in root.findall("c:chart/c:plotArea/*", NS):
        series_nodes.extend(plot.findall("c:ser", NS))
    parts: list[TablePart] = []
    footnotes = list(axis_notes)
    for series_number, series in enumerate(series_nodes, 1):
        name = _series_name(series, series_number)
        category_container = series.find("c:xVal", NS)
        if category_container is None:
            category_container = series.find("c:cat", NS)
        value_container = series.find("c:yVal", NS)
        if value_container is None:
            value_container = series.find("c:val", NS)
        categories, category_format = _point_map(category_container)
        values, value_format = _point_map(value_container)
        errors, error_notes = _error_bars(series)
        footnotes.extend(f"Series {name}: {note}" for note in error_notes)
        if category_format:
            footnotes.append(f"Series {name} category format: {category_format}")
        if value_format:
            footnotes.append(f"Series {name} value format: {value_format}")

        indices = set(categories) | set(values)
        for error in errors:
            indices.update(error["plus"])
            indices.update(error["minus"])
        if not indices:
            warnings.append(
                _warning(
                    "pptx_chart_series_cache_empty",
                    f"Slide {slide_number} chart {chart_number} series {series_number} has no cached data",
                    source,
                    supplement_id,
                    source_locator=f"slide={slide_number};chart-part={chart_part};series={series_number}",
                )
            )
            indices = {0}
        ordered_indices = sorted(indices)
        columns: list[tuple[str, Any]] = [("Category", categories), (name, values)]
        for error in errors:
            suffix = "" if len(errors) == 1 else f" {error['number']}"
            include_plus = error["bar_type"] in {"both", "plus"}
            include_minus = error["bar_type"] in {"both", "minus"}
            if include_plus:
                columns.append(
                    (
                        f"{name} error {error['direction']} +{suffix}",
                        error["plus"] or error["scalar"],
                    )
                )
            if include_minus:
                columns.append(
                    (
                        f"{name} error {error['direction']} −{suffix}",
                        error["minus"] or error["scalar"],
                    )
                )
        rows = [
            [TableCell(text=label, markdown=_escape_run_text(label), header=True) for label, _ in columns]
        ]
        for index in ordered_indices:
            row: list[TableCell] = []
            for _, source_values in columns:
                if isinstance(source_values, dict):
                    value = source_values.get(index, "")
                else:
                    value = source_values
                row.append(
                    TableCell(
                        text=str(value),
                        markdown=_escape_run_text(str(value)),
                        header=False,
                    )
                )
            rows.append(row)
        parts.append(
            TablePart(
                part_id=f"{table_id}_series_{series_number:03d}",
                rows=rows,
            )
        )

    if not series_nodes:
        warnings.append(
            _warning(
                "pptx_chart_cache_empty",
                f"Slide {slide_number} chart {chart_number} contains no cached series",
                source,
                supplement_id,
                source_locator=f"slide={slide_number};chart-part={chart_part}",
            )
        )
        parts = [TablePart(part_id=f"{table_id}_series_001", rows=[])]
    title = chart_title or f"Slide {slide_number} chart {chart_number}"
    return _presentation_table_item(
        table_id=table_id,
        source_id=chart_part,
        label=f"Slide {slide_number} chart {chart_number}",
        title_markdown=_escape_run_text(title),
        title_plain=title,
        parts=parts,
        footnotes_markdown=[_escape_run_text(note) for note in footnotes],
        footnotes_plain=footnotes,
        source_path=source.relative_path,
        source_locator=f"slide={slide_number};{shape.locator};chart-part={chart_part}",
    )


def _chart_relationship_id(shape: _Shape) -> str:
    chart = shape.element.find(".//c:chart", NS)
    return chart.get(RELATIONSHIP_ATTRIBUTE, "") if chart is not None else ""


def _slide_parts(
    archive: zipfile.ZipFile, names: set[str]
) -> list[str]:
    presentation = _xml_root(archive, names, "ppt/presentation.xml")
    rels = {
        relationship.relationship_id: relationship
        for relationship in _relationships(archive, names, "ppt/presentation.xml")
    }
    result: list[str] = []
    for slide_id in presentation.findall("p:sldIdLst/p:sldId", NS):
        relationship_id = slide_id.get(RELATIONSHIP_ATTRIBUTE, "")
        relationship = rels.get(relationship_id)
        if relationship is None or relationship.resolved_target is None:
            raise ValueError("presentation contains an unresolved slide relationship")
        if relationship.resolved_target not in names:
            raise ValueError("presentation references a missing slide XML part")
        result.append(relationship.resolved_target)
    return result


def _external_relationship_warnings(
    relationships: Iterable[_Relationship],
    source: SourceFile,
    supplement_id: str,
    source_part: str,
    seen: set[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for relationship in relationships:
        if relationship.target_mode.casefold() != "external":
            continue
        key = (source_part, relationship.relationship_id, relationship.target)
        if key in seen:
            continue
        seen.add(key)
        warnings.append(
            _warning(
                "pptx_external_relationship_not_followed",
                "An external PowerPoint relationship was retained only as diagnostic metadata",
                source,
                supplement_id,
                source_locator=f"part={source_part};relationship={relationship.relationship_id}",
                relationship_type=relationship.relationship_type,
                target=relationship.target,
            )
        )
    return warnings


def _reachable_media(
    archive: zipfile.ZipFile,
    names: set[str],
    slide_parts: list[str],
    source: SourceFile,
    supplement_id: str,
    warnings: list[dict[str, Any]],
) -> tuple[dict[str, set[int]], set[tuple[str, str, str]]]:
    media_slides: dict[str, set[int]] = defaultdict(set)
    external_seen: set[tuple[str, str, str]] = set()
    visited: set[tuple[int, str]] = set()
    queue: deque[tuple[int, str]] = deque(
        (number, part) for number, part in enumerate(slide_parts, 1)
    )
    while queue:
        slide_number, part = queue.popleft()
        key = (slide_number, part)
        if key in visited:
            continue
        visited.add(key)
        rels = _relationships(archive, names, part)
        warnings.extend(
            _external_relationship_warnings(
                rels, source, supplement_id, part, external_seen
            )
        )
        for relationship in rels:
            target = relationship.resolved_target
            if target is None:
                continue
            if target.startswith("ppt/media/"):
                if target not in names:
                    warnings.append(
                        _warning(
                            "pptx_referenced_media_missing",
                            f"Slide {slide_number} references a missing media part",
                            source,
                            supplement_id,
                            source_locator=f"part={part};relationship={relationship.relationship_id}",
                            media_part=target,
                        )
                    )
                else:
                    media_slides[target].add(slide_number)
                continue
            if target in names and _relationship_part(target) in names:
                queue.append((slide_number, target))
    return media_slides, external_seen


def _portable_embedded_filename(part: str) -> str:
    """Return an internal package basename only when it is safe to reproduce."""

    filename = PurePosixPath(part).name
    if (
        not filename
        or filename in {".", ".."}
        or unicodedata.normalize("NFC", filename) != filename
        or filename.endswith((" ", "."))
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        or any(character in '<>:"/\\|?*' for character in filename)
    ):
        raise ValueError(f"embedded workbook has a non-portable filename: {part!r}")
    stem = filename.split(".", 1)[0].casefold()
    if stem in {"con", "prn", "aux", "nul"} or re.fullmatch(
        r"(?:com|lpt)[1-9]", stem
    ):
        raise ValueError(f"embedded workbook uses a reserved filename: {part!r}")
    return filename


def _embedded_output_path(supplement_id: str, workbook_part: str) -> str:
    return (
        PurePosixPath("supplementary")
        / supplement_id
        / "embedded"
        / _portable_embedded_filename(workbook_part)
    ).as_posix()


def _reachable_embedded_workbooks(
    archive: zipfile.ZipFile,
    names: set[str],
    chart_table_ids: dict[str, str],
    defaults: dict[str, str],
    overrides: dict[str, str],
    source: SourceFile,
    supplement_id: str,
    warnings: list[dict[str, Any]],
) -> list[_EmbeddedWorkbook]:
    """Find XLSX package relationships owned by charts reachable from slides."""

    workbooks: list[_EmbeddedWorkbook] = []
    target_owners: dict[str, str] = {}
    for chart_part, table_id in sorted(
        chart_table_ids.items(), key=lambda item: (item[0].casefold(), item[0])
    ):
        relationships = sorted(
            _relationships(archive, names, chart_part),
            key=lambda relationship: (
                (relationship.resolved_target or relationship.target).casefold(),
                relationship.resolved_target or relationship.target,
                relationship.relationship_id,
            ),
        )
        for relationship in relationships:
            if (
                relationship.target_mode.casefold() == "external"
                or relationship.relationship_type != "package"
            ):
                continue
            target = relationship.resolved_target
            if target is None:
                raise ValueError(
                    f"chart package relationship could not be resolved: {chart_part} "
                    f"{relationship.relationship_id}"
                )
            if target not in names:
                raise ValueError(
                    f"chart package relationship references a missing part: {target}"
                )
            if PurePosixPath(target).suffix.casefold() != ".xlsx":
                warnings.append(
                    _warning(
                        "pptx_reachable_package_unsupported",
                        "A reachable chart package is not an XLSX workbook and was not extracted",
                        source,
                        supplement_id,
                        source_locator=(
                            f"chart-part={chart_part};relationship="
                            f"{relationship.relationship_id}"
                        ),
                        package_part=target,
                        media_type=_media_type(target, defaults, overrides),
                    )
                )
                continue
            media_type = _media_type(target, defaults, overrides)
            if media_type != XLSX_MEDIA_TYPE:
                raise ValueError(
                    f"embedded XLSX workbook has an unexpected content type: "
                    f"{target} ({media_type})"
                )
            _portable_embedded_filename(target)
            prior_owner = target_owners.get(target)
            if prior_owner is not None:
                raise ValueError(
                    f"embedded workbook is related to multiple chart tables: "
                    f"{target} ({prior_owner}, {table_id})"
                )
            target_owners[target] = table_id
            workbooks.append(
                _EmbeddedWorkbook(
                    chart_part=chart_part,
                    relationship_id=relationship.relationship_id,
                    workbook_part=target,
                    table_id=table_id,
                )
            )

    output_owners: dict[str, str] = {}
    for workbook in workbooks:
        output = _embedded_output_path(supplement_id, workbook.workbook_part)
        folded = output.casefold()
        prior = output_owners.get(folded)
        if prior is not None:
            raise ValueError(
                "embedded workbook filenames collide at the extraction destination: "
                f"{prior} and {workbook.workbook_part}"
            )
        output_owners[folded] = workbook.workbook_part

    reachable_parts = {workbook.workbook_part for workbook in workbooks}
    packaged_workbooks = {
        part
        for part in names
        if part.startswith("ppt/embeddings/")
        and PurePosixPath(part).suffix.casefold() == ".xlsx"
    }
    unreachable = sorted(packaged_workbooks - reachable_parts, key=str.casefold)
    if unreachable:
        warnings.append(
            _warning(
                "pptx_unreachable_embedded_workbooks",
                (
                    f"PowerPoint package contains {len(unreachable)} XLSX embedding(s) "
                    "that are not reachable from an extracted chart; they were not copied"
                ),
                source,
                supplement_id,
                source_locator="pptx-package=ppt/embeddings",
                embedding_parts=unreachable,
            )
        )
    return sorted(
        workbooks,
        key=lambda workbook: (
            workbook.table_id.casefold(),
            workbook.table_id,
            workbook.workbook_part.casefold(),
            workbook.workbook_part,
            workbook.relationship_id,
        ),
    )


def _prepare_extraction_root(extraction_root: Path) -> Path:
    if extraction_root.exists() and is_reparse_point(extraction_root):
        raise ValueError("extraction root cannot be a reparse point")
    extraction_root.mkdir(parents=True, exist_ok=True)
    if is_reparse_point(extraction_root):
        raise ValueError("extraction root cannot be a reparse point")
    return extraction_root.resolve(strict=True)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_asset(extraction_root: Path, relative_path: str, data: bytes) -> None:
    destination = ensure_within(
        extraction_root / Path(PurePosixPath(relative_path)),
        extraction_root,
        require_exists=False,
    )
    reject_reparse_chain(destination.parent, extraction_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    reject_reparse_chain(destination.parent, extraction_root)
    if destination.exists():
        if is_reparse_point(destination) or not stat.S_ISREG(destination.lstat().st_mode):
            raise ValueError(f"unsafe existing PPTX asset destination: {destination}")
        if destination.read_bytes() != data:
            raise FileExistsError(f"refusing to replace a different PPTX asset: {destination}")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A same-directory hard link publishes the fully flushed temporary
            # file without the replacement semantics of ``os.replace``.  The
            # operation therefore fails atomically if another writer creates
            # the destination after our preflight check.
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise FileExistsError(
                f"PPTX asset destination appeared during copy: {destination}"
            ) from exc
        temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if destination.read_bytes() != data:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"PPTX asset copy failed verification: {destination}")


def _asset_kind(media_type: str) -> str:
    if media_type.startswith("image/"):
        return "supplement_image"
    if media_type.startswith("video/"):
        return "supplement_video"
    if media_type.startswith("audio/"):
        return "supplement_audio"
    return "supplement_media"


def _materialize_media(
    archive: zipfile.ZipFile,
    names: set[str],
    media_slides: dict[str, set[int]],
    semantic_labels: dict[str, list[str]],
    defaults: dict[str, str],
    overrides: dict[str, str],
    source: SourceFile,
    supplement_id: str,
    extraction_root: Path,
) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    for number, part in enumerate(sorted(media_slides, key=_natural_part_key), 1):
        relative_media = PurePosixPath(part).relative_to("ppt/media")
        output_path = (
            PurePosixPath("supplementary")
            / supplement_id
            / "media"
            / relative_media
        ).as_posix()
        data = _read_member(archive, names, part)
        _write_asset(extraction_root, output_path, data)
        media_type = _media_type(part, defaults, overrides)
        slides = sorted(media_slides[part])
        slide_label = ", ".join(str(value) for value in slides)
        labels = semantic_labels.get(part, [])
        label = labels[0] if labels else f"Slide {slide_label} embedded {PurePosixPath(part).name}"
        sha256 = hashlib.sha256(data).hexdigest()
        assets.append(
            {
                "schema_version": "1.0",
                "asset_id": f"{supplement_id}_media_{number:03d}",
                "category": _asset_kind(media_type),
                "label": label,
                "source_path": source.relative_path,
                "source_locator": f"pptx-part={part};slides={slide_label}",
                "source_sha256": source.sha256,
                "output_path": output_path,
                "media_type": media_type,
                "sha256": sha256,
                "bytes": len(data),
                "ocr_performed": False,
                "parent_id": supplement_id,
                "content_id": sha256,
            }
        )
    return assets


def _expected_embedded_assets(
    workbooks: Iterable[_EmbeddedWorkbook], supplement_id: str
) -> dict[str, tuple[_EmbeddedWorkbook, str]]:
    expected: dict[str, tuple[_EmbeddedWorkbook, str]] = {}
    table_counts: dict[str, int] = defaultdict(int)
    for workbook in workbooks:
        table_counts[workbook.table_id] += 1
        asset_id = f"{workbook.table_id}_data_{table_counts[workbook.table_id]:03d}"
        if asset_id in expected:
            raise ValueError(f"duplicate embedded workbook asset ID: {asset_id}")
        expected[asset_id] = (
            workbook,
            _embedded_output_path(supplement_id, workbook.workbook_part),
        )
    return expected


def _materialize_embedded_workbooks(
    archive: zipfile.ZipFile,
    names: set[str],
    workbooks: list[_EmbeddedWorkbook],
    source: SourceFile,
    supplement_id: str,
    extraction_root: Path,
) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    for asset_id, (workbook, output_path) in _expected_embedded_assets(
        workbooks, supplement_id
    ).items():
        data = _read_member(archive, names, workbook.workbook_part)
        _write_asset(extraction_root, output_path, data)
        sha256 = hashlib.sha256(data).hexdigest()
        filename = _portable_embedded_filename(workbook.workbook_part)
        assets.append(
            {
                "schema_version": "1.0",
                "asset_id": asset_id,
                "category": "supplement_data",
                "label": f"Embedded source workbook for {workbook.table_id}: {filename}",
                "source_path": source.relative_path,
                "source_locator": (
                    f"chart-part={workbook.chart_part};relationship="
                    f"{workbook.relationship_id};embedded-part={workbook.workbook_part}"
                ),
                "source_sha256": source.sha256,
                "output_path": output_path,
                "media_type": XLSX_MEDIA_TYPE,
                "sha256": sha256,
                "bytes": len(data),
                "ocr_performed": False,
                "parent_id": workbook.table_id,
                "content_id": sha256,
            }
        )
    return assets


def _validate_embedded_workbook_assets(
    archive: zipfile.ZipFile,
    names: set[str],
    workbooks: list[_EmbeddedWorkbook],
    assets: list[dict[str, Any]],
    supplement_id: str,
    extraction_root: Path,
) -> None:
    """Enforce a one-to-one, byte-identical result for every reachable workbook."""

    expected = _expected_embedded_assets(workbooks, supplement_id)
    actual: dict[str, dict[str, Any]] = {}
    for asset in assets:
        asset_id = str(asset.get("asset_id", ""))
        if not asset_id or asset_id in actual:
            raise RuntimeError("embedded workbook materialization returned duplicate asset IDs")
        actual[asset_id] = asset
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        raise RuntimeError(
            "reachable embedded workbooks did not materialize one-to-one "
            f"(missing={missing}, unexpected={unexpected})"
        )

    for asset_id, (workbook, output_path) in expected.items():
        asset = actual[asset_id]
        required = {
            "category": "supplement_data",
            "media_type": XLSX_MEDIA_TYPE,
            "parent_id": workbook.table_id,
            "output_path": output_path,
        }
        for key, value in required.items():
            if asset.get(key) != value:
                raise RuntimeError(
                    f"embedded workbook asset {asset_id} has inconsistent {key}"
                )
        destination = ensure_within(
            extraction_root / Path(PurePosixPath(output_path)),
            extraction_root,
            require_exists=True,
        )
        reject_reparse_chain(destination.parent, extraction_root)
        if is_reparse_point(destination) or not stat.S_ISREG(destination.lstat().st_mode):
            raise RuntimeError(
                f"embedded workbook asset is not a regular file: {destination}"
            )
        source_bytes = _read_member(archive, names, workbook.workbook_part)
        output_bytes = destination.read_bytes()
        digest = hashlib.sha256(source_bytes).hexdigest()
        if output_bytes != source_bytes:
            raise RuntimeError(
                f"embedded workbook asset is not byte-identical: {destination}"
            )
        if (
            asset.get("sha256") != digest
            or asset.get("content_id") != digest
            or asset.get("bytes") != len(source_bytes)
        ):
            raise RuntimeError(
                f"embedded workbook asset metadata is inconsistent: {asset_id}"
            )


def _thumbnail_part(
    archive: zipfile.ZipFile, names: set[str]
) -> str | None:
    for relationship in _relationships(archive, names, ""):
        if relationship.relationship_type == "thumbnail":
            return relationship.resolved_target
    for candidate in ("docProps/thumbnail.jpeg", "docProps/thumbnail.jpg", "docProps/thumbnail.png"):
        if candidate in names:
            return candidate
    return None


def _materialize_thumbnail(
    archive: zipfile.ZipFile,
    names: set[str],
    part: str,
    defaults: dict[str, str],
    overrides: dict[str, str],
    source: SourceFile,
    supplement_id: str,
    extraction_root: Path,
) -> dict[str, Any]:
    suffix = PurePosixPath(part).suffix.casefold() or ".bin"
    output_path = (
        PurePosixPath("supplementary")
        / supplement_id
        / "previews"
        / f"package-thumbnail{suffix}"
    ).as_posix()
    data = _read_member(archive, names, part)
    _write_asset(extraction_root, output_path, data)
    sha256 = hashlib.sha256(data).hexdigest()
    return {
        "schema_version": "1.0",
        "asset_id": f"{supplement_id}_package_thumbnail",
        "category": "supplement_slide_preview",
        "label": "Low-resolution PowerPoint package thumbnail (not a rendered slide)",
        "source_path": source.relative_path,
        "source_locator": f"pptx-part={part};package-thumbnail",
        "source_sha256": source.sha256,
        "output_path": output_path,
        "media_type": _media_type(part, defaults, overrides),
        "sha256": sha256,
        "bytes": len(data),
        "ocr_performed": False,
        "parent_id": supplement_id,
        "content_id": sha256,
    }


def _semantic_media_labels(
    shapes: list[_Shape],
    relationships: dict[str, _Relationship],
) -> dict[str, list[str]]:
    labels: dict[str, list[str]] = defaultdict(list)
    for shape in shapes:
        descriptions = [
            value
            for value in _accessibility_descriptions(shape)
            if not _looks_like_authoring_path(value)
        ]
        if not descriptions:
            continue
        relationship_ids: set[str] = set()
        for node in shape.element.iter():
            for attribute in (EMBED_ATTRIBUTE, LINK_ATTRIBUTE, RELATIONSHIP_ATTRIBUTE):
                value = node.get(attribute)
                if value:
                    relationship_ids.add(value)
        for relationship_id in relationship_ids:
            relationship = relationships.get(relationship_id)
            if (
                relationship is not None
                and relationship.resolved_target is not None
                and relationship.resolved_target.startswith("ppt/media/")
            ):
                for description in descriptions:
                    if description not in labels[relationship.resolved_target]:
                        labels[relationship.resolved_target].append(description)
    return labels


def extract_pptx_supplement(
    source: SourceFile,
    supplement_id: str,
    *,
    pptx_path: Path,
    extraction_root: Path,
) -> tuple[
    list[ContentBlock],
    list[TableItem],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Extract one PPTX supplement without executing or rewriting the package.

    Returns ``(blocks, tables, assets, warnings)``.  Assets follow the
    ``SupplementExtraction.assets`` dictionary contract and are written only
    beneath ``extraction_root``.
    """

    if not supplement_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", supplement_id):
        raise ValueError("supplement_id must be a portable non-empty identifier")
    path = Path(pptx_path)
    if not path.exists() or is_reparse_point(path) or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("pptx_path must be a regular, non-reparse-point file")
    actual_size = path.stat().st_size
    if actual_size > MAX_PACKAGE_BYTES:
        raise ValueError("PowerPoint package exceeds the configured byte limit")
    if actual_size != source.size:
        raise RuntimeError(
            f"PowerPoint supplement size differs from discovery metadata: {source.relative_path}"
        )
    if _sha256_path(path).casefold() != source.sha256.casefold():
        raise RuntimeError(
            f"PowerPoint supplement hash differs from discovery metadata: {source.relative_path}"
        )
    requested_root = Path(extraction_root)
    if requested_root.exists() and is_reparse_point(requested_root):
        raise ValueError("extraction root cannot be a reparse point")

    blocks: list[ContentBlock] = []
    tables: list[TableItem] = []
    assets: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    semantic_media_labels: dict[str, list[str]] = defaultdict(list)
    chart_table_ids: dict[str, str] = {}

    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ValueError("supplement is not a valid PPTX ZIP package") from exc
    with archive:
        names = _validate_package(path, archive)
        required = {"[Content_Types].xml", "ppt/presentation.xml"}
        missing = sorted(required - names)
        if missing:
            raise ValueError(f"PowerPoint package is missing required parts: {', '.join(missing)}")
        defaults, overrides = _content_types(archive, names)
        slide_parts = _slide_parts(archive, names)
        media_slides, external_seen = _reachable_media(
            archive, names, slide_parts, source, supplement_id, warnings
        )

        block_number = 0
        table_number = 0
        chart_number = 0
        for slide_number, slide_part in enumerate(slide_parts, 1):
            slide = _xml_root(archive, names, slide_part)
            tree = slide.find("p:cSld/p:spTree", NS)
            if tree is None:
                warnings.append(
                    _warning(
                        "pptx_slide_shape_tree_missing",
                        f"Slide {slide_number} has no shape tree",
                        source,
                        supplement_id,
                        source_locator=f"slide={slide_number};part={slide_part}",
                    )
                )
                continue
            shapes = _iter_shapes(tree)
            slide_relationships = _relationships(archive, names, slide_part)
            relationship_by_id = {
                relationship.relationship_id: relationship
                for relationship in slide_relationships
            }
            warnings.extend(
                _external_relationship_warnings(
                    slide_relationships,
                    source,
                    supplement_id,
                    slide_part,
                    external_seen,
                )
            )
            for part, labels in _semantic_media_labels(shapes, relationship_by_id).items():
                for label in labels:
                    if label not in semantic_media_labels[part]:
                        semantic_media_labels[part].append(label)

            authoring_paths: list[dict[str, str]] = []
            for shape in shapes:
                descriptions = _accessibility_descriptions(shape)
                for description in descriptions:
                    if _looks_like_authoring_path(description):
                        authoring_paths.append(
                            {"source_locator": f"slide={slide_number};{shape.locator}", "description": description}
                        )

                native_table = shape.element.find(".//a:tbl", NS)
                if native_table is not None:
                    table_number += 1
                    tables.append(
                        _native_table(
                            source,
                            supplement_id,
                            slide_number,
                            table_number,
                            shape,
                        )
                    )
                    continue

                chart_relationship_id = _chart_relationship_id(shape)
                if chart_relationship_id:
                    relationship = relationship_by_id.get(chart_relationship_id)
                    if (
                        relationship is None
                        or relationship.resolved_target is None
                        or relationship.resolved_target not in names
                    ):
                        warnings.append(
                            _warning(
                                "pptx_chart_part_missing",
                                f"Slide {slide_number} contains an unresolved chart",
                                source,
                                supplement_id,
                                source_locator=f"slide={slide_number};{shape.locator}",
                            )
                        )
                    else:
                        chart_number += 1
                        chart_table = _chart_table(
                            archive,
                            names,
                            source,
                            supplement_id,
                            slide_number,
                            chart_number,
                            shape,
                            relationship.resolved_target,
                            warnings,
                        )
                        prior_table_id = chart_table_ids.get(
                            relationship.resolved_target
                        )
                        if prior_table_id is not None:
                            raise ValueError(
                                "a chart part is referenced by multiple extracted chart "
                                f"tables: {relationship.resolved_target} "
                                f"({prior_table_id}, {chart_table.table_id})"
                            )
                        chart_table_ids[relationship.resolved_target] = (
                            chart_table.table_id
                        )
                        tables.append(chart_table)
                    continue

                paragraphs = _shape_paragraphs(shape)
                for paragraph_number, (markdown, plain) in enumerate(paragraphs, 1):
                    if not plain:
                        continue
                    block_number += 1
                    blocks.append(
                        ContentBlock(
                            block_id=f"{supplement_id}-slide-{slide_number:03d}-block-{block_number:03d}",
                            kind=_block_kind(plain),
                            markdown=markdown,
                            plain_text=plain,
                            source_path=source.relative_path,
                            source_locator=(
                                f"slide={slide_number};{shape.locator};paragraph={paragraph_number}"
                            ),
                            source_geometry=_geometry(slide_number, shape),
                        )
                    )

                semantic_descriptions = [
                    value for value in descriptions if not _looks_like_authoring_path(value)
                ]
                if not paragraphs:
                    for description_number, description in enumerate(semantic_descriptions, 1):
                        block_number += 1
                        blocks.append(
                            ContentBlock(
                                block_id=f"{supplement_id}-slide-{slide_number:03d}-block-{block_number:03d}",
                                kind="image_description",
                                markdown=_escape_run_text(description),
                                plain_text=description,
                                source_path=source.relative_path,
                                source_locator=(
                                    f"slide={slide_number};{shape.locator};"
                                    f"accessibility-description={description_number}"
                                ),
                                source_geometry=_geometry(slide_number, shape),
                            )
                        )
            if authoring_paths:
                warnings.append(
                    _warning(
                        "pptx_authoring_path_accessibility_descriptions",
                        (
                            f"Slide {slide_number} contains {len(authoring_paths)} accessibility "
                            "description(s) that are authoring-system paths; they were omitted "
                            "from extracted content and retained only in diagnostics"
                        ),
                        source,
                        supplement_id,
                        source_locator=f"slide={slide_number};part={slide_part}",
                        descriptions=authoring_paths,
                    )
                )

            notes_relationship = next(
                (
                    relationship
                    for relationship in slide_relationships
                    if relationship.relationship_type == "notesSlide"
                    and relationship.resolved_target is not None
                ),
                None,
            )
            if notes_relationship and notes_relationship.resolved_target in names:
                notes_part = notes_relationship.resolved_target
                notes = _xml_root(archive, names, notes_part)
                notes_tree = notes.find("p:cSld/p:spTree", NS)
                if notes_tree is not None:
                    for shape in _iter_shapes(notes_tree):
                        placeholder = _placeholder_type(shape)
                        if placeholder in _EXCLUDED_NOTES_PLACEHOLDERS:
                            continue
                        for paragraph_number, (markdown, plain) in enumerate(
                            _shape_paragraphs(shape), 1
                        ):
                            if not plain:
                                continue
                            block_number += 1
                            blocks.append(
                                ContentBlock(
                                    block_id=(
                                        f"{supplement_id}-slide-{slide_number:03d}-"
                                        f"note-{block_number:03d}"
                                    ),
                                    kind="speaker_note",
                                    markdown=markdown,
                                    plain_text=plain,
                                    source_path=source.relative_path,
                                    source_locator=(
                                        f"slide={slide_number};notes-part={notes_part};"
                                        f"{shape.locator};paragraph={paragraph_number}"
                                    ),
                                    source_geometry=_geometry(slide_number, shape),
                                )
                            )

        embedded_workbooks = _reachable_embedded_workbooks(
            archive,
            names,
            chart_table_ids,
            defaults,
            overrides,
            source,
            supplement_id,
            warnings,
        )

        # The package has been fully validated and parsed before any extracted
        # asset is materialized.  Source files are never modified.
        root_path = _prepare_extraction_root(requested_root)
        assets.extend(
            _materialize_media(
                archive,
                names,
                media_slides,
                semantic_media_labels,
                defaults,
                overrides,
                source,
                supplement_id,
                root_path,
            )
        )
        embedded_assets = _materialize_embedded_workbooks(
            archive,
            names,
            embedded_workbooks,
            source,
            supplement_id,
            root_path,
        )
        _validate_embedded_workbook_assets(
            archive,
            names,
            embedded_workbooks,
            embedded_assets,
            supplement_id,
            root_path,
        )
        assets.extend(embedded_assets)
        thumbnail = _thumbnail_part(archive, names)
        if thumbnail and thumbnail in names:
            thumbnail_asset = _materialize_thumbnail(
                archive,
                names,
                thumbnail,
                defaults,
                overrides,
                source,
                supplement_id,
                root_path,
            )
            thumbnail_asset["presentation_slide_count"] = len(slide_parts)
            assets.append(thumbnail_asset)
        else:
            warnings.append(
                _warning(
                    "pptx_package_thumbnail_missing",
                    "PowerPoint package has no low-resolution package thumbnail",
                    source,
                    supplement_id,
                )
            )

    return blocks, tables, assets, warnings


__all__ = ["extract_pptx_supplement"]
