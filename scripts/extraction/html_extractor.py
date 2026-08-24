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
BIBLIOGRAPHY_HREF = re.compile(
    r"#(?:b\d+|[A-Za-z0-9_.:-]+-bib-\d+)",
    re.IGNORECASE,
)
NUMERIC_BIBLIOGRAPHY_LABEL = re.compile(
    r"(?:"
    r"\d+(?:[A-Za-z]|\([A-Za-z]\))?"
    r"(?:\s*[-–—,]\s*\d+(?:[A-Za-z]|\([A-Za-z]\))?)*"
    r"|"
    r"\[\s*\d+(?:[A-Za-z]|\([A-Za-z]\))?"
    r"(?:\s*[-–—,]\s*\d+(?:[A-Za-z]|\([A-Za-z]\))?)*\s*\]"
    r"(?:\s*\([A-Za-z]\))?"
    r")\.?"
)
ALT_CAPTION_GENERIC_BODIES = frozenset(
    {
        "refer to the image caption for details",
        "description unavailable",
        "the caption contains a description of this image",
    }
)
SCIENCEDIRECT_ARTICLE_TYPE_BADGES = frozenset(
    {
        "article",
        "communication",
        "full paper",
        "regular article",
        "research article",
        "research paper",
        "review",
        "review article",
        "short communication",
    }
)
ACS_TITLE_NOTE_HREF = re.compile(r"^#[A-Za-z0-9_.:-]+AF\d+$", re.IGNORECASE)
ACS_TITLE_NOTE_MARKERS = frozenset({"*", "†", "‡", "§", "‖", "¶"})


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


def _inside_literal_bracket_group(element: Any) -> bool:
    container = next(
        (
            ancestor
            for ancestor in element.iterancestors()
            if _tag(ancestor) in {"p", "div", "li", "td", "th", "figcaption"}
        ),
        element.getparent(),
    )
    if container is None:
        return False

    before: list[str] = []
    after: list[str] = []
    found = False

    def append(value: str | None) -> None:
        if value:
            (after if found else before).append(value)

    def walk(node: Any) -> None:
        nonlocal found
        if node is element:
            found = True
            return
        append(node.text)
        for child in node:
            walk(child)
            append(child.tail)

    walk(container)
    if not found:
        return False

    depth = 0
    for character in "".join(before):
        if character == "[":
            depth += 1
        elif character == "]" and depth:
            depth -= 1
    if not depth:
        return False
    for character in "".join(after):
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if not depth:
                return True
    return False


def _is_abbreviated_author_year_citation(element: Any, label: str) -> bool:
    """Recognize a year-only continuation in an author-year citation list.

    ScienceDirect can encode ``Weber et al., 2021, 2022`` as two bibliography
    anchors, with only ``2022`` visible in the second anchor.  A bare year also
    matches the numeric-citation grammar, so require a comma-linked preceding
    bibliography anchor whose chain begins with an author-year label.
    """

    if not re.fullmatch(r"(?:18|19|20|21)\d{2}[a-z]?", label, re.IGNORECASE):
        return False
    current = element
    for _ in range(8):
        previous = current.getprevious()
        if previous is None or _tag(previous) != "a":
            return False
        if normalize_space(previous.tail or "") != ",":
            return False
        if not (previous.get("href") or "").strip().startswith("#bib"):
            return False
        previous_label = normalize_space("".join(previous.itertext()))
        if re.search(
            r"[A-Za-z].*,\s*(?:18|19|20|21)\d{2}[a-z]?$",
            previous_label,
            re.IGNORECASE,
        ):
            return True
        if not re.fullmatch(
            r"(?:18|19|20|21)\d{2}[a-z]?", previous_label, re.IGNORECASE
        ):
            return False
        current = previous
    return False


def _wrap_inline_markup(child: Any, rendered: str, *, markup: str) -> str:
    def semantic_tag(tag: str) -> str:
        # Keep source whitespace at the semantic-tag boundary outside the tag.
        # Stripping it from ``<i>P\u2003&lt;\u2003</i>0.05`` changes the visible
        # claim from ``P < 0.05`` to ``P <0.05`` and breaks the paired plain
        # and rich representations.
        leading = " " if rendered[:1].isspace() else ""
        trailing = " " if rendered[-1:].isspace() else ""
        return f"{leading}<{tag}>{rendered.strip()}</{tag}>{trailing}"

    child_tag = _tag(child)
    if child_tag == "math" and normalize_space(rendered):
        # MathJax display containers often omit literal whitespace between the
        # preceding citation and the MathML node.
        return f" {rendered.strip()} "
    if child_tag in {"i", "em"} and normalize_space(rendered):
        return semantic_tag("em")
    if child_tag in {"b", "strong"} and normalize_space(rendered):
        return semantic_tag("strong")
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
    if BIBLIOGRAPHY_HREF.fullmatch(href) and label:
        if _tag(child.getparent()) == "sup":
            return label
        if not re.fullmatch(r"<sup>.*</sup>", label, re.DOTALL):
            return f"<sup>{label}</sup>"
        return rendered
    if href.startswith("#bib") and label:
        if not NUMERIC_BIBLIOGRAPHY_LABEL.fullmatch(label):
            return label
        if _is_abbreviated_author_year_citation(child, label):
            return label
        if _inside_literal_bracket_group(child):
            # Some ScienceDirect snapshots redundantly serialize both a
            # literal outer citation group and brackets inside every numeric
            # anchor: ``[<a>[5]</a>, <a>[6]</a>]``.  Keep the authored outer
            # group and remove only that redundant anchor-local pair.
            bracketed = re.fullmatch(
                r"\[\s*(?P<body>[^\[\]]+?)\s*\]"
                r"(?P<suffix>\s*\([A-Za-z]\))?(?P<period>\.?)",
                label,
            )
            if bracketed:
                return (
                    bracketed.group("body")
                    + (bracketed.group("suffix") or "")
                    + (bracketed.group("period") or "")
                )
            return label
        return label if label.startswith("[") else f"[{label}]"
    if href.startswith(("http://", "https://", "mailto:")) and label:
        # Preserve authored whitespace around external-link text. Legacy ACS
        # reference entries often put a DOI in a block wrapper whose anchor
        # begins with a newline. The plain renderer retains that boundary;
        # dropping it here would concatenate the citation punctuation and URL
        # in rich text (``5919.https://...``).
        leading = " " if rendered[:1].isspace() else ""
        trailing = " " if rendered[-1:].isspace() else ""
        if label == href or href.startswith("mailto:"):
            return f"{leading}{label}{trailing}"
        if markup == "markdown":
            # Parentheses are legal URL characters but also delimit the
            # extractor's small Markdown-link syntax. Percent-encode them so
            # the rich-text conversion cannot truncate a valid publisher URL.
            markdown_href = href.replace("(", "%28").replace(")", "%29")
            return f"{leading}[{label}]({markdown_href}){trailing}"
        return (
            f'{leading}<a href="{html_stdlib.escape(href, quote=True)}">'
            f"{label}</a>{trailing}"
        )
    return label


