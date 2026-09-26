"""Convert extractor markup to deterministic, non-executable HTML fragments."""

from __future__ import annotations

import html
from html.parser import HTMLParser
import re
import unicodedata
from urllib.parse import urlsplit


class UnsafeRichTextError(ValueError):
    """Raised when extracted rich text contains executable or unknown markup."""


_ALLOWED_INLINE_TAGS = frozenset(
    {"a", "br", "em", "strong", "sub", "sup", "u", "span"}
)
_ALLOWED_FRAGMENT_TAGS = _ALLOWED_INLINE_TAGS | frozenset({"li", "ol", "ul"})
_VOID_TAGS = frozenset({"br"})
_MARKDOWN_LINK = re.compile(r"\[([^\[\]\n]+)\]\(([^()\s<>]+)\)")
_CITATION_PART_LABEL = re.compile(
    r"\d+(?:[A-Za-z])?(?:\s*[-–—,]\s*\d+(?:[A-Za-z])?)*"
)
_CITATION_PART_TARGET = re.compile(r"[A-Za-z](?:[-–—,][A-Za-z])*")
_MARKDOWN_STRONG = re.compile(
    r"\*\*((?:\\.|(?!\*\*).)+?)\*\*", flags=re.DOTALL
)
_MARKDOWN_EMPHASIS = re.compile(
    r"(?<!\\)\*(?!\*)(.+?)(?<!\\)\*(?!\*)", flags=re.DOTALL
)
_LIST_ITEM = re.compile(
    r"^\s*(?P<marker>-|\d+\.|[A-Za-z]\.)\s+(?P<text>.+?)\s*$"
)
_SUBSCRIPT_CHARS = "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₔ"
_SUPERSCRIPT_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿ"
_SCRIPT_TRANSLATION = str.maketrans(
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₔ⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿ",
    "0123456789+-=()aeoxə0123456789+-=()in",
)
_ADJACENT_SCRIPT_RUNS = re.compile(
    r"(?P<marker>[_^])\{(?P<left>[^{}]*)\}(?P=marker)\{(?P<right>[^{}]*)\}"
)


def _coalesce_adjacent_script_runs(value: str) -> str:
    """Join contiguous same-script fragments split by inline styling."""

    while _ADJACENT_SCRIPT_RUNS.search(value):
        value = _ADJACENT_SCRIPT_RUNS.sub(
            lambda match: (
                f"{match.group('marker')}"
                f"{{{match.group('left')}{match.group('right')}}}"
            ),
            value,
        )
    return value


def _safe_href(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or "\\" in value
        or any(ord(character) < 0x20 for character in value)
    ):
        raise UnsafeRichTextError("rich-text link has an empty or invalid target")
    parsed = urlsplit(value)
    if parsed.scheme and parsed.scheme.casefold() not in {"http", "https", "mailto"}:
        raise UnsafeRichTextError(
            f"rich-text link uses forbidden URL scheme {parsed.scheme!r}"
        )
    if value.startswith(("//", "\\")):
        raise UnsafeRichTextError("rich-text link cannot be protocol-relative")
    return value


def _expand_limited_markdown(value: str) -> str:
    """Expand the small Markdown subset emitted by the extraction program."""

    def link(match: re.Match[str]) -> str:
        # Elsevier references can identify one part as ``[6](b)``. That is
        # authored citation text, not a Markdown link whose target is ``b``.
        # Keep this narrow so ordinary relative and external links still work.
        if _CITATION_PART_LABEL.fullmatch(
            match.group(1).strip()
        ) and _CITATION_PART_TARGET.fullmatch(match.group(2)):
            return match.group(0)
        href = html.escape(_safe_href(match.group(2)), quote=True)
        return f'<a href="{href}">{match.group(1)}</a>'

    value = _MARKDOWN_LINK.sub(link, value)
    # Extractor-generated strong runs never nest. Existing semantic HTML inside
    # the run is retained and checked by the sanitizer below.
    value = _MARKDOWN_STRONG.sub(r"<strong>\1</strong>", value)
    # Native PDF decoding uses single asterisks for source italic runs. Literal
    # stars are backslash-escaped before runs are assembled, so this limited
    # form is deterministic and does not need a general Markdown parser.
    value = _MARKDOWN_EMPHASIS.sub(r"<em>\1</em>", value)
    # PDF text escapes literal Markdown control characters before styled runs
    # are assembled.  Once actual strong markup has been expanded, restore
    # those literal characters for HTML output instead of exposing backslashes.
    return re.sub(r"\\([\\*_\[\]()])", r"\1", value)


