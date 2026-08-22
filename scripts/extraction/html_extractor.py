"""Non-destructive publisher-HTML extraction.

The source HTML is decoded explicitly as UTF-8 and parsed without fetching any
external resource. Publisher UI, figures, tables, and references are handled as
separate semantic streams so captions and tables can be consolidated later.
"""

from __future__ import annotations

import base64
import binascii
import copy
import html as html_stdlib
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from .models import (
    ContentBlock,
    EmbeddedAsset,
    FigureItem,
    HtmlExtraction,
    Repair,
    Section,
    TableCell,
    TableItem,
    TablePart,
)

try:
    from lxml import etree, html
except ModuleNotFoundError:  # pragma: no cover - exercised by CLI dependency check
    etree = None
    html = None


SPACE_PATTERN = re.compile(r"\s+")
PLACEHOLDER_PATTERN = re.compile(r"equation/tex2gif-sup-\d+\.gif")
SCIENCEDIRECT_BIBLIOGRAPHY_HREF = re.compile(r"#b\d+", re.IGNORECASE)


def normalize_space(value: str) -> str:
    return SPACE_PATTERN.sub(" ", unicodedata.normalize("NFC", value)).strip()


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value or "section"


def _tag(element: Any) -> str:
    if not isinstance(element.tag, str):
        return ""
    return element.tag.rsplit("}", 1)[-1].lower()


def _safe_text(value: str | None, *, markup: str) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFC", value)
    if markup == "plain":
        return value
    escaped = html_stdlib.escape(value, quote=False)
    if markup == "markdown":
        # Publisher text is not Markdown. Escape literal control characters so
        # sequences such as a significance marker inside ``<sup>**</sup>``
        # cannot be reinterpreted as Markdown emphasis by the record renderer.
        return escaped.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")
    return escaped


def _render_mathml(element: Any, *, markup: str) -> str:
    """Render MathML without flattening subscripts, powers, or fractions.

    The record format accepts a conservative HTML subset, so semantic HTML
    ``sub``/``sup`` tags preserve notation in both Markdown and HTML output.
    Plain text uses explicit ``_{...}``/``^{...}`` markers.  Source glyphs are
    deliberately retained; possible publisher symbol defects are diagnostics,
    not silent text substitutions.
    """

    def wrap_script(base: str, script: str, kind: str) -> str:
        if markup == "plain":
            marker = "_" if kind == "sub" else "^"
            return f"{base}{marker}{{{script}}}"
        return f"{base}<{kind}>{script}</{kind}>"

    def contents(node: Any) -> str:
        parts = [_safe_text(node.text, markup=markup)]
        for child in node:
            if _tag(child) not in {"annotation", "annotation-xml"}:
                parts.append(visit(child))
            parts.append(_safe_text(child.tail, markup=markup))
        return "".join(parts)

    def operands(node: Any) -> list[str]:
        return [
            visit(child)
            for child in node
            if _tag(child) not in {"annotation", "annotation-xml"}
        ]

    def visit(node: Any) -> str:
        tag = _tag(node)
        if tag in {"mi", "mn", "mtext", "ms", "mo"}:
            value = contents(node)
            operator = normalize_space(html_stdlib.unescape(value))
            if tag == "mo" and operator in {
                "=",
                "×",
                "±",
                "≠",
                "<",
                ">",
                "≤",
                "≥",
            }:
                return f" {normalize_space(value)} "
            return value
        if tag == "msub":
            parts = operands(node)
            return (
                wrap_script(parts[0], parts[1], "sub")
                if len(parts) >= 2
                else "".join(parts)
            )
        if tag == "msup":
            parts = operands(node)
            return (
                wrap_script(parts[0], parts[1], "sup")
                if len(parts) >= 2
                else "".join(parts)
            )
        if tag == "msubsup":
            parts = operands(node)
            if len(parts) >= 3:
                return wrap_script(
                    wrap_script(parts[0], parts[1], "sub"), parts[2], "sup"
                )
            return "".join(parts)
        if tag == "mfrac":
            parts = operands(node)
            if len(parts) >= 2:
                return f"({parts[0]})/({parts[1]})"
            return "".join(parts)
        if tag == "mfenced":
            opening = _safe_text(node.get("open", "("), markup=markup)
            closing = _safe_text(node.get("close", ")"), markup=markup)
            separator = _safe_text(node.get("separators", ","), markup=markup)
            return f"{opening}{separator.join(operands(node))}{closing}"
        if tag == "msqrt":
            return f"sqrt({contents(node)})"
        if tag == "mroot":
            parts = operands(node)
            if len(parts) >= 2:
                return f"root({parts[0]}, {parts[1]})"
            return "".join(parts)
        if tag == "semantics":
            parts = operands(node)
            return parts[0] if parts else ""
        return contents(node)

    rendered = visit(element)
    rendered = re.sub(r"[\t\r\n ]+", " ", rendered).strip()
    rendered = re.sub(r"\( +", "(", rendered)
    rendered = re.sub(r" +\)", ")", rendered)
    return rendered


