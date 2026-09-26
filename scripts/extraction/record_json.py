"""Serialize one extraction as canonical, content-only ``record.json``.

The JSON payload is the public extraction artifact. Source locations, geometry,
and confidence never enter it; they are returned separately as reverse coverage
for ``extraction_diagnostic``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, Mapping, Sequence

from .metadata import RecordMetadata
from .models import (
    ArticleExtraction,
    ContentBlock,
    FigureItem,
    Section,
    SupplementExtraction,
    TableItem,
)
from .paths import atomic_write_text
from .record_schema import RECORD_SCHEMA_VERSION, validate_record_schema
from .renderer import machine_table_records, typed_table_footnotes
from .rich_text import (
    UnsafeRichTextError,
    block_markup_to_safe_html,
    inline_markup_to_safe_html,
    normalize_visible_text,
    plain_text_from_safe_html,
    rich_text_matches_plain,
    validate_safe_html_fragment,
)


RECORD_ASSET_PATH_BASE = "extraction_root"
RECORD_CONTENT_FORMAT = "safe-html"
_TOP_LEVEL_KEY_ORDER = (
    "schema_version",
    "record",
    "asset_path_base",
    "content_format",
    "front_matter",
    "sections",
    "figures",
    "tables",
    "supporting_information",
    "supplements",
    "references",
    "assets",
)
_RECORD_KEY_ORDER = (
    "record_id",
    "title",
    "authors",
    "journal",
    "publication_year",
    "doi",
    "document_type",
    "bibliographic",
)
_FORBIDDEN_CONTENT_KEYS = frozenset(
    {
        "bytes",
        "confidence",
        "ocr_confidence",
        "repair_id",
        "sha256",
        "source_geometry",
        "source_locator",
        "source_path",
        "source_sha256",
    }
)
_MEDIA_TYPES = {
    ".avif": "image/avif",
    ".csv": "text/csv",
    ".gif": "image/gif",
    ".html": "text/html",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".m4a": "audio/mp4",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".tsv": "text/tab-separated-values",
    ".wav": "audio/wav",
    ".webm": "video/webm",
    ".webp": "image/webp",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
}


class RecordJsonError(ValueError):
    """Raised when content cannot be represented safely and losslessly."""


@dataclass(frozen=True)
class RecordJsonResult:
    """Canonical content and its private, deterministic reverse coverage."""

    payload: dict[str, Any]
    coverage: list[dict[str, Any]]


def _ordered_for_write(value: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        if not path:
            preferred = _TOP_LEVEL_KEY_ORDER
        elif path == ("record",):
            preferred = _RECORD_KEY_ORDER
        else:
            preferred = ()
        known = [key for key in preferred if key in value]
        remaining = sorted(key for key in value if key not in known)
        return {
            key: _ordered_for_write(value[key], path + (key,))
            for key in (*known, *remaining)
        }
    if isinstance(value, list):
        return [_ordered_for_write(child, path + ("[]",)) for child in value]
    return value


def canonical_record_json(payload: Mapping[str, Any]) -> str:
    """Return stable UTF-8 JSON with record identity near the file beginning."""

    ordered = _ordered_for_write(dict(payload))
    return json.dumps(
        ordered,
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
        separators=(",", ": "),
    ) + "\n"


def _pointer(*parts: str | int) -> str:
    return "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1")
        for part in parts
    )


def _asset_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RecordJsonError(f"{field} must be a nonempty relative path")
    if any(character in value for character in "\x00\r\n\\?:"):
        raise RecordJsonError(f"{field} is not a portable relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("/"):
        raise RecordJsonError(f"{field} must be relative to the extraction root")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise RecordJsonError(f"{field} contains traversal or empty components")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        raise RecordJsonError(f"{field} cannot be a URL or data URI")
    normalized = PurePosixPath(value).as_posix()
    if normalized != value:
        raise RecordJsonError(f"{field} is not in canonical POSIX form: {value!r}")
    return normalized


def _media_type(path: str, supplied: Any = None) -> str:
    if isinstance(supplied, str) and supplied.strip():
        return supplied.strip().casefold()
    return _MEDIA_TYPES.get(PurePosixPath(path).suffix.casefold(), "application/octet-stream")


def _rich(plain_text: str, markup: str, *, kind: str) -> dict[str, str]:
    try:
        rendered = block_markup_to_safe_html(markup, kind=kind)
    except UnsafeRichTextError as exc:
        raise RecordJsonError(f"unsafe rich text in {kind}: {exc}") from exc
    canonical_plain = _canonical_plain_for_rich(plain_text, rendered)
    if canonical_plain is None:
        raise RecordJsonError(
            f"plain and rich text disagree in {kind}: "
            f"{plain_text!r} != {rendered!r}"
        )
    return {"plain_text": canonical_plain, "html": rendered}


def _rich_from_markup(markup: str, *, kind: str) -> dict[str, str]:
    """Build both canonical representations when only styled text is available."""

    try:
        rendered = block_markup_to_safe_html(markup, kind=kind)
    except UnsafeRichTextError as exc:
        raise RecordJsonError(f"unsafe rich text in {kind}: {exc}") from exc
    return {
        "plain_text": plain_text_from_safe_html(rendered),
        "html": rendered,
    }


_EXPLICIT_PLAIN_SCRIPT = re.compile(
    r"[_^]\{[^{}]*\}|[₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₔ⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿ]"
)
_SCRIPT_NOTATION = re.compile(r"[_^]\{([^{}]*)\}")
_REFERENCE_BRACKETS = re.compile(
    r"\[(\d+(?:[a-z])?(?:\s*[-–—,]\s*\d+(?:[a-z])?)*)\]"
)


def _canonical_plain_for_rich(plain_text: str, rendered: str) -> str | None:
    """Return an AI-explicit plain form when rich text supplies script markup.

    Native PDF extraction can faithfully identify a subscript or superscript in
    the rich representation while its geometric plain-text layer necessarily
    flattens that presentation (``C6H`` beside ``C<sub>6</sub>H``).  In that
    narrow case, use the independently parsed rich fragment to write
    ``C_{6}H`` into the canonical plain field.  If the incoming plain text
    already declares script semantics, a sub/sup disagreement remains fatal.
    """

    semantic_plain = plain_text_from_safe_html(rendered)
    if rich_text_matches_plain(plain_text, rendered):
        return semantic_plain if re.search(r"<(?:sub|sup)>", rendered) else plain_text
    has_script_markup = bool(re.search(r"<(?:sub|sup)>", rendered))
    has_reference_markup = bool(_REFERENCE_BRACKETS.search(semantic_plain))
    if not (has_script_markup or has_reference_markup):
        return None
    flattened_plain = normalize_visible_text(plain_text)
    flattened_rich = normalize_visible_text(semantic_plain)
    if not _EXPLICIT_PLAIN_SCRIPT.search(plain_text):
        # A raised chemical group can itself contain a subscript, e.g.
        # <sup>H<sub>2</sub>N</sup>. Flatten innermost runs first, then
        # enclosing runs, only in this geometry-derived plain-text branch.
        while _SCRIPT_NOTATION.search(flattened_plain):
            flattened_plain = _SCRIPT_NOTATION.sub(r"\1", flattened_plain)
        while _SCRIPT_NOTATION.search(flattened_rich):
            flattened_rich = _SCRIPT_NOTATION.sub(r"\1", flattened_rich)
    flattened_plain = _REFERENCE_BRACKETS.sub(r"\1", flattened_plain)
    flattened_rich = _REFERENCE_BRACKETS.sub(r"\1", flattened_rich)
    # Geometry-derived PDF text can insert a space at a script boundary (for
    # example ``[M+ Na]+`` while the styled form is ``[M+Na]<sup>+</sup>``).
    # Compare the character stream without layout whitespace only inside this
    # script-recovery branch; the canonical result still comes from safe HTML.
    flattened_plain = re.sub(r"\s+", "", flattened_plain)
    flattened_rich = re.sub(r"\s+", "", flattened_rich)
    return semantic_plain if flattened_plain == flattened_rich else None


def _table_cell(cell: Any, *, table_id: str) -> dict[str, Any]:
    rendered = inline_markup_to_safe_html(cell.markdown)
    canonical_text = _canonical_plain_for_rich(cell.text, rendered)
    if canonical_text is None:
        raise RecordJsonError(
            f"plain and rich text disagree in table {table_id} cell: "
            f"{cell.text!r} != {rendered!r}"
        )
    return {
        "text": canonical_text,
        "html": rendered,
        "header": cell.header,
        "rowspan": cell.rowspan,
        "colspan": cell.colspan,
    }


def _block(block: ContentBlock) -> dict[str, Any]:
    try:
        content = _rich(block.plain_text, block.markdown, kind=block.kind)
    except RecordJsonError as exc:
        raise RecordJsonError(f"block {block.block_id!r}: {exc}") from exc
    return {"block_id": block.block_id, "kind": block.kind, "content": content}


def _figure(figure: FigureItem, asset_ids: set[str]) -> dict[str, Any]:
    try:
        caption = _rich(
            figure.caption_plain,
            figure.caption_markdown,
            kind=f"{figure.kind}_caption",
        )
    except RecordJsonError as exc:
        raise RecordJsonError(f"figure {figure.figure_id!r}: {exc}") from exc
    result: dict[str, Any] = {
        "figure_id": figure.figure_id,
        "label": figure.label,
        "kind": figure.kind,
        "caption": caption,
    }
    if figure.output_path:
        if figure.figure_id not in asset_ids:
            raise RecordJsonError(
                f"figure {figure.figure_id!r} has no matching asset registry entry"
            )
        result["asset_id"] = figure.figure_id
    return result


def _table(table: TableItem) -> dict[str, Any]:
    if len(table.footnotes_plain) != len(table.footnotes_markdown):
        raise RecordJsonError(
            f"table {table.table_id!r} has mismatched plain and rich footnotes"
        )
    structure_assets = {
        str(key): _asset_path(
            value,
            field=f"table {table.table_id} structure asset {key!r}",
        )
        for key, value in sorted(table.structure_assets.items())
    }
    records = deepcopy(machine_table_records(table))
    for index, record in enumerate(records):
        value = record.get("structure_asset")
        if value is not None:
            record["structure_asset"] = _asset_path(
                value,
                field=f"table {table.table_id} machine record {index} structure_asset",
            )
    result: dict[str, Any] = {
        "table_id": table.table_id,
        "label": table.label,
        "title": _rich(
            table.title_plain, table.title_markdown, kind="table_title"
        ),
        "parts": [
            {
                "part_id": part.part_id,
                "rows": [
                    [_table_cell(cell, table_id=table.table_id) for cell in row]
                    for row in part.rows
                ],
            }
            for part in table.parts
        ],
        "footnotes": [
            _rich(plain, markup, kind="table_footnote")
            for plain, markup in zip(
                table.footnotes_plain, table.footnotes_markdown, strict=True
            )
        ],
        "footnotes_typed": typed_table_footnotes(table),
        "structure_assets": structure_assets,
        "machine_records": records,
        "source_kind": table.source_kind,
    }
    if table.image_path:
        result["source_image"] = _asset_path(
            table.image_path, field=f"table {table.table_id} source image"
        )
    return result


def _content_asset(raw: Mapping[str, Any]) -> dict[str, str]:
    asset_id = str(raw.get("asset_id", "")).strip()
    if not asset_id:
        raise RecordJsonError("asset is missing asset_id")
    path = _asset_path(raw.get("output_path"), field=f"asset {asset_id} path")
    result = {
        "asset_id": asset_id,
        "kind": str(raw.get("category") or raw.get("kind") or "asset").strip(),
        "label": str(raw.get("label") or asset_id).strip(),
        "path": path,
        "media_type": _media_type(path, raw.get("media_type")),
    }
    parent = raw.get("parent_table_id") or raw.get("parent_id") or raw.get(
        "supplement_id"
    )
    if parent:
        result["parent_id"] = str(parent)
    content_id = raw.get("compound_id") or raw.get("content_id")
    if content_id:
        result["content_id"] = str(content_id)
    if not result["kind"]:
        raise RecordJsonError(f"asset {asset_id!r} has an empty kind")
    return result


def _asset_registry(
    article: ArticleExtraction,
    supplements: Sequence[SupplementExtraction],
    raw_assets: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    registry: dict[str, dict[str, str]] = {}

    def add(value: dict[str, str]) -> None:
        existing = registry.get(value["asset_id"])
        if existing is None:
            registry[value["asset_id"]] = value
            return
        for key in ("path", "media_type", "kind", "label"):
            if existing.get(key) != value.get(key):
                raise RecordJsonError(
                    f"asset ID {value['asset_id']!r} identifies conflicting {key} values"
                )
        for key in ("parent_id", "content_id"):
            if key in existing and key in value and existing[key] != value[key]:
                raise RecordJsonError(
                    f"asset ID {value['asset_id']!r} identifies conflicting {key} values"
                )
            if key in value:
                existing[key] = value[key]

    raw_asset_ids: set[str] = set()
    for raw in raw_assets:
        value = _content_asset(raw)
        if value["asset_id"] in raw_asset_ids:
            raise RecordJsonError(f"duplicate input asset_id: {value['asset_id']!r}")
        raw_asset_ids.add(value["asset_id"])
        add(value)

    def add_figure(figure: FigureItem) -> None:
        if not figure.output_path:
            return
        path = _asset_path(
            figure.output_path, field=f"figure {figure.figure_id} asset path"
        )
        existing = registry.get(figure.figure_id)
        if existing is not None:
            if existing["path"] != path:
                raise RecordJsonError(
                    f"figure {figure.figure_id!r} conflicts with its asset registry path"
                )
            return
        add(
            {
                "asset_id": figure.figure_id,
                "kind": figure.kind,
                "label": figure.label,
                "path": path,
                "media_type": _media_type(path),
            }
        )

    for figure in article.figures:
        add_figure(figure)
    for supplement in supplements:
        for figure in supplement.figures:
            add_figure(figure)

    def add_table_assets(table: TableItem) -> None:
        if table.image_path:
            path = _asset_path(
                table.image_path, field=f"table {table.table_id} source image"
            )
            existing = registry.get(table.table_id)
            if existing is not None:
                if existing["path"] != path:
                    raise RecordJsonError(
                        f"table {table.table_id!r} conflicts with its source-image asset"
                    )
            else:
                add(
                    {
                        "asset_id": table.table_id,
                        "kind": "table",
                        "label": table.label,
                        "path": path,
                        "media_type": _media_type(path),
                    }
                )
        registered_paths = {asset["path"] for asset in registry.values()}
        for index, (content_id, raw_path) in enumerate(
            sorted(table.structure_assets.items()), start=1
        ):
            path = _asset_path(
                raw_path,
                field=f"table {table.table_id} structure asset {content_id!r}",
            )
            if path in registered_paths:
                continue
            add(
                {
                    "asset_id": f"{table.table_id}_structure_{index:03d}",
                    "kind": "table_cell",
                    "label": f"{table.label} structure {content_id}",
                    "path": path,
                    "media_type": _media_type(path),
                    "parent_id": table.table_id,
                    "content_id": str(content_id),
                }
            )
            registered_paths.add(path)

    for table in article.tables:
        add_table_assets(table)
    for supplement in supplements:
        for table in getattr(supplement, "tables", ()):
            add_table_assets(table)
    return [registry[key] for key in sorted(registry)]


def _coverage(
    *,
    coverage_id: str,
    content_kind: str,
    source_path: str,
    source_locator: str,
    output_id: str,
    pointer: str,
    source_geometry: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "status": "included",
        "coverage_id": coverage_id,
        "content_kind": content_kind,
        "source_path": source_path,
        "source_locator": source_locator,
        "output_path": "record.json",
        "output_locator": {"json_pointer": pointer},
        "output_id": output_id,
    }
    if source_geometry:
        result["source_geometry"] = deepcopy(source_geometry)
    return result


def _block_coverage(block: ContentBlock, pointer: str) -> dict[str, Any]:
    return _coverage(
        coverage_id=f"content-{block.block_id}",
        content_kind=block.kind,
        source_path=block.source_path,
        source_locator=block.source_locator,
        output_id=block.block_id,
        pointer=pointer,
        source_geometry=block.source_geometry,
    )


def _section_coverage(section: Section, pointer: str) -> dict[str, Any]:
    return _coverage(
        coverage_id=f"heading-{section.section_id}",
        content_kind="section_heading",
        source_path=section.source_path,
        source_locator=section.source_locator,
        output_id=section.section_id,
        pointer=pointer,
        source_geometry=section.source_geometry,
    )


def _figure_coverage(figure: FigureItem, pointer: str) -> list[dict[str, Any]]:
    values = [
        _coverage(
            coverage_id=f"figure-{figure.figure_id}",
            content_kind=figure.kind,
            source_path=figure.source_path,
            source_locator=figure.source_locator,
            output_id=figure.figure_id,
            pointer=pointer,
        )
    ]
    if figure.caption_markdown or figure.caption_plain:
        values.append(
            _coverage(
                coverage_id=f"caption-{figure.figure_id}",
                content_kind=f"{figure.kind}_caption",
                source_path=figure.source_path,
                source_locator=figure.source_locator,
                output_id=f"caption-{figure.figure_id}",
                pointer=pointer + "/caption",
            )
        )
    return values


def _table_coverage(table: TableItem, pointer: str) -> list[dict[str, Any]]:
    values = [
        _coverage(
            coverage_id=f"table-{table.table_id}",
            content_kind="table",
            source_path=table.source_path,
            source_locator=table.source_locator,
            output_id=table.table_id,
            pointer=pointer,
        ),
        _coverage(
            coverage_id=f"table-title-{table.table_id}",
            content_kind="table_title",
            source_path=table.source_path,
            source_locator=table.source_locator,
            output_id=f"table-title-{table.table_id}",
            pointer=pointer + "/title",
        ),
        _coverage(
            coverage_id=f"table-body-{table.table_id}",
            content_kind="table_body",
            source_path=table.source_path,
            source_locator=table.source_locator,
            output_id=f"table-body-{table.table_id}",
            pointer=pointer + "/parts",
        ),
    ]
    for footnote_index, _ in enumerate(table.footnotes_plain):
        output_id = f"table-footnotes-{table.table_id}-{footnote_index + 1:02d}"
        values.append(
            _coverage(
                coverage_id=output_id,
                content_kind="table_footnotes",
                source_path=table.source_path,
                source_locator=table.source_locator,
                output_id=output_id,
                pointer=pointer + _pointer("footnotes", footnote_index),
            )
        )
    return values


def _asset_coverage(
    asset: Mapping[str, Any], raw: Mapping[str, Any], pointer: str
) -> dict[str, Any]:
    page = raw.get("page")
    box = raw.get("box")
    parts = raw.get("parts")
    if page:
        locator = f"page {page}, box {box}"
    elif (
        isinstance(parts, list)
        and len(parts) >= 2
        and all(
            isinstance(part, Mapping)
            and part.get("page")
            and isinstance(part.get("box"), list)
            for part in parts
        )
    ):
        locator = "parts: " + "; ".join(
            f"page {part['page']}, box {part['box']}" for part in parts
        )
    else:
        locator = "copied or generated asset"
    return _coverage(
        coverage_id=f"asset-{asset['asset_id']}",
        content_kind="asset_link",
        source_path=str(raw.get("source_path", "")),
        source_locator=locator,
        output_id=str(asset["asset_id"]),
        pointer=pointer,
    )


def _forbidden_content_key(value: Any, path: str = "$") -> tuple[str, str] | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _FORBIDDEN_CONTENT_KEYS:
                return path, key
            # Domain-specific machine records contain author data, where names
            # such as "confidence" or "bytes" can be scientifically meaningful.
            # The serializer supplies this subtree from table content only.
            if key == "machine_records":
                continue
            found = _forbidden_content_key(child, f"{path}/{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _forbidden_content_key(child, f"{path}/{index}")
            if found:
                return found
    return None


def _unique_ids(values: Iterable[Any], field: str) -> None:
    seen: set[str] = set()
    for value in values:
        identifier = str(value)
        if identifier in seen:
            raise RecordJsonError(f"duplicate {field}: {identifier!r}")
        seen.add(identifier)


def validate_record_payload(payload: Any, *, source_path: str | None = None) -> None:
    """Validate schema plus content/provenance separation and reference safety."""

    validate_record_schema(payload, source_path=source_path)
    assert isinstance(payload, dict)
    forbidden = _forbidden_content_key(payload)
    if forbidden:
        path, key = forbidden
        raise RecordJsonError(f"diagnostic field {key!r} is forbidden at {path}")

    assets = payload["assets"]
    _unique_ids((asset["asset_id"] for asset in assets), "asset_id")
    asset_ids = {asset["asset_id"] for asset in assets}
    asset_paths = {asset["path"] for asset in assets}
    for asset in assets:
        _asset_path(asset["path"], field=f"asset {asset['asset_id']} path")

    figures = list(payload["figures"])
    tables = list(payload["tables"])
    blocks = list(payload["front_matter"]) + list(payload["supporting_information"])
    blocks += list(payload["references"])
    section_ids: list[str] = []
    for section in payload["sections"]:
        section_ids.append(section["section_id"])
        blocks.extend(section["blocks"])
    supplement_ids: list[str] = []
    for supplement in payload["supplements"]:
        supplement_ids.append(supplement["supplement_id"])
        _asset_path(
            supplement["file"]["path"],
            field=f"supplement {supplement['supplement_id']} file",
        )
        blocks.extend(supplement["blocks"])
        figures.extend(supplement["figures"])
        tables.extend(supplement["tables"])
        for asset_id in supplement["asset_ids"]:
            if asset_id not in asset_ids:
                raise RecordJsonError(
                    f"supplement {supplement['supplement_id']!r} refers to "
                    f"unknown asset {asset_id!r}"
                )

    _unique_ids(section_ids, "section_id")
    _unique_ids(supplement_ids, "supplement_id")
    _unique_ids((block["block_id"] for block in blocks), "block_id")
    _unique_ids((figure["figure_id"] for figure in figures), "figure_id")
    _unique_ids((table["table_id"] for table in tables), "table_id")
    _unique_ids(
        (
            part["part_id"]
            for table in tables
            for part in table["parts"]
        ),
        "part_id",
    )

    rich_values = [block["content"] for block in blocks]
    rich_values.extend(
        section["heading"]
        for section in payload["sections"]
        if isinstance(section.get("heading"), dict)
    )
    for figure in figures:
        rich_values.append(figure["caption"])
        asset_id = figure.get("asset_id")
        if asset_id is not None and asset_id not in asset_ids:
            raise RecordJsonError(
                f"figure {figure['figure_id']!r} refers to unknown asset {asset_id!r}"
            )
    for table in tables:
        rich_values.append(table["title"])
        rich_values.extend(table["footnotes"])
        for path in table["structure_assets"].values():
            _asset_path(path, field=f"table {table['table_id']} structure asset")
            if path not in asset_paths:
                raise RecordJsonError(
                    f"table {table['table_id']!r} structure asset is absent "
                    f"from the asset registry: {path!r}"
                )
        if "source_image" in table:
            _asset_path(
                table["source_image"], field=f"table {table['table_id']} source image"
            )
            if table["source_image"] not in asset_paths:
                raise RecordJsonError(
                    f"table {table['table_id']!r} source image is absent "
                    "from the asset registry"
                )
        for part in table["parts"]:
            for row in part["rows"]:
                for cell in row:
                    validate_safe_html_fragment(cell["html"])
                    if not rich_text_matches_plain(cell["text"], cell["html"]):
                        raise RecordJsonError(
                            f"table {table['table_id']!r} cell plain text and HTML disagree"
                        )
    for rich in rich_values:
        validate_safe_html_fragment(rich["html"])
        if not rich_text_matches_plain(rich["plain_text"], rich["html"]):
            raise RecordJsonError("plain text and rich HTML disagree")


def build_record_json(
    metadata: RecordMetadata,
    article: ArticleExtraction,
    supplements: Sequence[SupplementExtraction],
    assets: Sequence[Mapping[str, Any]],
) -> RecordJsonResult:
    """Build deterministic public content and private JSON-Pointer coverage."""

    if article.title != metadata.title:
        raise RecordJsonError(
            f"article title differs from record metadata: {article.title!r} != {metadata.title!r}"
        )
    asset_values = _asset_registry(article, supplements, assets)
    asset_ids = {asset["asset_id"] for asset in asset_values}

    payload: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "asset_path_base": RECORD_ASSET_PATH_BASE,
        "content_format": RECORD_CONTENT_FORMAT,
        "record": {
            **metadata.as_dict(),
            "bibliographic": dict(sorted(article.bibliographic.items())),
        },
        "front_matter": [_block(block) for block in article.front_matter],
        "sections": [
            {
                "section_id": section.section_id,
                "heading": _rich_from_markup(
                    section.heading, kind="section_heading"
                ),
                "level": section.level,
                "blocks": [_block(block) for block in section.blocks],
            }
            for section in article.sections
        ],
        "figures": [_figure(figure, asset_ids) for figure in article.figures],
        "tables": [_table(table) for table in article.tables],
        "supporting_information": [
            _block(block) for block in article.supporting_information
        ],
        "supplements": [],
        "references": [_block(block) for block in article.references],
        "assets": asset_values,
    }

    for supplement in supplements:
        supplement_tables = list(getattr(supplement, "tables", ()))
        explicit_asset_ids = list(getattr(supplement, "asset_ids", ()))
        related_asset_ids = [
            asset["asset_id"]
            for asset in asset_values
            if asset.get("parent_id") == supplement.supplement_id
        ]
        payload["supplements"].append(
            {
                "supplement_id": supplement.supplement_id,
                "file": {
                    "path": _asset_path(
                        supplement.copied_path,
                        field=f"supplement {supplement.supplement_id} file",
                    ),
                    "media_type": supplement.source.detected_format,
                },
                "blocks": [_block(block) for block in supplement.blocks],
                "figures": [
                    _figure(figure, asset_ids) for figure in supplement.figures
                ],
                "tables": [_table(table) for table in supplement_tables],
                "asset_ids": sorted(set(explicit_asset_ids + related_asset_ids)),
            }
        )

    coverage: list[dict[str, Any]] = []
    metadata_source = f"database/records/{metadata.record_id}.yaml"
    coverage.extend(
        [
            _coverage(
                coverage_id="record-title",
                content_kind="title",
                source_path=metadata_source,
                source_locator="title",
                output_id="record-title",
                pointer="/record/title",
            ),
            _coverage(
                coverage_id="record-authors",
                content_kind="authors",
                source_path=metadata_source,
                source_locator="authors",
                output_id="record-authors",
                pointer="/record/authors",
            ),
            _coverage(
                coverage_id="record-citation",
                content_kind="citation",
                source_path=metadata_source,
                source_locator="journal, publication_year, doi; extracted bibliographic details",
                output_id="record-citation",
                pointer="/record",
            ),
        ]
    )
    for index, block in enumerate(article.front_matter):
        coverage.append(_block_coverage(block, _pointer("front_matter", index)))
    for section_index, section in enumerate(article.sections):
        base = _pointer("sections", section_index)
        coverage.append(_section_coverage(section, base + "/heading"))
        for block_index, block in enumerate(section.blocks):
            coverage.append(
                _block_coverage(block, base + _pointer("blocks", block_index))
            )
    for index, figure in enumerate(article.figures):
        coverage.extend(_figure_coverage(figure, _pointer("figures", index)))
    for index, table in enumerate(article.tables):
        base = _pointer("tables", index)
        coverage.extend(_table_coverage(table, base))
    for index, block in enumerate(article.supporting_information):
        coverage.append(
            _block_coverage(block, _pointer("supporting_information", index))
        )
    for index, block in enumerate(article.references):
        coverage.append(_block_coverage(block, _pointer("references", index)))

    for supplement_index, supplement in enumerate(supplements):
        base = _pointer("supplements", supplement_index)
        coverage.append(
            _coverage(
                coverage_id=f"supplement-file-{supplement.supplement_id}",
                content_kind="supplement_file",
                source_path=supplement.source.relative_path,
                source_locator="copied source",
                output_id=f"supplement-file-{supplement.supplement_id}",
                pointer=base + "/file/path",
            )
        )
        for block_index, block in enumerate(supplement.blocks):
            coverage.append(
                _block_coverage(block, base + _pointer("blocks", block_index))
            )
        for figure_index, figure in enumerate(supplement.figures):
            coverage.extend(
                _figure_coverage(
                    figure, base + _pointer("figures", figure_index)
                )
            )
        for table_index, table in enumerate(getattr(supplement, "tables", ())):
            coverage.extend(
                _table_coverage(table, base + _pointer("tables", table_index))
            )

    raw_by_id = {str(asset.get("asset_id")): asset for asset in assets}
    for index, asset in enumerate(asset_values):
        raw = raw_by_id.get(asset["asset_id"], {})
        coverage.append(
            _asset_coverage(asset, raw, _pointer("assets", index, "path"))
        )

    validate_record_payload(payload, source_path="record.json")
    return RecordJsonResult(payload=payload, coverage=coverage)


def record_payload(
    metadata: RecordMetadata,
    article: ArticleExtraction,
    supplements: Sequence[SupplementExtraction],
    assets: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return just the validated canonical public payload."""

    return build_record_json(metadata, article, supplements, assets).payload


def write_record_json(
    metadata: RecordMetadata,
    article: ArticleExtraction,
    supplements: Sequence[SupplementExtraction],
    assets: Sequence[Mapping[str, Any]],
    extraction_root: Path,
) -> RecordJsonResult:
    """Atomically write ``record.json`` and return its diagnostic coverage."""

    result = build_record_json(metadata, article, supplements, assets)
    atomic_write_text(
        extraction_root / "record.json", canonical_record_json(result.payload)
    )
    return result


__all__ = [
    "RECORD_ASSET_PATH_BASE",
    "RECORD_CONTENT_FORMAT",
    "RECORD_SCHEMA_VERSION",
    "RecordJsonError",
    "RecordJsonResult",
    "build_record_json",
    "canonical_record_json",
    "record_payload",
    "validate_record_payload",
    "write_record_json",
]
