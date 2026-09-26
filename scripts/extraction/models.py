"""Shared data models for extraction and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TABLE_SOURCE_KINDS = frozenset(
    {"html", "pdf", "image", "presentation", "document", "spreadsheet"}
)
IMAGE_TABLE_SOURCE_KINDS = frozenset({"pdf", "image"})


@dataclass(frozen=True)
class SourceFile:
    role: str
    path: Path
    relative_path: str
    size: int
    sha256: str
    detected_format: str
    page_count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": self.role,
            "path": self.relative_path,
            "bytes": self.size,
            "sha256": self.sha256,
            "detected_format": self.detected_format,
        }
        if self.page_count is not None:
            result["page_count"] = self.page_count
        return result


@dataclass
class ContentBlock:
    block_id: str
    kind: str
    markdown: str
    plain_text: str
    source_path: str
    source_locator: str
    source_geometry: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Section:
    section_id: str
    heading: str
    level: int = 2
    blocks: list[ContentBlock] = field(default_factory=list)
    source_path: str = ""
    source_locator: str = ""
    source_geometry: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FigureItem:
    figure_id: str
    source_id: str | None
    label: str
    kind: str
    caption_markdown: str
    caption_plain: str
    source_path: str
    source_locator: str
    output_path: str | None = None


@dataclass
class TableCell:
    text: str
    markdown: str
    header: bool
    rowspan: int = 1
    colspan: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "html": self.markdown,
            "header": self.header,
            "rowspan": self.rowspan,
            "colspan": self.colspan,
        }


@dataclass
class TablePart:
    part_id: str
    rows: list[list[TableCell]]


@dataclass
class TableItem:
    table_id: str
    source_id: str
    label: str
    title_markdown: str
    title_plain: str
    parts: list[TablePart]
    footnotes_markdown: list[str]
    footnotes_plain: list[str]
    source_path: str
    source_locator: str
    json_path: str | None = None
    structure_assets: dict[str, str] = field(default_factory=dict)
    image_path: str | None = None
    source_kind: str = "html"
    machine_records: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.source_kind not in TABLE_SOURCE_KINDS:
            allowed = ", ".join(sorted(TABLE_SOURCE_KINDS))
            raise ValueError(
                f"unsupported table source_kind {self.source_kind!r}; expected one of {allowed}"
            )

    @property
    def requires_source_image(self) -> bool:
        """Whether the table's authoritative representation is visual."""

        return self.source_kind in IMAGE_TABLE_SOURCE_KINDS


@dataclass
class Repair:
    repair_id: str
    pattern: str
    replacement: str
    occurrences: int
    reason: str
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "repair_id": self.repair_id,
            "pattern": self.pattern,
            "replacement": self.replacement,
            "occurrences": self.occurrences,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class EmbeddedAsset:
    """A source-embedded binary waiting for candidate materialization."""

    asset_id: str
    category: str
    label: str
    media_type: str
    output_path: str
    data: bytes
    source_path: str
    source_locator: str
    ocr_performed: bool = False
    parent_table_id: str | None = None
    compound_id: str | None = None


@dataclass
class ArticleExtraction:
    """Source-neutral representation of an extracted main article.

    ``text_extraction`` records how the article body was obtained.  Supported
    reporting keys are ``source_role``, ``source_path``, ``method``,
    ``ocr_performed``, and ``supporting_source``.  An empty mapping preserves
    the original publisher-HTML/no-OCR reporting behavior.

    ``page_diagnostics`` contains optional, JSON-serializable page-analysis
    rows.  Diagnostics writers keep these private rather than adding them to
    the machine-ready extraction.
    """

    title: str
    bibliographic: dict[str, str]
    sections: list[Section]
    figures: list[FigureItem]
    tables: list[TableItem]
    references: list[ContentBlock]
    supporting_information: list[ContentBlock]
    repairs: list[Repair]
    warnings: list[dict[str, Any]]
    front_matter: list[ContentBlock] = field(default_factory=list)
    text_extraction: dict[str, Any] = field(default_factory=dict)
    page_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    embedded_assets: list[EmbeddedAsset] = field(default_factory=list)


# Backward compatibility for the existing HTML extractor and external callers.
# ArticleExtraction is the canonical name for new source-neutral code.
HtmlExtraction = ArticleExtraction


@dataclass
class SupplementExtraction:
    supplement_id: str
    source: SourceFile
    copied_path: str
    blocks: list[ContentBlock]
    figures: list[FigureItem]
    warnings: list[dict[str, Any]]
    repairs: list[dict[str, Any]] = field(default_factory=list)
    exclusions: list[dict[str, Any]] = field(default_factory=list)
    tables: list[TableItem] = field(default_factory=list)
    assets: list[dict[str, Any]] = field(default_factory=list)
    asset_ids: list[str] = field(default_factory=list)


@dataclass
class RenderedRecord:
    text: str
    coverage: list[dict[str, Any]]


@dataclass
class ValidationFinding:
    code: str
    severity: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.path is not None:
            result["path"] = self.path
        return result