def _wrap_inline_markup(child: Any, rendered: str, *, markup: str) -> str:
    child_tag = _tag(child)
    if child_tag == "math" and normalize_space(rendered):
        # MathJax display containers often omit literal whitespace between the
        # preceding citation and the MathML node.
        return f" {rendered.strip()} "
    if child_tag in {"i", "em"} and normalize_space(rendered):
        return f"<em>{rendered.strip()}</em>"
    if child_tag in {"b", "strong"} and normalize_space(rendered):
        return f"<strong>{rendered.strip()}</strong>"
    if child_tag == "sup":
        return f"<sup>{rendered.strip()}</sup>"
    if child_tag == "sub":
        return f"<sub>{rendered.strip()}</sub>"
    if child_tag == "br":
        return "  \n" if markup == "markdown" else "<br>"
    if child_tag != "a":
        return rendered

    href = (child.get("href") or "").strip()
    label = rendered.strip()
    if SCIENCEDIRECT_BIBLIOGRAPHY_HREF.fullmatch(href) and label:
        if not re.fullmatch(r"<sup>.*</sup>", label, re.DOTALL):
            return f"<sup>{label}</sup>"
        return rendered
    if href.startswith("#bib") and label:
        return f"[{label}]"
    if href.startswith(("http://", "https://", "mailto:")) and label:
        if label == href or href.startswith("mailto:"):
            return label
        if markup == "markdown":
            return f"[{label}]({href})"
        return f'<a href="{html_stdlib.escape(href, quote=True)}">{label}</a>'
    return label


def _wrap_inline_plain(child: Any, rendered: str) -> str:
    child_tag = _tag(child)
    if child_tag == "math" and rendered.strip():
        return f" {rendered.strip()} "
    if (
        child_tag == "a"
        and SCIENCEDIRECT_BIBLIOGRAPHY_HREF.fullmatch(
            (child.get("href") or "").strip()
        )
        and rendered.strip()
        and not any(_tag(descendant) == "sup" for descendant in child.iter())
    ):
        return f"^{{{rendered.strip()}}}"
    if child_tag == "sup" and rendered.strip():
        return f"^{{{rendered.strip()}}}"
    if child_tag == "sub" and rendered.strip():
        return f"_{{{rendered.strip()}}}"
    return rendered


def _normalize_inline_markup(rendered: str) -> str:
    rendered = re.sub(r"[\t\r\n ]+", " ", rendered)
    rendered = re.sub(r" +([,;:!?]|\.(?!\.))", r"\1", rendered)
    # Adjacent publisher spans commonly express a single bold chemical name
    # with an italic H in the middle. Join equivalent adjacent semantic tags
    # without changing the underlying text or emphasis.
    rendered = rendered.replace("</strong><strong>", "")
    rendered = rendered.replace("</em><em>", "")
    return rendered.strip()


def render_inline(element: Any, *, markup: str = "markdown") -> str:
    """Render an element's inline content without executing or fetching links."""

    def visit(node: Any) -> str:
        if _tag(node) == "math":
            return _render_mathml(node, markup=markup)
        parts = [_safe_text(node.text, markup=markup)]
        for child in node:
            if child.get("aria-hidden") == "true":
                rendered = ""
            else:
                rendered = visit(child)
                rendered = _wrap_inline_markup(child, rendered, markup=markup)
            parts.append(rendered)
            parts.append(_safe_text(child.tail, markup=markup))
        return "".join(parts)

    return _normalize_inline_markup(visit(element))


def plain_text(element: Any) -> str:
    def visit(node: Any) -> str:
        if _tag(node) == "math":
            return _render_mathml(node, markup="plain")
        parts = [node.text or ""]
        for child in node:
            if child.get("aria-hidden") != "true":
                rendered = visit(child)
                rendered = _wrap_inline_plain(child, rendered)
                parts.append(rendered)
            parts.append(child.tail or "")
        return "".join(parts)

    return normalize_space(visit(element))


def _bibliographic_details(document: Any) -> dict[str, str]:
    """Extract visible journal citation fields from publisher front matter."""

    details: dict[str, str] = {}
    for element in document.xpath(".//*"):
        value = plain_text(element)
        if not value or len(value) > 200:
            continue
        volume = re.fullmatch(
            r"Volume\s+(\d+)\s*,\s*Issue\s+(\d+)", value, flags=re.IGNORECASE
        )
        if volume:
            details.setdefault("volume", volume.group(1))
            details.setdefault("issue", volume.group(2))
        pages = re.fullmatch(
            r"(?:Pages\s*:|pp?\.?)\s*([0-9]+\s*[-–]\s*[0-9]+)",
            value,
            flags=re.IGNORECASE,
        )
        if pages:
            details.setdefault("pages", re.sub(r"\s+", "", pages.group(1)))
        publication_date = re.fullmatch(
            r"Date\s*:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
            value,
            re.IGNORECASE,
        )
        if publication_date:
            details.setdefault("date", publication_date.group(1).strip())

    for element in document.xpath(".//span"):
        if plain_text(element).casefold() != "first published:":
            continue
        sibling = element.getnext()
        if sibling is not None:
            published = plain_text(sibling)
            if published:
                details["first_published"] = published
        break
    return details


def _has_ancestor(element: Any, predicate: Any) -> bool:
    parent = element.getparent()
    while parent is not None:
        if predicate(parent):
            return True
        parent = parent.getparent()
    return False


def _is_table_container_id(value: str) -> bool:
    folded = value.casefold()
    return folded.startswith("tbl") or bool(re.fullmatch(r"t\d+", folded))


