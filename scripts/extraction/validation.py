"""Independent validation of a staged extraction candidate.

This module deliberately does not import the extraction pipeline or any of its
parsers.  It validates only the rendered candidate, its local assets, and the
diagnostic files written alongside it.  Keeping this boundary independent
makes it less likely that an extraction bug is repeated by its validator.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, Iterator, Mapping, Sequence
import unicodedata
from urllib.parse import unquote, urlsplit

from PIL import Image

from .models import ValidationFinding
from .paths import sha256_file
from .record_schema import (
    SUPPORTED_RECORD_SCHEMA_VERSIONS,
    RecordSchemaError,
    validate_record_schema,
)
from .table_schema import TableSchemaError, validate_table_payload


SEVERITIES = ("critical", "scientific", "structural", "cosmetic")
_SEVERITY_ORDER = {name: index for index, name in enumerate(SEVERITIES)}

# Count names accepted by ``expected_counts`` and emitted by the report.
COUNT_KEYS = (
    "pages",
    "references",
    "equations",
    "main_figures",
    "main_schemes",
    "main_figures_and_schemes",
    "tables",
    "supplementary_figures",
    "supplementary_files",
    "presentation_embedded_files",
)

_COUNT_ALIASES = {
    "figure_captions": "main_figures",
    "figures": "main_figures",
    "scheme_captions": "main_schemes",
    "schemes": "main_schemes",
    "main_figure_and_scheme_captions": "main_figures_and_schemes",
    "main_figures_and_scheme_captions": "main_figures_and_schemes",
    "main_figure_scheme_captions": "main_figures_and_schemes",
    "main_assets": "main_figures_and_schemes",
    "figures_and_schemes": "main_figures_and_schemes",
    "table_captions": "tables",
    "supplement_figures": "supplementary_figures",
    "supplementary_figure_captions": "supplementary_figures",
    "supplements": "supplementary_files",
    "embedded_workbooks": "presentation_embedded_files",
    "presentation_workbooks": "presentation_embedded_files",
}

_PILOT_00559_COUNTS = {
    "main_figures_and_schemes": 11,
    "tables": 2,
    "supplementary_figures": 1,
    "supplementary_files": 1,
}

_REQUIRED_DIAGNOSTICS = (
    "manifest.json",
    "sources.json",
    "coverage.jsonl",
    "quality.json",
    "confidence.json",
)

_ALLOWED_COVERAGE_STATUSES = {
    "included",
    "duplicate",
    "intentionally_excluded",
    "unresolved",
}

# Page classifications emitted by the deterministic PDF text decoder.  The
# deliberately vague ``unclassified`` state is not accepted: a PDF-primary
# candidate must say what kind of source content every page actually held.
_ALLOWED_PDF_PAGE_CLASSIFICATIONS = {
    "blank",
    "image_only",
    "native_text",
    "native_text_with_rotated_text",
    "rotated_text_only",
}

_REFERENCE_DEFINITION_RE = re.compile(
    r"(?m)^ {0,3}\[([^\]\n]+)\]:\s*(?:<([^>\n]+)>|(\S+))"
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_CAPTION_LABEL_RE = re.compile(
    r"\b(Figure|Scheme|Table)\s+(S?\d+)\b",
    re.IGNORECASE,
)
_PDF_MULTI_BLOCK_LOCATOR_RE = re.compile(
    r"^PDF pages (?P<pages>\d+(?:, \d+)+); exact per-line page/bbox geometry "
    r"is stored in source_geometry$"
)
_PDF_SINGLE_BLOCK_LOCATOR_RE = re.compile(
    r"^PDF page (?P<page>\d+), box \["
    r"(?P<x0>-?\d+(?:\.\d+)?), (?P<y0>-?\d+(?:\.\d+)?), "
    r"(?P<x1>-?\d+(?:\.\d+)?), (?P<y1>-?\d+(?:\.\d+)?)\]$"
)
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_TABLE_SOURCE_KINDS = frozenset({"html", "pdf", "image", "presentation"})
_IMAGE_TABLE_SOURCE_KINDS = frozenset({"pdf", "image"})
_EQUATION_BLOCK_KINDS = frozenset({"equation", "display_equation", "math"})
_PDF_CROP_VISUAL_ROLES = frozenset({"figure", "scheme", "graphical_abstract"})
_PDF_CROP_NEAR_WHITE_THRESHOLD = 245
_PDF_CROP_MIN_BOUNDARY_PIXELS = 1
_XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


@dataclass(frozen=True)
class MarkdownLink:
    """A link destination found in Markdown or embedded HTML."""

    destination: str
    line: int
    label: str = ""
    source: str = "markdown"


@dataclass(frozen=True)
class ValidationReport:
    """Deterministic result returned by :func:`validate_candidate`."""

    extraction_dir: str
    diagnostic_dir: str
    expected_title: str
    findings: tuple[ValidationFinding, ...]
    counts: Mapping[str, int]
    expected_counts: Mapping[str, int]
    checked_files: int

    @property
    def severity_counts(self) -> dict[str, int]:
        counts = Counter(finding.severity for finding in self.findings)
        return {severity: counts.get(severity, 0) for severity in SEVERITIES}

    @property
    def passed(self) -> bool:
        """Cosmetic findings alone do not fail scientific validation."""

        failing = {"critical", "scientific", "structural"}
        return not any(finding.severity in failing for finding in self.findings)

    @property
    def ok(self) -> bool:
        """Backward-compatible, concise spelling used by the validation CLI."""

        return self.passed

    @property
    def status(self) -> str:
        return "pass" if self.passed else "fail"

    @property
    def quality_status(self) -> str:
        """Alias used by diagnostic writers."""

        return self.status

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "status": self.status,
            "expected_title": self.expected_title,
            "counts": dict(sorted(self.counts.items())),
            "expected_counts": dict(sorted(self.expected_counts.items())),
            "checked_files": self.checked_files,
            "severity_counts": self.severity_counts,
            "findings": [finding.as_dict() for finding in self.findings],
        }

    def __iter__(self) -> Iterator[ValidationFinding]:
        return iter(self.findings)

    def __len__(self) -> int:
        return len(self.findings)


class _EmbeddedLinkParser(HTMLParser):
    def __init__(self, line_offset: int = 0) -> None:
        super().__init__(convert_charrefs=True)
        self.line_offset = line_offset
        self.links: list[MarkdownLink] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        wanted_by_tag = {
            "a": {"href"},
            "area": {"href"},
            "link": {"href"},
            "img": {"src"},
            "audio": {"src"},
            "video": {"src", "poster"},
            "source": {"src"},
            "track": {"src"},
            "script": {"src"},
            "iframe": {"src"},
            "embed": {"src"},
            "object": {"data"},
        }
        wanted = wanted_by_tag.get(tag)
        if wanted is None:
            return
        for name, value in attrs:
            if name.lower() in wanted and value is not None:
                line, _ = self.getpos()
                self.links.append(
                    MarkdownLink(
                        destination=value,
                        line=self.line_offset + line,
                        label=tag,
                        source="html",
                    )
                )


@dataclass(frozen=True)
class _HashRecord:
    path: str
    sha256: str
    role: str
    origin: str


def _finding(
    code: str,
    severity: str,
    message: str,
    path: str | None = None,
) -> ValidationFinding:
    if severity not in _SEVERITY_ORDER:
        raise ValueError(f"unknown validation severity: {severity}")
    return ValidationFinding(code=code, severity=severity, message=message, path=path)


def _sort_findings(findings: Iterable[ValidationFinding]) -> tuple[ValidationFinding, ...]:
    unique: dict[tuple[str, str, str, str | None], ValidationFinding] = {}
    for finding in findings:
        key = (finding.code, finding.severity, finding.message, finding.path)
        unique[key] = finding
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                _SEVERITY_ORDER[item.severity],
                item.code,
                item.path or "",
                item.message,
            ),
        )
    )


def _strip_code(text: str) -> str:
    """Blank fenced and inline code without changing newline positions."""

    lines = text.splitlines(keepends=True)
    rendered: list[str] = []
    fence: str | None = None
    for line in lines:
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker[0]
            elif marker[0] == fence:
                fence = None
            rendered.append("\n" if line.endswith(("\n", "\r")) else "")
            continue
        if fence is not None:
            rendered.append("\n" if line.endswith(("\n", "\r")) else "")
            continue

        # Replace inline code spans, including spans that contain brackets.
        rendered.append(re.sub(r"(`+)(.*?)\1", lambda m: " " * len(m.group(0)), line))
    return "".join(rendered)


def _matching_bracket(text: str, start: int, opener: str, closer: str) -> int | None:
    depth = 0
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == opener:
            depth += 1
        elif character == closer:
            depth -= 1
            if depth == 0:
                return index
    return None


def _inline_destination(text: str, open_paren: int) -> tuple[str, int] | None:
    """Parse the destination portion of ``(... optional title)``."""

    index = open_paren + 1
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    if index >= len(text):
        return None

    if text[index] == "<":
        end = index + 1
        escaped = False
        while end < len(text):
            if escaped:
                escaped = False
            elif text[end] == "\\":
                escaped = True
            elif text[end] == ">":
                destination = text[index + 1 : end]
                close = text.find(")", end + 1)
                if close == -1:
                    return None
                return destination, close + 1
            end += 1
        return None

    destination_start = index
    nested = 0
    escaped = False
    while index < len(text):
        character = text[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "(":
            nested += 1
        elif character == ")":
            if nested == 0:
                return text[destination_start:index], index + 1
            nested -= 1
        elif character.isspace() and nested == 0:
            destination = text[destination_start:index]
            close = _matching_bracket(text, open_paren, "(", ")")
            if close is None:
                return None
            return destination, close + 1
        index += 1
    return None


def parse_markdown_links(text: str) -> tuple[MarkdownLink, ...]:
    """Return Markdown and embedded-HTML links with source line numbers.

    The scanner supports nested link labels, balanced parentheses in inline
    destinations, angle-bracket destinations, images, and full/collapsed
    reference links.  Code spans and fenced code blocks are intentionally
    ignored.
    """

    scanned = _strip_code(text)
    references: dict[str, str] = {}
    for match in _REFERENCE_DEFINITION_RE.finditer(scanned):
        key = " ".join(match.group(1).strip().casefold().split())
        references[key] = match.group(2) or match.group(3) or ""

    links: list[MarkdownLink] = []
    index = 0
    while index < len(scanned):
        image = scanned.startswith("![", index)
        if scanned[index] != "[" and not image:
            index += 1
            continue
        label_start = index + (2 if image else 1)
        opener = index + (1 if image else 0)
        if index > 0 and scanned[index - 1] == "\\":
            index += 1
            continue
        label_end = _matching_bracket(scanned, opener, "[", "]")
        if label_end is None:
            index += 1
            continue
        label = scanned[label_start:label_end]
        next_index = label_end + 1
        line = scanned.count("\n", 0, index) + 1

        if next_index < len(scanned) and scanned[next_index] == "(":
            parsed = _inline_destination(scanned, next_index)
            if parsed is not None:
                destination, _end = parsed
                links.append(MarkdownLink(destination, line, label, "markdown"))
                # Continue inside the label so an image nested in a link (a
                # common thumbnail/full-size pattern) is validated too.
                index = label_start
                continue
        elif next_index < len(scanned) and scanned[next_index] == "[":
            reference_end = _matching_bracket(scanned, next_index, "[", "]")
            if reference_end is not None:
                reference = scanned[next_index + 1 : reference_end] or label
                key = " ".join(reference.strip().casefold().split())
                if key in references:
                    links.append(
                        MarkdownLink(references[key], line, label, "markdown-reference")
                    )
                index = label_start
                continue

        index = label_end + 1

    # Reference definitions are link declarations even when not used.  Checking
    # them prevents a stale broken definition from hiding in record.md.
    for match in _REFERENCE_DEFINITION_RE.finditer(scanned):
        destination = match.group(2) or match.group(3) or ""
        links.append(
            MarkdownLink(
                destination=destination,
                line=scanned.count("\n", 0, match.start()) + 1,
                label=match.group(1),
                source="markdown-definition",
            )
        )

    html_parser = _EmbeddedLinkParser()
    try:
        html_parser.feed(scanned)
    except Exception:
        # HTML correctness is not this parser's responsibility.  HTMLParser is
        # tolerant, but malformed fragments must never stop other validation.
        pass
    links.extend(html_parser.links)

    deduplicated = {
        (link.destination, link.line, link.label, link.source): link for link in links
    }
    return tuple(
        sorted(
            deduplicated.values(),
            key=lambda item: (item.line, item.destination, item.source, item.label),
        )
    )


def _is_external_link(destination: str) -> bool:
    value = destination.strip()
    if value.startswith("//"):
        return True
    return bool(_URI_SCHEME_RE.match(value)) and not bool(_DRIVE_PATH_RE.match(value))


def _validate_links(
    record_text: str,
    extraction_dir: Path,
) -> tuple[list[ValidationFinding], set[Path], tuple[MarkdownLink, ...]]:
    findings: list[ValidationFinding] = []
    linked_files: set[Path] = set()
    links = parse_markdown_links(record_text)
    resolved_root = extraction_dir.resolve(strict=False)

    for link in links:
        destination = link.destination.strip()
        link_path = f"record.md:{link.line}"
        if not destination:
            findings.append(
                _finding("empty_link", "structural", "Markdown link has no destination.", link_path)
            )
            continue
        if destination.startswith("#"):
            continue

        decoded_destination = unquote(destination)
        if decoded_destination.casefold().startswith("file:"):
            findings.append(
                _finding(
                    "unsafe_local_link",
                    "critical",
                    f"Unsafe local link {destination!r}: file URIs are not allowed.",
                    link_path,
                )
            )
            continue
        scheme_match = _URI_SCHEME_RE.match(decoded_destination)
        if scheme_match and not _DRIVE_PATH_RE.match(decoded_destination):
            scheme = decoded_destination.split(":", 1)[0].casefold()
            if scheme not in {"http", "https", "mailto"}:
                findings.append(
                    _finding(
                        "unsafe_external_scheme",
                        "critical",
                        f"Unsafe external URI scheme in {destination!r}.",
                        link_path,
                    )
                )
                continue
        if _is_external_link(decoded_destination):
            continue
        # Split URL syntax before decoding so a legitimate percent-encoded '#'
        # or '?' in a preserved publisher filename remains part of the path.
        decoded_path = unquote(urlsplit(destination).path)
        if not decoded_path:
            continue
        pure = PurePosixPath(decoded_path)
        unsafe_reason: str | None = None
        if "\\" in decoded_path:
            unsafe_reason = "backslashes are not allowed in local links"
        elif decoded_path.startswith(("/", "\\")) or _DRIVE_PATH_RE.match(decoded_path):
            unsafe_reason = "absolute local paths are not allowed"
        elif any(part == ".." for part in pure.parts):
            unsafe_reason = "parent-directory segments are not allowed"

        if unsafe_reason is not None:
            findings.append(
                _finding(
                    "unsafe_local_link",
                    "critical",
                    f"Unsafe local link {destination!r}: {unsafe_reason}.",
                    link_path,
                )
            )
            continue

        candidate = extraction_dir.joinpath(*pure.parts)
        resolved = candidate.resolve(strict=False)
        try:
            relative = resolved.relative_to(resolved_root)
        except ValueError:
            findings.append(
                _finding(
                    "escaping_local_link",
                    "critical",
                    f"Local link escapes the extraction directory: {destination!r}.",
                    link_path,
                )
            )
            continue

        if not candidate.exists():
            findings.append(
                _finding(
                    "missing_local_link",
                    "critical",
                    f"Linked local file does not exist: {destination!r}.",
                    link_path,
                )
            )
            continue
        if not candidate.is_file():
            findings.append(
                _finding(
                    "non_file_local_link",
                    "structural",
                    f"Local link does not identify a regular file: {destination!r}.",
                    link_path,
                )
            )
            continue
        linked_files.add(relative)

    return findings, linked_files, links


def _read_json_diagnostics(
    diagnostic_dir: Path,
) -> tuple[dict[str, Any], list[ValidationFinding]]:
    loaded: dict[str, Any] = {}
    findings: list[ValidationFinding] = []
    if not diagnostic_dir.is_dir():
        findings.append(
            _finding(
                "diagnostic_directory_missing",
                "critical",
                "The extraction_diagnostic directory is missing.",
                str(diagnostic_dir),
            )
        )
        return loaded, findings

    present = {
        path.relative_to(diagnostic_dir).as_posix()
        for path in diagnostic_dir.rglob("*")
        if path.is_file()
    }
    for name in _REQUIRED_DIAGNOSTICS:
        if name not in present:
            findings.append(
                _finding(
                    "required_diagnostic_missing",
                    "structural",
                    f"Required diagnostic file is missing: {name}.",
                    name,
                )
            )

    for path in sorted(diagnostic_dir.rglob("*"), key=lambda value: value.as_posix()):
        if not path.is_file() or path.suffix.lower() not in {".json", ".jsonl"}:
            continue
        relative = path.relative_to(diagnostic_dir).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            findings.append(
                _finding(
                    "diagnostic_not_utf8",
                    "critical",
                    f"Diagnostic is not valid UTF-8: {exc}.",
                    relative,
                )
            )
            continue
        except OSError as exc:
            findings.append(
                _finding(
                    "diagnostic_unreadable",
                    "critical",
                    f"Could not read diagnostic: {exc}.",
                    relative,
                )
            )
            continue

        if path.suffix.lower() == ".json":
            try:
                loaded[relative] = json.loads(text)
            except json.JSONDecodeError as exc:
                findings.append(
                    _finding(
                        "invalid_diagnostic_json",
                        "critical",
                        f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}.",
                        relative,
                    )
                )
            continue

        rows: list[Any] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                findings.append(
                    _finding(
                        "invalid_diagnostic_jsonl",
                        "critical",
                        f"Invalid JSONL row {line_number}, column {exc.colno}: {exc.msg}.",
                        relative,
                    )
                )
                continue
            if not isinstance(row, dict):
                findings.append(
                    _finding(
                        "invalid_diagnostic_jsonl_row",
                        "structural",
                        f"JSONL row {line_number} must be an object.",
                        relative,
                    )
                )
            rows.append(row)
        loaded[relative] = rows
    return loaded, findings


def _validate_coverage(coverage: Any) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if not isinstance(coverage, list):
        return findings
    if not coverage:
        return [
            _finding(
                "coverage_ledger_empty",
                "structural",
                "coverage.jsonl must account for source and output content.",
                "coverage.jsonl",
            )
        ]
    seen_ids: set[str] = set()
    for index, row in enumerate(coverage, start=1):
        if not isinstance(row, dict):
            continue
        status = row.get("status")
        row_id = next(
            (
                row.get(key)
                for key in ("coverage_id", "content_id", "block_id", "id")
                if isinstance(row.get(key), str) and row.get(key)
            ),
            None,
        )
        row_path = f"coverage.jsonl:{index}"
        if status not in _ALLOWED_COVERAGE_STATUSES:
            findings.append(
                _finding(
                    "invalid_coverage_status",
                    "structural",
                    f"Coverage status must be one of {sorted(_ALLOWED_COVERAGE_STATUSES)}.",
                    row_path,
                )
            )
        elif status == "unresolved":
            label = f" ({row_id})" if row_id else ""
            findings.append(
                _finding(
                    "unresolved_coverage",
                    "scientific",
                    f"Coverage item remains unresolved{label}.",
                    row_path,
                )
            )
        if row_id:
            if row_id in seen_ids:
                findings.append(
                    _finding(
                        "duplicate_coverage_id",
                        "structural",
                        f"Coverage identifier is duplicated: {row_id!r}.",
                        row_path,
                    )
                )
            seen_ids.add(row_id)
        if (
            status == "included"
            and row.get("content_kind") == "source_file"
            and not (
                isinstance(row.get("output_ids"), list)
                and row.get("output_ids")
                and all(
                    isinstance(value, str) and value
                    for value in row["output_ids"]
                )
            )
        ):
            findings.append(
                _finding(
                    "source_coverage_has_no_outputs",
                    "structural",
                    "An included source file must list the output IDs it contributes to.",
                    row_path,
                )
            )
        if status == "included":
            missing = [
                key
                for key in ("source_path", "source_locator")
                if not isinstance(row.get(key), str) or not row[key].strip()
            ]
            if missing:
                findings.append(
                    _finding(
                        "included_coverage_provenance_missing",
                        "structural",
                        (
                            "Included coverage must identify nonempty source "
                            f"provenance; missing: {', '.join(missing)}."
                        ),
                        row_path,
                    )
                )
    return findings


def _validate_record_line_coverage(
    record_text: str, coverage: Any
) -> list[ValidationFinding]:
    if not record_text or not isinstance(coverage, list):
        return []
    covered: set[int] = set()
    for row in coverage:
        if not isinstance(row, dict) or row.get("output_path") != "record.md":
            continue
        locator = row.get("output_locator")
        if not isinstance(locator, dict):
            continue
        start = locator.get("start_line")
        end = locator.get("end_line")
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 1 <= start <= end
        ):
            covered.update(range(start, end + 1))
    missing = [
        number
        for number, line in enumerate(record_text.splitlines(), start=1)
        if line.strip() and number not in covered
    ]
    if not missing:
        return []
    preview = ", ".join(str(number) for number in missing[:12])
    suffix = "…" if len(missing) > 12 else ""
    return [
        _finding(
            "record_lines_not_covered",
            "structural",
            f"Nonblank record.md lines lack reverse coverage: {preview}{suffix}.",
            "coverage.jsonl",
        )
    ]


def _validate_diagnostic_warnings(loaded: Mapping[str, Any]) -> list[ValidationFinding]:
    rows = loaded.get("warnings.jsonl")
    if not isinstance(rows, list):
        return []
    coverage = loaded.get("coverage.jsonl")
    graphical_abstract_is_covered = bool(
        isinstance(coverage, list)
        and any(
            isinstance(row, dict)
            and row.get("coverage_id") == "unresolved-graphical-abstract-image"
            and row.get("status") == "unresolved"
            for row in coverage
        )
    )
    findings: list[ValidationFinding] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "unspecified")
        if code == "graphical_abstract_image_unavailable" and graphical_abstract_is_covered:
            continue
        raw_severity = str(row.get("severity") or "structural").casefold()
        severity = raw_severity if raw_severity in SEVERITIES else "structural"
        findings.append(
            _finding(
                f"diagnostic_warning_{code}",
                severity,
                str(row.get("message") or f"Unresolved diagnostic warning: {code}."),
                f"warnings.jsonl:{index}",
            )
        )
    return findings


def _is_unresolved_glyph_row(row: Mapping[str, Any]) -> bool:
    """Return whether a page-analysis row records an unresolved glyph."""

    status = str(row.get("status") or "").strip().casefold()
    kind = str(row.get("kind") or "").strip().casefold()
    code = str(row.get("code") or "").strip().casefold()
    if code == "unresolved_glyph":
        return True
    if status != "unresolved":
        return False
    return (
        "glyph" in kind
        or "glyph" in code
        or any(key in row for key in ("glyph_name", "encoded_code", "font"))
    )


def _validate_pdf_page_analysis(loaded: Mapping[str, Any]) -> list[ValidationFinding]:
    """Independently audit per-page diagnostics for a PDF-primary candidate.

    HTML-primary candidates intentionally bypass these checks so the existing
    extraction contract remains backward compatible.
    """

    manifest = loaded.get("manifest.json")
    if not isinstance(manifest, dict):
        return []
    text_extraction = manifest.get("text_extraction")
    if (
        not isinstance(text_extraction, dict)
        or text_extraction.get("source_role") != "main_pdf"
    ):
        return []

    findings: list[ValidationFinding] = []
    expected_reference = "page_analysis.jsonl"
    if manifest.get("page_analysis") != expected_reference:
        findings.append(
            _finding(
                "pdf_page_analysis_reference_invalid",
                "structural",
                "A PDF-primary manifest must reference page_analysis.jsonl.",
                "manifest.json",
            )
        )

    sources_document = loaded.get("sources.json")
    sources = sources_document.get("sources") if isinstance(sources_document, dict) else None
    main_pdf_sources = (
        [
            source
            for source in sources
            if isinstance(source, dict) and source.get("role") == "main_pdf"
        ]
        if isinstance(sources, list)
        else []
    )
    page_count: int | None = None
    source_path: str | None = None
    if len(main_pdf_sources) != 1:
        findings.append(
            _finding(
                "pdf_main_source_invalid",
                "structural",
                (
                    "sources.json must declare exactly one main_pdf source for "
                    "a PDF-primary candidate."
                ),
                "sources.json",
            )
        )
    else:
        source = main_pdf_sources[0]
        raw_page_count = source.get("page_count")
        if (
            not isinstance(raw_page_count, int)
            or isinstance(raw_page_count, bool)
            or raw_page_count < 1
        ):
            findings.append(
                _finding(
                    "pdf_page_count_invalid",
                    "structural",
                    "The main_pdf source must declare a positive integer page_count.",
                    "sources.json",
                )
            )
        else:
            page_count = raw_page_count
        raw_source_path = source.get("path")
        if isinstance(raw_source_path, str) and raw_source_path.strip():
            source_path = raw_source_path.replace("\\", "/")

    declared_text_path = text_extraction.get("source_path")
    if (
        source_path is not None
        and isinstance(declared_text_path, str)
        and declared_text_path.replace("\\", "/") != source_path
    ):
        findings.append(
            _finding(
                "pdf_text_source_mismatch",
                "structural",
                "The PDF text-extraction source does not match sources.json.",
                "manifest.json",
            )
        )

    rows = loaded.get(expected_reference)
    if not isinstance(rows, list):
        findings.append(
            _finding(
                "pdf_page_analysis_missing",
                "structural",
                "page_analysis.jsonl is required for a PDF-primary candidate.",
                expected_reference,
            )
        )
        return findings

    summaries: list[tuple[int, dict[str, Any]]] = []
    for index, row in enumerate(rows, start=1):
        row_path = f"{expected_reference}:{index}"
        if not isinstance(row, dict):
            continue
        if _is_unresolved_glyph_row(row):
            findings.append(
                _finding(
                    "pdf_unresolved_glyph",
                    "scientific",
                    "PDF page analysis contains an unresolved glyph.",
                    row_path,
                )
            )
        if row.get("kind") != "page_summary":
            continue

        page = row.get("page")
        classification = row.get("classification")
        ocr_performed = row.get("ocr_performed")
        malformed_fields: list[str] = []
        if (
            not isinstance(page, int)
            or isinstance(page, bool)
            or page < 1
        ):
            malformed_fields.append("page")
        if (
            not isinstance(classification, str)
            or classification.strip() not in _ALLOWED_PDF_PAGE_CLASSIFICATIONS
        ):
            malformed_fields.append("classification")
        if not isinstance(ocr_performed, bool):
            malformed_fields.append("ocr_performed")
        row_source_path = row.get("source_path")
        if source_path is not None and (
            not isinstance(row_source_path, str)
            or row_source_path.replace("\\", "/") != source_path
        ):
            malformed_fields.append("source_path")
        if malformed_fields:
            findings.append(
                _finding(
                    "pdf_page_summary_malformed",
                    "structural",
                    (
                        "PDF page summary has invalid field(s): "
                        f"{', '.join(malformed_fields)}."
                    ),
                    row_path,
                )
            )
        if isinstance(page, int) and not isinstance(page, bool) and page >= 1:
            summaries.append((index, row))

    summary_counts = Counter(row["page"] for _, row in summaries)
    for page, count in sorted(summary_counts.items()):
        if count > 1:
            findings.append(
                _finding(
                    "pdf_page_summary_duplicate",
                    "structural",
                    f"PDF page {page} has {count} page_summary rows; exactly one is required.",
                    expected_reference,
                )
            )

    if page_count is not None:
        expected_pages = set(range(1, page_count + 1))
        actual_pages = set(summary_counts)
        missing = sorted(expected_pages - actual_pages)
        unexpected = sorted(actual_pages - expected_pages)
        if missing:
            findings.append(
                _finding(
                    "pdf_page_summary_missing",
                    "structural",
                    "Missing page_summary row(s) for PDF page(s): "
                    + ", ".join(map(str, missing))
                    + ".",
                    expected_reference,
                )
            )
        if unexpected:
            findings.append(
                _finding(
                    "pdf_page_summary_unexpected",
                    "structural",
                    "page_summary row(s) exceed the declared PDF page count: "
                    + ", ".join(map(str, unexpected))
                    + ".",
                    expected_reference,
                )
            )

    summary_ocr = any(row.get("ocr_performed") is True for _, row in summaries)
    top_level_ocr = manifest.get("ocr_performed")
    nested_ocr = text_extraction.get("ocr_performed")
    for label, value in (
        ("manifest.ocr_performed", top_level_ocr),
        ("manifest.text_extraction.ocr_performed", nested_ocr),
    ):
        if not isinstance(value, bool):
            findings.append(
                _finding(
                    "pdf_manifest_ocr_flag_invalid",
                    "structural",
                    f"{label} must be boolean for a PDF-primary candidate.",
                    "manifest.json",
                )
            )
        elif value != summary_ocr:
            findings.append(
                _finding(
                    "pdf_ocr_summary_mismatch",
                    "structural",
                    (
                        f"{label}={value!r} does not agree with the page summaries "
                        f"(OCR performed={summary_ocr!r})."
                    ),
                    "manifest.json",
                )
            )
    return findings


_OCR_TEXT_ROLES = frozenset(
    {"document_text", "textual_table", "equation", "caption"}
)
_OCR_VISUAL_ROLES = frozenset({"figure", "scheme"})
_OCR_REQUIRED_MODEL_ROLES = frozenset(
    {"detector", "classifier", "recognizer"}
)


def _ocr_box(value: Any) -> tuple[float, float, float, float] | None:
    """Return one finite, positive-area PDF box or ``None``."""

    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(
        not isinstance(coordinate, (int, float))
        or isinstance(coordinate, bool)
        or not math.isfinite(float(coordinate))
        for coordinate in value
    ):
        return None
    box = tuple(float(coordinate) for coordinate in value)
    return box if box[0] < box[2] and box[1] < box[3] else None


def _ocr_intersection(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    intersection = (
        max(left[0], right[0]),
        max(left[1], right[1]),
        min(left[2], right[2]),
        min(left[3], right[3]),
    )
    return (
        intersection
        if intersection[0] < intersection[2] and intersection[1] < intersection[3]
        else None
    )


def _ocr_boxes_equal(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    tolerance: float = 1e-6,
) -> bool:
    return all(
        math.isclose(a, b, rel_tol=0.0, abs_tol=tolerance)
        for a, b in zip(left, right)
    )


def _ocr_polygon_box(
    value: Any,
    *,
    bounds: tuple[float, float, float, float],
    tolerance: float = 1e-4,
) -> tuple[float, float, float, float] | None:
    """Validate one OCR polygon and return its positive-area bounding box."""

    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    points: list[tuple[float, float]] = []
    for raw_point in value:
        if not isinstance(raw_point, (list, tuple)) or len(raw_point) != 2:
            return None
        if any(
            not isinstance(coordinate, (int, float))
            or isinstance(coordinate, bool)
            or not math.isfinite(float(coordinate))
            for coordinate in raw_point
        ):
            return None
        points.append((float(raw_point[0]), float(raw_point[1])))

    if any(
        x < bounds[0] - tolerance
        or x > bounds[2] + tolerance
        or y < bounds[1] - tolerance
        or y > bounds[3] + tolerance
        for x, y in points
    ):
        return None
    polygon_box = (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )
    if polygon_box[0] >= polygon_box[2] or polygon_box[1] >= polygon_box[3]:
        return None
    # A nonzero bounding box alone does not rule out a collinear polygon.
    signed_twice_area = sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])
    )
    return polygon_box if not math.isclose(signed_twice_area, 0.0, abs_tol=1e-9) else None


def _ocr_polygon_mapping_matches(
    pixel_value: Any,
    pdf_value: Any,
    *,
    region_box: tuple[float, float, float, float],
    dimensions: tuple[int, int],
) -> bool:
    """Check that pixel and PDF polygons describe the same detection."""

    if (
        not isinstance(pixel_value, (list, tuple))
        or not isinstance(pdf_value, (list, tuple))
        or len(pixel_value) != len(pdf_value)
    ):
        return False
    width, height = dimensions
    left, top, right, bottom = region_box
    for pixel_point, pdf_point in zip(pixel_value, pdf_value):
        if (
            not isinstance(pixel_point, (list, tuple))
            or len(pixel_point) != 2
            or not isinstance(pdf_point, (list, tuple))
            or len(pdf_point) != 2
        ):
            return False
        expected_x = left + float(pixel_point[0]) * (right - left) / width
        expected_y = top + float(pixel_point[1]) * (bottom - top) / height
        if not (
            math.isclose(expected_x, float(pdf_point[0]), rel_tol=0.0, abs_tol=1e-3)
            and math.isclose(
                expected_y,
                float(pdf_point[1]),
                rel_tol=0.0,
                abs_tol=1e-3,
            )
        ):
            return False
    return True


def _absolute_local_path(value: str) -> bool:
    normalized = value.strip()
    return bool(
        normalized.startswith(("/", "\\\\"))
        or _DRIVE_PATH_RE.match(normalized)
    )


def _portable_basename(value: str) -> str:
    return re.split(r"[/\\\\]", value)[-1]


def _ocr_model_signatures(
    engine: Any,
    *,
    diagnostic: bool,
    path: str,
) -> tuple[dict[str, tuple[str, int, str, str]], list[str], list[ValidationFinding]]:
    """Return stable OCR model signatures and any absolute diagnostic paths."""

    findings: list[ValidationFinding] = []
    signatures: dict[str, tuple[str, int, str, str]] = {}
    absolute_paths: list[str] = []
    models = engine.get("models") if isinstance(engine, dict) else None
    if not isinstance(models, list) or not models:
        return (
            signatures,
            absolute_paths,
            [
                _finding(
                    "ocr_model_metadata_invalid",
                    "structural",
                    "OCR engine metadata must contain a nonempty models list.",
                    path,
                )
            ],
        )

    for index, model in enumerate(models, start=1):
        model_path = f"{path}.models[{index}]"
        if not isinstance(model, dict):
            findings.append(
                _finding(
                    "ocr_model_metadata_invalid",
                    "structural",
                    "OCR model metadata must be an object.",
                    model_path,
                )
            )
            continue
        role = model.get("role")
        version = model.get("version")
        byte_count = model.get("bytes")
        sha256 = model.get("sha256")
        raw_name = model.get("path" if diagnostic else "model_name")
        metadata_valid = (
            isinstance(role, str)
            and bool(role.strip())
            and isinstance(version, str)
            and bool(version.strip())
            and isinstance(byte_count, int)
            and not isinstance(byte_count, bool)
            and byte_count > 0
            and isinstance(sha256, str)
            and bool(re.fullmatch(r"[0-9a-fA-F]{64}", sha256))
            and isinstance(raw_name, str)
            and bool(raw_name.strip())
        )
        if diagnostic:
            metadata_valid = metadata_valid and isinstance(raw_name, str) and _absolute_local_path(
                raw_name
            )
        else:
            if "path" in model:
                findings.append(
                    _finding(
                        "ocr_model_path_exposed",
                        "critical",
                        "Absolute OCR model paths belong only in private page diagnostics.",
                        model_path,
                    )
                )
            metadata_valid = (
                metadata_valid
                and isinstance(raw_name, str)
                and not _absolute_local_path(raw_name)
                and raw_name == _portable_basename(raw_name)
            )
        if not metadata_valid:
            findings.append(
                _finding(
                    "ocr_model_metadata_invalid",
                    "structural",
                    "OCR model role, version, byte size, hash, or filename/path is invalid.",
                    model_path,
                )
            )
            continue
        assert isinstance(role, str)
        assert isinstance(version, str)
        assert isinstance(byte_count, int)
        assert isinstance(sha256, str)
        assert isinstance(raw_name, str)
        normalized_role = role.strip().casefold()
        if normalized_role in signatures:
            findings.append(
                _finding(
                    "ocr_model_metadata_invalid",
                    "structural",
                    f"OCR model role {normalized_role!r} is duplicated.",
                    model_path,
                )
            )
            continue
        if diagnostic:
            absolute_paths.append(raw_name)
        signatures[normalized_role] = (
            version.strip(),
            byte_count,
            sha256.casefold(),
            _portable_basename(raw_name),
        )

    missing_roles = sorted(_OCR_REQUIRED_MODEL_ROLES - signatures.keys())
    if missing_roles:
        findings.append(
            _finding(
                "ocr_model_metadata_invalid",
                "structural",
                "OCR model metadata is missing required role(s): "
                + ", ".join(missing_roles)
                + ".",
                path,
            )
        )
    return signatures, absolute_paths, findings


def _validate_pdf_ocr_diagnostics(
    loaded: Mapping[str, Any],
) -> list[ValidationFinding]:
    """Independently audit a region-aware OCR-primary PDF extraction.

    This intentionally consumes only serialized diagnostics. HTML-primary and
    native-text PDF candidates bypass it, preserving their existing contract.
    """

    manifest = loaded.get("manifest.json")
    if not isinstance(manifest, dict):
        return []
    text_extraction = manifest.get("text_extraction")
    if (
        not isinstance(text_extraction, dict)
        or text_extraction.get("source_role") != "main_pdf"
    ):
        return []
    page_rows = loaded.get("page_analysis.jsonl")
    row_objects = (
        [row for row in page_rows if isinstance(row, dict)]
        if isinstance(page_rows, list)
        else []
    )
    page_summary_ocr = any(
        row.get("kind") == "page_summary" and row.get("ocr_performed") is True
        for row in row_objects
    )
    if not (
        manifest.get("ocr_performed") is True
        or text_extraction.get("ocr_performed") is True
        or page_summary_ocr
    ):
        return []

    findings: list[ValidationFinding] = []
    sources_document = loaded.get("sources.json")
    sources = (
        sources_document.get("sources")
        if isinstance(sources_document, dict)
        else None
    )
    main_sources = (
        [
            source
            for source in sources
            if isinstance(source, dict) and source.get("role") == "main_pdf"
        ]
        if isinstance(sources, list)
        else []
    )
    main_source = main_sources[0] if len(main_sources) == 1 else None
    source_path = (
        str(main_source.get("path")).replace("\\", "/")
        if isinstance(main_source, dict)
        and isinstance(main_source.get("path"), str)
        and str(main_source.get("path")).strip()
        else None
    )
    source_sha256 = (
        str(main_source.get("sha256")).casefold()
        if isinstance(main_source, dict)
        and isinstance(main_source.get("sha256"), str)
        and re.fullmatch(r"[0-9a-fA-F]{64}", str(main_source.get("sha256")))
        else None
    )
    page_count = (
        main_source.get("page_count")
        if isinstance(main_source, dict)
        and isinstance(main_source.get("page_count"), int)
        and not isinstance(main_source.get("page_count"), bool)
        and main_source.get("page_count") > 0
        else None
    )

    run_rows = [row for row in row_objects if row.get("kind") == "ocr_run"]
    if len(run_rows) != 1:
        findings.append(
            _finding(
                "ocr_run_count_invalid",
                "structural",
                f"OCR-primary diagnostics require exactly one ocr_run row; found {len(run_rows)}.",
                "page_analysis.jsonl",
            )
        )
    run = run_rows[0] if len(run_rows) == 1 else None

    manifest_dpi = text_extraction.get("ocr_dpi")
    manifest_region_count = text_extraction.get("ocr_region_count")
    manifest_engine = text_extraction.get("ocr_engine")
    if (
        not isinstance(manifest_dpi, int)
        or isinstance(manifest_dpi, bool)
        or manifest_dpi <= 0
    ):
        findings.append(
            _finding(
                "ocr_dpi_invalid",
                "structural",
                "Manifest OCR DPI must be a positive integer.",
                "manifest.json",
            )
        )
    if (
        not isinstance(manifest_region_count, int)
        or isinstance(manifest_region_count, bool)
        or manifest_region_count <= 0
    ):
        findings.append(
            _finding(
                "ocr_region_count_invalid",
                "structural",
                "Manifest OCR region count must be a positive integer.",
                "manifest.json",
            )
        )
    if not isinstance(manifest_engine, dict):
        findings.append(
            _finding(
                "ocr_engine_metadata_invalid",
                "structural",
                "Manifest OCR engine metadata is missing or malformed.",
                "manifest.json",
            )
        )

    run_dpi: Any = None
    run_region_count: Any = None
    run_engine: Any = None
    diagnostic_model_paths: list[str] = []
    if run is not None:
        run_dpi = run.get("dpi")
        run_region_count = run.get("region_count")
        run_engine = run.get("engine")
        run_source_path = run.get("source_path")
        run_source_sha256 = run.get("source_sha256")
        if run.get("ocr_performed") is not True:
            findings.append(
                _finding(
                    "ocr_run_malformed",
                    "structural",
                    "The ocr_run row must declare ocr_performed=true.",
                    "page_analysis.jsonl",
                )
            )
        if source_path is None or not isinstance(run_source_path, str) or (
            run_source_path.replace("\\", "/") != source_path
        ):
            findings.append(
                _finding(
                    "ocr_source_path_mismatch",
                    "critical",
                    "The OCR run source path does not match the declared main PDF.",
                    "page_analysis.jsonl",
                )
            )
        if source_sha256 is None or not isinstance(run_source_sha256, str) or (
            not re.fullmatch(r"[0-9a-fA-F]{64}", run_source_sha256)
            or run_source_sha256.casefold() != source_sha256
        ):
            findings.append(
                _finding(
                    "ocr_source_hash_mismatch",
                    "critical",
                    "The OCR run source hash does not match the declared main PDF.",
                    "page_analysis.jsonl",
                )
            )
        if (
            not isinstance(run_dpi, int)
            or isinstance(run_dpi, bool)
            or run_dpi <= 0
        ):
            findings.append(
                _finding(
                    "ocr_dpi_invalid",
                    "structural",
                    "The ocr_run DPI must be a positive integer.",
                    "page_analysis.jsonl",
                )
            )
        elif isinstance(manifest_dpi, int) and run_dpi != manifest_dpi:
            findings.append(
                _finding(
                    "ocr_dpi_mismatch",
                    "structural",
                    "Manifest and ocr_run DPI values do not agree.",
                    "manifest.json",
                )
            )
        if (
            not isinstance(run_region_count, int)
            or isinstance(run_region_count, bool)
            or run_region_count <= 0
        ):
            findings.append(
                _finding(
                    "ocr_region_count_invalid",
                    "structural",
                    "The ocr_run region count must be a positive integer.",
                    "page_analysis.jsonl",
                )
            )

        engine_required_strings = (
            "name",
            "version",
            "backend",
            "backend_version",
            "execution_provider",
            "network_access",
        )
        if not isinstance(run_engine, dict) or any(
            not isinstance(run_engine.get(key), str)
            or not str(run_engine.get(key)).strip()
            for key in engine_required_strings
        ):
            findings.append(
                _finding(
                    "ocr_engine_metadata_invalid",
                    "structural",
                    "The ocr_run engine identity and version metadata is incomplete.",
                    "page_analysis.jsonl",
                )
            )
        elif run_engine.get("network_access") != "disabled":
            findings.append(
                _finding(
                    "ocr_engine_network_policy_invalid",
                    "critical",
                    "OCR diagnostics must record disabled network access.",
                    "page_analysis.jsonl",
                )
            )

    manifest_models, _unused_paths, model_findings = _ocr_model_signatures(
        manifest_engine,
        diagnostic=False,
        path="manifest.json.text_extraction.ocr_engine",
    )
    findings.extend(model_findings)
    diagnostic_models: dict[str, tuple[str, int, str, str]] = {}
    if run is not None:
        diagnostic_models, diagnostic_model_paths, model_findings = (
            _ocr_model_signatures(
                run_engine,
                diagnostic=True,
                path="page_analysis.jsonl.ocr_run.engine",
            )
        )
        findings.extend(model_findings)

    if isinstance(manifest_engine, dict) and isinstance(run_engine, dict):
        for key in (
            "name",
            "version",
            "backend",
            "backend_version",
            "execution_provider",
            "network_access",
            "settings",
        ):
            if manifest_engine.get(key) != run_engine.get(key):
                findings.append(
                    _finding(
                        "ocr_engine_mismatch",
                        "structural",
                        f"Manifest and ocr_run engine field {key!r} do not agree.",
                        "manifest.json",
                    )
                )
        if manifest_models != diagnostic_models:
            findings.append(
                _finding(
                    "ocr_model_mismatch",
                    "critical",
                    "Manifest and ocr_run model identities or hashes do not agree.",
                    "manifest.json",
                )
            )
        requested_engine = run.get("requested_engine") if run is not None else None
        if requested_engine != run_engine.get("name"):
            findings.append(
                _finding(
                    "ocr_engine_mismatch",
                    "structural",
                    "The requested OCR engine does not match the executed engine.",
                    "page_analysis.jsonl",
                )
            )

        pipeline = manifest.get("pipeline")
        if isinstance(pipeline, dict):
            for package, expected_version in (
                (
                    str(run_engine.get("name") or "").casefold(),
                    run_engine.get("version"),
                ),
                (
                    str(run_engine.get("backend") or "").casefold(),
                    run_engine.get("backend_version"),
                ),
            ):
                if package and pipeline.get(package) != expected_version:
                    findings.append(
                        _finding(
                            "ocr_pipeline_version_mismatch",
                            "structural",
                            f"Pipeline version for {package!r} does not match the OCR run.",
                            "manifest.json",
                        )
                    )
        else:
            findings.append(
                _finding(
                    "ocr_pipeline_version_mismatch",
                    "structural",
                    "Manifest pipeline dependency versions are missing.",
                    "manifest.json",
                )
            )

    # The manifest is shareable metadata; installed model locations are private
    # runtime diagnostics. Catch both explicit path keys and accidental copies.
    manifest_strings = [
        value
        for item in _walk_objects(manifest)
        for value in item.values()
        if isinstance(value, str)
    ]
    if isinstance(manifest_engine, dict) and any(
        _absolute_local_path(value)
        for item in _walk_objects(manifest_engine)
        for value in item.values()
        if isinstance(value, str)
    ):
        findings.append(
            _finding(
                "ocr_model_path_exposed",
                "critical",
                "Absolute OCR runtime paths belong only in private page diagnostics.",
                "manifest.json.text_extraction.ocr_engine",
            )
        )
    for model_path in diagnostic_model_paths:
        if model_path in manifest_strings:
            findings.append(
                _finding(
                    "ocr_model_path_exposed",
                    "critical",
                    "An absolute OCR model path leaked into manifest.json.",
                    "manifest.json",
                )
            )

    region_rows = [row for row in row_objects if row.get("kind") == "ocr_region"]
    actual_region_count = len(region_rows)
    if isinstance(manifest_region_count, int) and manifest_region_count != actual_region_count:
        findings.append(
            _finding(
                "ocr_region_count_mismatch",
                "structural",
                (
                    f"Manifest declares {manifest_region_count} OCR regions; "
                    f"diagnostics contain {actual_region_count}."
                ),
                "page_analysis.jsonl",
            )
        )
    if isinstance(run_region_count, int) and run_region_count != actual_region_count:
        findings.append(
            _finding(
                "ocr_region_count_mismatch",
                "structural",
                (
                    f"ocr_run declares {run_region_count} OCR regions; "
                    f"diagnostics contain {actual_region_count}."
                ),
                "page_analysis.jsonl",
            )
        )

    summary_pages = {
        row.get("page")
        for row in row_objects
        if row.get("kind") == "page_summary"
        and row.get("ocr_performed") is True
        and isinstance(row.get("page"), int)
        and not isinstance(row.get("page"), bool)
    }
    region_pages: set[int] = set()
    seen_region_ids: set[str] = set()
    parsed_regions: list[
        tuple[
            str,
            int,
            tuple[float, float, float, float],
            tuple[int, int],
            list[dict[str, Any]],
            list[tuple[tuple[float, float, float, float], int]],
        ]
    ] = []
    for index, region in enumerate(region_rows, start=1):
        row_path = f"page_analysis.jsonl:ocr_region:{index}"
        region_id = region.get("region_id")
        page = region.get("page")
        role = region.get("role")
        box = _ocr_box(region.get("box"))
        dimensions = region.get("dimensions_pixels")
        width = dimensions.get("width") if isinstance(dimensions, dict) else None
        height = dimensions.get("height") if isinstance(dimensions, dict) else None
        region_dpi = region.get("dpi")
        rendered_hash = region.get("rendered_input_sha256")
        masked = region.get("masked_exclusions")
        observations = region.get("observations")
        malformed = (
            not isinstance(region_id, str)
            or not bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", region_id))
            or not isinstance(page, int)
            or isinstance(page, bool)
            or page < 1
            or (isinstance(page_count, int) and page > page_count)
            or role not in _OCR_TEXT_ROLES
            or box is None
            or region.get("coordinate_system") != "pdf-points-top-left"
            or not isinstance(width, int)
            or isinstance(width, bool)
            or width <= 0
            or not isinstance(height, int)
            or isinstance(height, bool)
            or height <= 0
            or not isinstance(region_dpi, int)
            or isinstance(region_dpi, bool)
            or region_dpi <= 0
            or not isinstance(rendered_hash, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", rendered_hash)
            or not isinstance(masked, list)
            or not isinstance(observations, list)
            or region.get("ocr_performed") is not True
        )
        if malformed:
            findings.append(
                _finding(
                    "ocr_region_malformed",
                    "structural",
                    (
                        "OCR region identity, page, role, geometry, DPI, input "
                        "hash, or observations are invalid."
                    ),
                    row_path,
                )
            )
            continue
        assert isinstance(region_id, str)
        assert isinstance(page, int)
        assert box is not None
        assert isinstance(width, int)
        assert isinstance(height, int)
        assert isinstance(masked, list)
        assert isinstance(observations, list)
        if region_id in seen_region_ids:
            findings.append(
                _finding(
                    "ocr_region_duplicate",
                    "structural",
                    f"OCR region ID {region_id!r} is duplicated.",
                    row_path,
                )
            )
        seen_region_ids.add(region_id)
        region_pages.add(page)
        if isinstance(manifest_dpi, int) and region_dpi != manifest_dpi:
            findings.append(
                _finding(
                    "ocr_dpi_mismatch",
                    "structural",
                    f"OCR region {region_id!r} DPI does not match the manifest.",
                    row_path,
                )
            )

        valid_observation_boxes: list[
            tuple[tuple[float, float, float, float], int]
        ] = []
        for observation_index, observation in enumerate(observations, start=1):
            confidence = (
                observation.get("confidence")
                if isinstance(observation, dict)
                else None
            )
            text_value = observation.get("text") if isinstance(observation, dict) else None
            pixel_box = (
                _ocr_polygon_box(
                    observation.get("polygon_pixels"),
                    bounds=(0.0, 0.0, float(width), float(height)),
                )
                if isinstance(observation, dict)
                else None
            )
            pdf_box = (
                _ocr_polygon_box(
                    observation.get("polygon_pdf_points"),
                    bounds=box,
                )
                if isinstance(observation, dict)
                else None
            )
            if (
                not isinstance(text_value, str)
                or not text_value.strip()
                or not isinstance(confidence, (int, float))
                or isinstance(confidence, bool)
                or not math.isfinite(float(confidence))
                or not 0.0 <= float(confidence) <= 1.0
                or pixel_box is None
                or pdf_box is None
                or not _ocr_polygon_mapping_matches(
                    observation.get("polygon_pixels")
                    if isinstance(observation, dict)
                    else None,
                    observation.get("polygon_pdf_points")
                    if isinstance(observation, dict)
                    else None,
                    region_box=box,
                    dimensions=(width, height),
                )
            ):
                findings.append(
                    _finding(
                        "ocr_observation_invalid",
                        "scientific",
                        (
                            f"OCR observation {observation_index} has invalid "
                            "text, confidence, or geometry."
                        ),
                        row_path,
                    )
                )
            elif pdf_box is not None:
                valid_observation_boxes.append((pdf_box, observation_index))

        parsed_regions.append(
            (
                region_id,
                page,
                box,
                (width, height),
                [entry for entry in masked if isinstance(entry, dict)],
                valid_observation_boxes,
            )
        )

    missing_region_pages = sorted(summary_pages - region_pages)
    unexpected_region_pages = sorted(region_pages - summary_pages)
    if missing_region_pages:
        findings.append(
            _finding(
                "ocr_page_region_missing",
                "structural",
                "OCR page summary lacks region diagnostics for page(s): "
                + ", ".join(map(str, missing_region_pages))
                + ".",
                "page_analysis.jsonl",
            )
        )
    if unexpected_region_pages:
        findings.append(
            _finding(
                "ocr_region_page_summary_missing",
                "structural",
                "OCR regions lack an OCR page_summary for page(s): "
                + ", ".join(map(str, unexpected_region_pages))
                + ".",
                "page_analysis.jsonl",
            )
        )

    assets = manifest.get("assets")
    visual_assets: list[
        tuple[str, str, int, tuple[float, float, float, float]]
    ] = []
    if isinstance(assets, list):
        for index, asset in enumerate(assets, start=1):
            if not isinstance(asset, dict):
                continue
            role = str(asset.get("category") or asset.get("kind") or "").casefold()
            if role not in _OCR_VISUAL_ROLES:
                continue
            asset_id = asset.get("asset_id")
            page = asset.get("page")
            box = _ocr_box(asset.get("box"))
            asset_path = f"manifest.json.assets[{index}]"
            asset_source_path = asset.get("source_path")
            asset_source_hash = asset.get("source_sha256")
            if (
                not isinstance(asset_id, str)
                or not asset_id.strip()
                or not isinstance(page, int)
                or isinstance(page, bool)
                or page < 1
                or (isinstance(page_count, int) and page > page_count)
                or box is None
                or asset.get("coordinate_system") != "pdf-points-top-left"
            ):
                findings.append(
                    _finding(
                        "ocr_visual_asset_invalid",
                        "structural",
                        "Figure/scheme crop identity, page, or PDF geometry is invalid.",
                        asset_path,
                    )
                )
                continue
            if asset.get("ocr_performed") is not False:
                findings.append(
                    _finding(
                        "ocr_visual_asset_ocr_forbidden",
                        "scientific",
                        "Figure and scheme pixels must explicitly declare ocr_performed=false.",
                        asset_path,
                    )
                )
            if (
                source_path is None
                or not isinstance(asset_source_path, str)
                or asset_source_path.replace("\\", "/") != source_path
                or source_sha256 is None
                or not isinstance(asset_source_hash, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", asset_source_hash)
                or asset_source_hash.casefold() != source_sha256
            ):
                findings.append(
                    _finding(
                        "ocr_visual_asset_source_mismatch",
                        "critical",
                        "Figure/scheme crop source path or hash does not match the main PDF.",
                        asset_path,
                    )
                )
            assert isinstance(asset_id, str)
            assert isinstance(page, int)
            assert box is not None
            visual_assets.append((asset_id, role, page, box))

    for region_id, page, region_box, dimensions, masked, observations in parsed_regions:
        for exclusion_index, exclusion in enumerate(masked, start=1):
            exclusion_box = _ocr_box(exclusion.get("box"))
            intersection_box = _ocr_box(exclusion.get("intersection_box"))
            pixel_box = _ocr_box(exclusion.get("masked_pixel_box"))
            expected_intersection = (
                _ocr_intersection(region_box, exclusion_box)
                if exclusion_box is not None
                else None
            )
            matching_assets = [
                asset_id
                for asset_id, role, asset_page, asset_box in visual_assets
                if asset_page == page
                and exclusion.get("role") == role
                and exclusion_box is not None
                and _ocr_boxes_equal(exclusion_box, asset_box)
            ]
            if (
                exclusion.get("role") not in _OCR_VISUAL_ROLES
                or not isinstance(exclusion.get("exclusion_id"), str)
                or exclusion_box is None
                or intersection_box is None
                or pixel_box is None
                or pixel_box[0] < 0
                or pixel_box[1] < 0
                or pixel_box[2] > dimensions[0]
                or pixel_box[3] > dimensions[1]
                or expected_intersection is None
                or not _ocr_boxes_equal(intersection_box, expected_intersection)
            ):
                findings.append(
                    _finding(
                        "ocr_visual_exclusion_invalid",
                        "structural",
                        (
                            f"Masked exclusion {exclusion_index} in OCR region "
                            f"{region_id!r} is invalid."
                        ),
                        "page_analysis.jsonl",
                    )
                )
            if not matching_assets:
                findings.append(
                    _finding(
                        "ocr_visual_exclusion_unmatched",
                        "structural",
                        (
                            f"Masked exclusion {exclusion_index} in OCR region "
                            f"{region_id!r} has no matching figure/scheme crop."
                        ),
                        "page_analysis.jsonl",
                    )
                )

        for asset_id, role, asset_page, asset_box in visual_assets:
            if asset_page != page:
                continue
            intersection = _ocr_intersection(region_box, asset_box)
            if intersection is None:
                continue
            matching_mask = None
            for exclusion in masked:
                exclusion_box = _ocr_box(exclusion.get("box"))
                intersection_box = _ocr_box(exclusion.get("intersection_box"))
                if (
                    exclusion.get("role") == role
                    and exclusion_box is not None
                    and intersection_box is not None
                    and _ocr_boxes_equal(exclusion_box, asset_box)
                    and _ocr_boxes_equal(intersection_box, intersection)
                ):
                    matching_mask = exclusion
                    break
            if matching_mask is None:
                findings.append(
                    _finding(
                        "ocr_visual_exclusion_missing",
                        "scientific",
                        (
                            f"OCR region {region_id!r} intersects {role} "
                            f"{asset_id!r} without a recorded mask."
                        ),
                        "page_analysis.jsonl",
                    )
                )
            for observation_box, observation_index in observations:
                if _ocr_intersection(observation_box, asset_box) is not None:
                    findings.append(
                        _finding(
                            "ocr_observation_overlaps_visual",
                            "scientific",
                            (
                                f"OCR observation {observation_index} in region "
                                f"{region_id!r} overlaps {role} {asset_id!r}."
                            ),
                            "page_analysis.jsonl",
                        )
                    )

    return findings


def _source_geometry_pages(
    value: Any, page_count: int | None
) -> list[int] | None:
    if not isinstance(value, list) or not value:
        return None
    pages: list[int] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        page = item.get("page")
        bbox = item.get("bbox")
        if (
            not isinstance(page, int)
            or isinstance(page, bool)
            or page < 1
            or (page_count is not None and page > page_count)
            or not isinstance(bbox, list)
            or len(bbox) != 4
            or any(
                not isinstance(coordinate, (int, float))
                or isinstance(coordinate, bool)
                or not math.isfinite(float(coordinate))
                for coordinate in bbox
            )
            or not (bbox[0] < bbox[2] and bbox[1] < bbox[3])
        ):
            return None
        pages.append(page)
    return pages if pages == sorted(pages) else None


def _pdf_locator_matches_geometry(
    locator: str, geometry: Sequence[Mapping[str, Any]]
) -> bool:
    if "page_analysis.jsonl" in locator:
        return False
    pages = [int(item["page"]) for item in geometry]
    unique_pages = sorted(set(pages))
    locator_match = _PDF_MULTI_BLOCK_LOCATOR_RE.fullmatch(locator)
    if len(unique_pages) > 1:
        if locator_match is None:
            return False
        locator_pages = [
            int(value) for value in locator_match.group("pages").split(", ")
        ]
        return locator_pages == unique_pages
    if len(unique_pages) != 1:
        return False
    single_match = _PDF_SINGLE_BLOCK_LOCATOR_RE.fullmatch(locator)
    if single_match is None or int(single_match.group("page")) != unique_pages[0]:
        return False
    boxes = [item["bbox"] for item in geometry]
    expected_box = (
        min(float(box[0]) for box in boxes),
        min(float(box[1]) for box in boxes),
        max(float(box[2]) for box in boxes),
        max(float(box[3]) for box in boxes),
    )
    expected_locator = (
        f"PDF page {unique_pages[0]}, box "
        f"[{expected_box[0]:.2f}, {expected_box[1]:.2f}, "
        f"{expected_box[2]:.2f}, {expected_box[3]:.2f}]"
    )
    return locator == expected_locator


def _validate_pdf_block_provenance(
    loaded: Mapping[str, Any],
) -> list[ValidationFinding]:
    """Require independently checkable source geometry for PDF article blocks."""

    manifest = loaded.get("manifest.json")
    if not isinstance(manifest, dict):
        return []
    text_extraction = manifest.get("text_extraction")
    if (
        not isinstance(text_extraction, dict)
        or text_extraction.get("source_role") != "main_pdf"
    ):
        return []

    findings: list[ValidationFinding] = []
    declared_path = text_extraction.get("source_path")
    if not isinstance(declared_path, str) or not declared_path.strip():
        findings.append(
            _finding(
                "pdf_text_source_provenance_missing",
                "structural",
                "A PDF-primary manifest must identify its text source path.",
                "manifest.json",
            )
        )
        return findings
    source_path = declared_path.replace("\\", "/")

    page_count: int | None = None
    sources_document = loaded.get("sources.json")
    sources = sources_document.get("sources") if isinstance(sources_document, dict) else None
    if isinstance(sources, list):
        main_source = next(
            (
                source
                for source in sources
                if isinstance(source, dict)
                and source.get("role") == "main_pdf"
                and isinstance(source.get("path"), str)
                and source["path"].replace("\\", "/") == source_path
            ),
            None,
        )
        if isinstance(main_source, dict):
            raw_page_count = main_source.get("page_count")
            if (
                isinstance(raw_page_count, int)
                and not isinstance(raw_page_count, bool)
                and raw_page_count > 0
            ):
                page_count = raw_page_count

    blocks = loaded.get("blocks.jsonl")
    if not isinstance(blocks, list) or not blocks:
        findings.append(
            _finding(
                "pdf_blocks_missing",
                "structural",
                "blocks.jsonl is required for a PDF-primary candidate.",
                "blocks.jsonl",
            )
        )
        return findings

    coverage_rows = loaded.get("coverage.jsonl")
    coverage_by_output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if isinstance(coverage_rows, list):
        for row in coverage_rows:
            if (
                isinstance(row, dict)
                and row.get("status") == "included"
                and isinstance(row.get("output_id"), str)
                and row["output_id"]
            ):
                coverage_by_output[row["output_id"]].append(row)

    pdf_heading_rows = [
        row
        for row in coverage_rows or []
        if isinstance(row, dict)
        and row.get("status") == "included"
        and row.get("content_kind") == "section_heading"
    ] if isinstance(coverage_rows, list) else []
    if not pdf_heading_rows:
        findings.append(
            _finding(
                "pdf_section_heading_provenance_missing",
                "structural",
                "A PDF-primary candidate must map section headings to PDF evidence.",
                "coverage.jsonl",
            )
        )
    for row in pdf_heading_rows:
        coverage_id = row.get("coverage_id")
        row_path = (
            f"coverage.jsonl:{coverage_id}"
            if isinstance(coverage_id, str) and coverage_id
            else "coverage.jsonl"
        )
        locator = row.get("source_locator")
        geometry = row.get("source_geometry")
        geometry_pages = _source_geometry_pages(geometry, page_count)
        row_source_path = row.get("source_path")
        if (
            not isinstance(row_source_path, str)
            or row_source_path.replace("\\", "/") != source_path
            or not isinstance(locator, str)
            or not locator.strip()
            or geometry_pages is None
            or not _pdf_locator_matches_geometry(locator, geometry)
        ):
            findings.append(
                _finding(
                    "pdf_section_heading_provenance_invalid",
                    "structural",
                    (
                        "A PDF section heading must identify the main PDF and store "
                        "valid source line geometry."
                    ),
                    row_path,
                )
            )

    main_pdf_blocks = 0
    for index, row in enumerate(blocks, start=1):
        if not isinstance(row, dict):
            continue
        row_path = f"blocks.jsonl:{index}"
        block_id = row.get("block_id")
        row_source_path = row.get("source_path")
        locator = row.get("source_locator")
        missing = [
            key
            for key, value in (
                ("block_id", block_id),
                ("source_path", row_source_path),
                ("source_locator", locator),
            )
            if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            findings.append(
                _finding(
                    "block_source_provenance_missing",
                    "structural",
                    "Block provenance is missing: " + ", ".join(missing) + ".",
                    row_path,
                )
            )
            continue
        assert isinstance(block_id, str)
        assert isinstance(row_source_path, str)
        assert isinstance(locator, str)
        if row_source_path.replace("\\", "/") != source_path:
            continue
        main_pdf_blocks += 1

        geometry = row.get("source_geometry")
        if not isinstance(geometry, list) or not geometry:
            findings.append(
                _finding(
                    "pdf_block_geometry_missing",
                    "structural",
                    "A main-PDF block must store ordered line geometry in source_geometry.",
                    row_path,
                )
            )
            geometry_valid = False
            geometry_pages: list[int] = []
        else:
            parsed_pages = _source_geometry_pages(geometry, page_count)
            geometry_valid = parsed_pages is not None
            geometry_pages = parsed_pages or []
            if not geometry_valid:
                findings.append(
                    _finding(
                        "pdf_block_geometry_invalid",
                        "structural",
                        (
                            "source_geometry must contain ordered objects with a valid "
                            "PDF page and nonempty finite bbox."
                        ),
                        row_path,
                    )
                )

        locator_unsupported = "page_analysis.jsonl" in locator or (
            geometry_valid
            and not _pdf_locator_matches_geometry(locator, geometry)
        )
        if locator_unsupported:
            findings.append(
                _finding(
                    "pdf_block_locator_unsupported",
                    "structural",
                    (
                        "A multi-page block locator must reference its own exact "
                        "source_geometry, not an unrelated page-analysis report."
                    ),
                    row_path,
                )
            )

        matching_coverage = coverage_by_output.get(block_id, [])
        if not matching_coverage:
            findings.append(
                _finding(
                    "pdf_block_coverage_provenance_missing",
                    "structural",
                    "A main-PDF block has no included reverse-coverage entry.",
                    row_path,
                )
            )
        elif not any(
            candidate.get("source_path") == row_source_path
            and candidate.get("source_locator") == locator
            and candidate.get("source_geometry") == geometry
            for candidate in matching_coverage
        ):
            findings.append(
                _finding(
                    "pdf_block_coverage_provenance_mismatch",
                    "structural",
                    (
                        "The block and its included coverage entry disagree on "
                        "source path, locator, or geometry."
                    ),
                    row_path,
                )
            )

    if main_pdf_blocks == 0:
        findings.append(
            _finding(
                "pdf_main_blocks_missing",
                "structural",
                "blocks.jsonl contains no blocks attributed to the main PDF.",
                "blocks.jsonl",
            )
        )
    return findings


def _validate_confidence(value: Any) -> list[ValidationFinding]:
    if not isinstance(value, dict):
        return [
            _finding(
                "confidence_invalid",
                "structural",
                "confidence.json must contain an object.",
                "confidence.json",
            )
        ]
    categories = value.get("categories")
    if not isinstance(categories, dict) or not categories:
        return [
            _finding(
                "confidence_categories_missing",
                "structural",
                "confidence.json must contain nonempty confidence categories.",
                "confidence.json",
            )
        ]
    findings: list[ValidationFinding] = []
    for category, entry in categories.items():
        if (
            not isinstance(category, str)
            or not isinstance(entry, dict)
            or entry.get("level")
            not in {"high", "medium", "unresolved", "not_applicable"}
            or not isinstance(entry.get("basis"), str)
            or not entry.get("basis", "").strip()
        ):
            findings.append(
                _finding(
                    "confidence_category_invalid",
                    "structural",
                    f"Invalid confidence category: {category!r}.",
                    "confidence.json",
                )
            )
    return findings


def _walk_objects(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_objects(nested)


def _collect_hash_records(loaded: Mapping[str, Any]) -> list[_HashRecord]:
    records: list[_HashRecord] = []
    path_keys = ("output_path", "copied_path", "relative_path", "path", "file")
    for origin in ("manifest.json", "sources.json"):
        document = loaded.get(origin)
        if document is None:
            continue
        for item in _walk_objects(document):
            sha = item.get("sha256")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
                continue
            path = next(
                (item.get(key) for key in path_keys if isinstance(item.get(key), str)),
                None,
            )
            if path is None:
                continue
            role = str(item.get("role") or item.get("kind") or "")
            records.append(_HashRecord(path, sha.lower(), role, origin))
    return records


def _candidate_relative_path(raw_path: str) -> PurePosixPath | None:
    normalized = raw_path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized.startswith("/") or _DRIVE_PATH_RE.match(normalized):
        return None
    if "/extraction/" in normalized:
        normalized = normalized.split("/extraction/", 1)[1]
    elif normalized.startswith("extraction/"):
        normalized = normalized[len("extraction/") :]
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        return None
    return pure


def _validate_pdf_crop_boundaries(
    extraction_dir: Path,
    loaded: Mapping[str, Any],
) -> list[ValidationFinding]:
    """Warn when authored-looking pixels reach a rendered PDF crop boundary.

    The check is deliberately advisory.  Edge-to-edge artwork can be valid, so
    a boundary hit requires visual adjudication rather than automatic recropping
    or rejection.  Tables are excluded because authored table rules commonly
    and harmlessly meet the image boundary.
    """

    manifest = loaded.get("manifest.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("assets"), list):
        return []

    try:
        resolved_root = extraction_dir.resolve(strict=True)
    except OSError:
        return []

    findings: list[ValidationFinding] = []
    for asset in manifest["assets"]:
        if not isinstance(asset, dict):
            continue
        role = str(asset.get("category") or asset.get("kind") or "")
        role = role.strip().casefold().replace("-", "_").replace(" ", "_")
        if role not in _PDF_CROP_VISUAL_ROLES:
            continue
        if asset.get("coordinate_system") != "pdf-points-top-left":
            continue

        raw_path = asset.get("output_path")
        if not isinstance(raw_path, str):
            continue
        relative = _candidate_relative_path(raw_path)
        if relative is None:
            continue
        candidate = extraction_dir.joinpath(*relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            # Unsafe and missing asset paths are reported by the canonical asset
            # checks; do not duplicate those findings here.
            continue
        if not resolved.is_file():
            continue

        media_type = str(asset.get("media_type") or "").casefold()
        if media_type and media_type != "image/png":
            continue
        if not media_type and relative.suffix.casefold() != ".png":
            continue

        try:
            with Image.open(resolved) as opened:
                image = opened.convert("RGB")
        except (OSError, ValueError):
            # Image readability is outside this narrowly scoped heuristic.
            continue

        width, height = image.size
        if width < 1 or height < 1:
            continue

        def meaningful_count(box: tuple[int, int, int, int]) -> int:
            edge = image.crop(box)
            return sum(
                1
                for pixel in edge.getdata()
                if min(pixel) < _PDF_CROP_NEAR_WHITE_THRESHOLD
            )

        boundary_counts = {
            "left": meaningful_count((0, 0, 1, height)),
            "right": meaningful_count((width - 1, 0, width, height)),
            "top": meaningful_count((0, 0, width, 1)),
            "bottom": meaningful_count((0, height - 1, width, height)),
        }
        touched = [
            (side, count)
            for side, count in boundary_counts.items()
            if count >= _PDF_CROP_MIN_BOUNDARY_PIXELS
        ]
        if not touched:
            continue

        asset_name = str(asset.get("label") or asset.get("asset_id") or relative)
        boundary_summary = ", ".join(
            f"{side} ({count} {'pixel' if count == 1 else 'pixels'})"
            for side, count in touched
        )
        findings.append(
            _finding(
                "pdf_crop_content_touches_boundary",
                "cosmetic",
                (
                    f"PDF crop {asset_name!r} has meaningful pixels touching the "
                    f"{boundary_summary} boundary. Visually verify that authored "
                    "content is complete; widen the crop if it is clipped, or "
                    "accept the warning for intentional edge-to-edge artwork."
                ),
                relative.as_posix(),
            )
        )
    return findings


def _validate_manifest_file_inventory(
    extraction_dir: Path,
    loaded: Mapping[str, Any],
) -> list[ValidationFinding]:
    """Require ``manifest.files`` to describe the exact extraction tree."""

    findings: list[ValidationFinding] = []
    manifest = loaded.get("manifest.json")
    if not isinstance(manifest, dict):
        return findings
    rows = manifest.get("files")
    if not isinstance(rows, list):
        return [
            _finding(
                "manifest_files_invalid",
                "critical",
                "manifest.files must be an array containing every extraction file.",
                "manifest.json",
            )
        ]

    listed: dict[str, tuple[int, str]] = {}
    for index, row in enumerate(rows, start=1):
        location = f"manifest.json/files/{index - 1}"
        if not isinstance(row, dict):
            findings.append(
                _finding(
                    "manifest_file_entry_invalid",
                    "critical",
                    "Each manifest.files entry must be an object.",
                    location,
                )
            )
            continue
        raw_path = row.get("path")
        byte_count = row.get("bytes")
        sha256 = row.get("sha256")
        if not isinstance(raw_path, str):
            findings.append(
                _finding(
                    "manifest_file_entry_invalid",
                    "critical",
                    "Manifest file entry must contain a string path.",
                    location,
                )
            )
            continue
        pure = _candidate_relative_path(raw_path)
        if (
            pure is None
            or raw_path != pure.as_posix()
            or "\\" in raw_path
            or raw_path.startswith("./")
        ):
            findings.append(
                _finding(
                    "invalid_manifest_path",
                    "critical",
                    f"Manifest output path is unsafe or noncanonical: {raw_path!r}.",
                    location,
                )
            )
            continue
        relative = pure.as_posix()
        if relative in listed:
            findings.append(
                _finding(
                    "duplicate_manifest_file",
                    "critical",
                    f"manifest.files lists {relative!r} more than once.",
                    location,
                )
            )
            continue
        if (
            not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 0
            or not isinstance(sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        ):
            findings.append(
                _finding(
                    "manifest_file_entry_invalid",
                    "critical",
                    "Manifest file entry must contain non-negative bytes and lowercase SHA-256.",
                    location,
                )
            )
            continue
        listed[relative] = (byte_count, sha256)

    actual = {
        path.relative_to(extraction_dir).as_posix(): path
        for path in extraction_dir.rglob("*")
        if path.is_file()
    } if extraction_dir.is_dir() else {}
    for relative in sorted(set(actual) - set(listed)):
        findings.append(
            _finding(
                "manifest_file_unlisted",
                "critical",
                "Extraction file is absent from manifest.files.",
                relative,
            )
        )
    for relative in sorted(set(listed) - set(actual)):
        findings.append(
            _finding(
                "manifest_file_missing",
                "critical",
                "File listed in manifest.files is missing from the extraction.",
                relative,
            )
        )
    for relative in sorted(set(actual) & set(listed)):
        expected_bytes, _expected_sha256 = listed[relative]
        try:
            actual_bytes = actual[relative].stat().st_size
        except OSError as exc:
            findings.append(
                _finding(
                    "output_hash_unreadable",
                    "critical",
                    f"Could not inspect output file: {exc}.",
                    relative,
                )
            )
            continue
        if actual_bytes != expected_bytes:
            findings.append(
                _finding(
                    "output_size_mismatch",
                    "critical",
                    f"Manifest bytes expected {expected_bytes}, found {actual_bytes}.",
                    relative,
                )
            )
    return findings


def _validate_hashes(
    extraction_dir: Path,
    loaded: Mapping[str, Any],
) -> list[ValidationFinding]:
    findings = _validate_manifest_file_inventory(extraction_dir, loaded)
    records = _collect_hash_records(loaded)
    supplement_files = sorted(
        (
            path
            for path in (extraction_dir / "supplementary").rglob("*")
            if path.is_file()
        ),
        key=lambda value: value.as_posix(),
    ) if (extraction_dir / "supplementary").is_dir() else []

    resolved_root = extraction_dir.resolve(strict=False)
    for record in records:
        relative = _candidate_relative_path(record.path)
        if relative is None:
            if record.origin == "manifest.json":
                findings.append(
                    _finding(
                        "invalid_manifest_path",
                        "critical",
                        f"Manifest output path is unsafe: {record.path!r}.",
                        "manifest.json",
                    )
                )
            continue
        candidate = extraction_dir.joinpath(*relative.parts)
        # A sources.json path such as pdf/main.pdf can accidentally look local;
        # only manifest paths, copied paths, and explicit extraction paths are
        # direct output-hash assertions.
        explicit_output = (
            record.origin == "manifest.json"
            or record.path.replace("\\", "/").startswith("extraction/")
            or "/extraction/" in record.path.replace("\\", "/")
            or relative.parts[:1] == ("supplementary",)
        )
        if not explicit_output:
            continue
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            findings.append(
                _finding(
                    "manifest_output_escapes",
                    "critical",
                    f"Manifest output resolves outside extraction: {record.path!r}.",
                    relative.as_posix(),
                )
            )
            continue
        if not candidate.is_file():
            if record.origin == "manifest.json":
                findings.append(
                    _finding(
                        "manifest_file_missing",
                        "critical",
                        "File listed in manifest.json is missing.",
                        relative.as_posix(),
                    )
                )
            continue
        try:
            actual = sha256_file(candidate)
        except OSError as exc:
            findings.append(
                _finding(
                    "output_hash_unreadable",
                    "critical",
                    f"Could not hash output file: {exc}.",
                    relative.as_posix(),
                )
            )
            continue
        if actual != record.sha256:
            findings.append(
                _finding(
                    "output_hash_mismatch",
                    "critical",
                    (
                        f"SHA-256 differs from {record.origin}: expected "
                        f"{record.sha256}, got {actual}."
                    ),
                    relative.as_posix(),
                )
            )

    source_supplements = [
        record
        for record in records
        if record.origin == "sources.json"
        and (
            "supplement" in record.role.casefold()
            or "/supplementary/" in f"/{record.path.replace(chr(92), '/').casefold()}"
        )
    ]
    files_by_name: dict[str, list[Path]] = defaultdict(list)
    for path in supplement_files:
        files_by_name[path.name.casefold()].append(path)

    for source in source_supplements:
        basename = PurePosixPath(source.path.replace("\\", "/")).name.casefold()
        matches = files_by_name.get(basename, [])
        if len(matches) != 1:
            findings.append(
                _finding(
                    "supplement_copy_not_identifiable",
                    "structural",
                    (
                        f"Expected one copied supplementary file named {basename!r}; "
                        f"found {len(matches)}."
                    ),
                    "supplementary",
                )
            )
            continue
        copied = matches[0]
        relative = copied.relative_to(extraction_dir).as_posix()
        try:
            copied.resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError):
            findings.append(
                _finding(
                    "supplement_copy_escapes",
                    "critical",
                    "Copied supplement resolves outside extraction.",
                    relative,
                )
            )
            continue
        try:
            actual = sha256_file(copied)
        except OSError as exc:
            findings.append(
                _finding(
                    "supplement_hash_unreadable",
                    "critical",
                    f"Could not hash copied supplement: {exc}.",
                    relative,
                )
            )
            continue
        if actual != source.sha256:
            findings.append(
                _finding(
                    "supplement_copy_hash_mismatch",
                    "critical",
                    (
                        f"Copied supplement differs from its source: expected "
                        f"{source.sha256}, got {actual}."
                    ),
                    relative,
                )
            )

    if supplement_files and not source_supplements:
        findings.append(
            _finding(
                "supplement_hash_unverifiable",
                "structural",
                "No supplementary source checksums were found in sources.json.",
                "sources.json",
            )
        )
    return findings


def _content_findings(record_text: str) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    checks: Sequence[tuple[str, str, str, re.Pattern[str]]] = (
        (
            "publisher_equation_placeholder",
            "scientific",
            "Publisher equation-image placeholder remains in record.md.",
            re.compile(r"equation/tex2gif[^\s)>\]]*", re.IGNORECASE),
        ),
        (
            "cid_placeholder",
            "scientific",
            "Unresolved PDF CID placeholder remains in record.md.",
            re.compile(r"\(\s*cid:\s*\d+\s*\)", re.IGNORECASE),
        ),
        (
            "unicode_replacement_character",
            "scientific",
            "Unicode replacement character remains in record.md.",
            re.compile("\ufffd"),
        ),
        (
            "mojibake",
            "scientific",
            "Probable mojibake remains in record.md.",
            re.compile(
                r"(?:"
                r"Ã(?=[\u0080-\u00ff\u2010-\u202f])|"
                r"Â(?=[\s\u0080-\u00ff\u2010-\u202f])|"
                r"â(?=[\u0080-\u00ff\u02c6\u2010-\u2030\u20ac])|"
                r"[ÎÏ](?=[\u0080-\u00ff\u2010-\u202f\u20ac])|"
                r"ðŸ|ï»¿"
                r")"
            ),
        ),
        (
            "ambiguous_markdown_emphasis",
            "structural",
            "Ambiguous run of four or more Markdown emphasis delimiters remains.",
            re.compile(r"\*{4,}"),
        ),
    )
    for code, severity, message, pattern in checks:
        for match in pattern.finditer(record_text):
            line = record_text.count("\n", 0, match.start()) + 1
            findings.append(_finding(code, severity, message, f"record.md:{line}"))

    ui_phrases = (
        "Get access",
        "Log in to Wiley Online Library",
        "Sign in to access",
        "Open in figure viewer",
        "Download PDF",
        "View Full Text",
        "FiguresReferencesRelatedDetails",
        "Additional linksAbout Wiley Online Library",
        "ToolsRequest permission",
        "Request Username",
        "Forgot your password",
    )
    for phrase in ui_phrases:
        for match in re.finditer(re.escape(phrase), record_text, re.IGNORECASE):
            line = record_text.count("\n", 0, match.start()) + 1
            findings.append(
                _finding(
                    "publisher_ui_text",
                    "structural",
                    f"Publisher interface text remains: {phrase!r}.",
                    f"record.md:{line}",
                )
            )
    return findings


def _heading_findings(record_text: str) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    previous_level: int | None = None
    h1_lines: list[int] = []
    for line_number, line in enumerate(record_text.splitlines(), start=1):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        if level == 1:
            h1_lines.append(line_number)
        if previous_level is not None and level > previous_level + 1:
            findings.append(
                _finding(
                    "heading_level_jump",
                    "structural",
                    f"Heading level jumps from H{previous_level} to H{level}.",
                    f"record.md:{line_number}",
                )
            )
        previous_level = level
    if len(h1_lines) != 1 or h1_lines != [1]:
        findings.append(
            _finding(
                "invalid_h1_structure",
                "structural",
                "record.md must contain exactly one H1 heading, on line 1.",
                "record.md",
            )
        )
    return findings


def _required_asset_findings(record_text: str) -> list[ValidationFinding]:
    """Require figures to have pixels and every table to link its JSON."""

    findings: list[ValidationFinding] = []
    active_h2 = ""
    pending: tuple[str, int, str] | None = None
    has_required_asset = False

    def flush() -> None:
        nonlocal pending, has_required_asset
        if pending is not None and not has_required_asset:
            label, line_number, kind = pending
            findings.append(
                _finding(
                    "missing_consolidated_asset",
                    "scientific",
                    f"{label} has no linked local {kind} asset in its consolidated section.",
                    f"record.md:{line_number}",
                )
            )
        pending = None
        has_required_asset = False

    for line_number, line in enumerate(_strip_code(record_text).splitlines(), start=1):
        h2 = re.fullmatch(r"##\s+(.+?)\s*", line)
        if h2:
            flush()
            active_h2 = h2.group(1).casefold()
            continue
        heading = re.fullmatch(
            r"#{3,6}\s+(Figure|Scheme|Table)\s+([A-Za-z]?\d+)\b.*",
            line,
            flags=re.IGNORECASE,
        )
        if heading:
            flush()
            kind = heading.group(1).casefold()
            identifier = heading.group(2).upper()
            label = f"{heading.group(1).title()} {identifier}"
            if (
                active_h2 == "figure and scheme captions"
                and kind in {"figure", "scheme"}
                and not identifier.startswith("S")
            ) or (
                active_h2 == "supplementary materials"
                and kind == "figure"
                and identifier.startswith("S")
            ):
                pending = (label, line_number, "figure")
            elif active_h2 == "tables" and kind == "table":
                pending = (label, line_number, "structured JSON")
            continue
        if pending is not None:
            if pending[2] == "figure" and re.match(r"^Asset:\s+\[", line):
                has_required_asset = True
            elif pending[2] == "structured JSON" and "[structured data](" in line:
                has_required_asset = True
    flush()
    return findings


def _table_derivative_findings(
    extraction_dir: Path, linked_files: set[Path]
) -> list[ValidationFinding]:
    """Validate source-aware table JSON/image output policy.

    New table JSON explicitly declares top-level ``source_kind`` and, when
    needed, ``source_image``.  Source paths and locators remain private
    diagnostics.  A narrow legacy fallback still supplies actionable
    source-policy findings for older pilots, although only current JSON can
    satisfy the versioned schema.
    """

    findings: list[ValidationFinding] = []
    tables_root = extraction_dir / "tables"
    if tables_root.is_dir():
        for csv_path in sorted(
            tables_root.rglob("*.csv"),
            key=lambda path: path.relative_to(extraction_dir).as_posix(),
        ):
            findings.append(
                _finding(
                    "unexpected_table_csv",
                    "structural",
                    "Table extractions must use JSON, not CSV derivatives.",
                    csv_path.relative_to(extraction_dir).as_posix(),
                )
            )

    table_root = tables_root / "main"
    if not table_root.is_dir():
        return findings

    schema_unavailable_reported = False
    for json_path in sorted(table_root.glob("*.json"), key=lambda path: path.name):
        relative_json = json_path.relative_to(extraction_dir).as_posix()
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            findings.append(
                _finding(
                    "invalid_table_json",
                    "critical",
                    f"Table JSON cannot be read as valid UTF-8 JSON: {exc}.",
                    relative_json,
                )
            )
            continue
        if not isinstance(payload, dict):
            findings.append(
                _finding(
                    "invalid_table_json",
                    "structural",
                    "Table JSON root must be an object.",
                    relative_json,
                )
            )
            continue

        try:
            validate_table_payload(payload, source_path=relative_json)
        except TableSchemaError as exc:
            if exc.violations:
                for violation in exc.violations:
                    findings.append(
                        _finding(
                            "invalid_table_schema",
                            "structural",
                            (
                                f"Table JSON {violation.instance_path}: "
                                f"{violation.message} "
                                f"(schema {violation.schema_path})."
                            ),
                            relative_json,
                        )
                    )
            elif not schema_unavailable_reported:
                findings.append(
                    _finding(
                        "table_schema_unavailable",
                        "critical",
                        str(exc),
                        "scripts/extraction/schemas/table-1.0.schema.json",
                    )
                )
                schema_unavailable_reported = True

        table_id = str(payload.get("table_id") or json_path.stem)
        label = str(payload.get("label") or table_id)
        legacy_source = payload.get("source")
        if not isinstance(legacy_source, dict):
            legacy_source = {}
        declared_kind = payload.get("source_kind", legacy_source.get("kind"))
        source_kind = str(declared_kind or "").strip().casefold()
        if not source_kind:
            # Compatibility with pilot JSON written before source_kind was
            # explicit. HTML can be inferred safely from its former private
            # source path; other formats remain unknown.
            source_suffix = PurePosixPath(
                str(legacy_source.get("path") or "")
            ).suffix.casefold()
            source_kind = "html" if source_suffix in {".html", ".htm"} else ""
        if source_kind not in _TABLE_SOURCE_KINDS:
            findings.append(
                _finding(
                    "invalid_table_source_kind",
                    "structural",
                    f"{label} has unsupported source kind {declared_kind!r}.",
                    relative_json,
                )
            )
            continue

        raw_image_path = payload.get(
            "source_image", legacy_source.get("image_path")
        )
        image_path = str(raw_image_path).strip() if raw_image_path is not None else ""
        canonical_image = table_root / f"{table_id}.png"

        if source_kind == "html":
            if declared_kind is not None and (image_path or canonical_image.is_file()):
                findings.append(
                    _finding(
                        "unexpected_html_table_source_image",
                        "structural",
                        f"{label} is HTML-native and must not emit a redundant full-table image.",
                        image_path or canonical_image.relative_to(extraction_dir).as_posix(),
                    )
                )
            continue

        if not image_path:
            findings.append(
                _finding(
                    "missing_table_source_image",
                    "scientific",
                    f"{label} is {source_kind}-sourced but its JSON does not identify a source image.",
                    relative_json,
                )
            )
            continue

        pure = PurePosixPath(image_path)
        if (
            "\\" in image_path
            or pure.is_absolute()
            or _DRIVE_PATH_RE.match(image_path)
            or ".." in pure.parts
        ):
            findings.append(
                _finding(
                    "unsafe_table_source_image",
                    "critical",
                    f"{label} has an unsafe source-image path {image_path!r}.",
                    relative_json,
                )
            )
            continue
        relative_image = Path(*pure.parts)
        candidate = extraction_dir / relative_image
        if not candidate.is_file():
            findings.append(
                _finding(
                    "missing_table_source_image",
                    "scientific",
                    f"{label} source image does not exist: {image_path!r}.",
                    image_path,
                )
            )
        elif relative_image not in linked_files:
            findings.append(
                _finding(
                    "unlinked_table_source_image",
                    "structural",
                    f"{label} source image is not linked from record.md.",
                    image_path,
                )
            )

    return findings


def _caption_counts(record_text: str, extraction_dir: Path) -> dict[str, int]:
    figures: set[str] = set()
    schemes: set[str] = set()
    tables: set[str] = set()
    supplementary_figures: set[str] = set()
    references = 0

    # Count only canonical consolidated-section headings. Labels in the local
    # asset index, article prose, or captions must not let a truncated record
    # satisfy completeness checks.
    active_h2 = ""
    for line in _strip_code(record_text).splitlines():
        h2 = re.fullmatch(r"##\s+(.+?)\s*", line)
        if h2:
            active_h2 = h2.group(1).casefold()
            continue
        if active_h2 == "references" and re.match(r"^\s*-\s+\d+[.\]]\s+", line):
            references += 1
        heading = re.fullmatch(
            r"#{3,6}\s+(Figure|Scheme|Table)\s+([A-Za-z]?\d+)\b.*",
            line,
            flags=re.IGNORECASE,
        )
        if not heading:
            continue
        kind = heading.group(1).casefold()
        identifier = heading.group(2).upper()
        if active_h2 == "figure and scheme captions":
            if kind == "figure" and not identifier.startswith("S"):
                figures.add(identifier)
            elif kind == "scheme" and not identifier.startswith("S"):
                schemes.add(identifier)
        elif active_h2 == "tables":
            if kind == "table" and not identifier.startswith("S"):
                tables.add(identifier)
        elif active_h2 == "supplementary materials":
            if kind == "figure" and identifier.startswith("S"):
                supplementary_figures.add(identifier)

    supplement_root = extraction_dir / "supplementary"
    # Original preserved supplements live either directly below
    # ``supplementary/`` or directly below a ``supplement_NNN/`` directory.
    # Extracted figures/tables beneath those directories are derivatives and
    # must not inflate the source-supplement count.
    supplementary_files = 0
    if supplement_root.is_dir():
        for path in supplement_root.rglob("*"):
            if not path.is_file():
                continue
            depth = len(path.relative_to(supplement_root).parts)
            if depth <= 2:
                supplementary_files += 1
    return {
        "references": references,
        "equations": 0,
        "main_figures": len(figures),
        "main_schemes": len(schemes),
        "main_figures_and_schemes": len(figures) + len(schemes),
        "tables": len(tables),
        "supplementary_figures": len(supplementary_figures),
        "supplementary_files": supplementary_files,
        "presentation_embedded_files": 0,
    }


def _diagnostic_page_count(loaded: Mapping[str, Any]) -> int:
    sources_document = loaded.get("sources.json")
    sources = (
        sources_document.get("sources")
        if isinstance(sources_document, dict)
        else None
    )
    if not isinstance(sources, list):
        return 0
    main_pdfs = [
        item
        for item in sources
        if isinstance(item, dict) and item.get("role") == "main_pdf"
    ]
    if len(main_pdfs) != 1:
        return 0
    value = main_pdfs[0].get("page_count")
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else 0
    )


def _normalize_expected_counts(raw: Mapping[str, Any]) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for raw_key, value in raw.items():
        key = _COUNT_ALIASES.get(str(raw_key), str(raw_key))
        if key not in COUNT_KEYS or isinstance(value, bool):
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count >= 0:
            normalized[key] = count
    return normalized


def _diagnostic_expected_counts(loaded: Mapping[str, Any]) -> dict[str, int]:
    for name in ("quality.json", "manifest.json"):
        document = loaded.get(name)
        for item in _walk_objects(document):
            raw = item.get("expected_counts")
            if isinstance(raw, dict):
                normalized = _normalize_expected_counts(raw)
                if normalized:
                    return normalized
    return {}


def _record_id(loaded: Mapping[str, Any], extraction_dir: Path) -> str | None:
    for name in ("manifest.json", "sources.json", "quality.json"):
        for item in _walk_objects(loaded.get(name)):
            value = item.get("record_id")
            if isinstance(value, str) and re.fullmatch(r"\d{5}", value):
                return value
    for path in (extraction_dir, *extraction_dir.parents):
        if re.fullmatch(r"\d{5}", path.name):
            return path.name
    return None


def _count_findings(
    actual: Mapping[str, int],
    expected: Mapping[str, int],
    *,
    record_path: str = "record.md",
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for key, expected_value in sorted(expected.items()):
        actual_value = actual.get(key, 0)
        if actual_value != expected_value:
            findings.append(
                _finding(
                    "content_count_mismatch",
                    "scientific",
                    f"{key}: expected {expected_value}, found {actual_value}.",
                    record_path,
                )
            )
    return findings


_RECORD_JSON_MEDIA_TYPE = "application/vnd.pip-litdb.record+json"
_SAFE_RECORD_TAGS = frozenset(
    {"a", "br", "em", "strong", "sub", "sup", "li", "ol", "ul"}
)
_VOID_RECORD_TAGS = frozenset({"br"})
_SUBSCRIPT_RECORD_CHARS = "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₔ"
_SUPERSCRIPT_RECORD_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿ"
_RECORD_SCRIPT_TRANSLATION = str.maketrans(
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₔ⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿ",
    "0123456789+-=()aeoxə0123456789+-=()in",
)
_DIAGNOSTIC_ONLY_RECORD_KEYS = frozenset(
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


class _SafeRecordHtmlParser(HTMLParser):
    """Independently check the executable surface of record rich text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        folded = tag.casefold()
        if folded not in _SAFE_RECORD_TAGS:
            self.errors.append(f"forbidden tag <{folded}>")
            return
        if folded == "a":
            if len(attrs) != 1 or attrs[0][0].casefold() != "href":
                self.errors.append("links may contain only href")
            else:
                value = attrs[0][1] or ""
                parsed = urlsplit(value)
                if (
                    not value
                    or value.startswith(("//", "\\"))
                    or (parsed.scheme and parsed.scheme.casefold() not in {"http", "https", "mailto"})
                ):
                    self.errors.append("link uses an unsafe target")
        elif attrs:
            self.errors.append(f"tag <{folded}> may not have attributes")
        if folded in {"br", "li"}:
            self.text_parts.append("\n")
        elif folded == "sub":
            self.text_parts.append("_{")
        elif folded == "sup":
            self.text_parts.append("^{")
        if folded not in _VOID_RECORD_TAGS:
            self.stack.append(folded)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        folded = tag.casefold()
        if folded not in _VOID_RECORD_TAGS:
            self.handle_endtag(folded)

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if folded in _VOID_RECORD_TAGS:
            return
        if not self.stack or self.stack[-1] != folded:
            self.errors.append(f"unmatched </{folded}> tag")
            return
        self.stack.pop()
        if folded in {"li", "ol", "ul"}:
            self.text_parts.append("\n")
        elif folded in {"sub", "sup"}:
            self.text_parts.append("}")

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)

    def finish(self) -> tuple[str, ...]:
        if self.stack:
            self.errors.append(f"unclosed <{self.stack[-1]}> tag")
        return tuple(self.errors)

    @property
    def visible_text(self) -> str:
        return "".join(self.text_parts)