class _InlineSanitizer(HTMLParser):
    def __init__(self, *, allowed_tags: frozenset[str] = _ALLOWED_INLINE_TAGS) -> None:
        super().__init__(convert_charrefs=False)
        self.allowed_tags = allowed_tags
        self.parts: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.casefold()
        if tag not in self.allowed_tags:
            raise UnsafeRichTextError(f"rich text contains forbidden HTML tag <{tag}>")
        if tag == "a":
            if len(attrs) != 1 or attrs[0][0].casefold() != "href":
                raise UnsafeRichTextError("rich-text links may contain only href")
            href = _safe_href(attrs[0][1] or "")
            self.parts.append(f'<a href="{html.escape(href, quote=True)}">')
        elif tag == "ol" and attrs:
            if (
                len(attrs) != 1
                or attrs[0][0].casefold() != "type"
                or attrs[0][1] not in {"a", "A"}
            ):
                raise UnsafeRichTextError(
                    "rich-text ordered lists allow only type a or A"
                )
            self.parts.append(f'<ol type="{attrs[0][1]}">')
        elif tag == "span" and attrs:
            if (len(attrs) != 1 or attrs[0][0].casefold() != "style"
                    or not re.fullmatch(r"color:#[0-9A-Fa-f]{6}", attrs[0][1] or "")):
                raise UnsafeRichTextError("rich-text spans allow only a hexadecimal text color")
            self.parts.append(f'<span style="{attrs[0][1].lower()}">')
        elif attrs:
            raise UnsafeRichTextError(f"rich-text tag <{tag}> may not have attributes")
        else:
            self.parts.append(f"<{tag}>")
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        folded = tag.casefold()
        if folded not in _VOID_TAGS:
            self.handle_endtag(folded)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            raise UnsafeRichTextError(f"rich text has an unmatched </{tag}> tag")
        self.stack.pop()
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if name + ";" in html.entities.html5 or name in html.entities.html5:
            self.parts.append(f"&{name};")
        else:
            self.parts.append(html.escape(f"&{name};", quote=False))

    def handle_charref(self, name: str) -> None:
        if not re.fullmatch(r"(?:[xX][0-9A-Fa-f]+|[0-9]+)", name):
            raise UnsafeRichTextError("rich text contains an invalid character reference")
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        raise UnsafeRichTextError("rich text may not contain HTML comments")

    def handle_decl(self, decl: str) -> None:
        raise UnsafeRichTextError("rich text may not contain HTML declarations")

    def unknown_decl(self, data: str) -> None:
        raise UnsafeRichTextError("rich text may not contain HTML declarations")

    def finish(self) -> str:
        if self.stack:
            raise UnsafeRichTextError(
                f"rich text has an unclosed <{self.stack[-1]}> tag"
            )
        return "".join(self.parts)


def inline_markup_to_safe_html(value: str) -> str:
    """Return safe HTML while preserving supported scientific semantics."""

    sanitizer = _InlineSanitizer()
    sanitizer.feed(_expand_limited_markdown(value))
    sanitizer.close()
    return sanitizer.finish()


def block_markup_to_safe_html(value: str, *, kind: str) -> str:
    """Convert one extraction block, including deterministic list structure."""

    if kind == "subsection_heading":
        value = re.sub(r"^\s*#{1,6}\s+", "", value, count=1)
        return f"<strong>{inline_markup_to_safe_html(value)}</strong>"
    if kind == "list":
        matches = [_LIST_ITEM.fullmatch(line) for line in value.splitlines() if line.strip()]
        if matches and all(match is not None for match in matches):
            concrete = [match for match in matches if match is not None]
            ordered = all(match.group("marker") != "-" for match in concrete)
            unordered = all(match.group("marker") == "-" for match in concrete)
            if ordered or unordered:
                tag = "ol" if ordered else "ul"
                markers = [match.group("marker") for match in concrete]
                alpha_lower = ordered and all(
                    re.fullmatch(r"[a-z]\.", marker) for marker in markers
                )
                alpha_upper = ordered and all(
                    re.fullmatch(r"[A-Z]\.", marker) for marker in markers
                )
                numeric = ordered and all(
                    re.fullmatch(r"\d+\.", marker) for marker in markers
                )
                if ordered and not (alpha_lower or alpha_upper or numeric):
                    return "<br>".join(
                        inline_markup_to_safe_html(line)
                        for line in value.splitlines()
                    )
                items = "".join(
                    f"<li>{inline_markup_to_safe_html(match.group('text'))}</li>"
                    for match in concrete
                )
                attribute = (
                    ' type="a"' if alpha_lower
                    else ' type="A"' if alpha_upper
                    else ""
                )
                return f"<{tag}{attribute}>{items}</{tag}>"
    return "<br>".join(
        inline_markup_to_safe_html(line) for line in value.splitlines()
    )