def _inside_excluded_body_region(element: Any) -> bool:
    def excluded(parent: Any) -> bool:
        parent_tag = _tag(parent)
        parent_id = parent.get("id") or ""
        return (
            parent_tag in {"figure", "figcaption", "table", "header"}
            or _is_table_container_id(parent_id)
            or parent_id.startswith("article-references")
        )

    return _has_ancestor(element, excluded)


def _content_root(document: Any) -> Any:
    articles = document.xpath(".//article")
    if not articles:
        return document
    return max(articles, key=lambda node: len(node.xpath(".//p")))


def _remove_terminal_crossref_appendices(root: Any) -> None:
    """Drop Wiley's redundant, sentence-final figure/table linkout clusters.

    In the pilot HTML these are direct-child anchors appended after a completed
    sentence (for example ``...rules.</a>2, 3``). Contextual references such as
    ``Figure <a>3</a>.`` are retained because the text immediately before the
    anchor does not end a sentence.
    """

    for paragraph in root.xpath(".//p"):
        suffix: list[Any] = []
        for child in reversed(list(paragraph)):
            href = (child.get("href") or "") if _tag(child) == "a" else ""
            if not href.startswith(("#fig", "#sch", "#tbl")):
                break
            if not re.fullmatch(r"[\s,;]*", child.tail or ""):
                break
            suffix.append(child)
        if not suffix:
            continue
        first = suffix[-1]
        prefix_parts = [paragraph.text or ""]
        for child in paragraph:
            if child is first:
                break
            prefix_parts.append("".join(child.itertext()))
            prefix_parts.append(child.tail or "")
        preceding = "".join(prefix_parts).rstrip()
        # Wiley sometimes inserts a bibliography-link span between the final
        # sentence punctuation and its redundant figure/table linkout cluster.
        if not re.search(
            r"[.?!](?:\s*\d+[a-z]?(?:\s*[,;]\s*\d+[a-z]?)*)?\s*$",
            preceding,
            flags=re.IGNORECASE,
        ):
            continue
        for child in suffix:
            paragraph.remove(child)


def _apply_repairs(
    source: str, repair_specs: Iterable[dict[str, Any]]
) -> tuple[str, list[Repair]]:
    repairs: list[Repair] = []
    repaired = source
    for index, spec in enumerate(repair_specs, 1):
        pattern = str(spec.get("pattern", ""))
        replacement = str(spec.get("replacement", ""))
        if not pattern:
            continue
        repaired, count = re.subn(pattern, replacement, repaired)
        if count:
            repairs.append(
                Repair(
                    repair_id=f"repair-{index:03d}",
                    pattern=pattern,
                    replacement=replacement,
                    occurrences=count,
                    reason=str(spec.get("reason", "private reviewed override")),
                    evidence=str(spec.get("evidence", "")),
                )
            )
    return repaired, repairs


def _figure_caption(figure: Any) -> tuple[str, str, str]:
    """Return label hint plus rich/plain caption across publisher dialects."""

    caption_containers = figure.xpath(".//figcaption[1]")
    if caption_containers:
        container = caption_containers[0]
        paragraphs = container.xpath(".//p") or [container]
        strong = container.xpath(".//strong[1]")
        label_hint = plain_text(strong[0]) if strong else ""
    else:
        # ScienceDirect rendered DOM uses ``cn####`` spans rather than the
        # semantic ``figcaption`` element.
        paragraphs = figure.xpath(
            './/*[@id and starts-with(@id, "cn")]//p'
        )
        label_hint = ""
    caption_markdown = "\n\n".join(
        value for value in (render_inline(paragraph) for paragraph in paragraphs) if value
    )
    caption_plain = " ".join(
        value for value in (plain_text(paragraph) for paragraph in paragraphs) if value
    )
    return label_hint, caption_markdown, caption_plain


def _is_graphical_abstract_figure(figure: Any) -> bool:
    for ancestor in (figure, *figure.iterancestors()):
        headings = ancestor.xpath("./h2")
        if any(plain_text(heading).casefold() == "graphical abstract" for heading in headings):
            return True
    return False


def _figure_items(root: Any, source_path: str) -> list[FigureItem]:
    figures: list[FigureItem] = []
    counters: dict[str, int] = defaultdict(int)
    for figure in root.xpath(".//figure"):
        source_id = figure.get("id")
        label_hint, caption_markdown, caption_plain = _figure_caption(figure)
        caption_label = re.match(
            r"^\s*(Fig(?:ure)?|Scheme)\.?\s*(\d+)\b",
            caption_plain or label_hint,
            flags=re.IGNORECASE,
        )
        if caption_label:
            kind = (
                "scheme"
                if caption_label.group(1).casefold().startswith("scheme")
                else "figure"
            )
            number = int(caption_label.group(2))
            counters[kind] = max(counters[kind], number)
            figure_id = f"{kind}_{number:03d}"
            label = f"{'Scheme' if kind == 'scheme' else 'Figure'} {number}"
        elif _is_graphical_abstract_figure(figure):
            kind = "graphical_abstract"
            counters[kind] += 1
            figure_id = (
                "graphical_abstract"
                if counters[kind] == 1
                else f"graphical_abstract_{counters[kind]:03d}"
            )
            label = "Graphical Abstract"
        elif source_id and source_id.casefold().startswith(("sch", "fig")):
            kind = "scheme" if source_id.casefold().startswith("sch") else "figure"
            number_match = re.search(r"\d+", source_id)
            if number_match:
                number = int(number_match.group())
                counters[kind] = max(counters[kind], number)
            else:
                counters[kind] += 1
                number = counters[kind]
            figure_id = f"{kind}_{number:03d}"
            label = label_hint or f"{'Scheme' if kind == 'scheme' else 'Figure'} {number}"
        elif source_id and re.fullmatch(r"f\d+", source_id, flags=re.IGNORECASE):
            # A caption normally supplies the public number. If damaged
            # ScienceDirect markup omits it, preserve document order rather
            # than treating the opaque f#### source identifier as that number.
            kind = "figure"
            counters[kind] += 1
            number = counters[kind]
            figure_id = f"figure_{number:03d}"
            label = f"Figure {number}"
        else:
            kind = "graphical_abstract"
            counters[kind] += 1
            figure_id = (
                "graphical_abstract"
                if counters[kind] == 1
                else f"graphical_abstract_{counters[kind]:03d}"
            )
            label = label_hint or "Graphical Abstract"
        figures.append(
            FigureItem(
                figure_id=figure_id,
                source_id=source_id,
                label=label,
                kind=kind,
                caption_markdown=caption_markdown,
                caption_plain=caption_plain,
                source_path=source_path,
                source_locator=figure.getroottree().getpath(figure),
            )
        )
    return figures