def _normalize_record_visible_text(value: str) -> str:
    value = re.sub(
        f"[{re.escape(_SUBSCRIPT_RECORD_CHARS)}]+",
        lambda match: "_{"
        + match.group(0).translate(_RECORD_SCRIPT_TRANSLATION)
        + "}",
        value,
    )
    value = re.sub(
        f"[{re.escape(_SUPERSCRIPT_RECORD_CHARS)}]+",
        lambda match: "^{"
        + match.group(0).translate(_RECORD_SCRIPT_TRANSLATION)
        + "}",
        value,
    )
    # Mirror the serializer's narrow presentation equivalence for compact
    # oxidation states such as FeII / Fe<sup>II</sup>.  Other superscripts keep
    # their markers so exponent and charge contradictions are still rejected.
    value = re.sub(r"\^\{([IVXLCDM]+)\}", r"\1", value)
    value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    value = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", "", value)
    return " ".join(value.split())


def _json_pointer(*parts: str | int) -> str:
    return "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1")
        for part in parts
    )


def _portable_record_asset_path(value: Any) -> PurePosixPath | None:
    if not isinstance(value, str) or not value:
        return None
    if any(character in value for character in "\x00\r\n\\?:"):
        return None
    pure = PurePosixPath(value)
    if pure.is_absolute() or value.startswith("/"):
        return None
    if any(part in {"", ".", ".."} for part in value.split("/")):
        return None
    if pure.as_posix() != value or _URI_SCHEME_RE.match(value):
        return None
    return pure