def validate_safe_html_fragment(value: str) -> None:
    """Reject executable, attributed, or structurally invalid output HTML."""

    sanitizer = _InlineSanitizer(allowed_tags=_ALLOWED_FRAGMENT_TAGS)
    sanitizer.feed(value)
    sanitizer.close()
    sanitizer.finish()


class _VisibleTextParser(HTMLParser):
    """Extract only the user-visible text from an already-safe fragment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.list_stack: list[tuple[str, int, str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        folded = tag.casefold()
        if folded in {"ol", "ul"}:
            marker_type = ""
            if folded == "ol":
                marker_type = next(
                    (
                        value or ""
                        for name, value in attrs
                        if name.casefold() == "type"
                    ),
                    "",
                )
            self.list_stack.append((folded, 0, marker_type))
        elif folded == "li":
            self.parts.append("\n")
            if self.list_stack:
                list_kind, count, marker_type = self.list_stack[-1]
                count += 1
                self.list_stack[-1] = (list_kind, count, marker_type)
                if list_kind == "ol" and marker_type in {"a", "A"} and count <= 26:
                    letter = chr(ord(marker_type) + count - 1)
                    self.parts.append(f"{letter}. ")
                else:
                    self.parts.append(f"{count}. " if list_kind == "ol" else "- ")
        elif folded == "br":
            self.parts.append("\n")
        elif folded == "sub":
            self.parts.append("_{")
        elif folded == "sup":
            self.parts.append("^{")

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if folded == "li":
            self.parts.append("\n")
        elif folded in {"ol", "ul"}:
            if self.list_stack:
                self.list_stack.pop()
            self.parts.append("\n")
        elif folded in {"sub", "sup"}:
            self.parts.append("}")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text_from_safe_html(value: str) -> str:
    """Return visible text after independently rechecking the safe fragment."""

    validate_safe_html_fragment(value)
    parser = _VisibleTextParser()
    parser.feed(value)
    parser.close()
    result = "".join(parser.parts)
    # Publisher HTML can split one continuous script run at an emphasis
    # boundary, for example ``<em>P21<sup>WAF1</sup></em><sup>/</sup>``
    # followed by another italic superscript.  Adjacent same-script elements
    # have no baseline separation and therefore make one semantic exponent in
    # the AI-readable plain form.  Keep different script types and runs with
    # intervening whitespace distinct.
    return _coalesce_adjacent_script_runs(result)


def normalize_visible_text(value: str) -> str:
    """Normalize presentation-only differences for dual-text comparison."""

    value = _coalesce_adjacent_script_runs(value)
    value = re.sub(
        f"[{re.escape(_SUBSCRIPT_CHARS)}]+",
        lambda match: "_{" + match.group(0).translate(_SCRIPT_TRANSLATION) + "}",
        value,
    )
    value = re.sub(
        f"[{re.escape(_SUPERSCRIPT_CHARS)}]+",
        lambda match: "^{" + match.group(0).translate(_SCRIPT_TRANSLATION) + "}",
        value,
    )
    # Oxidation states are sometimes marked up typographically in the rich
    # source while the paired plain-text extraction retains the conventional
    # compact form (for example, ``FeII`` versus ``Fe<sup>II</sup>``).  Treat
    # only uppercase Roman-numeral superscripts as presentation-only; numeric
    # exponents and charge signs remain semantically significant.
    value = re.sub(r"\^\{([IVXLCDM]+)\}", r"\1", value)
    value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    return " ".join(value.split())


def rich_text_matches_plain(plain_text: str, safe_html: str) -> bool:
    """Return whether plain and rich forms make the same text claim."""

    semantic_plain = plain_text_from_safe_html(safe_html)
    if re.fullmatch(
        r"\s*<(?:ol|ul)>.*</(?:ol|ul)>\s*", safe_html, re.DOTALL
    ):
        # Markdown supplies an explicit marker for each item while HTML list
        # structure supplies the same marker semantically. Remove exactly one
        # outer marker per line only for an actual list fragment; doing this
        # globally would hide real omissions such as ``1. Introduction``.
        def strip_outer_marker(value: str) -> str:
            return re.sub(
                r"(?m)^\s*(?:[-*•]|\d+[.)]|[A-Za-z][.)])\s+", "", value
            )

        plain_text = strip_outer_marker(plain_text)
        semantic_plain = strip_outer_marker(semantic_plain)
    return normalize_visible_text(plain_text) == normalize_visible_text(
        semantic_plain
    )


__all__ = [
    "UnsafeRichTextError",
    "block_markup_to_safe_html",
    "inline_markup_to_safe_html",
    "normalize_visible_text",
    "plain_text_from_safe_html",
    "rich_text_matches_plain",
    "validate_safe_html_fragment",
]