DATA_IMAGE_PATTERN = re.compile(
    r"\Adata:([^;,]+)(?:;[^;,=]+=[^;,]*)*;base64,(.*)\Z",
    flags=re.IGNORECASE | re.DOTALL,
)
EMBEDDED_IMAGE_FORMATS: dict[str, tuple[str, Any]] = {
    "image/jpeg": ("jpg", lambda value: len(value) >= 4 and value.startswith(b"\xff\xd8\xff")),
    "image/png": ("png", lambda value: value.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/gif": ("gif", lambda value: value.startswith((b"GIF87a", b"GIF89a"))),
    "image/webp": (
        "webp",
        lambda value: len(value) >= 12
        and value.startswith(b"RIFF")
        and value[8:12] == b"WEBP",
    ),
}


def _decode_data_image(value: str) -> tuple[str, str, bytes]:
    match = DATA_IMAGE_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError("image data URI is not base64 encoded")
    media_type = match.group(1).casefold()
    definition = EMBEDDED_IMAGE_FORMATS.get(media_type)
    if definition is None:
        raise ValueError(f"unsupported embedded image media type {media_type!r}")
    payload = re.sub(r"\s+", "", match.group(2))
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("image data URI contains invalid base64") from error
    extension, valid_header = definition
    if not valid_header(decoded):
        raise ValueError(f"embedded bytes do not match declared media type {media_type!r}")
    return media_type, extension, decoded


def _embedded_figure_assets(
    root: Any, figures: list[FigureItem], source_path: str
) -> tuple[list[EmbeddedAsset], list[dict[str, Any]]]:
    assets: list[EmbeddedAsset] = []
    warnings: list[dict[str, Any]] = []
    for element, figure in zip(root.xpath(".//figure"), figures):
        decoded_images: list[tuple[Any, str, str, bytes]] = []
        for image in element.xpath(".//img[@src]"):
            source = (image.get("src") or "").strip()
            if not source.casefold().startswith("data:image/"):
                # Remote and non-image sources are intentionally never fetched
                # or decoded by the offline HTML extractor.
                continue
            try:
                media_type, extension, decoded = _decode_data_image(source)
            except ValueError as error:
                warnings.append(
                    {
                        "schema_version": "1.0",
                        "code": "invalid_embedded_figure_image",
                        "severity": "structural",
                        "message": f"{figure.label}: {error}.",
                        "source_path": source_path,
                        "source_locator": image.getroottree().getpath(image),
                    }
                )
                continue
            decoded_images.append((image, media_type, extension, decoded))
        if not decoded_images:
            continue
        if len(decoded_images) > 1:
            warnings.append(
                {
                    "schema_version": "1.0",
                    "code": "multiple_embedded_images_for_figure",
                    "severity": "structural",
                    "message": (
                        f"{figure.label} contains {len(decoded_images)} embedded images; "
                        "only the first is attached pending reviewed composition."
                    ),
                    "source_path": source_path,
                    "source_locator": element.getroottree().getpath(element),
                }
            )
        image, media_type, extension, decoded = decoded_images[0]
        assets.append(
            EmbeddedAsset(
                asset_id=figure.figure_id,
                category=figure.kind,
                label=figure.label,
                media_type=media_type,
                output_path=f"figures/main/{figure.figure_id}.{extension}",
                data=decoded,
                source_path=source_path,
                source_locator=image.getroottree().getpath(image),
            )
        )
    return assets, warnings


def _parse_rows(table: Any) -> list[list[TableCell]]:
    rows: list[list[TableCell]] = []
    row_nodes = table.xpath("./thead/tr | ./tbody/tr | ./tfoot/tr | ./tr")
    parsed_row_nodes: list[Any] = []
    for row in row_nodes:
        cells: list[TableCell] = []
        for cell in row.xpath("./th | ./td"):
            cells.append(
                TableCell(
                    text=plain_text(cell),
                    markdown=render_inline(cell, markup="html"),
                    header=_tag(cell) == "th",
                    rowspan=max(1, int(cell.get("rowspan") or 1)),
                    colspan=max(1, int(cell.get("colspan") or 1)),
                )
            )
        if cells:
            rows.append(cells)
            parsed_row_nodes.append(row)
    return _combine_diagram_rows(rows, parsed_row_nodes)


def _combine_diagram_rows(
    rows: list[list[TableCell]], row_nodes: list[Any]
) -> list[list[TableCell]]:
    if len(rows) < 3:
        return rows
    result = [rows[0]]
    body = rows[1:]
    index = 0
    while index < len(body):
        current = body[index]
        following = body[index + 1] if index + 1 < len(body) else None
        first = current[0].text.strip() if current else ""
        raw_current = row_nodes[index + 1] if index + 1 < len(row_nodes) else None
        raw_cells = raw_current.xpath("./th | ./td") if raw_current is not None else []
        first_raw_cell = raw_cells[0] if raw_cells else None
        # Wiley uses an internal ``#forNNN`` anchor with a negative placeholder
        # in a value row, followed by the diagram/label row. Requiring that
        # marker prevents legitimate negative rows elsewhere from being merged.
        has_diagram_marker = bool(
            first_raw_cell is not None
            and first_raw_cell.xpath('.//a[starts-with(@href, "#for")]')
        )
        if (
            following is not None
            and has_diagram_marker
            and re.fullmatch(r"-\d+", first)
            and 1 <= len(following) <= len(current)
        ):
            combined: list[TableCell] = []
            for column, upper in enumerate(current):
                lower = following[column] if column < len(following) else None
                if column == 0:
                    text = lower.text if lower is not None else ""
                    markup = lower.markdown if lower is not None else ""
                else:
                    text = upper.text
                    markup = upper.markdown
                    if lower is not None and lower.text:
                        text = f"{text} {lower.text}".strip()
                        markup = f"{markup}<br>{lower.markdown}".strip()
                combined.append(
                    TableCell(
                        text=text,
                        markdown=markup,
                        header=False,
                        rowspan=1,
                        colspan=1,
                    )
                )
            result.append(combined)
            index += 2
        else:
            result.append(current)
            index += 1
    return result


def _table_items(root: Any, source_path: str) -> list[TableItem]:
    tables: list[TableItem] = []
    containers = [
        element
        for element in root.xpath(".//*[@id]")
        if _is_table_container_id(element.get("id") or "")
        and bool(element.xpath(".//table"))
    ]
    seen: set[str] = set()
    for container in containers:
        source_id = container.get("id") or ""
        if source_id in seen:
            continue
        seen.add(source_id)
        headers = container.xpath("./header[1] | .//header[1]")
        captions = container.xpath(
            './/*[@id and starts-with(@id, "cn")]//p[1]'
        )
        title_node = headers[0] if headers else (captions[0] if captions else None)
        title_markdown = render_inline(title_node) if title_node is not None else ""
        title_plain = plain_text(title_node) if title_node is not None else ""
        title_number = re.search(r"\bTable\s*(\d+)\b", title_plain, re.IGNORECASE)
        source_number = re.search(r"\d+", source_id)
        if title_number:
            number = int(title_number.group(1))
        elif source_number:
            number = int(source_number.group())
        else:
            number = len(tables) + 1
        label = f"Table {number}"
        title_markdown = title_markdown or label
        title_plain = title_plain or label
        parts = [
            TablePart(part_id=f"{source_id}-part-{index:02d}", rows=_parse_rows(table))
            for index, table in enumerate(container.xpath(".//table"), 1)
        ]
        footnote_nodes = container.xpath('.//li[starts-with(@id, "note-")]')
        footnotes_markdown = [render_inline(node) for node in footnote_nodes]
        footnotes_plain = [plain_text(node) for node in footnote_nodes]
        tables.append(
            TableItem(
                table_id=f"table_{number:03d}",
                source_id=source_id,
                label=label,
                title_markdown=title_markdown,
                title_plain=title_plain,
                parts=parts,
                footnotes_markdown=[value for value in footnotes_markdown if value],
                footnotes_plain=[value for value in footnotes_plain if value],
                source_path=source_path,
                source_locator=container.getroottree().getpath(container),
                source_kind="html",
            )
        )
    return tables


def _sciencedirect_reference(item: Any) -> tuple[str, str] | None:
    number_anchors = [
        anchor
        for anchor in item.xpath(".//a[@id]")
        if re.fullmatch(r"ref-id-b\d+", anchor.get("id") or "", re.IGNORECASE)
    ]
    content_spans = [
        span
        for span in item.xpath(".//span[@id]")
        if re.fullmatch(r"h\d+", span.get("id") or "", re.IGNORECASE)
    ]
    if not number_anchors or not content_spans:
        return None

    label = plain_text(number_anchors[0]).rstrip(". ")
    content = content_spans[0]
    body_divs = [
        div
        for div in content.xpath("./div")
        if not (div.get("lang") and div.xpath(".//a"))
    ]
    if not body_divs:
        return None
    rendered_body = " ".join(
        value for value in (render_inline(div) for div in body_divs) if value
    )
    visible_body = " ".join(
        value for value in (plain_text(div) for div in body_divs) if value
    )
    if not visible_body:
        return None
    rendered = f"{label}. {rendered_body}" if label else rendered_body
    visible = f"{label}. {visible_body}" if label else visible_body

    doi_links = [
        (anchor.get("href") or "").strip()
        for anchor in item.xpath(".//a[@href]")
        if (anchor.get("href") or "").casefold().startswith("https://doi.org/")
    ]
    if doi_links and doi_links[0] not in visible:
        rendered = f"{rendered} DOI: {doi_links[0]}"
        visible = f"{visible} DOI: {doi_links[0]}"
    return rendered, visible


def _reference_blocks(root: Any, source_path: str) -> list[ContentBlock]:
    headings = [
        heading
        for heading in root.xpath(".//h2")
        if normalize_space(" ".join(heading.itertext())).casefold() == "references"
    ]
    if not headings:
        return []
    section = headings[0]
    while section is not None and _tag(section) != "section":
        section = section.getparent()
    if section is None:
        return []
    references: list[ContentBlock] = []
    for item in section.xpath(".//li"):
        sciencedirect = _sciencedirect_reference(item)
        if sciencedirect is not None:
            rendered, visible = sciencedirect
            references.append(
                ContentBlock(
                    block_id=f"reference-{len(references) + 1:03d}",
                    kind="reference",
                    markdown=rendered,
                    plain_text=visible,
                    source_path=source_path,
                    source_locator=item.getroottree().getpath(item),
                )
            )
            continue
        doi = ""
        for candidate in item.xpath("./div/span[1]"):
            value = plain_text(candidate)
            if re.fullmatch(r"10\.\d{4,9}/\S+", value, flags=re.IGNORECASE):
                doi = value
                break
        clone = copy.deepcopy(item)
        # Direct-child divs are publisher linkout controls. Preserve the DOI
        # value captured above, but omit CAS/Scholar/OpenURL interface text.
        for div in clone.xpath("./div"):
            parent = div.getparent()
            if parent is not None:
                parent.remove(div)
        rendered = render_inline(clone)
        visible = plain_text(clone)
        rendered = re.sub(r"\[\s+(?=<(?:em|strong)>)", "[", rendered)
        visible = re.sub(r"\[\s+(?=[A-Za-z])", "[", visible)
        direct_spans = clone.xpath("./span[1]")
        label = plain_text(direct_spans[0]) if direct_spans else ""
        remainder = normalize_space(visible[len(label) :]) if visible.startswith(label) else visible
        if not remainder:
            continue
        if doi:
            doi_url = "https://doi.org/" + quote(doi, safe="/():;,.+-_~")
            rendered = f"{rendered} DOI: {doi_url}"
            visible = f"{visible} DOI: {doi_url}"
        references.append(
            ContentBlock(
                block_id=f"reference-{len(references) + 1:03d}",
                kind="reference",
                markdown=rendered,
                plain_text=visible,
                source_path=source_path,
                source_locator=item.getroottree().getpath(item),
            )
        )
    return references


def _is_supporting_heading(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:[a-z]\.\s*)?(?:supporting|supplementary)\s+(?:information|data)",
            value.casefold(),
        )
    )