def _walk_record_json(
    value: Any, parts: tuple[str | int, ...] = ()
) -> Iterator[tuple[str, Any]]:
    pointer = _json_pointer(*parts)
    yield pointer, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_record_json(child, (*parts, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_record_json(child, (*parts, index))


def _resolve_json_pointer(document: Any, pointer: str) -> tuple[bool, Any]:
    if pointer == "":
        return True, document
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False, None
    current = document
    for raw in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            return False, None
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return False, None
            current = current[token]
        elif isinstance(current, list):
            if not re.fullmatch(r"0|[1-9]\d*", token):
                return False, None
            index = int(token)
            if index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def _record_json_content_findings(payload: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    checks: Sequence[tuple[str, str, str, re.Pattern[str]]] = (
        (
            "publisher_equation_placeholder",
            "scientific",
            "Publisher equation-image placeholder remains in record.json.",
            re.compile(r"equation/tex2gif[^\s)>\]]*", re.IGNORECASE),
        ),
        (
            "cid_placeholder",
            "scientific",
            "Unresolved PDF CID placeholder remains in record.json.",
            re.compile(r"\(\s*cid:\s*\d+\s*\)", re.IGNORECASE),
        ),
        (
            "unicode_replacement_character",
            "scientific",
            "Unicode replacement character remains in record.json.",
            re.compile("\ufffd"),
        ),
        (
            "mojibake",
            "scientific",
            "Probable mojibake remains in record.json.",
            re.compile(
                r"(?:Ã(?=[\u0080-\u00ff\u2010-\u202f])|"
                r"Â(?=[\s\u0080-\u00ff\u2010-\u202f])|"
                r"â(?=[\u0080-\u00ff\u02c6\u2010-\u2030\u20ac])|"
                r"[ÎÏ](?=[\u0080-\u00ff\u2010-\u202f\u20ac])|ðŸ|ï»¿)"
            ),
        ),
    )
    ui_phrases = (
        "Get access",
        "Log in to Wiley Online Library",
        "Sign in to access",
        "Open in figure viewer",
        "Download PDF",
        "View Full Text",
        "FiguresReferencesRelatedDetails",
        "Additional linksAbout Wiley Online Library",
        "ToolsRequest permission",
        "Request Username",
        "Forgot your password",
    )
    for pointer, value in _walk_record_json(payload):
        if not isinstance(value, str):
            continue
        if pointer.endswith("/html"):
            parser = _SafeRecordHtmlParser()
            try:
                parser.feed(value)
                parser.close()
                html_errors = parser.finish()
            except (ValueError, RuntimeError) as exc:
                html_errors = (str(exc),)
            for message in html_errors:
                findings.append(
                    _finding(
                        "unsafe_record_html",
                        "critical",
                        f"Unsafe rich HTML: {message}.",
                        f"record.json{pointer}",
                    )
                )
            continue
        if not (
            pointer.endswith("/plain_text")
            or pointer.endswith("/heading")
            or pointer in {"/record/title"}
            or re.search(r"/parts/\d+/rows/\d+/\d+/text$", pointer)
        ):
            continue
        for code, severity, message, pattern in checks:
            if pattern.search(value):
                findings.append(
                    _finding(code, severity, message, f"record.json{pointer}")
                )
        for phrase in ui_phrases:
            if re.search(re.escape(phrase), value, re.IGNORECASE):
                findings.append(
                    _finding(
                        "publisher_ui_text",
                        "structural",
                        f"Publisher interface text remains: {phrase!r}.",
                        f"record.json{pointer}",
                    )
                )
    return findings


def _record_json_dual_text_findings(
    payload: Mapping[str, Any]
) -> list[ValidationFinding]:
    """Reject contradictory machine/plain and human/rich representations."""

    findings: list[ValidationFinding] = []
    for pointer, value in _walk_record_json(payload):
        if not isinstance(value, dict) or "/machine_records/" in pointer:
            continue
        pairs: list[tuple[str, str]] = []
        if isinstance(value.get("plain_text"), str) and isinstance(value.get("html"), str):
            pairs.append((value["plain_text"], value["html"]))
        if (
            isinstance(value.get("text"), str)
            and isinstance(value.get("html"), str)
            and {"header", "rowspan", "colspan"}.issubset(value)
        ):
            pairs.append((value["text"], value["html"]))
        for plain_text, rich_html in pairs:
            parser = _SafeRecordHtmlParser()
            try:
                parser.feed(rich_html)
                parser.close()
                errors = parser.finish()
            except (ValueError, RuntimeError) as exc:
                errors = (str(exc),)
            if errors:
                continue
            if _normalize_record_visible_text(plain_text) != _normalize_record_visible_text(
                parser.visible_text
            ):
                findings.append(
                    _finding(
                        "record_dual_text_mismatch",
                        "critical",
                        "Plain text and rich HTML make different visible-text claims.",
                        f"record.json{pointer}",
                    )
                )
    return findings


def _record_json_identity_findings(
    payload: Mapping[str, Any],
    loaded: Mapping[str, Any],
    extraction_dir: Path,
    expected_metadata: Mapping[str, Any] | None,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    record = payload.get("record")
    if not isinstance(record, dict):
        return findings
    recorded_id = _record_id(loaded, extraction_dir)
    if recorded_id is not None and record.get("record_id") != recorded_id:
        findings.append(
            _finding(
                "record_identity_mismatch",
                "critical",
                f"Canonical record_id must match diagnostic record {recorded_id!r}.",
                "record.json/record/record_id",
            )
        )
    if expected_metadata is None:
        return findings
    for key in (
        "record_id",
        "title",
        "authors",
        "journal",
        "publication_year",
        "doi",
        "document_type",
    ):
        if key not in expected_metadata:
            continue
        expected = expected_metadata[key]
        if isinstance(expected, tuple):
            expected = list(expected)
        if record.get(key) != expected:
            findings.append(
                _finding(
                    "record_metadata_mismatch",
                    "critical",
                    f"Canonical metadata field {key!r} differs from the authoritative record.",
                    f"record.json/record/{key}",
                )
            )
    return findings


def _record_json_forbidden_key_findings(
    payload: Mapping[str, Any]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []

    def visit(value: Any, parts: tuple[str | int, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                pointer = _json_pointer(*parts, key)
                if key in _DIAGNOSTIC_ONLY_RECORD_KEYS:
                    findings.append(
                        _finding(
                            "diagnostic_field_in_record_json",
                            "critical",
                            f"Diagnostic-only field {key!r} appears in canonical content.",
                            f"record.json{pointer}",
                        )
                    )
                if key != "machine_records":
                    visit(child, (*parts, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, (*parts, index))

    visit(payload)
    return findings


def _record_json_asset_findings(
    payload: Mapping[str, Any],
    extraction_dir: Path,
    loaded: Mapping[str, Any],
) -> tuple[list[ValidationFinding], set[Path]]:
    findings: list[ValidationFinding] = []
    linked: set[Path] = set()
    asset_ids: dict[str, str] = {}
    asset_paths: dict[str, str] = {}
    asset_records: dict[str, Mapping[str, Any]] = {}
    manifest = loaded.get("manifest.json")
    manifest_assets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    if isinstance(manifest, dict) and isinstance(manifest.get("assets"), list):
        for raw in manifest["assets"]:
            if isinstance(raw, dict) and isinstance(raw.get("asset_id"), str):
                manifest_assets[raw["asset_id"]].append(raw)

    def register_path(value: Any, *, location: str, asset_id: str | None = None) -> None:
        pure = _portable_record_asset_path(value)
        if pure is None:
            findings.append(
                _finding(
                    "unsafe_record_asset_path",
                    "critical",
                    f"Canonical record contains an unsafe asset path: {value!r}.",
                    location,
                )
            )
            return
        relative = Path(*pure.parts)
        linked.add(relative)
        if asset_id:
            previous = asset_paths.get(pure.as_posix())
            if previous is not None and previous != asset_id:
                findings.append(
                    _finding(
                        "duplicate_record_asset_path",
                        "structural",
                        f"Asset path is assigned to both {previous!r} and {asset_id!r}.",
                        location,
                    )
                )
            asset_paths[pure.as_posix()] = asset_id
        candidate = extraction_dir / relative
        if not candidate.is_file():
            findings.append(
                _finding(
                    "missing_record_asset",
                    "scientific",
                    f"Referenced local asset does not exist: {pure.as_posix()!r}.",
                    location,
                )
            )

    assets = payload.get("assets")
    if isinstance(assets, list):
        for index, raw in enumerate(assets):
            if not isinstance(raw, dict):
                continue
            identifier = raw.get("asset_id")
            location = f"record.json/assets/{index}"
            if isinstance(identifier, str):
                if identifier in asset_ids:
                    findings.append(
                        _finding(
                            "duplicate_record_asset_id",
                            "structural",
                            f"Duplicate asset_id {identifier!r}.",
                            location,
                        )
                    )
                asset_ids[identifier] = location
                asset_records[identifier] = raw
            register_path(raw.get("path"), location=location, asset_id=identifier if isinstance(identifier, str) else None)
            if isinstance(identifier, str):
                matches = manifest_assets.get(identifier, [])
                if len(matches) != 1:
                    findings.append(
                        _finding(
                            "record_asset_manifest_mismatch",
                            "critical",
                            f"Canonical asset must match exactly one manifest asset; found {len(matches)}.",
                            location,
                        )
                    )
                else:
                    manifest_asset = matches[0]
                    if (
                        manifest_asset.get("output_path") != raw.get("path")
                        or str(manifest_asset.get("category") or manifest_asset.get("kind") or "")
                        != str(raw.get("kind") or "")
                    ):
                        findings.append(
                            _finding(
                                "record_asset_manifest_mismatch",
                                "critical",
                                "Canonical and diagnostic manifest asset relationships disagree.",
                                location,
                            )
                        )

    content_ids: dict[str, str] = {}

    def unique(values: Any, key: str, base: str) -> None:
        if not isinstance(values, list):
            return
        for index, raw in enumerate(values):
            if not isinstance(raw, dict) or not isinstance(raw.get(key), str):
                continue
            identifier = raw[key]
            location = f"record.json/{base}/{index}"
            previous = content_ids.get(f"{key}:{identifier}")
            if previous is not None:
                findings.append(
                    _finding(
                        "duplicate_record_content_id",
                        "structural",
                        f"Duplicate {key} {identifier!r}.",
                        location,
                    )
                )
            content_ids[f"{key}:{identifier}"] = location

    unique(payload.get("front_matter"), "block_id", "front_matter")
    unique(payload.get("supporting_information"), "block_id", "supporting_information")
    unique(payload.get("references"), "block_id", "references")
    unique(payload.get("figures"), "figure_id", "figures")
    unique(payload.get("tables"), "table_id", "tables")
    unique(payload.get("sections"), "section_id", "sections")
    unique(payload.get("supplements"), "supplement_id", "supplements")
    for section_index, section in enumerate(payload.get("sections", [])):
        if isinstance(section, dict):
            unique(section.get("blocks"), "block_id", f"sections/{section_index}/blocks")

    for index, figure in enumerate(payload.get("figures", [])):
        if not isinstance(figure, dict):
            continue
        asset_id = figure.get("asset_id")
        if asset_id is None:
            if str(figure.get("kind", "")).casefold() != "graphical_abstract":
                findings.append(
                    _finding(
                        "missing_figure_asset_reference",
                        "scientific",
                        f"{figure.get('label') or figure.get('figure_id')} has no asset_id.",
                        f"record.json/figures/{index}",
                    )
                )
        elif asset_id not in asset_ids:
            findings.append(
                _finding(
                    "unknown_figure_asset_reference",
                    "scientific",
                    f"Figure refers to unknown asset_id {asset_id!r}.",
                    f"record.json/figures/{index}/asset_id",
                )
            )
        else:
            if asset_id != figure.get("figure_id"):
                findings.append(
                    _finding(
                        "figure_asset_identity_mismatch",
                        "critical",
                        "Figure asset_id must equal its stable figure_id.",
                        f"record.json/figures/{index}/asset_id",
                    )
                )
            asset = asset_records.get(asset_id)
            if isinstance(asset, Mapping):
                expected_kind = str(figure.get("kind") or "").casefold()
                actual_kind = str(asset.get("kind") or "").casefold()
                if expected_kind and actual_kind != expected_kind:
                    findings.append(
                        _finding(
                            "figure_asset_kind_mismatch",
                            "critical",
                            "Figure and referenced asset declare different kinds.",
                            f"record.json/figures/{index}/asset_id",
                        )
                    )

    def check_table(table: Any, location: str) -> None:
        if not isinstance(table, dict):
            return
        for key, path in (table.get("structure_assets") or {}).items():
            register_path(path, location=f"{location}/structure_assets/{key}")
            pure = _portable_record_asset_path(path)
            if pure is not None and pure.as_posix() not in asset_paths:
                findings.append(
                    _finding(
                        "unregistered_table_asset",
                        "structural",
                        "Table structure asset is absent from the canonical asset registry.",
                        f"{location}/structure_assets/{key}",
                    )
                )
            elif pure is not None:
                asset_id = asset_paths.get(pure.as_posix())
                asset = asset_records.get(asset_id or "")
                if not isinstance(asset, Mapping) or (
                    asset.get("parent_id") != table.get("table_id")
                    or str(asset.get("content_id")) != str(key)
                ):
                    findings.append(
                        _finding(
                            "table_asset_relationship_mismatch",
                            "critical",
                            "Table structure asset registry relationship is inconsistent.",
                            f"{location}/structure_assets/{key}",
                        )
                    )
        if "source_image" in table:
            source_image = table.get("source_image")
            register_path(source_image, location=f"{location}/source_image")
            pure = _portable_record_asset_path(source_image)
            if pure is not None and pure.as_posix() not in asset_paths:
                findings.append(
                    _finding(
                        "unregistered_table_asset",
                        "structural",
                        "Table source image is absent from the canonical asset registry.",
                        f"{location}/source_image",
                    )
                )
            elif pure is not None and asset_paths.get(pure.as_posix()) != table.get("table_id"):
                findings.append(
                    _finding(
                        "table_asset_relationship_mismatch",
                        "critical",
                        "Table source image must use the table_id in the asset registry.",
                        f"{location}/source_image",
                    )
                )

    for index, table in enumerate(payload.get("tables", [])):
        if isinstance(table, dict):
            unique(table.get("parts"), "part_id", f"tables/{index}/parts")
        check_table(table, f"record.json/tables/{index}")

    for supplement_index, supplement in enumerate(payload.get("supplements", [])):
        if not isinstance(supplement, dict):
            continue
        unique(supplement.get("blocks"), "block_id", f"supplements/{supplement_index}/blocks")
        unique(supplement.get("figures"), "figure_id", f"supplements/{supplement_index}/figures")
        unique(supplement.get("tables"), "table_id", f"supplements/{supplement_index}/tables")
        file_value = supplement.get("file")
        if isinstance(file_value, dict):
            register_path(
                file_value.get("path"),
                location=f"record.json/supplements/{supplement_index}/file/path",
            )
        for figure_index, figure in enumerate(supplement.get("figures", [])):
            if not isinstance(figure, dict):
                continue
            asset_id = figure.get("asset_id")
            if asset_id is None:
                findings.append(
                    _finding(
                        "missing_figure_asset_reference",
                        "scientific",
                        f"{figure.get('label') or figure.get('figure_id')} has no asset_id.",
                        f"record.json/supplements/{supplement_index}/figures/{figure_index}",
                    )
                )
            elif asset_id not in asset_ids:
                findings.append(
                    _finding(
                        "unknown_figure_asset_reference",
                        "scientific",
                        f"Supplement figure refers to unknown asset_id {asset_id!r}.",
                        f"record.json/supplements/{supplement_index}/figures/{figure_index}/asset_id",
                    )
                )
            elif asset_id != figure.get("figure_id"):
                findings.append(
                    _finding(
                        "figure_asset_identity_mismatch",
                        "critical",
                        "Supplement figure asset_id must equal its stable figure_id.",
                        f"record.json/supplements/{supplement_index}/figures/{figure_index}/asset_id",
                    )
                )
        for table_index, table in enumerate(supplement.get("tables", [])):
            if isinstance(table, dict):
                unique(
                    table.get("parts"),
                    "part_id",
                    f"supplements/{supplement_index}/tables/{table_index}/parts",
                )
            check_table(
                table,
                f"record.json/supplements/{supplement_index}/tables/{table_index}",
            )
        for asset_index, asset_id in enumerate(supplement.get("asset_ids", [])):
            if asset_id not in asset_ids:
                findings.append(
                    _finding(
                        "unknown_supplement_asset_reference",
                        "scientific",
                        f"Supplement refers to unknown asset_id {asset_id!r}.",
                        f"record.json/supplements/{supplement_index}/asset_ids/{asset_index}",
                    )
                )
    return findings, linked


def _record_json_counts(payload: Mapping[str, Any]) -> dict[str, int]:
    equations = sum(
        1
        for section in payload.get("sections", [])
        if isinstance(section, dict)
        for block in section.get("blocks", [])
        if isinstance(block, dict)
        and str(block.get("kind", "")).casefold() in _EQUATION_BLOCK_KINDS
    )
    main_figures = sum(
        1
        for figure in payload.get("figures", [])
        if isinstance(figure, dict)
        and str(figure.get("kind", "")).casefold() == "figure"
    )
    main_schemes = sum(
        1
        for figure in payload.get("figures", [])
        if isinstance(figure, dict)
        and str(figure.get("kind", "")).casefold() == "scheme"
    )
    supplementary_figures = 0
    presentation_table_supplements: dict[str, str] = {}
    for supplement in payload.get("supplements", []):
        if not isinstance(supplement, dict):
            continue
        supplement_id = supplement.get("supplement_id")
        if isinstance(supplement_id, str):
            for table in supplement.get("tables", []):
                if (
                    isinstance(table, dict)
                    and str(table.get("source_kind", "")).casefold()
                    == "presentation"
                    and isinstance(table.get("table_id"), str)
                ):
                    presentation_table_supplements[table["table_id"]] = supplement_id
        figure_items = sum(
            1
            for figure in supplement.get("figures", [])
            if isinstance(figure, dict)
            and str(figure.get("kind", "")).casefold() == "figure"
        )
        caption_blocks = sum(
            1
            for block in supplement.get("blocks", [])
            if isinstance(block, dict)
            and str(block.get("kind", "")).casefold() == "figure_caption"
        )
        # PDF supplements expose both a semantic figure item and its caption
        # block, while presentations expose the caption block alongside their
        # component assets. Count the larger representation, not both.
        supplementary_figures += max(figure_items, caption_blocks)

    presentation_embedded_files = 0
    for asset in payload.get("assets", []):
        if not isinstance(asset, dict):
            continue
        parent_id = asset.get("parent_id")
        supplement_id = presentation_table_supplements.get(str(parent_id))
        path = asset.get("path")
        if supplement_id is None or not isinstance(path, str):
            continue
        pure = PurePosixPath(path)
        expected_parent = PurePosixPath(
            "supplementary", supplement_id, "embedded"
        )
        if (
            str(asset.get("kind", "")).casefold() == "supplement_data"
            and str(asset.get("media_type", "")).casefold() == _XLSX_MEDIA_TYPE
            and pure.parent == expected_parent
            and pure.suffix.casefold() == ".xlsx"
        ):
            presentation_embedded_files += 1
    return {
        "references": len(payload.get("references", [])),
        "equations": equations,
        "main_figures": main_figures,
        "main_schemes": main_schemes,
        "main_figures_and_schemes": main_figures + main_schemes,
        "tables": len(payload.get("tables", [])),
        "supplementary_figures": supplementary_figures,
        "supplementary_files": len(payload.get("supplements", [])),
        "presentation_embedded_files": presentation_embedded_files,
    }


def _expected_record_json_coverage(payload: Mapping[str, Any]) -> dict[str, str]:
    expected = {
        "record-title": "/record/title",
        "record-authors": "/record/authors",
        "record-citation": "/record",
    }

    def blocks(values: Any, base: tuple[str | int, ...]) -> None:
        if not isinstance(values, list):
            return
        for index, block in enumerate(values):
            if isinstance(block, dict) and isinstance(block.get("block_id"), str):
                expected[f"content-{block['block_id']}"] = _json_pointer(*base, index)

    def figures(values: Any, base: tuple[str | int, ...]) -> None:
        if not isinstance(values, list):
            return
        for index, figure in enumerate(values):
            if not isinstance(figure, dict) or not isinstance(figure.get("figure_id"), str):
                continue
            figure_id = figure["figure_id"]
            pointer = _json_pointer(*base, index)
            expected[f"figure-{figure_id}"] = pointer
            caption = figure.get("caption")
            if isinstance(caption, dict) and (caption.get("plain_text") or caption.get("html")):
                expected[f"caption-{figure_id}"] = pointer + "/caption"

    def tables(values: Any, base: tuple[str | int, ...]) -> None:
        if not isinstance(values, list):
            return
        for index, table in enumerate(values):
            if not isinstance(table, dict) or not isinstance(table.get("table_id"), str):
                continue
            table_id = table["table_id"]
            pointer = _json_pointer(*base, index)
            expected[f"table-{table_id}"] = pointer
            expected[f"table-title-{table_id}"] = pointer + "/title"
            expected[f"table-body-{table_id}"] = pointer + "/parts"
            for note_index, _ in enumerate(table.get("footnotes", []), start=1):
                expected[f"table-footnotes-{table_id}-{note_index:02d}"] = (
                    pointer + _json_pointer("footnotes", note_index - 1)
                )

    blocks(payload.get("front_matter"), ("front_matter",))
    blocks(payload.get("supporting_information"), ("supporting_information",))
    blocks(payload.get("references"), ("references",))
    for section_index, section in enumerate(payload.get("sections", [])):
        if not isinstance(section, dict) or not isinstance(section.get("section_id"), str):
            continue
        base = ("sections", section_index)
        expected[f"heading-{section['section_id']}"] = _json_pointer(*base, "heading")
        blocks(section.get("blocks"), (*base, "blocks"))
    figures(payload.get("figures"), ("figures",))
    tables(payload.get("tables"), ("tables",))
    for supplement_index, supplement in enumerate(payload.get("supplements", [])):
        if not isinstance(supplement, dict) or not isinstance(supplement.get("supplement_id"), str):
            continue
        base = ("supplements", supplement_index)
        supplement_id = supplement["supplement_id"]
        expected[f"supplement-file-{supplement_id}"] = _json_pointer(*base, "file", "path")
        blocks(supplement.get("blocks"), (*base, "blocks"))
        figures(supplement.get("figures"), (*base, "figures"))
        tables(supplement.get("tables"), (*base, "tables"))
    for asset_index, asset in enumerate(payload.get("assets", [])):
        if isinstance(asset, dict) and isinstance(asset.get("asset_id"), str):
            expected[f"asset-{asset['asset_id']}"] = _json_pointer("assets", asset_index, "path")
    return expected


def _validate_record_json_coverage(
    payload: Mapping[str, Any], coverage: Any
) -> list[ValidationFinding]:
    if not isinstance(coverage, list):
        return []
    findings: list[ValidationFinding] = []
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in coverage:
        if not isinstance(row, dict):
            continue
        coverage_id = row.get("coverage_id")
        if isinstance(coverage_id, str):
            rows[coverage_id].append(row)
        if row.get("output_path") == "record.json":
            locator = row.get("output_locator")
            pointer = locator.get("json_pointer") if isinstance(locator, dict) else None
            ok, _value = _resolve_json_pointer(payload, pointer)
            if not ok:
                findings.append(
                    _finding(
                        "invalid_record_json_coverage_pointer",
                        "structural",
                        f"Coverage JSON Pointer does not resolve: {pointer!r}.",
                        "coverage.jsonl",
                    )
                )
        elif row.get("output_path") == "record.md":
            findings.append(
                _finding(
                    "stale_markdown_coverage",
                    "structural",
                    "JSON-mode diagnostics contain stale record.md coverage.",
                    "coverage.jsonl",
                )
            )
    for coverage_id, pointer in _expected_record_json_coverage(payload).items():
        matches = rows.get(coverage_id, [])
        valid = [
            row
            for row in matches
            if row.get("status") == "included"
            and row.get("output_path") == "record.json"
            and isinstance(row.get("output_locator"), dict)
            and row["output_locator"].get("json_pointer") == pointer
        ]
        if len(valid) != 1:
            findings.append(
                _finding(
                    "record_json_reverse_coverage_missing",
                    "structural",
                    f"Expected exactly one included coverage row for {coverage_id!r} at {pointer!r}; found {len(valid)}.",
                    "coverage.jsonl",
                )
            )
    return findings


def _select_record_document(
    extraction: Path, loaded: Mapping[str, Any]
) -> tuple[str | None, list[ValidationFinding]]:
    findings: list[ValidationFinding] = []
    has_json = (extraction / "record.json").is_file()
    has_markdown = (extraction / "record.md").is_file()
    if has_json and has_markdown:
        findings.append(
            _finding(
                "multiple_record_documents",
                "critical",
                "An extraction must contain exactly one canonical record document.",
                str(extraction),
            )
        )
    manifest = loaded.get("manifest.json")
    declaration = manifest.get("record_document") if isinstance(manifest, dict) else None
    if declaration is not None:
        if not isinstance(declaration, dict):
            findings.append(
                _finding(
                    "invalid_record_document_declaration",
                    "critical",
                    "manifest.record_document must be an object.",
                    "manifest.json",
                )
            )
            return "json" if has_json else ("markdown" if has_markdown else None), findings
        if (
            declaration.get("path") != "record.json"
            or declaration.get("media_type") != _RECORD_JSON_MEDIA_TYPE
            or declaration.get("schema_version") not in SUPPORTED_RECORD_SCHEMA_VERSIONS
        ):
            findings.append(
                _finding(
                    "invalid_record_document_declaration",
                    "critical",
                    "manifest.record_document does not declare a supported record.json "
                    f"contract ({', '.join(SUPPORTED_RECORD_SCHEMA_VERSIONS)}).",
                    "manifest.json",
                )
            )
        if not has_json:
            findings.append(
                _finding(
                    "record_missing",
                    "critical",
                    "Manifest-declared record.json is missing from the extraction root.",
                    "record.json",
                )
            )
        return "json", findings
    if has_json:
        findings.append(
            _finding(
                "record_document_declaration_missing",
                "critical",
                "A JSON extraction must declare manifest.record_document.",
                "manifest.json",
            )
        )
        return "json", findings
    if has_markdown:
        return "markdown", findings
    findings.append(
        _finding(
            "record_missing",
            "critical",
            "Neither record.json nor legacy record.md exists in the extraction root.",
            str(extraction),
        )
    )
    return None, findings


def _record_json_sidecar_findings(extraction: Path) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    tables = extraction / "tables"
    if not tables.is_dir():
        return findings
    for path in sorted(tables.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.suffix.casefold() not in {".json", ".csv"}:
            continue
        findings.append(
            _finding(
                "redundant_table_sidecar",
                "structural",
                "JSON-mode tables must be embedded in record.json, not duplicated as JSON or CSV sidecars.",
                path.relative_to(extraction).as_posix(),
            )
        )
    return findings


def _all_candidate_files(extraction_dir: Path) -> list[Path]:
    if not extraction_dir.is_dir():
        return []
    return sorted(
        (path for path in extraction_dir.rglob("*") if path.is_file()),
        key=lambda value: value.as_posix(),
    )


def validate_candidate(
    extraction_dir: str | Path,
    diagnostic_dir: str | Path,
    *,
    expected_title: str,
    expected_counts: Mapping[str, int] | None = None,
    expected_metadata: Mapping[str, Any] | None = None,
    allowed_orphans: Iterable[str] = (),
) -> ValidationReport:
    """Validate a staged extraction and return a deterministic report.

    ``expected_counts`` uses the explicit keys in :data:`COUNT_KEYS`.  When it
    is omitted, the validator first checks diagnostic ``expected_counts`` and
    then applies the known pilot expectations for record ``00559``.  Cosmetic
    findings are reported but do not cause ``report.status`` to be ``"fail"``.
    """

    extraction = Path(extraction_dir)
    diagnostic = Path(diagnostic_dir)
    findings: list[ValidationFinding] = []

    if not expected_title or "\n" in expected_title or "\r" in expected_title:
        raise ValueError("expected_title must be a non-empty single line")

    if not extraction.is_dir():
        findings.append(
            _finding(
                "extraction_directory_missing",
                "critical",
                "The staged extraction directory is missing.",
                str(extraction),
            )
        )

    loaded, diagnostic_findings = _read_json_diagnostics(diagnostic)
    findings.extend(diagnostic_findings)
    record_mode, selection_findings = _select_record_document(extraction, loaded)
    findings.extend(selection_findings)

    record_path = extraction / "record.md"
    record_text = ""
    record_payload: dict[str, Any] | None = None
    linked_files: set[Path] = set()

    if record_mode == "markdown" and record_path.is_file():
        try:
            record_text = record_path.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            findings.append(
                _finding(
                    "record_not_utf8",
                    "critical",
                    f"record.md is not valid UTF-8: {exc}.",
                    "record.md",
                )
            )
        except OSError as exc:
            findings.append(
                _finding(
                    "record_unreadable",
                    "critical",
                    f"Could not read record.md: {exc}.",
                    "record.md",
                )
            )

        expected_first_line = f"# {expected_title}"
        if not any(
            finding.code in {"record_not_utf8", "record_unreadable"}
            for finding in findings
        ):
            first_line = record_text.splitlines()[0] if record_text.splitlines() else ""
            if first_line != expected_first_line:
                findings.append(
                    _finding(
                        "title_mismatch",
                        "critical",
                        f"First line must be exactly {expected_first_line!r}; found {first_line!r}.",
                        "record.md:1",
                    )
                )
        if record_text:
            findings.extend(_content_findings(record_text))
            findings.extend(_heading_findings(record_text))
            findings.extend(_required_asset_findings(record_text))
            link_findings, linked_files, _links = _validate_links(
                record_text, extraction
            )
            findings.extend(link_findings)
        findings.extend(_table_derivative_findings(extraction, linked_files))

    if record_mode == "json":
        json_path = extraction / "record.json"
        if json_path.is_file():
            try:
                decoded = json.loads(
                    json_path.read_text(encoding="utf-8", errors="strict")
                )
            except UnicodeDecodeError as exc:
                findings.append(
                    _finding(
                        "record_not_utf8",
                        "critical",
                        f"record.json is not valid UTF-8: {exc}.",
                        "record.json",
                    )
                )
            except json.JSONDecodeError as exc:
                findings.append(
                    _finding(
                        "invalid_record_json",
                        "critical",
                        f"record.json is not valid JSON: {exc}.",
                        "record.json",
                    )
                )
            except OSError as exc:
                findings.append(
                    _finding(
                        "record_unreadable",
                        "critical",
                        f"Could not read record.json: {exc}.",
                        "record.json",
                    )
                )
            else:
                schema_valid = True
                try:
                    validate_record_schema(decoded, source_path="record.json")
                except RecordSchemaError as exc:
                    schema_valid = False
                    if exc.violations:
                        for violation in exc.violations:
                            findings.append(
                                _finding(
                                    "invalid_record_schema",
                                    "structural",
                                    (
                                        f"Record JSON {violation.instance_path}: "
                                        f"{violation.message} "
                                        f"(schema {violation.schema_path})."
                                    ),
                                    "record.json",
                                )
                            )
                    else:
                        findings.append(
                            _finding(
                                "record_schema_unavailable",
                                "critical",
                                str(exc),
                                "scripts/extraction/schemas",
                            )
                        )
                if schema_valid and isinstance(decoded, dict):
                    manifest = loaded.get("manifest.json")
                    declaration = (
                        manifest.get("record_document")
                        if isinstance(manifest, dict)
                        else None
                    )
                    declared_version = (
                        declaration.get("schema_version")
                        if isinstance(declaration, dict)
                        else None
                    )
                    if (
                        isinstance(declaration, dict)
                        and declared_version in SUPPORTED_RECORD_SCHEMA_VERSIONS
                        and declared_version != decoded.get("schema_version")
                    ):
                        findings.append(
                            _finding(
                                "record_schema_declaration_mismatch",
                                "critical",
                                "manifest.record_document.schema_version must match "
                                "record.json schema_version.",
                                "manifest.json",
                            )
                        )
                    record_payload = decoded
                    metadata = decoded.get("record")
                    actual_title = metadata.get("title") if isinstance(metadata, dict) else None
                    if actual_title != expected_title:
                        findings.append(
                            _finding(
                                "title_mismatch",
                                "critical",
                                f"record.json title must be exactly {expected_title!r}; found {actual_title!r}.",
                                "record.json/record/title",
                            )
                        )
                    findings.extend(_record_json_content_findings(decoded))
                    findings.extend(_record_json_dual_text_findings(decoded))
                    findings.extend(
                        _record_json_identity_findings(
                            decoded, loaded, extraction, expected_metadata
                        )
                    )
                    findings.extend(_record_json_forbidden_key_findings(decoded))
                    asset_findings, linked_files = _record_json_asset_findings(
                        decoded, extraction, loaded
                    )
                    findings.extend(asset_findings)
                    findings.extend(_record_json_sidecar_findings(extraction))

    findings.extend(_validate_coverage(loaded.get("coverage.jsonl")))
    if record_mode == "markdown":
        findings.extend(
            _validate_record_line_coverage(record_text, loaded.get("coverage.jsonl"))
        )
    elif record_payload is not None:
        findings.extend(
            _validate_record_json_coverage(
                record_payload, loaded.get("coverage.jsonl")
            )
        )
    findings.extend(_validate_diagnostic_warnings(loaded))
    findings.extend(_validate_pdf_crop_boundaries(extraction, loaded))
    findings.extend(_validate_pdf_page_analysis(loaded))
    findings.extend(_validate_pdf_ocr_diagnostics(loaded))
    findings.extend(_validate_pdf_block_provenance(loaded))
    findings.extend(_validate_confidence(loaded.get("confidence.json")))
    findings.extend(_validate_hashes(extraction, loaded))

    files = _all_candidate_files(extraction)
    canonical_name = "record.json" if record_mode == "json" else "record.md"
    allowed = {PurePosixPath(canonical_name)}
    for value in allowed_orphans:
        pure = PurePosixPath(str(value).replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(f"unsafe allowed orphan path: {value!r}")
        allowed.add(pure)

    for path in files:
        relative = PurePosixPath(path.relative_to(extraction).as_posix())
        if path.stat().st_size == 0:
            findings.append(
                _finding(
                    "empty_output_file",
                    "structural",
                    "Output file is empty.",
                    relative.as_posix(),
                )
            )
        if relative not in allowed and Path(*relative.parts) not in linked_files:
            findings.append(
                _finding(
                    "orphan_output_file",
                    "structural",
                    f"Output file is not referenced from {canonical_name}.",
                    relative.as_posix(),
                )
            )

    actual_counts = (
        _record_json_counts(record_payload)
        if record_payload is not None
        else _caption_counts(record_text, extraction)
    )
    actual_counts["pages"] = _diagnostic_page_count(loaded)
    if expected_counts is not None:
        normalized_expected = _normalize_expected_counts(expected_counts)
        unknown_keys = sorted(
            str(key)
            for key in expected_counts
            if _COUNT_ALIASES.get(str(key), str(key)) not in COUNT_KEYS
        )
        for key in unknown_keys:
            findings.append(
                _finding(
                    "unknown_expected_count",
                    "structural",
                    f"Unknown expected count key: {key!r}.",
                    "validation",
                )
            )
        for key, value in expected_counts.items():
            normalized_key = _COUNT_ALIASES.get(str(key), str(key))
            if normalized_key not in COUNT_KEYS:
                continue
            valid_value = (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            ) or (
                isinstance(value, str) and bool(re.fullmatch(r"\d+", value))
            )
            if not valid_value:
                findings.append(
                    _finding(
                        "invalid_expected_count",
                        "structural",
                        f"Expected count {key!r} must be a non-negative integer.",
                        "validation",
                    )
                )
    else:
        normalized_expected = _diagnostic_expected_counts(loaded)
        if not normalized_expected and _record_id(loaded, extraction) == "00559":
            normalized_expected = dict(_PILOT_00559_COUNTS)
    findings.extend(
        _count_findings(
            actual_counts,
            normalized_expected,
            record_path=canonical_name,
        )
    )

    return ValidationReport(
        extraction_dir=str(extraction),
        diagnostic_dir=str(diagnostic),
        expected_title=expected_title,
        findings=_sort_findings(findings),
        counts=dict(sorted(actual_counts.items())),
        expected_counts=dict(sorted(normalized_expected.items())),
        checked_files=len(files),
    )


__all__ = [
    "COUNT_KEYS",
    "MarkdownLink",
    "SEVERITIES",
    "ValidationReport",
    "parse_markdown_links",
    "validate_candidate",
]