def _wrap_inline_plain(child: Any, rendered: str) -> str:
    child_tag = _tag(child)
    if child_tag == "math" and rendered.strip():
        return f" {rendered.strip()} "
    if child_tag == "a":
        href = (child.get("href") or "").strip()
        label = rendered.strip()
        if (
            href.startswith("#bib")
            and label
            and NUMERIC_BIBLIOGRAPHY_LABEL.fullmatch(label)
            and _inside_literal_bracket_group(child)
        ):
            bracketed = re.fullmatch(
                r"\[\s*(?P<body>[^\[\]]+?)\s*\]"
                r"(?P<suffix>\s*\([A-Za-z]\))?(?P<period>\.?)",
                label,
            )
            if bracketed:
                return (
                    bracketed.group("body")
                    + (bracketed.group("suffix") or "")
                    + (bracketed.group("period") or "")
                )
    if (
        child_tag == "a"
        and BIBLIOGRAPHY_HREF.fullmatch(
            (child.get("href") or "").strip()
        )
        and _tag(child.getparent()) != "sup"
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
    rendered = re.sub(r"\s+", " ", rendered)
    rendered = re.sub(r" +([,;:!?]|\.(?![.0-9]))", r"\1", rendered)
    # Adjacent publisher spans commonly express a single bold chemical name
    # with an italic H in the middle. Join equivalent adjacent semantic tags
    # without changing the underlying text or emphasis.
    rendered = rendered.replace("</strong><strong>", "")
    rendered = rendered.replace("</em><em>", "")
    # The same prescript can straddle a bold/italic boundary when an entire
    # compound heading is styled. Rejoin the identical wrapper on both sides
    # before folding the script tokens themselves.
    rendered = re.sub(
        r"<(strong|em)>((?:(?!</\1>).)*)<sup>"
        r"((?:(?!</sup>).)*)</sup></\1><sub><sup>"
        r"((?:(?!</sup>).)*)</sup></sub><\1><sup>"
        r"((?:(?!</sup>).)*)</sup>((?:(?!</\1>).)*)</\1>",
        r"<\1>\2<sup>\3<sub>\4</sub>\5</sup>\6</\1>",
        rendered,
    )
    # ACS also represents a prescript such as H₂N to the left of γ as three
    # sibling scripts: SUP(H), SUB(SUP(2)), SUP(N). Keep the authored spatial
    # meaning in one linear semantic group instead of emitting three unrelated
    # machine tokens.
    rendered = re.sub(
        r"<sup>((?:(?!</sup>).)*)</sup><sub><sup>"
        r"((?:(?!</sup>).)*)</sup></sub><sup>"
        r"((?:(?!</sup>).)*)</sup>",
        r"<sup>\1<sub>\2</sub>\3</sup>",
        rendered,
    )
    # Legacy ACS HTML can split one authored superscript across adjacent SUP
    # nodes, with an EM/STRONG wrapper around individual tokens. Coalesce only
    # zero-whitespace runs so ``γ<sup>(</sup><em><sup>R</sup></em><sup>)-CBI</sup>``
    # becomes the semantically equivalent, machine-readable single script
    # ``γ<sup>(<em>R</em>)-CBI</sup>``. Separate scientific powers remain apart.
    previous = ""
    while rendered != previous:
        previous = rendered
        rendered = re.sub(
            r"<sup>((?:(?!</sup>).)*)</sup><sup>((?:(?!</sup>).)*)</sup>",
            r"<sup>\1\2</sup>",
            rendered,
        )
        rendered = re.sub(
            r"<sup>((?:(?!</sup>).)*)</sup><(em|strong)><sup>"
            r"((?:(?!</sup>).)*)</sup></\2>",
            r"<sup>\1<\2>\3</\2></sup>",
            rendered,
        )
        rendered = re.sub(
            r"<(em|strong)><sup>((?:(?!</sup>).)*)</sup></\1><sup>"
            r"((?:(?!</sup>).)*)</sup>",
            r"<sup><\1>\2</\1>\3</sup>",
            rendered,
        )
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

    rendered = normalize_space(visit(element))
    # Mirror rich-text normalization for publisher source whitespace that is
    # serialized immediately before sentence punctuation (for example,
    # ``United States</span> , at`` in ScienceDirect acknowledgements).  This
    # is presentation whitespace, and retaining it only in the plain form
    # makes the paired representations disagree.
    rendered = re.sub(r" +([,;:!?]|\.(?![.0-9]))", r"\1", rendered)
    rendered = re.sub(
        r"\^\{([^{}]*)\}_\{\^\{([^{}]*)\}\}\^\{([^{}]*)\}",
        r"^{\1_{\2}\3}",
        rendered,
    )
    # Mirror the rich representation's zero-whitespace SUP coalescing. This
    # turns adjacent ``^{(}^{R}^{)-CBI}`` fragments into one exact script,
    # ``^{(R)-CBI}``, without joining independently spaced exponents.
    previous = ""
    while rendered != previous:
        previous = rendered
        rendered = re.sub(r"\^\{([^{}]*)\}\^\{([^{}]*)\}", r"^{\1\2}", rendered)
    return rendered


def _bibliographic_details(document: Any) -> dict[str, str]:
    """Extract visible journal citation fields from publisher front matter."""

    details: dict[str, str] = {}

    def inside_reference_region(element: Any) -> bool:
        """Return whether a broad fallback candidate is inside References."""

        for ancestor in (element, *element.iterancestors()):
            if _tag(ancestor) == "li" and any(
                re.fullmatch(
                    r"ref-id-(?:b|bib)\d+",
                    anchor.get("id") or "",
                    flags=re.IGNORECASE,
                )
                for anchor in ancestor.xpath(".//a[@id]")
            ):
                return True
            if _tag(ancestor) == "section" and any(
                plain_text(heading).casefold() == "references"
                for heading in ancestor.xpath("./h2 | ./h3 | ./h4")
            ):
                return True
        # Broad candidates such as ``article`` or layout wrappers aggregate
        # all descendant text. Skip them when that aggregate includes a
        # recognized reference region; their non-reference descendants are
        # still visited independently by the fallback loop.
        if any(
            any(
                plain_text(heading).casefold() == "references"
                for heading in section.xpath("./h2 | ./h3 | ./h4")
            )
            for section in element.xpath(".//section")
        ):
            return True
        if any(
            re.fullmatch(
                r"ref-id-(?:b|bib)\d+",
                anchor.get("id") or "",
                flags=re.IGNORECASE,
            )
            for anchor in element.xpath(".//li//a[@id]")
        ):
            return True
        return False

    aacr_issue_nodes = document.xpath(
        '//*[@id="issueInfo-IssueInfo_Article"][1]'
    )
    if aacr_issue_nodes:
        # Archived AACR pages expose the issue date as its own exact text node
        # and the page range in a compact journal citation rather than the
        # ``Pages`` labels used by other publisher dialects.  Keep this branch
        # gated by AACR's issue-information marker and strict visible grammar.
        for element in aacr_issue_nodes[0].xpath(".//*"):
            issue_date = re.fullmatch(
                r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
                plain_text(element),
            )
            if issue_date:
                details.setdefault("date", issue_date.group(1))
                break
        for element in document.xpath(".//div[em]"):
            citation = re.fullmatch(
                r".+?\s+\((\d{4})\)\s+(\d+)\s+\((\d+)\)\s*:\s*"
                r"([0-9]+\s*[-–]\s*[0-9]+)\.",
                plain_text(element),
            )
            if not citation:
                continue
            details.setdefault("volume", citation.group(2))
            details.setdefault("issue", citation.group(3))
            details.setdefault(
                "pages", re.sub(r"\s+", "", citation.group(4))
            )
            break
    publication_nodes = document.xpath('.//*[@id="publication"][1]')
    if publication_nodes:
        citation = plain_text(publication_nodes[0])
        volume = re.search(
            # Block-only wrappers can concatenate the journal title directly
            # with ``Volume`` in plain text (for example
            # ``Biosensors and BioelectronicsVolume 230``).  ``#publication``
            # itself is the strict publisher boundary, so do not require a
            # preceding word boundary here.
            r"Volume\s+(\d+)\b",
            citation,
            flags=re.IGNORECASE,
        )
        if volume:
            details["volume"] = volume.group(1)
        issue = re.search(
            r"\bIssue\s+(\d+)\b",
            citation,
            flags=re.IGNORECASE,
        )
        if issue:
            details["issue"] = issue.group(1)
        pages = re.search(
            r"\bPages\s+([0-9]+\s*[-–]\s*[0-9]+)\b",
            citation,
            flags=re.IGNORECASE,
        )
        if pages:
            details["pages"] = re.sub(r"\s+", "", pages.group(1))
        publication_date = re.search(
            # Issue banners may state either a precise online/issue date
            # (``15 June 2023``) or only a month and year (``May 2024``).
            # The exact ``#publication`` boundary keeps this broader date
            # grammar from matching dates inside article prose or references.
            r"\b((?:\d{1,2}\s+)?[A-Za-z]+\s+\d{4})\b",
            citation,
            flags=re.IGNORECASE,
        )
        if publication_date:
            details["date"] = publication_date.group(1)
        if not pages:
            # Current ScienceDirect article-number pages commonly use the
            # issue-less form ``Volume 230, 15 June 2023, 115256``.  Parse the
            # terminal identifier only inside the publisher's exact
            # ``#publication`` banner so a cited number cannot leak into the
            # containing article's metadata.
            article_number = re.search(r",\s*(\d{4,})\s*$", citation)
            if article_number:
                details["article_number"] = article_number.group(1)
    for element in document.xpath(".//*"):
        # This generic fallback must not mistake a cited article's volume or
        # page range for the containing article's bibliographic metadata.
        # Explicit publisher front-matter branches above remain authoritative.
        if inside_reference_region(element):
            continue
        value = plain_text(element)
        if not value or len(value) > 200:
            continue
        volume = re.search(
            r"\bVolume\s+(\d+)\s*,\s*Issue\s+(\d+)\b",
            value,
            flags=re.IGNORECASE,
        )
        if volume:
            details.setdefault("volume", volume.group(1))
            details.setdefault("issue", volume.group(2))
        pages = re.search(
            r"\b(?:Pages\s*:|pp?\.?)\s*([0-9]+\s*[-–]\s*[0-9]+)\b",
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
        # Current ScienceDirect snapshots can render their journal summary as
        # three short label/value rows without the older ``#publication``
        # wrapper. Require each whole visible row to match so a cited volume
        # or a body sentence mentioning an article number cannot leak into the
        # containing record's bibliography.
        current_volume = re.fullmatch(
            r"Volume\s*:\s*(?:Volume\s+)?(\d+)",
            value,
            re.IGNORECASE,
        )
        if current_volume:
            details.setdefault("volume", current_volume.group(1))
        current_article = re.fullmatch(
            r"Article\s*:\s*(\d{4,})",
            value,
            re.IGNORECASE,
        )
        if current_article:
            details.setdefault("article_number", current_article.group(1))
        first_published = re.search(
            r"\bFirst published:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})\b",
            value,
            flags=re.IGNORECASE,
        )
        if first_published:
            details.setdefault("first_published", first_published.group(1))

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


def _sciencedirect_front_matter(document: Any, source_path: str) -> list[ContentBlock]:
    """Recover useful authored front matter from a ScienceDirect banner."""

    blocks: list[ContentBlock] = []
    author_groups = document.xpath('.//*[@id="author-group"][1]')
    is_sciencedirect = bool(
        author_groups
        and (
            document.xpath('.//*[@id="publication"][1]')
            or document.xpath('//h1[@id="screen-reader-main-title"][1]')
        )
    )

    def add(label: str, value: str, source: Any) -> None:
        value = normalize_space(value)
        if not value:
            return
        blocks.append(
            ContentBlock(
                block_id=f"front-matter-html-{len(blocks) + 1:03d}",
                kind="front_matter",
                markdown=(
                    f"**{label}:** {_safe_text(value, markup='markdown')}"
                ),
                plain_text=f"{label}: {value}",
                source_path=source_path,
                source_locator=source.getroottree().getpath(source),
            )
        )

    if author_groups:
        group = author_groups[0]
        assignments: list[str] = []
        for child in group.xpath("./span | ./a"):
            markers = [
                marker
                for marker in child.xpath(
                    './/*[@id and starts-with(@id, "baff")]'
                )
                if re.fullmatch(r"baff\d+", marker.get("id") or "")
            ]
            if not markers:
                continue
            marker_values: list[str] = []
            for marker in markers:
                value = normalize_space("".join(marker.itertext()))
                if value and value not in marker_values:
                    marker_values.append(value)
            if not marker_values:
                continue
            visible = normalize_space("".join(child.itertext()))
            name = visible
            for marker in reversed(marker_values):
                name = re.sub(
                    rf"\s*{re.escape(marker)}\s*$", "", name, count=1
                ).strip()
            if name:
                assignments.append(f"{name} ({','.join(marker_values)})")
        if assignments:
            add("Affiliation assignments", "; ".join(assignments), group)

        author_note_assignments: list[str] = []
        author_note_markers: list[str] = []
        for child in group.xpath("./span | ./a"):
            markers = child.xpath('.//*[@id and starts-with(@id, "bfn")][1]')
            if not markers:
                continue
            marker_id = markers[0].get("id") or ""
            if not re.fullmatch(r"bfn\d+", marker_id):
                continue
            marker = normalize_space("".join(markers[0].itertext()))
            visible = normalize_space("".join(child.itertext()))
            name = re.sub(
                rf"\s*{re.escape(marker)}\s*$", "", visible, count=1
            ).strip()
            if name and marker:
                author_note_assignments.append(f"{name} ({marker})")
            if marker_id not in author_note_markers:
                author_note_markers.append(marker_id)
        if author_note_assignments:
            add(
                "Author note assignments",
                "; ".join(author_note_assignments),
                group,
            )

        for marker_id in author_note_markers:
            notes = document.xpath(
                f'.//dl[dt/a[@href="#{marker_id}"] and dd][1]'
            )
            if not notes:
                continue
            note = notes[0]
            marker = normalize_space(
                "".join(note.xpath("./dt[1]")[0].itertext())
            ) or marker_id[3:]
            value = plain_text(note.xpath("./dd[1]")[0])
            if marker and value:
                add(f"Author note {marker}", value, note)

        for affiliation in group.xpath("./dl[dt and dd]"):
            marker = normalize_space("".join(affiliation.xpath("./dt[1]")[0].itertext()))
            value = plain_text(affiliation.xpath("./dd[1]")[0])
            if marker and value:
                add(f"Affiliation {marker}", value, affiliation)

    banners = document.xpath('.//*[@id="banner"][1]')
    if banners:
        for paragraph in banners[0].xpath(".//p"):
            value = plain_text(paragraph)
            if re.match(r"^(?:Received|Submitted|Available online)\b", value):
                add("Article history", value, paragraph)

    license_links = document.xpath(
        './/a[contains(translate(@href, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", '
        '"abcdefghijklmnopqrstuvwxyz"), "creativecommons.org/licenses/")][1]'
    )
    if license_links:
        license_link = license_links[0]
        href = normalize_space(license_link.get("href") or "")
        if href:
            add("License", f"Creative Commons license: {href}", license_link)

    if is_sciencedirect:
        for element in document.xpath(".//*[self::div or self::span]"):
            if plain_text(element).casefold() == "open access":
                add("Access", "Open access", element)
                break

    for element in document.xpath(".//*[self::span or self::p or self::div]"):
        value = plain_text(element)
        is_explicit = re.fullmatch(
            r"Copyright\s+©\s*\d{4}\b.*", value, flags=re.IGNORECASE
        )
        is_sciencedirect_short = is_sciencedirect and re.fullmatch(
            r"©\s*\d{4}\b.*", value, flags=re.IGNORECASE
        )
        if is_explicit or is_sciencedirect_short:
            add("Copyright", value, element)
            break
    return blocks


def _acs_front_matter(document: Any, source_path: str) -> list[ContentBlock]:
    """Recover legacy ACS author and article-information metadata.

    Archived ACS pages expose corresponding-author notes through ``reveal-id``
    controls and place publication dates, publisher, and ISSNs in an unlabelled
    information panel.  Those details are authored bibliographic content, not
    the adjacent search/share controls.
    """

    corresponding_controls = document.xpath(
        './/a[@reveal-id and contains('
        'translate(@aria-label, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", '
        '"abcdefghijklmnopqrstuvwxyz"), "corresponding author note")]'
    )
    publisher_nodes = [
        node
        for node in document.xpath(".//*[self::div or self::span or self::p]")
        if re.fullmatch(
            r"Publisher:\s*American Chemical Society",
            plain_text(node),
            flags=re.IGNORECASE,
        )
    ]
    if not corresponding_controls and not publisher_nodes:
        return []

    blocks: list[ContentBlock] = []
    seen: set[tuple[str, str]] = set()

    def add(label: str, value: str, source: Any) -> None:
        value = normalize_space(value)
        key = (label.casefold(), value.casefold())
        if not value or key in seen:
            return
        seen.add(key)
        blocks.append(
            ContentBlock(
                block_id=f"front-matter-acs-{len(blocks) + 1:03d}",
                kind="front_matter",
                markdown=f"**{label}:** {_safe_text(value, markup='markdown')}",
                plain_text=f"{label}: {value}",
                source_path=source_path,
                source_locator=source.getroottree().getpath(source),
            )
        )

    for control in corresponding_controls:
        note_id = normalize_space(control.get("reveal-id") or "")
        if not note_id:
            continue
        note_nodes = [
            node
            for node in document.xpath('.//*[@content-id]')
            if normalize_space(node.get("content-id") or "") == note_id
        ]
        note = plain_text(note_nodes[0]) if note_nodes else ""
        author = ""
        for ancestor in control.iterancestors("div"):
            names = ancestor.xpath('.//a[@aria-haspopup="true"][1]')
            if names:
                author = plain_text(names[0])
                if author:
                    break
        if not author:
            continue
        detail = re.sub(
            r"^Corresponding author\.\s*", "", note, flags=re.IGNORECASE
        ).strip()
        value = f"{author}. {detail}" if detail else author
        add("Corresponding author", value, note_nodes[0] if note_nodes else control)

    if publisher_nodes:
        value = re.sub(
            r"^Publisher:\s*", "", plain_text(publisher_nodes[0]), flags=re.IGNORECASE
        )
        add("Publisher", value, publisher_nodes[0])

    history: list[str] = []
    history_source: Any | None = None
    for label in ("Received", "Published Online", "Published in Issue"):
        candidates = [
            node
            for node in document.xpath(".//span")
            if plain_text(node).casefold() == f"{label}:".casefold()
        ]
        if not candidates:
            continue
        value_node = candidates[0].getnext()
        value = plain_text(value_node) if value_node is not None else ""
        if not value:
            continue
        history.append(f"{label}: {value}")
        if history_source is None:
            parent = candidates[0].getparent()
            history_source = parent if parent is not None else candidates[0]
    if history and history_source is not None:
        add("Article history", "; ".join(history), history_source)

    for label in ("Online ISSN", "Print ISSN"):
        pattern = re.compile(rf"{re.escape(label)}:\s*(\S+)", flags=re.IGNORECASE)
        for node in document.xpath(".//*[self::div or self::span or self::p]"):
            match = pattern.fullmatch(plain_text(node))
            if match:
                add(label, match.group(1), node)
                break

    return blocks


def _wiley_front_matter(document: Any, source_path: str) -> list[ContentBlock]:
    """Recover authored details from Wiley's duplicated author popovers.

    Wiley commonly supplies the same author-detail panels twice (``am*`` and
    ``a*`` IDs).  The normal body walker correctly excludes those interface
    panels, but their affiliations, correspondence, and ORCID identifiers are
    publication content.  Extract the relationships once without retaining
    the surrounding "Search for more papers" controls.
    """

    controls = document.xpath(
        './/a[@id and @aria-controls and starts-with(@href, "/authored-by/")]'
    )

    affiliations: list[tuple[str, str, Any]] = []
    correspondences: list[tuple[str, Any]] = []
    orcids: list[tuple[str, str, Any]] = []
    seen_affiliations: set[tuple[str, str]] = set()
    seen_correspondence: set[str] = set()
    seen_orcids: set[tuple[str, str]] = set()

    for control in controls:
        author = plain_text(control)
        panel_id = normalize_space(control.get("aria-controls") or "")
        if not author or not panel_id:
            continue
        panels = document.xpath('.//*[@id=$panel_id]', panel_id=panel_id)
        if len(panels) != 1:
            continue
        panel = panels[0]
        paragraphs = panel.xpath("./p")
        correspondence_index: int | None = None
        for index, paragraph in enumerate(paragraphs):
            value = plain_text(paragraph)
            folded = value.casefold()
            if folded == "correspondence":
                correspondence_index = index
                break
            if not value or folded in {"corresponding author", author.casefold()}:
                continue
            if folded.startswith(("email:", "orcid")):
                continue
            key = (author, value)
            if key not in seen_affiliations:
                seen_affiliations.add(key)
                affiliations.append((author, value, paragraph))

        if correspondence_index is not None:
            values = [
                plain_text(paragraph)
                for paragraph in paragraphs[correspondence_index + 1 :]
                if plain_text(paragraph)
            ]
            value = normalize_space(" ".join(values))
            if value and value not in seen_correspondence:
                seen_correspondence.add(value)
                correspondences.append((value, paragraphs[correspondence_index]))
        elif any(
            plain_text(paragraph).casefold() == "corresponding author"
            for paragraph in paragraphs
        ):
            # Legacy Wiley popovers can put the correspondence sentence as a
            # direct text node followed by a mailto link rather than in the
            # paragraph structure used by newer pages.  Recover that authored
            # content without retaining the adjacent author-search control.
            direct_text = [
                normalize_space(value)
                for value in panel.xpath("./text()")
                if normalize_space(value)
            ]
            mailto_links = panel.xpath(
                './/a[starts-with(translate(@href, "MAILTO", "mailto"), "mailto:")]'
            )
            emails = [
                normalize_space((link.get("href") or "")[7:]) or plain_text(link)
                for link in mailto_links
            ]
            value = normalize_space(" ".join([*direct_text, *emails]))
            if value and value not in seen_correspondence:
                seen_correspondence.add(value)
                correspondences.append((value, panel))

        for link in panel.xpath('.//a[contains(translate(@href, "ORCID", "orcid"), "orcid.org/")]'):
            href = normalize_space(link.get("href") or "")
            if not re.fullmatch(
                r"https://orcid\.org/\d{4}-\d{4}-\d{4}-[\dX]{4}",
                href,
                flags=re.IGNORECASE,
            ):
                continue
            key = (author, href)
            if key not in seen_orcids:
                seen_orcids.add(key)
                orcids.append((author, href, link))

    # Older archived Wiley HTML can contain a simple author list rather than
    # the newer author-popover controls.  Its author-specific affiliation and
    # correspondence paragraphs are publication content, not navigation.
    if not controls:
        author_sections = document.xpath(
            './/header//section[translate(@id, "AUTHORS", "authors")="authors"]'
        )
        if author_sections:
            for item in author_sections[0].xpath("./ul/li"):
                names = item.xpath("./strong[1]")
                author = plain_text(names[0]) if names else ""
                if not author:
                    continue
                is_corresponding = any(
                    "corresponding author" in plain_text(span).casefold()
                    for span in item.xpath("./span")
                )
                for paragraph in item.xpath("./p"):
                    value = plain_text(paragraph)
                    if not value:
                        continue
                    if re.fullmatch(r"[^\s@]+@[^\s@]+", value):
                        if is_corresponding and value not in seen_correspondence:
                            seen_correspondence.add(value)
                            correspondences.append((f"{author}: {value}", paragraph))
                        continue
                    key = (author, value)
                    if key not in seen_affiliations:
                        seen_affiliations.add(key)
                        affiliations.append((author, value, paragraph))

    blocks: list[ContentBlock] = []
    seen_blocks: set[tuple[str, str]] = set()

    def add(label: str, value: str, source: Any) -> None:
        label = normalize_space(label)
        value = normalize_space(value)
        key = (label.casefold(), value.casefold())
        if not label or not value or key in seen_blocks:
            return
        seen_blocks.add(key)
        blocks.append(
            ContentBlock(
                block_id=f"front-matter-html-{len(blocks) + 1:03d}",
                kind="front_matter",
                markdown=f"**{label}:** {_safe_text(value, markup='markdown')}",
                plain_text=f"{label}: {value}",
                source_path=source_path,
                source_locator=source.getroottree().getpath(source),
            )
        )

    for author, value, source in affiliations:
        add(f"Affiliation ({author})", value, source)
    for value, source in correspondences:
        add("Correspondence", value, source)
    for author, value, source in orcids:
        add(f"ORCID ({author})", value, source)

    # Front-matter notes use the same ``note`` token as body/table notes and
    # title controls.  Accept only nonempty note divs outside semantic article
    # sections and other content containers; this retains authored contribution
    # notes without leaking footnotes or interface controls.
    top_note_id = re.compile(
        r"(?:[A-Za-z0-9_]+-)?note-\d+",
        flags=re.IGNORECASE,
    )
    for note in document.xpath(".//div[@id]"):
        note_id = normalize_space(note.get("id") or "")
        if not top_note_id.fullmatch(note_id):
            continue
        if note.xpath(
            "ancestor::section | ancestor::table | ancestor::figure | ancestor::h1"
        ):
            continue
        add("Author note", plain_text(note), note)

    # Wiley's information panel is outside the article body but contains
    # authored publication metadata.  Parse only the known panel and known
    # headed sections.  In particular, do not retain Crossmark/update controls
    # or unrelated recommendation/navigation sections.
    for panel in document.xpath('.//*[@id="pane-pcw-details"]'):
        for section in panel.xpath(".//section[./h3[1]]"):
            heading = plain_text(section.xpath("./h3[1]")[0]).casefold()
            if heading == "details":
                for paragraph in section.xpath("./p"):
                    value = plain_text(paragraph)
                    if value.startswith("©") or value.casefold().startswith(
                        "copyright"
                    ):
                        add("Copyright", value, paragraph)
            elif heading == "research funding":
                values = [
                    plain_text(item)
                    for item in section.xpath("./ul/li")
                    if plain_text(item)
                ]
                if values:
                    add("Research funding", "; ".join(values), section)
            elif heading == "keywords":
                values = [
                    plain_text(item)
                    for item in section.xpath("./div/ul/li")
                    if plain_text(item)
                ]
                if values:
                    add("Keywords", "; ".join(values), section)
            elif heading == "publication history":
                values = [
                    plain_text(item)
                    for item in section.xpath("./ul/li")
                    if plain_text(item)
                ]
                if values:
                    add("Publication history", "; ".join(values), section)
    return blocks


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


def _remove_sciencedirect_table_of_contents_navigation(root: Any) -> None:
    """Remove the exact ScienceDirect article-outline navigation widget.

    The archived widget repeats interface labels such as Outline, Figures, and
    Tables inside the article root.  Both attributes are required so authored
    headings and near-miss navigation containers remain untouched.
    """

    for navigation in root.xpath(
        './/*[@aria-label="Table of contents" and @role="navigation"]'
    ):
        parent = navigation.getparent()
        if parent is not None:
            parent.remove(navigation)


def _remove_legacy_acs_duplicate_citation_parentheses(document: Any) -> None:
    """Collapse the redundant nested parentheses in one legacy ACS dialect.

    These snapshots encode a visible citation as ``(<em> (<a ...>1</a>)</em>)``.
    The PDF and intended browser text contain one parenthetical group, not two.
    Require the ACS-only ``ref-data-modal-source-id`` anchors, a numeric-only
    emphasized group, and literal surrounding parentheses before touching the
    in-memory DOM.
    """

    for emphasis in document.xpath(".//em | .//i"):
        anchors = emphasis.xpath(".//a[@ref-data-modal-source-id]")
        label = plain_text(emphasis)
        if not anchors or not re.fullmatch(r"\(\s*\d+(?:\s*,\s*\d+)*\s*\)", label):
            continue
        parent = emphasis.getparent()
        if parent is None:
            continue
        previous = emphasis.getprevious()
        before = previous.tail if previous is not None else parent.text
        after = emphasis.tail or ""
        first_text = emphasis.text or ""
        last_tail = anchors[-1].tail or ""
        if (
            not (before or "").rstrip().endswith("(")
            or not re.match(r"^\s*\)", after)
            or not re.search(r"\s*\(\s*$", first_text)
            or not re.match(r"^\s*\)", last_tail)
        ):
            continue
        emphasis.text = re.sub(r"\s*\(\s*$", "", first_text, count=1)
        anchors[-1].tail = re.sub(r"^\s*\)", "", last_tail, count=1)


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

    label_pattern = r"^\s*(Fig(?:ure)?|Scheme)\.?\s*(\d+)\b"
    label_hint = ""
    caption_containers = figure.xpath(".//figcaption[1]")
    if caption_containers:
        container = caption_containers[0]
        paragraphs = container.xpath(".//p")
        if not paragraphs:
            # Wiley captions without paragraph tags place their visible body
            # beside a label/download-control div.  Falling back to the whole
            # figcaption leaks "Open in figure viewer" and "PowerPoint" into
            # canonical content, so retain only non-control direct children.
            paragraphs = [
                child
                for child in container
                if not any(
                    plain_text(anchor).casefold()
                    in {"open in figure viewer", "powerpoint"}
                    for anchor in child.xpath(".//a")
                )
                and not any(
                    re.fullmatch(
                        r"(?:fig(?:ure)?|scheme)\.?\s*\d+\.?",
                        plain_text(strong),
                        flags=re.IGNORECASE,
                    )
                    for strong in child.xpath(".//strong")
                )
            ] or [container]
        strong = container.xpath(".//strong[1]")
        label_hint = plain_text(strong[0]) if strong else ""
    else:
        # ScienceDirect rendered DOM uses ``cn####`` spans rather than the
        # semantic ``figcaption`` element. Some archived snapshots instead
        # put the same authored caption directly in a ``p`` whose first span
        # is ``Fig. N`` or ``Scheme N``.
        paragraphs = []
        # A newer ScienceDirect dialect links the image to a local caption
        # container with ``aria-describedby``.  That container commonly
        # splits the title into a ``p`` and the complete multi-panel legend
        # into a sibling ``div``.  Selecting only the paragraph silently
        # truncates the authored caption, so retain every non-empty direct
        # prose child of the exact locally referenced container, in order.
        for image in figure.xpath('.//img[@aria-describedby]'):
            described_by = normalize_space(image.get("aria-describedby") or "")
            for caption_id in described_by.split():
                containers = figure.xpath(
                    './/*[@id=$caption_id]', caption_id=caption_id
                )
                if len(containers) != 1:
                    continue
                caption_parts = [
                    child
                    for child in containers[0].xpath('./p | ./div')
                    if plain_text(child)
                ]
                if caption_parts:
                    paragraphs = caption_parts
                    break
            if paragraphs:
                break
        if not paragraphs:
            paragraphs = figure.xpath(
                './/*[@id and starts-with(@id, "cn")]//p'
            )
        if not paragraphs:
            paragraphs = [
                paragraph
                for paragraph in figure.xpath(".//p")
                if re.match(
                    label_pattern,
                    plain_text(paragraph),
                    flags=re.IGNORECASE,
                )
            ]
        if not paragraphs:
            # Older ACS/JACS snapshots can serialize an authored caption as
            # ``figure > div > (div[label], div[p[body]])`` without a
            # semantic ``figcaption``. Require the exact two-child shape and
            # a numbered label so image controls and unrelated prose cannot
            # be promoted into the caption stream.
            for candidate in figure.xpath("./div"):
                children = candidate.xpath("./div")
                if len(children) != 2:
                    continue
                candidate_label = plain_text(children[0])
                if not re.fullmatch(
                    rf"{label_pattern}\.?\s*",
                    candidate_label,
                    flags=re.IGNORECASE,
                ):
                    continue
                candidate_paragraphs = children[1].xpath(".//p")
                if not candidate_paragraphs:
                    continue
                label_hint = candidate_label
                paragraphs = candidate_paragraphs
                break
        if not paragraphs:
            # Some archived AACR figures omit semantic caption markup and put
            # the complete authored caption in the sole image alt attribute.
            # Keep this fallback deliberately narrow so generic accessibility
            # text and file-name alts cannot displace publisher caption DOM.
            images = figure.xpath(".//img[@alt]")
            if len(images) == 1:
                alt = normalize_space(images[0].get("alt") or "")
                alt_start = re.match(label_pattern, alt, flags=re.IGNORECASE)
                if alt_start:
                    body = normalize_space(
                        re.sub(r"^\s*\.\s*", "", alt[alt_start.end() :], count=1)
                    )
                    generic_key = body.casefold().strip(" \t\r\n.;:,!?")
                    if (
                        re.search(r"\w", body, flags=re.UNICODE)
                        and generic_key not in ALT_CAPTION_GENERIC_BODIES
                    ):
                        return (
                            alt_start.group(0).strip(),
                            _safe_text(body, markup="markdown"),
                            _safe_text(body, markup="plain"),
                        )
        if not label_hint:
            caption_start = (
                re.match(label_pattern, plain_text(paragraphs[0]), flags=re.IGNORECASE)
                if paragraphs
                else None
            )
            label_hint = caption_start.group(0).strip() if caption_start else ""
    caption_markdown = "\n\n".join(
        value for value in (render_inline(paragraph) for paragraph in paragraphs) if value
    )
    caption_plain = " ".join(
        value for value in (plain_text(paragraph) for paragraph in paragraphs) if value
    )
    if not caption_containers and label_hint:
        # The label is represented separately in record.json. Remove only the
        # exact leading label and its sentence punctuation from the caption.
        prefix = rf"^\s*{re.escape(label_hint)}\s*\.?\s*"
        caption_markdown = re.sub(prefix, "", caption_markdown, count=1)
        caption_plain = re.sub(prefix, "", caption_plain, count=1)
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
        label_pattern = r"^\s*(Fig(?:ure)?|Scheme)\.?\s*(\d+)\b"
        caption_label = re.match(
            label_pattern, label_hint, flags=re.IGNORECASE
        ) or re.match(label_pattern, caption_plain, flags=re.IGNORECASE)
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


def _table_cell_content(cell: Any) -> tuple[str, str]:
    """Preserve a whole-cell authored list as readable inline-safe content."""

    direct_lists = cell.xpath("./ul | ./ol")
    if (
        len(direct_lists) == 1
        and not normalize_space(cell.text or "")
        and all(
            child is direct_lists[0]
            or (
                not plain_text(child)
                and not normalize_space(child.tail or "")
            )
            for child in cell
        )
        and not normalize_space(direct_lists[0].tail or "")
    ):
        list_node = direct_lists[0]
        item_nodes = list_node.xpath("./li")
        rendered_items: list[str] = []
        plain_items: list[str] = []
        ordered = _tag(list_node) == "ol"
        for index, item in enumerate(item_nodes, 1):
            rendered = re.sub(
                r"^[•·]\s*", "", render_inline(item, markup="html")
            ).strip()
            visible = re.sub(r"^[•·]\s*", "", plain_text(item)).strip()
            if not rendered or not visible:
                return plain_text(cell), render_inline(cell, markup="html")
            marker = f"{index}." if ordered else "•"
            rendered_items.append(f"{marker} {rendered}")
            plain_items.append(f"{marker} {visible}")
        if rendered_items:
            return "\n".join(plain_items), "<br>".join(rendered_items)
    return plain_text(cell), render_inline(cell, markup="html")


def _parse_rows(table: Any) -> list[list[TableCell]]:
    rows: list[list[TableCell]] = []
    row_nodes = table.xpath("./thead/tr | ./tbody/tr | ./tfoot/tr | ./tr")
    parsed_row_nodes: list[Any] = []
    for row in row_nodes:
        cells: list[TableCell] = []
        for cell in row.xpath("./th | ./td"):
            cell_text, cell_markup = _table_cell_content(cell)
            cells.append(
                TableCell(
                    text=cell_text,
                    markdown=cell_markup,
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
        if not captions:
            # Some ScienceDirect snapshots use a ``cap####`` wrapper for the
            # authored table title. Keep this fallback scoped to a recognized
            # table container so figure captions and other global cap IDs can
            # never become table titles.
            captions = container.xpath(
                './/*[@id and starts-with(@id, "cap")]//p[1]'
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
        # Current ScienceDirect snapshots can put an unmarked authored table
        # note (most often an abbreviations key) in a nested ``tspara####``
        # div after the table wrapper.  Its exact ID and recognized table
        # container provide the necessary scope; broad trailing-div capture
        # would risk absorbing adjacent article prose.
        for note in container.xpath('.//*[@id]'):
            if not re.fullmatch(
                r"tspara\d+", note.get("id") or "", re.IGNORECASE
            ):
                continue
            if note is title_node or note.xpath("ancestor::header"):
                continue
            note_markdown = render_inline(note)
            note_plain = plain_text(note)
            if note_markdown and note_plain:
                footnotes_markdown.append(note_markdown)
                footnotes_plain.append(note_plain)
        # Wiley archives can serialize a numbered table note as ``li#fnN``
        # beneath the recognized table container, with its authored marker in
        # the first direct-child span.  Require that exact ID/shape and scope
        # so article references and unrelated page lists are never captured.
        known_footnotes = set(zip(footnotes_plain, footnotes_markdown, strict=True))
        for note in container.xpath('.//li[@id]'):
            if not re.fullmatch(r"fn\d+", note.get("id") or "", re.IGNORECASE):
                continue
            label_nodes = note.xpath("./span[1]")
            if len(label_nodes) != 1:
                continue
            label_plain = plain_text(label_nodes[0]).strip("[]() .:")
            label_markdown = render_inline(label_nodes[0])
            note_plain = plain_text(note)
            note_markdown = render_inline(note)
            if not label_plain or not note_plain.startswith(plain_text(label_nodes[0])):
                continue
            definition_plain = note_plain[len(plain_text(label_nodes[0])) :].lstrip(
                " .:"
            )
            if label_markdown and note_markdown.startswith(label_markdown):
                definition_markdown = note_markdown[len(label_markdown) :].lstrip(
                    " .:"
                )
            else:
                definition_markdown = definition_plain
            if not definition_plain or not definition_markdown:
                continue
            footnote_plain = f"[{label_plain}] {definition_plain}"
            footnote_markdown = f"[{label_plain}] {definition_markdown}"
            pair = (footnote_plain, footnote_markdown)
            if pair in known_footnotes:
                continue
            footnotes_plain.append(footnote_plain)
            footnotes_markdown.append(footnote_markdown)
            known_footnotes.add(pair)
        # ScienceDirect and several older publisher archives serialize table
        # notes as a direct-child definition list.  Keep this scoped to an
        # already-recognized table container so affiliation and glossary lists
        # elsewhere cannot be mistaken for table notes.
        for term in container.xpath("./dl/dt"):
            definitions = term.xpath("following-sibling::*[1][self::dd]")
            if not definitions:
                continue
            definition = definitions[0]
            label_markdown = render_inline(term)
            label_plain = plain_text(term)
            definition_markdown = render_inline(definition)
            definition_plain = plain_text(definition)
            if not all(
                (
                    label_markdown,
                    label_plain,
                    definition_markdown,
                    definition_plain,
                )
            ):
                continue
            footnote_markdown = f"[{label_markdown}] {definition_markdown}"
            footnote_plain = f"[{label_plain}] {definition_plain}"
            pair = (footnote_plain, footnote_markdown)
            if pair in known_footnotes:
                continue
            footnotes_plain.append(footnote_plain)
            footnotes_markdown.append(footnote_markdown)
            known_footnotes.add(pair)
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
    matched_pair: tuple[Any, Any] | None = None
    for anchor in item.xpath(".//a[@id]"):
        anchor_match = re.fullmatch(
            r"ref-id-(?:b|bib)(\d+)",
            anchor.get("id") or "",
            flags=re.IGNORECASE,
        )
        if not anchor_match:
            continue
        for span in item.xpath(".//span[@id]"):
            span_match = re.fullmatch(
                r"(?:h|sref)(\d+)",
                span.get("id") or "",
                flags=re.IGNORECASE,
            )
            if span_match and int(span_match.group(1)) == int(anchor_match.group(1)):
                matched_pair = (anchor, span)
                break
        if matched_pair is not None:
            break
    if matched_pair is None:
        return None

    number_anchor, content = matched_pair
    label = plain_text(number_anchor).rstrip(". ")
    body_divs = [
        div
        for div in content.xpath("./div")
        if not (div.get("lang") and div.xpath(".//a"))
    ]
    if not body_divs:
        return None

    def render_body_div(div: Any, renderer: Any) -> str:
        """Preserve boundaries between nested ScienceDirect reference blocks."""

        children = list(div)
        nested_blocks_only = (
            bool(children)
            and all(_tag(child) == "div" for child in children)
            and not normalize_space(div.text or "")
            and all(not normalize_space(child.tail or "") for child in children)
        )
        if nested_blocks_only:
            return " ".join(
                value for value in (renderer(child) for child in children) if value
            )
        return renderer(div)

    rendered_body = " ".join(
        value
        for value in (render_body_div(div, render_inline) for div in body_divs)
        if value
    )
    visible_body = " ".join(
        value
        for value in (render_body_div(div, plain_text) for div in body_divs)
        if value
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


def _aacr_flat_div_references(
    heading: Any, source_path: str
) -> list[ContentBlock] | None:
    """Parse AACR's target-scoped flat ``div`` bibliography dialect.

    Older AACR snapshots place an unsectioned ``References`` heading beside a
    ``<heading-id>-content`` target.  Each reference is a direct child of one
    wrapper and stores its numeric label and body in a fixed nested ``div``
    shape.  Do not fall back to a broad ancestor here: some pages wrap the
    whole interface in one ``section``, whose navigation ``li`` elements are
    not references.

    The strict structure and contiguous-label gate keeps this branch local to
    the observed AACR dialect.  A malformed or different target returns
    ``None`` so existing publisher-specific parsing remains unchanged.
    """

    heading_id = (heading.get("id") or "").strip()
    if not heading_id:
        return None
    target_id = f"{heading_id}-content"
    targets = heading.getroottree().xpath(
        '//*[@id=$target_id]', target_id=target_id
    )
    if len(targets) != 1:
        return None
    items = targets[0].xpath("./div/div")
    if not items:
        return None

    references: list[ContentBlock] = []
    for expected_number, item in enumerate(items, start=1):
        content_nodes = item.xpath("./div/div")
        label_nodes = item.xpath("./div/div/span[1]")
        if len(content_nodes) != 1 or len(label_nodes) != 1:
            return None
        label = plain_text(label_nodes[0]).rstrip(". ")
        if not label.isdigit() or int(label) != expected_number:
            return None
        # AACR normally stores one DIV body after the numeric label. Legacy
        # ACS uses the same target-scoped outer shape but places each authored
        # lettered subentry in its own sibling DIV. Clone the common content
        # wrapper and remove only the numeric label so all such subentries are
        # retained instead of silently keeping only the first one.
        body = copy.deepcopy(content_nodes[0])
        cloned_labels = body.xpath("./span[1]")
        if len(cloned_labels) != 1:
            return None
        body.remove(cloned_labels[0])
        # Linkout controls are publisher interface, not bibliography text.
        # Older ACS snapshots nest them inside the same flat reference body as
        # the citation, so remove only anchors with exact observed control
        # labels. DOI anchors and all authored citation content remain.
        linkout_labels = {
            "crossref",
            "google scholar",
            "openurl",
            "pubmed",
            "search ads",
        }
        for anchor in body.xpath(".//a"):
            label_text = plain_text(anchor).casefold()
            if label_text not in linkout_labels:
                continue
            removable = anchor
            parent = removable.getparent()
            while (
                parent is not None
                and parent is not body
                and _tag(parent) in {"div", "span"}
                and plain_text(parent).casefold() == label_text
            ):
                removable = parent
                parent = removable.getparent()
            if parent is not None:
                parent.remove(removable)
        body_parts = [child for child in body if plain_text(child)]
        if not body_parts:
            return None
        # Block siblings express separate bibliography subentries even when
        # the legacy source omits literal whitespace between closing/opening
        # DIV tags. Preserve that authored boundary in both representations.
        rendered_body = " ".join(render_inline(part) for part in body_parts)
        visible_body = " ".join(plain_text(part) for part in body_parts)
        rendered_body = re.sub(r"(\([a-z]\))(?=[A-Z])", r"\1 ", rendered_body)
        visible_body = re.sub(r"(\([a-z]\))(?=[A-Z])", r"\1 ", visible_body)
        if not rendered_body or not visible_body:
            return None
        references.append(
            ContentBlock(
                block_id=f"reference-{expected_number:03d}",
                kind="reference",
                markdown=f"{expected_number}. {rendered_body}",
                plain_text=f"{expected_number}. {visible_body}",
                source_path=source_path,
                source_locator=item.getroottree().getpath(item),
            )
        )
    return references


def _reference_blocks(root: Any, source_path: str) -> list[ContentBlock]:
    headings = [
        heading
        for heading in root.xpath(".//h2")
        if normalize_space(" ".join(heading.itertext())).casefold() == "references"
    ]
    if not headings:
        return []
    for heading in headings:
        aacr_references = _aacr_flat_div_references(heading, source_path)
        if aacr_references is not None:
            return aacr_references
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
        if label and visible.startswith(label):
            normalized_label = normalize_space(label).rstrip(". ")
            visible = f"{normalized_label}. {remainder}"
            rendered_label = render_inline(direct_spans[0])
            if rendered.startswith(rendered_label):
                rendered_remainder = rendered[len(rendered_label) :].lstrip()
                rendered = f"{normalized_label}. {rendered_remainder}"
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
        bool(re.search(r"previous article in (?:this )?issue", value))
        or bool(re.search(r"next article in (?:this )?issue", value))
    )


def _display_equation_containers(element: Any) -> list[Any]:
    """Return outermost recognized ScienceDirect display-equation containers.

    ScienceDirect archives use either an ``e####`` wrapper containing MathML or
    a nonempty ``span#ufd####`` wrapper containing source-rendered notation.
    Treat each outermost container as one equation so consecutive equations are
    retained, nested wrappers are not duplicated, and publisher equation labels
    remain attached to their math.  MathML outside these containers stays inline.
    """

    containers = [
        candidate
        for candidate in element.xpath('.//*[@id]')
        if (
            (
                re.fullmatch(
                    r"e\d+", candidate.get("id") or "", re.IGNORECASE
                )
                and candidate.xpath('.//*[local-name() = "math"]')
            )
            or (
                _tag(candidate) == "span"
                and re.fullmatch(r"ufd\d+", candidate.get("id") or "")
                and bool(plain_text(candidate))
            )
        )
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

    def has_registered_scope(element: Any) -> bool:
        return any(ancestor in section_scopes for ancestor in element.iterancestors())

    def add_synthetic_introduction(container: Any) -> Section:
        nonlocal current, current_heading_plain
        base = "introduction"
        section_counts[base] += 1
        suffix = "" if section_counts[base] == 1 else f"-{section_counts[base]}"
        current = Section(
            section_id=f"section-{base}{suffix}",
            heading="Introduction",
            level=2,
            source_path=source_path,
            source_locator=container.getroottree().getpath(container),
        )
        current_heading_plain = "Introduction"
        sections.append(current)
        section_scopes[container] = (
            "section",
            current,
            current_heading_plain,
        )
        return current

    def abbreviation_table_content(element: Any) -> tuple[str, str] | None:
        rendered_items: list[str] = []
        plain_items: list[str] = []
        for row in element.xpath("./thead/tr | ./tbody/tr | ./tfoot/tr | ./tr"):
            cells = row.xpath("./th | ./td")
            if len(cells) != 2:
                return None
            term_markup = render_inline(cells[0])
            definition_markup = render_inline(cells[1])
            term_plain = plain_text(cells[0])
            definition_plain = plain_text(cells[1])
            if not all((term_markup, definition_markup, term_plain, definition_plain)):
                return None
            separator = "" if term_plain.endswith((":", "：")) else ":"
            rendered_items.append(
                f"- <strong>{term_markup}</strong>{separator} {definition_markup}"
            )
            plain_items.append(
                f"- {term_plain}{separator} {definition_plain}"
            )
        if not rendered_items:
            return None
        return "\n".join(rendered_items), "\n".join(plain_items)

    def abbreviation_definition_list_content(
        element: Any,
    ) -> tuple[str, str] | None:
        children = [
            child for child in element if _tag(child) in {"dt", "dd"}
        ]
        if len(children) != len(element) or not children or len(children) % 2:
            return None
        rendered_items: list[str] = []
        plain_items: list[str] = []
        for index in range(0, len(children), 2):
            term, definition = children[index : index + 2]
            if _tag(term) != "dt" or _tag(definition) != "dd":
                return None
            term_markup = render_inline(term)
            definition_markup = render_inline(definition)
            term_plain = plain_text(term)
            definition_plain = plain_text(definition)
            if not all((term_markup, definition_markup, term_plain, definition_plain)):
                return None
            separator = "" if term_plain.endswith((":", "：")) else ":"
            rendered_items.append(
                f"- <strong>{term_markup}</strong>{separator} {definition_markup}"
            )
            plain_items.append(
                f"- {term_plain}{separator} {definition_plain}"
            )
        return "\n".join(rendered_items), "\n".join(plain_items)

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
            if supporting_mode:
                current = None
                current_heading_plain = ""
                container = structural_container(element)
                if container is not None:
                    section_scopes[container] = ("supporting", None, "")
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
        # Some publisher DOMs put an unheaded conventional introduction in a
        # sibling structural section immediately after the explicitly headed
        # abstract.  Do not let the prior Abstract heading leak across that
        # source boundary.  The synthetic heading is limited to actual prose
        # inside an otherwise unscoped HTML <section>; flat, ambiguous markup
        # keeps the existing conservative behavior.
        if (
            tag in {"p", "ul", "ol"}
            and current is not None
            and current_heading_plain.casefold() in {"abstract", "graphical abstract"}
            and not has_registered_scope(element)
        ):
            container = structural_container(element)
            if container is not None:
                add_synthetic_introduction(container)
        scope_kind, scoped_section, scoped_heading_plain = active_scope(element)

        # Older Wiley pages can place a definition table under an
        # ``Abbreviations`` heading and then put the unheaded article
        # introduction in a nested sibling section.  The table is authored
        # front matter, not a numbered scientific table, and the following
        # prose must not remain scoped as abbreviation content.
        nearest_container = structural_container(element)
        if (
            tag in {"p", "ul", "ol"}
            and scope_kind == "section"
            and scoped_heading_plain.rstrip(":").casefold() == "abbreviations"
            and nearest_container is not None
            and nearest_container not in section_scopes
            and not nearest_container.xpath("./h2 | ./h3 | ./h4")
        ):
            scoped_section = add_synthetic_introduction(nearest_container)
            scope_kind = "section"
            scoped_heading_plain = "Introduction"

        sciencedirect_div = tag == "div" and (
            bool(re.fullmatch(r"p\d+", element_id, re.IGNORECASE))
            and not element.xpath(".//p")
        )
        abstract_div = (
            tag == "div"
            and scoped_section is not None
            and scoped_heading_plain.casefold() == "abstract"
            and bool(
                re.fullmatch(
                    r"(?:sp|abspara)\d+",
                    element_id,
                    re.IGNORECASE,
                )
            )
        )
        research_highlights_div = (
            tag == "div"
            and scoped_section is not None
            and scoped_heading_plain.casefold() == "research highlights"
            and bool(re.fullmatch(r"sp\d+", element_id, re.IGNORECASE))
        )
        keyword_div = (
            tag == "div"
            and scoped_section is not None
            and scoped_heading_plain.casefold() == "keywords"
            and (
                bool(re.fullmatch(r"k\d+", element_id, re.IGNORECASE))
                or (
                    element.getparent() is not None
                    and any(
                        plain_text(heading).casefold() == "keywords"
                        for heading in element.getparent().xpath("./h2 | ./h3")
                    )
                    and bool(element.xpath("./span"))
                )
            )
        )
        abbreviation_heading = (
            scope_kind == "section"
            and scoped_section is not None
            and scoped_heading_plain.rstrip(":").casefold()
            in {"abbreviations", "list of abbreviations"}
        )
        abbreviation_table = (
            abbreviation_table_content(element)
            if tag == "table"
            and abbreviation_heading
            else None
        )
        abbreviation_definition_list = (
            abbreviation_definition_list_content(element)
            if tag in {"ul", "dl"} and abbreviation_heading
            else None
        )
        abbreviation_content = abbreviation_table or abbreviation_definition_list
        inside_abbreviation_definition_list = bool(
            abbreviation_heading
            and abbreviation_definition_list is None
            and any(
                _tag(ancestor) in {"ul", "dl"}
                and abbreviation_definition_list_content(ancestor) is not None
                for ancestor in element.iterancestors()
            )
        )
        if inside_abbreviation_definition_list:
            continue
        if tag not in {"p", "ul", "ol", "dl"} and abbreviation_content is None and not (
            sciencedirect_div
            or abstract_div
            or research_highlights_div
            or keyword_div
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

        if abbreviation_content is not None:
            rendered, visible = abbreviation_content
            kind = "list"
            content_segments = [("prose", rendered, visible, None)]
        elif tag in {"ul", "ol"}:
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
            if research_highlights_div:
                rendered_items = [
                    normalize_space(item)
                    for item in re.split(r"[►•]", render_inline(element))
                    if normalize_space(item)
                ]
                plain_items = [
                    normalize_space(item)
                    for item in re.split(r"[►•]", plain_text(element))
                    if normalize_space(item)
                ]
                if not rendered_items or len(rendered_items) != len(plain_items):
                    continue
                kind = "list"
                content_segments = [
                    (
                        "prose",
                        "\n".join(f"- {item}" for item in rendered_items),
                        "\n".join(f"- {item}" for item in plain_items),
                        None,
                    )
                ]
                children = []
            else:
                content_segments = []
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
            if content_segments:
                pass
            elif standalone_bold_heading:
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
    # A publisher may expose an asset-only graphical-abstract container as a
    # headed section.  Its image is already represented in the figure stream,
    # so suppress the otherwise empty prose section.  Keep the section when it
    # contains an authored textual summary, which is distinct machine-readable
    # content rather than a duplicate asset label.
    sections = [
        section
        for section in sections
        if section.blocks
        or normalize_space(
            html_stdlib.unescape(re.sub(r"<[^>]+>", "", section.heading))
        ).casefold()
        != "graphical abstract"
    ]
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


def _article_title(document: Any) -> str:
    title_nodes = document.xpath(".//h1[1]")
    if not title_nodes:
        return ""
    title_node = title_nodes[0]
    original_title = plain_text(title_node)
    title_note_links = [
        descendant
        for descendant in title_node.xpath(".//a[@href]")
        if ACS_TITLE_NOTE_HREF.fullmatch(descendant.get("href", ""))
        and plain_text(descendant) in ACS_TITLE_NOTE_MARKERS
    ]
    if title_note_links:
        candidate = copy.deepcopy(title_node)
        for link in candidate.xpath(".//a[@href]"):
            if (
                ACS_TITLE_NOTE_HREF.fullmatch(link.get("href", ""))
                and plain_text(link) in ACS_TITLE_NOTE_MARKERS
            ):
                link.getparent().remove(link)
        title_without_notes = plain_text(candidate)
        if title_without_notes:
            original_title = title_without_notes
    if title_node.get("id") != "screen-reader-main-title":
        return original_title

    candidate = copy.deepcopy(title_node)
    article_type_badges = [
        child
        for child in candidate
        if _tag(child) == "div"
        and plain_text(child).casefold() in SCIENCEDIRECT_ARTICLE_TYPE_BADGES
    ]
    if not article_type_badges:
        return original_title
    for badge in article_type_badges:
        candidate.remove(badge)
    title_without_badges = plain_text(candidate)
    return title_without_badges or original_title


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
    _remove_legacy_acs_duplicate_citation_parentheses(document)
    root = _content_root(document)
    _remove_sciencedirect_table_of_contents_navigation(root)
    _remove_terminal_crossref_appendices(root)
    title = _article_title(document)
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
        front_matter=[
            *_sciencedirect_front_matter(document, source_path),
            *_wiley_front_matter(document, source_path),
            *_acs_front_matter(document, source_path),
        ],
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