def _is_publisher_navigation_list(element: Any) -> bool:
    value = plain_text(element).casefold()
    return (
        "previous article in this issue" in value
        or "next article in this issue" in value
    )


def _display_equation_containers(element: Any) -> list[Any]:
    """Return outermost ScienceDirect ``e####`` display-equation containers.

    ScienceDirect's stable equation containers are a stronger display signal
    than MathML's inconsistently emitted ``display`` attribute.  Treat each
    outermost container as one equation so consecutive equations are retained,
    nested wrappers are not duplicated, and publisher equation labels remain
    attached to their math.  MathML outside these containers stays inline.
    """

    containers = [
        candidate
        for candidate in element.xpath('.//*[@id]')
        if re.fullmatch(r"e\d+", candidate.get("id") or "", re.IGNORECASE)
        and candidate.xpath('.//*[local-name() = "math"]')
    ]
    container_set = set(containers)
    return [
        container
        for container in containers
        if not any(ancestor in container_set for ancestor in container.iterancestors())
    ]


def _segmented_inline_content(
    element: Any, containers: list[Any]
) -> list[tuple[str, str, str, Any | None]]:
    """Render prose and display equations in their original DOM order."""

    container_set = set(containers)

    def append_prose(
        events: list[tuple[str, str, str, Any | None]],
        markup: str,
        plain: str,
    ) -> None:
        if not markup and not plain:
            return
        if events and events[-1][0] == "prose":
            prior = events.pop()
            events.append(("prose", prior[1] + markup, prior[2] + plain, None))
        else:
            events.append(("prose", markup, plain, None))

    def visit(node: Any) -> list[tuple[str, str, str, Any | None]]:
        events: list[tuple[str, str, str, Any | None]] = []
        append_prose(
            events,
            _safe_text(node.text, markup="markdown"),
            _safe_text(node.text, markup="plain"),
        )
        for child in node:
            if child in container_set:
                events.append(("equation", "", "", child))
            elif child.get("aria-hidden") != "true":
                if _tag(child) == "math":
                    child_events = [
                        (
                            "prose",
                            _render_mathml(child, markup="markdown"),
                            _render_mathml(child, markup="plain"),
                            None,
                        )
                    ]
                else:
                    child_events = visit(child)
                for event_kind, markup, plain, equation in child_events:
                    if event_kind == "equation":
                        events.append((event_kind, markup, plain, equation))
                    else:
                        append_prose(
                            events,
                            _wrap_inline_markup(child, markup, markup="markdown"),
                            _wrap_inline_plain(child, plain),
                        )
            append_prose(
                events,
                _safe_text(child.tail, markup="markdown"),
                _safe_text(child.tail, markup="plain"),
            )
        return events

    result: list[tuple[str, str, str, Any | None]] = []
    for event_kind, markup, plain, equation in visit(element):
        if event_kind == "equation":
            result.append((event_kind, markup, plain, equation))
            continue
        rendered = _normalize_inline_markup(markup)
        visible = normalize_space(plain)
        if rendered or visible:
            result.append((event_kind, rendered, visible, None))
    return result


