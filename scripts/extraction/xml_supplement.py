"""Safely recover readable fields from XML supplementary files."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from .models import ContentBlock, SourceFile


_MAX_XML_BYTES = 50 * 1024 * 1024
_SPACE = re.compile(r"\s+")


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


def _name(tag: Any) -> str:
    value = str(tag)
    if value.startswith("{") and "}" in value:
        return value.split("}", 1)[1]
    return value


def _text(value: str | None) -> str:
    return _SPACE.sub(" ", value or "").strip()


def _block(
    supplement_id: str,
    number: int,
    *,
    kind: str,
    label: str,
    value: str,
    source: SourceFile,
    locator: str,
) -> ContentBlock:
    plain = f"{label}: {value}" if value else f"{label}:"
    rich = f"<strong>{html.escape(label)}:</strong>"
    if value:
        rich += f" {html.escape(value)}"
    return ContentBlock(
        block_id=f"{supplement_id}-xml-field-{number:03d}",
        kind=kind,
        markdown=rich,
        plain_text=plain,
        source_path=source.relative_path,
        source_locator=locator,
    )


def extract_xml_fields(
    source: SourceFile,
    supplement_id: str,
    xml_path: Path,
) -> tuple[list[ContentBlock], list[dict[str, Any]]]:
    """Return ordered scalar XML fields without modifying the source bytes.

    Recovery mode is deliberate: publisher XML occasionally contains a lone
    undeclared prefix even though the remaining tree is unambiguous.  Any such
    recovery is disclosed privately as a warning.
    """

    try:
        from lxml import etree
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency check
        raise RuntimeError("lxml is required for XML supplement extraction") from exc

    warnings: list[dict[str, Any]] = []
    if xml_path.stat().st_size > _MAX_XML_BYTES:
        return [], [
            _warning(
                "supplement_xml_size_limit",
                f"XML exceeds the {_MAX_XML_BYTES}-byte parsing limit; original preserved",
                source,
                supplement_id,
            )
        ]
    try:
        parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            dtd_validation=False,
            recover=True,
            huge_tree=False,
            remove_comments=False,
        )
        document = etree.parse(str(xml_path), parser)
        root = document.getroot()
    except Exception as exc:
        return [], [
            _warning(
                "supplement_xml_parse_failed",
                f"XML field extraction failed; original preserved ({type(exc).__name__})",
                source,
                supplement_id,
            )
        ]

    if parser.error_log:
        first = parser.error_log[0]
        warnings.append(
            _warning(
                "supplement_xml_recovered_malformed_source",
                (
                    "Publisher XML required non-destructive recovery before field "
                    f"extraction (line {first.line}: {first.message})"
                ),
                source,
                supplement_id,
            )
        )
    if root is None:
        warnings.append(
            _warning(
                "supplement_xml_empty_document",
                "XML has no recoverable root element; original preserved",
                source,
                supplement_id,
            )
        )
        return [], warnings

    blocks: list[ContentBlock] = []

    def visit(element: Any, path: str) -> None:
        for raw_name, raw_value in sorted(element.attrib.items(), key=lambda item: str(item[0])):
            value = _text(str(raw_value))
            blocks.append(
                _block(
                    supplement_id,
                    len(blocks) + 1,
                    kind="xml_attribute",
                    label=f"{_name(element.tag)}.@{_name(raw_name)}",
                    value=value,
                    source=source,
                    locator=f"{path}/@{_name(raw_name)}",
                )
            )

        direct = _text(element.text)
        children = [child for child in element if isinstance(child.tag, str)]
        if direct:
            blocks.append(
                _block(
                    supplement_id,
                    len(blocks) + 1,
                    kind="xml_field",
                    label=_name(element.tag),
                    value=direct,
                    source=source,
                    locator=f"{path}/text()",
                )
            )
        elif not children:
            blocks.append(
                _block(
                    supplement_id,
                    len(blocks) + 1,
                    kind="xml_empty_field",
                    label=_name(element.tag),
                    value="",
                    source=source,
                    locator=path,
                )
            )

        totals: dict[str, int] = {}
        for child in children:
            child_name = _name(child.tag)
            totals[child_name] = totals.get(child_name, 0) + 1
        seen: dict[str, int] = {}
        for child in children:
            child_name = _name(child.tag)
            seen[child_name] = seen.get(child_name, 0) + 1
            suffix = f"[{seen[child_name]}]" if totals[child_name] > 1 else ""
            visit(child, f"{path}/{child_name}{suffix}")
            tail = _text(child.tail)
            if tail:
                blocks.append(
                    _block(
                        supplement_id,
                        len(blocks) + 1,
                        kind="xml_text",
                        label=f"{_name(element.tag)} text",
                        value=tail,
                        source=source,
                        locator=f"{path}/text()[after {child_name}{suffix}]",
                    )
                )

    root_name = _name(root.tag)
    visit(root, f"/{root_name}")
    if not blocks:
        warnings.append(
            _warning(
                "supplement_xml_no_readable_fields",
                "XML contains no readable fields; original preserved",
                source,
                supplement_id,
            )
        )
    return blocks, warnings


__all__ = ["extract_xml_fields"]
