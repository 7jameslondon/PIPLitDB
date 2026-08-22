"""Render the single polished textual deliverable and table derivatives."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote

from .html_extractor import table_as_html
from .metadata import RecordMetadata
from .models import (
    ContentBlock,
    FigureItem,
    HtmlExtraction,
    RenderedRecord,
    SupplementExtraction,
    TableItem,
)
from .paths import atomic_write_json, atomic_write_text
from .table_schema import TABLE_ASSET_PATH_BASE, validate_table_payload


def _target(path: str) -> str:
    """Return a portable, URL-escaped relative Markdown target."""

    normalized = PurePosixPath(path).as_posix()
    return quote(normalized, safe="/._-~")


_MEASUREMENT_RE = re.compile(
    r"^\s*(?P<relation>[≤≥<>]?)\s*(?P<mantissa>\d+(?:\.\d+)?)"
    r"(?:\s*\(±(?P<uncertainty>\d+(?:\.\d+)?)\))?"
    r"\s*×\s*10\^\{(?P<exponent>[−-]?\d+)\}"
)
_SPECIFICITY_RE = re.compile(
    r"\[\s*(?P<relation>[≤≥<>]?)\s*(?P<value>\d+(?:\.\d+)?)\s*\]"
)
_FOOTNOTE_RE = re.compile(r"\^\{\[([A-Za-z])\]\}")
_RELATIONS = {"": "=", "≤": "<=", "≥": ">=", "<": "<", ">": ">"}


def machine_table_records(table: TableItem) -> list[dict[str, Any]]:
    """Create typed long-form records when a table contains Ka measurements."""

    records: list[dict[str, Any]] = []
    if "K_{a}" not in table.title_plain and "K a" not in table.title_plain:
        return records
    for part in table.parts:
        if len(part.rows) < 2 or len(part.rows[0]) < 2:
            continue
        headers = part.rows[0]
        group_match = re.search(r"\bon\s+(.+)$", headers[0].text, re.IGNORECASE)
        source_group = group_match.group(1).strip() if group_match else headers[0].text
        for row in part.rows[1:]:
            if len(row) != len(headers):
                continue
            compound_id = re.sub(r"\s+", "", row[0].text)
            for column_index, cell in enumerate(row[1:], start=1):
                match = _MEASUREMENT_RE.match(cell.text)
                if not match:
                    continue
                specificity_match = _SPECIFICITY_RE.search(
                    cell.text, match.end()
                )
                association_constant: dict[str, Any] = {
                    "relation": _RELATIONS[match.group("relation")],
                    "mantissa": float(match.group("mantissa")),
                    "exponent": int(match.group("exponent").replace("−", "-")),
                    "unit": "M^-1",
                }
                if match.group("uncertainty") is not None:
                    association_constant["uncertainty"] = float(
                        match.group("uncertainty")
                    )
                specificity = None
                if specificity_match:
                    specificity = {
                        "relation": _RELATIONS[specificity_match.group("relation")],
                        "value": float(specificity_match.group("value")),
                    }
                records.append(
                    {
                        "part_id": part.part_id,
                        "source_group": source_group,
                        "compound_id": compound_id,
                        "structure_asset": table.structure_assets.get(compound_id),
                        "sequence": headers[column_index].text,
                        "match_site": "<strong>" in cell.markdown,
                        "association_constant": association_constant,
                        "specificity": specificity,
                        "footnotes": _FOOTNOTE_RE.findall(cell.text),
                        "normalized_text": cell.text,
                    }
                )
    return records


def typed_table_footnotes(table: TableItem) -> list[dict[str, str]]:
    typed: list[dict[str, str]] = []
    marker = re.compile(r"\[([A-Za-z])\]\s*")
    for value in table.footnotes_plain:
        matches = list(marker.finditer(value))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
            text = value[match.end() : end].strip()
            if text:
                typed.append(
                    {
                        "label": match.group(1).lower(),
                        "scope": "table",
                        "text": text,
                    }
                )
    return typed


# Compatibility for callers that used these helpers before they became part of
# the record-JSON serialization contract.
_machine_table_records = machine_table_records
_typed_table_footnotes = typed_table_footnotes


def _table_dict(
    table: TableItem, machine_records: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "asset_path_base": TABLE_ASSET_PATH_BASE,
        "table_id": table.table_id,
        "label": table.label,
        "title": table.title_plain,
        "parts": [
            {
                "part_id": part.part_id,
                "rows": [
                    [cell.as_dict() for cell in row]
                    for row in part.rows
                ],
            }
            for part in table.parts
        ],
        "footnotes": table.footnotes_plain,
        "footnotes_typed": typed_table_footnotes(table),
        "structure_assets": dict(sorted(table.structure_assets.items())),
        "machine_records": machine_records,
        "source_kind": table.source_kind,
        **({"source_image": table.image_path} if table.image_path else {}),
    }


def write_table_derivatives(tables: Iterable[TableItem], extraction_root: Path) -> None:
    """Write one lossless, machine-ready JSON derivative for every table."""

    for table in tables:
        machine_records = machine_table_records(table)
        table.json_path = f"tables/main/{table.table_id}.json"
        payload = _table_dict(table, machine_records)
        validate_table_payload(payload, source_path=table.json_path)
        atomic_write_json(
            extraction_root / table.json_path,
            payload,
        )


class _Builder:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.coverage: list[dict[str, Any]] = []
        self.generated_number = 0

    def blank(self) -> None:
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    def add(self, value: str, coverage: dict[str, Any] | None = None) -> None:
        self.blank()
        start = len(self.lines) + 1
        value_lines = value.rstrip().splitlines() or [""]
        self.lines.extend(value_lines)
        end = len(self.lines)
        if coverage is None:
            self.generated_number += 1
            coverage = {
                "coverage_id": f"generated-{self.generated_number:04d}",
                "content_kind": "generated_presentation",
                "source_path": "generated",
                "source_locator": "deterministic renderer",
                "output_id": f"generated-{self.generated_number:04d}",
            }
        entry = {
            "schema_version": "1.0",
            "status": "included",
            "output_path": "record.md",
            "output_locator": {"start_line": start, "end_line": end},
            **coverage,
        }
        self.coverage.append(entry)

    def asset_line(self, value: str, asset: dict[str, Any]) -> None:
        self.add(
            value,
            {
                "coverage_id": f"asset-{asset['asset_id']}",
                "content_kind": "asset_link",
                "source_path": asset.get("source_path", ""),
                "source_locator": (
                    f"page {asset['page']}, box {asset.get('box')}"
                    if asset.get("page")
                    else "copied source"
                ),
                "output_id": asset["asset_id"],
            },
        )

    def finish(self) -> RenderedRecord:
        while self.lines and self.lines[-1] == "":
            self.lines.pop()
        return RenderedRecord(text="\n".join(self.lines) + "\n", coverage=self.coverage)


def _block_coverage(block: ContentBlock) -> dict[str, Any]:
    coverage: dict[str, Any] = {
        "coverage_id": f"content-{block.block_id}",
        "content_kind": block.kind,
        "source_path": block.source_path,
        "source_locator": block.source_locator,
        "output_id": block.block_id,
    }
    if block.source_geometry:
        coverage["source_geometry"] = block.source_geometry
    return coverage


def _section_coverage(section: Any) -> dict[str, Any]:
    coverage: dict[str, Any] = {
        "coverage_id": f"heading-{section.section_id}",
        "content_kind": "section_heading",
        "source_path": section.source_path,
        "source_locator": section.source_locator,
        "output_id": section.section_id,
    }
    if section.source_geometry:
        coverage["source_geometry"] = section.source_geometry
    return coverage


def _figure_asset(figure: FigureItem, assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (asset for asset in assets if asset.get("asset_id") == figure.figure_id),
        None,
    )


def _asset_link(label: str, path: str) -> str:
    return f"[{label}]({_target(path)})"


def _doi_text(doi: str) -> str:
    if not doi:
        return ""
    encoded = quote(doi, safe="/:;().-")
    if encoded == doi:
        return f"DOI: https://doi.org/{doi}"
    return f"DOI: `{doi}` · DOI link: https://doi.org/{encoded}"


def render_record(
    metadata: RecordMetadata,
    article: HtmlExtraction,
    supplements: list[SupplementExtraction],
    assets: list[dict[str, Any]],
) -> RenderedRecord:
    builder = _Builder()
    metadata_source = f"database/records/{metadata.record_id}.yaml"
    builder.add(
        f"# {metadata.title}",
        {
            "coverage_id": "record-title",
            "content_kind": "title",
            "source_path": metadata_source,
            "source_locator": "title",
            "output_id": "record-title",
        },
    )
    builder.add(
        f"**Authors:** {', '.join(metadata.authors)}",
        {
            "coverage_id": "record-authors",
            "content_kind": "authors",
            "source_path": metadata_source,
            "source_locator": "authors",
            "output_id": "record-authors",
        },
    )
    details = article.bibliographic
    citation = " · ".join(
        value
        for value in (
            metadata.journal,
            str(metadata.publication_year),
            f"Volume {details['volume']}" if details.get("volume") else "",
            f"Issue {details['issue']}" if details.get("issue") else "",
            f"pp. {details['pages']}" if details.get("pages") else "",
            (
                f"First published: {details['first_published']}"
                if details.get("first_published")
                else ""
            ),
            _doi_text(metadata.doi),
            f"PIP LitDB record: {metadata.record_id}",
        )
        if value
    )
    builder.add(
        citation,
        {
            "coverage_id": "record-citation",
            "content_kind": "citation",
            "source_path": metadata_source,
            "source_locator": "journal, publication_year, doi; extracted bibliographic details",
            "output_id": "record-citation",
        },
    )
    for item in article.front_matter:
        builder.add(item.markdown, _block_coverage(item))

    builder.add("## Local Assets")
    main_figures = [
        figure
        for figure in article.figures
        if figure.output_path and figure.kind != "graphical_abstract"
    ]
    if main_figures:
        builder.add("### Figures and Schemes")
        for figure in main_figures:
            asset = _figure_asset(figure, assets)
            line = f"- {_asset_link(figure.label, figure.output_path or '')}"
            if asset:
                builder.asset_line(line, asset)
            else:
                builder.add(line)
    if article.tables:
        builder.add("### Tables")
        for table in article.tables:
            links: list[str] = []
            if table.json_path:
                links.append(_asset_link("structured data", table.json_path))
            if table.image_path:
                links.append(_asset_link("source image", table.image_path))
            builder.add(f"- {table.label}: {' · '.join(links)}")
        cell_assets = [asset for asset in assets if asset.get("category") == "table_cell"]
        if cell_assets:
            builder.add("### Table Cell Images")
            for asset in cell_assets:
                label = str(asset.get("label") or asset["asset_id"])
                builder.asset_line(
                    f"- {_asset_link(label, str(asset['output_path']))}", asset
                )
    if supplements:
        builder.add("### Supplementary Materials")
        for supplement in supplements:
            filename = PurePosixPath(supplement.copied_path).name
            builder.add(
                f"- {_asset_link(f'{supplement.supplement_id}: {filename}', supplement.copied_path)}"
            )
            for figure in supplement.figures:
                if not figure.output_path:
                    continue
                asset = _figure_asset(figure, assets)
                value = f"  - {_asset_link(figure.label, figure.output_path)}"
                if asset:
                    builder.asset_line(value, asset)
                else:
                    builder.add(value)

    for section in article.sections:
        heading_level = min(max(int(section.level), 2), 6)
        builder.add(
            f"{'#' * heading_level} {section.heading}",
            _section_coverage(section),
        )
        for block in section.blocks:
            builder.add(block.markdown, _block_coverage(block))

    if article.supporting_information:
        builder.add("## Supporting Information Notice")
        for block in article.supporting_information:
            builder.add(block.markdown, _block_coverage(block))

    if article.references:
        builder.add("## References")
        for reference in article.references:
            builder.add(f"- {reference.markdown}", _block_coverage(reference))

    if article.tables:
        builder.add("## Tables")
        for table in article.tables:
            builder.add(
                f"### {table.label}",
                {
                    "coverage_id": f"table-{table.table_id}",
                    "content_kind": "table",
                    "source_path": table.source_path,
                    "source_locator": table.source_locator,
                    "output_id": table.table_id,
                },
            )
            if table.title_markdown and table.title_plain.casefold() != table.label.casefold():
                builder.add(
                    table.title_markdown,
                    {
                        "coverage_id": f"table-title-{table.table_id}",
                        "content_kind": "table_title",
                        "source_path": table.source_path,
                        "source_locator": table.source_locator,
                        "output_id": f"table-title-{table.table_id}",
                    },
                )
            links = []
            if table.json_path:
                links.append(_asset_link("structured data", table.json_path))
            if table.image_path:
                links.append(_asset_link("source image", table.image_path))
            if links:
                builder.add("Assets: " + " · ".join(links))
            related = [
                asset
                for asset in assets
                if asset.get("category") == "table_cell"
                and asset.get("parent_table_id") == table.table_id
            ]
            if related:
                builder.add(
                    "Graphical cell images: "
                    + " · ".join(
                        _asset_link(
                            str(asset.get("label") or asset["asset_id"]),
                            str(asset["output_path"]),
                        )
                        for asset in related
                    )
                )
            builder.add(
                table_as_html(table),
                {
                    "coverage_id": f"table-body-{table.table_id}",
                    "content_kind": "table_body",
                    "source_path": table.source_path,
                    "source_locator": table.source_locator,
                    "output_id": f"table-body-{table.table_id}",
                },
            )
            for index, footnote in enumerate(table.footnotes_markdown, start=1):
                builder.add(
                    footnote,
                    {
                        "coverage_id": f"table-footnotes-{table.table_id}-{index:02d}",
                        "content_kind": "table_footnotes",
                        "source_path": table.source_path,
                        "source_locator": table.source_locator,
                        "output_id": f"table-footnotes-{table.table_id}-{index:02d}",
                    },
                )

    captioned_figures = [
        figure for figure in article.figures if figure.caption_plain or figure.output_path
    ]
    if captioned_figures:
        builder.add("## Figure and Scheme Captions")
        for figure in captioned_figures:
            builder.add(
                f"### {figure.label}",
                {
                    "coverage_id": f"figure-{figure.figure_id}",
                    "content_kind": figure.kind,
                    "source_path": figure.source_path,
                    "source_locator": figure.source_locator,
                    "output_id": figure.figure_id,
                },
            )
            if figure.output_path:
                builder.add(f"Asset: {_asset_link(figure.label, figure.output_path)}")
            if figure.caption_markdown:
                builder.add(
                    figure.caption_markdown,
                    {
                        "coverage_id": f"caption-{figure.figure_id}",
                        "content_kind": f"{figure.kind}_caption",
                        "source_path": figure.source_path,
                        "source_locator": figure.source_locator,
                        "output_id": f"caption-{figure.figure_id}",
                    },
                )

    if supplements:
        builder.add("## Supplementary Materials")
        for supplement in supplements:
            filename = PurePosixPath(supplement.copied_path).name
            builder.add(f"### {supplement.supplement_id}: {filename}")
            builder.add(f"Source file: {_asset_link(filename, supplement.copied_path)}")
            for block in supplement.blocks:
                if block.kind in {"figure_caption", "scheme_caption", "footnote"}:
                    continue
                builder.add(block.markdown, _block_coverage(block))
            if supplement.figures:
                builder.add("#### Figures and Captions")
                for figure in supplement.figures:
                    builder.add(
                        f"##### {figure.label}",
                        {
                            "coverage_id": f"figure-{figure.figure_id}",
                            "content_kind": figure.kind,
                            "source_path": figure.source_path,
                            "source_locator": figure.source_locator,
                            "output_id": figure.figure_id,
                        },
                    )
                    if figure.output_path:
                        builder.add(f"Asset: {_asset_link(figure.label, figure.output_path)}")
                    if figure.caption_markdown:
                        builder.add(
                            figure.caption_markdown,
                            {
                                "coverage_id": f"caption-{figure.figure_id}",
                                "content_kind": f"{figure.kind}_caption",
                                "source_path": figure.source_path,
                                "source_locator": figure.source_locator,
                                "output_id": f"caption-{figure.figure_id}",
                            },
                        )
            for block in supplement.blocks:
                if block.kind == "footnote":
                    builder.add(block.markdown, _block_coverage(block))

    return builder.finish()