def _body_sections(root: Any, source_path: str) -> tuple[list[Section], list[ContentBlock]]:
    sections: list[Section] = []
    supporting: list[ContentBlock] = []
    current: Section | None = None
    section_counts: dict[str, int] = defaultdict(int)
    block_number = 0
    supporting_mode = False
    references_terminal = False
    seen_title = not bool(root.xpath(".//h1"))
    current_heading_plain = ""
    equation_number = 0
    # A nested HTML <section> has an explicit content boundary.  Remember the
    # heading target for each structural container so prose after a child
    # section returns to its parent instead of remaining attached to the most
    # recently encountered child heading.
    section_scopes: dict[Any, tuple[str, Section | None, str]] = {}

    def structural_container(element: Any) -> Any | None:
        return next(
            (
                ancestor
                for ancestor in element.iterancestors()
                if _tag(ancestor) == "section"
            ),
            None,
        )

    def active_scope(element: Any) -> tuple[str, Section | None, str]:
        for ancestor in element.iterancestors():
            if ancestor in section_scopes:
                return section_scopes[ancestor]
        if supporting_mode:
            return ("supporting", None, "")
        if current is not None:
            return ("section", current, current_heading_plain)
        return ("excluded", None, "")

    for element in root.iter():
        tag = _tag(element)
        if tag == "h1":
            seen_title = True
            continue
        if not seen_title:
            continue
        if tag in {"h2", "h3", "h4"}:
            if references_terminal:
                continue
            heading_plain = plain_text(element)
            heading_markup = render_inline(element)
            if not heading_plain or not heading_markup:
                continue
            folded = heading_plain.casefold()
            if folded == "references":
                references_terminal = True
                supporting_mode = False
                current = None
                current_heading_plain = ""
                continue
            supporting_mode = _is_supporting_heading(heading_plain)
            if supporting_mode or folded == "graphical abstract":
                current = None
                current_heading_plain = ""
                container = structural_container(element)
                if container is not None:
                    section_scopes[container] = (
                        "supporting" if supporting_mode else "excluded",
                        None,
                        "",
                    )
                continue
            base = slugify(heading_plain)
            section_counts[base] += 1
            suffix = "" if section_counts[base] == 1 else f"-{section_counts[base]}"
            current = Section(
                section_id=f"section-{base}{suffix}",
                heading=heading_markup,
                level=int(tag[1]),
                source_path=source_path,
                source_locator=element.getroottree().getpath(element),
            )
            current_heading_plain = heading_plain
            sections.append(current)
            container = structural_container(element)
            if container is not None:
                section_scopes[container] = (
                    "section",
                    current,
                    heading_plain,
                )
            continue
        if references_terminal:
            continue
        element_id = element.get("id") or ""
        scope_kind, scoped_section, scoped_heading_plain = active_scope(element)
        sciencedirect_div = tag == "div" and (
            bool(re.fullmatch(r"p\d+", element_id, re.IGNORECASE))
            and not element.xpath(".//p")
        )
        abstract_div = (
            tag == "div"
            and scoped_section is not None
            and scoped_heading_plain.casefold() == "abstract"
            and bool(re.fullmatch(r"sp\d+", element_id, re.IGNORECASE))
        )
        keyword_div = (
            tag == "div"
            and scoped_section is not None
            and scoped_heading_plain.casefold() == "keywords"
            and bool(re.fullmatch(r"k\d+", element_id, re.IGNORECASE))
        )
        if tag not in {"p", "ul", "ol"} and not (
            sciencedirect_div or abstract_div or keyword_div
        ):
            continue
        if _inside_excluded_body_region(element):
            continue
        if tag in {"ul", "ol"} and _is_publisher_navigation_list(element):
            continue
        if tag in {"ul", "ol"} and _has_ancestor(
            element, lambda parent: _tag(parent) in {"ul", "ol"}
        ):
            continue
        if tag in {"p", "div"} and _has_ancestor(
            element, lambda parent: _tag(parent) == "li"
        ):
            continue

        display_equations = (
            _display_equation_containers(element) if tag in {"p", "div"} else []
        )

        if tag in {"ul", "ol"}:
            item_nodes = element.xpath("./li")
            items = [render_inline(item) for item in item_nodes]
            items = [re.sub(r"^[•·]\s*", "", item) for item in items if item]
            if not items:
                continue
            prefix = "1." if tag == "ol" else "-"
            rendered = "\n".join(f"{prefix} {item}" for item in items)
            plain_items = [
                re.sub(r"^[•·]\s*", "", plain_text(item))
                for item in item_nodes
                if plain_text(item)
            ]
            visible = "\n".join(f"{prefix} {item}" for item in plain_items)
            kind = "list"
            content_segments = [("prose", rendered, visible, None)]
        else:
            children = list(element)
            standalone_bold_heading = bool(
                not display_equations
                and children
                and not (element.text or "").strip()
                and all(
                    _tag(child) in {"b", "strong"}
                    and not (child.tail or "").strip()
                    for child in children
                )
            )
            if standalone_bold_heading:
                visible = plain_text(element)
                rendered = f"### {visible}"
                kind = "subsection_heading"
                content_segments = [("prose", rendered, visible, None)]
            else:
                kind = "keyword" if keyword_div else "paragraph"
                content_segments = (
                    _segmented_inline_content(element, display_equations)
                    if display_equations
                    else [("prose", render_inline(element), plain_text(element), None)]
                )
        target = (
            supporting
            if scope_kind == "supporting"
            else scoped_section.blocks
            if scope_kind == "section" and scoped_section is not None
            else None
        )
        for segment_kind, rendered, visible, equation in content_segments:
            if segment_kind == "equation":
                rendered = render_inline(equation)
                visible = plain_text(equation)
                if not rendered or not visible:
                    continue
                equation_number += 1
                block = ContentBlock(
                    block_id=f"equation-{equation_number:03d}",
                    kind="equation",
                    markdown=rendered,
                    plain_text=visible,
                    source_path=source_path,
                    source_locator=equation.getroottree().getpath(equation),
                )
            else:
                if not visible:
                    continue
                block_number += 1
                block = ContentBlock(
                    block_id=f"main-{kind}-{block_number:04d}",
                    kind=kind,
                    markdown=rendered,
                    plain_text=visible,
                    source_path=source_path,
                    source_locator=element.getroottree().getpath(element),
                )
            if target is not None:
                target.append(block)
    return sections, supporting


def _math_symbol_warnings(root: Any, source_path: str) -> list[dict[str, Any]]:
    math_nodes = [
        node
        for node in root.xpath('.//*[local-name() = "math"]')
        if "∅" in plain_text(node)
    ]
    if not math_nodes:
        return []
    article_text = plain_text(root)
    if "quantum yield" not in article_text.casefold() or not any(
        symbol in article_text for symbol in ("ϕ", "φ")
    ):
        return []
    return [
        {
            "schema_version": "1.0",
            "code": "possible_publisher_math_symbol_substitution",
            "severity": "scientific",
            "message": (
                "MathML uses U+2205 EMPTY SET in a quantum-yield formula while "
                "surrounding source text uses a phi symbol. The source glyph was "
                "retained unchanged; reviewed reconciliation must decide whether "
                "it should be ϕ."
            ),
            "source_path": source_path,
            "source_locator": node.getroottree().getpath(node),
        }
        for node in math_nodes
    ]


def extract_html(
    source: Path,
    source_path: str,
    repair_specs: Iterable[dict[str, Any]] = (),
) -> HtmlExtraction:
    if html is None:
        raise RuntimeError("lxml is required for HTML extraction")
    decoded = source.read_text(encoding="utf-8", errors="strict")
    repaired_source, repairs = _apply_repairs(decoded, repair_specs)
    parser = html.HTMLParser(encoding="utf-8", recover=True, no_network=True)
    document = html.fromstring(repaired_source, parser=parser)
    root = _content_root(document)
    _remove_terminal_crossref_appendices(root)
    title_nodes = document.xpath(".//h1[1]")
    title = plain_text(title_nodes[0]) if title_nodes else ""
    sections, supporting = _body_sections(root, source_path)
    figures = _figure_items(root, source_path)
    embedded_assets, asset_warnings = _embedded_figure_assets(
        root, figures, source_path
    )
    warnings: list[dict[str, Any]] = [
        *asset_warnings,
        *_math_symbol_warnings(root, source_path),
    ]
    remaining_placeholders = PLACEHOLDER_PATTERN.findall(repaired_source)
    if remaining_placeholders:
        warnings.append(
            {
                "schema_version": "1.0",
                "code": "unresolved_inline_equation_placeholder",
                "severity": "scientific",
                "message": (
                    f"{len(remaining_placeholders)} inline equation image placeholders remain"
                ),
                "source_path": source_path,
            }
        )
    return HtmlExtraction(
        title=title,
        bibliographic=_bibliographic_details(document),
        sections=sections,
        figures=figures,
        tables=_table_items(root, source_path),
        references=_reference_blocks(root, source_path),
        supporting_information=supporting,
        repairs=repairs,
        warnings=warnings,
        embedded_assets=embedded_assets,
    )


def table_as_html(table: TableItem) -> str:
    chunks: list[str] = []
    for part in table.parts:
        chunks.append(f'<table data-part="{html_stdlib.escape(part.part_id, quote=True)}">')
        for row_index, row in enumerate(part.rows):
            if row_index == 0:
                chunks.append("<thead>")
            elif row_index == 1:
                chunks.append("</thead>")
                chunks.append("<tbody>")
            chunks.append("<tr>")
            for cell in row:
                tag = "th" if cell.header or row_index == 0 else "td"
                attributes = []
                if cell.rowspan != 1:
                    attributes.append(f'rowspan="{cell.rowspan}"')
                if cell.colspan != 1:
                    attributes.append(f'colspan="{cell.colspan}"')
                attribute_text = f" {' '.join(attributes)}" if attributes else ""
                chunks.append(f"<{tag}{attribute_text}>{cell.markdown}</{tag}>")
            chunks.append("</tr>")
        if part.rows:
            chunks.append("</tbody>")
        chunks.append("</table>")
    return "\n".join(chunks)
