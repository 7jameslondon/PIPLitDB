"""Extract native text, tables, and figure media from DOCX supplements.

The original DOCX is handled by :mod:`supplements` and remains byte-identical.
This parser reads OOXML directly, preserves embedded source-image bytes, and
creates lossless PNG display derivatives without OCR.
"""

from __future__ import annotations

import hashlib
import copy
import html
import io
import json
import os
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from xml.etree import ElementTree as ET

import pypdfium2

from .models import (
    ContentBlock,
    FigureItem,
    SourceFile,
    TableCell,
    TableItem,
    TablePart,
)
from .paths import ensure_within, is_reparse_point, reject_reparse_chain
from .pdf_extractor import render_pdf_crop


DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
LEGACY_DOC_MEDIA_TYPE = "application/msword"
DOCX_FIGURE_RENDER_DPI = 300
DOCX_DISPLAY_MAX_EDGE = 6000
DOCX_RENDER_TIMEOUT_SECONDS = 300
NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "v": "urn:schemas-microsoft-com:vml",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}
_FIGURE_LABEL = re.compile(
    r"^(?:"
    r"Fig(?:ure)?\.?\s+(?P<number>S\d+(?:[-‐‑‒–—]\d+)?[A-Za-z]?)"
    r"|(?P<long_kind>Supplement(?:al|ary)\s+Fig(?:ure)?\.?)\s+"
    r"(?P<long_number>S?\d+(?:[-‐‑‒–—]\d+)?[A-Za-z]?)"
    r")(?=$|[.:\s])(?!\s+(?:shows?|depicts?|illustrates?|presents?)\b)(?:[.:])?\s*(?P<title>.*)$",
    flags=re.IGNORECASE,
)
_SCHEME_LABEL = re.compile(
    r"^(?:"
    r"Scheme\s+(?P<number>S\d+[A-Za-z]?)"
    r"|(?P<long_kind>Supplement(?:al|ary)\s+Scheme)\s+"
    r"(?P<long_number>S?\d+[A-Za-z]?)"
    r")(?=$|[.:\s])(?:[.:])?\s*(?P<title>.*)$",
    flags=re.IGNORECASE,
)
_TABLE_LABEL = re.compile(
    r"^(?:"
    r"Table\s+(?P<number>(?:S:?)?\d+[A-Za-z]?)"
    r"|(?P<long_kind>Supplement(?:al|ary)\s+Table)\s+"
    r"(?P<long_number>S?\d+[A-Za-z]?)"
    r")(?=$|[.:\s])(?:[.:])?\s*(?P<title>.*)$",
    flags=re.IGNORECASE,
)
_SYMBOL_TEXT = str.maketrans(
    {
        "a": "α",
        "b": "β",
        "d": "δ",
        "g": "γ",
        "k": "κ",
        "l": "λ",
        "m": "µ",
        "W": "Ω",
        "I": "Ι",
        "D": "Δ",
        "\uf02a": "*",
        "\uf02f": "/",
        "\uf020": " ",
        "\uf057": "Ω",
        "\uf049": "Ι",
        "\uf044": "Δ",
        "\uf061": "α",
        "\uf062": "β",
        "\uf064": "δ",
        "\uf067": "γ",
        "\uf06b": "κ",
        "\uf06c": "λ",
        "\uf06d": "µ",
        "\uf0b0": "°",
        "\uf0b4": "×",
        "\uf0b1": "±",
    }
)
_ADJACENT_SCRIPT_NOTATION = re.compile(
    r"(?P<marker>[_^])\{(?P<left>[^{}]*)\}(?P=marker)\{(?P<right>[^{}]*)\}"
)
_MEDIA_TYPES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".emf": "image/emf",
    ".wmf": "image/wmf",
    ".pct": "image/x-pict",
}


class UnsupportedDocxImageError(ValueError):
    """An authored DOCX image needs the native-layout reviewed crop path."""


@dataclass(frozen=True)
class DocxFigureCropRequest:
    """One reviewed crop from the document's native page compositor."""

    asset_id: str
    page: int
    box: tuple[float, float, float, float]
    expected_page_count: int


@dataclass(frozen=True)
class RenderedDocxFigureCrop:
    """One complete figure raster returned by a document renderer."""

    asset_id: str
    path: Path
    pixel_width: int
    pixel_height: int
    page_count: int
    page: int
    box: tuple[float, float, float, float]
    dpi: int
    renderer: str
    renderer_version: str


DocxFigureRenderer = Callable[
    [Path, Path, tuple[DocxFigureCropRequest, ...], int],
    list[RenderedDocxFigureCrop],
]


class DocxFigureRenderError(RuntimeError):
    """Raised when a reviewed full-layout DOCX figure cannot be rendered."""


def _tag(namespace: str, local: str) -> str:
    return f"{{{NS[namespace]}}}{local}"


def _effective_descendants(
    element: ET.Element,
    wanted_tag: str,
) -> Any:
    """Yield descendants from one effective OOXML compatibility branch.

    Word commonly stores the same drawing and text twice inside
    ``mc:AlternateContent``: a modern DrawingML ``Choice`` and a legacy VML
    ``Fallback``.  Treating both branches as authored content duplicates
    captions and media.  Prefer the first modern choice that is present and
    use the fallback only when the package supplies no choice.
    """

    for child in list(element):
        if child.tag == _tag("mc", "AlternateContent"):
            choices = child.findall("mc:Choice", NS)
            branch = choices[0] if choices else child.find("mc:Fallback", NS)
            if branch is not None:
                yield from _effective_descendants(branch, wanted_tag)
            continue
        if child.tag == wanted_tag:
            yield child
        yield from _effective_descendants(child, wanted_tag)


def _attr(element: ET.Element | None, namespace: str, name: str) -> str:
    if element is None:
        return ""
    return str(element.get(_tag(namespace, name)) or "")


def _truthy(value: str | None) -> bool:
    return str(value or "").casefold() not in {"", "0", "false", "off", "no"}


def _normalize_inline(value: str) -> str:
    value = (
        unicodedata.normalize("NFC", value)
        .replace("\u00a0", " ")
        .replace("\u3000", " ")
    )
    paired_quote_positions: set[int] = set()
    open_quotes: list[int] = []
    for index, character in enumerate(value):
        if character == "‘":
            open_quotes.append(index)
        elif character == "’" and open_quotes:
            paired_quote_positions.update((open_quotes.pop(), index))

    def scientific_prime(match: re.Match[str]) -> str:
        return match.group(0) if match.start() in paired_quote_positions else "′"

    value = re.sub(
        r"(?<=\d)[‘’´ʹ](?=[A-Z\-‐‑–—·→←↔\s,;:.)]|$)",
        scientific_prime,
        value,
    )
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def _docx_body_child_range(locator: str) -> tuple[int, int] | None:
    match = re.fullmatch(
        r"docx-part=word/document\.xml;body-child(?:ren)?=(\d+)(?:-(\d+))?",
        locator,
    )
    if match is None:
        return None
    first = int(match.group(1))
    last = int(match.group(2) or first)
    return (first, last) if first <= last else None


def _merge_wrapped_docx_text_blocks(
    blocks: list[ContentBlock],
) -> list[ContentBlock]:
    """Rejoin exact adjacent OOXML paragraphs split inside one sentence.

    Some publisher supplements encode page-wrap fragments as consecutive Word
    paragraphs.  Merge only consecutive native body children when the first
    fragment has no sentence terminal and the next begins with lowercase text
    or a numeric continuation (for example ``Cas9 Nuclease 3NLS``).
    Authored headings, lists, complete paragraphs, and nonadjacent evidence
    boundaries therefore remain untouched.
    """

    merged: list[ContentBlock] = []
    for block in blocks:
        if not merged:
            merged.append(block)
            continue
        previous = merged[-1]
        previous_range = _docx_body_child_range(previous.source_locator)
        current_range = _docx_body_child_range(block.source_locator)
        next_character = next(
            (character for character in block.plain_text if not character.isspace()),
            "",
        )
        terminal = re.search(r"[.!?…](?:[\"'’”\)\]\}])?$", previous.plain_text)
        if not (
            previous.kind == block.kind == "text"
            and previous.source_path == block.source_path
            and previous_range is not None
            and current_range is not None
            and current_range[0] == previous_range[1] + 1
            and terminal is None
            and (next_character.islower() or next_character.isdigit())
        ):
            merged.append(block)
            continue
        merged[-1] = ContentBlock(
            block_id=previous.block_id,
            kind="text",
            markdown=f"{previous.markdown} {block.markdown}",
            plain_text=f"{previous.plain_text} {block.plain_text}",
            source_path=previous.source_path,
            source_locator=(
                "docx-part=word/document.xml;body-children="
                f"{previous_range[0]}-{current_range[1]}"
            ),
        )
    return merged


def _libreoffice_executable() -> Path | None:
    candidates = [
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ]
    for raw in candidates:
        if not raw:
            continue
        candidate = Path(raw)
        if candidate.is_file():
            return candidate.resolve(strict=True)
    return None


def _word_executable() -> Path | None:
    candidates = [
        shutil.which("WINWORD.EXE"),
        r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
        r"C:\Program Files (x86)\Microsoft Office\root\Office16\WINWORD.EXE",
    ]
    for raw in candidates:
        if not raw:
            continue
        candidate = Path(raw)
        if candidate.is_file():
            return candidate.resolve(strict=True)
    return None


def _powershell_executable() -> Path | None:
    raw = shutil.which("powershell.exe") or shutil.which("powershell")
    return Path(raw).resolve(strict=True) if raw else None


def _word_pdf_script() -> str:
    """Return the fixed, read-only Word COM conversion script."""

    return r'''param(
    [Parameter(Mandatory=$true)][string]$SourcePath,
    [Parameter(Mandatory=$true)][string]$OutputPath
)
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class PipWordNativeMethods {
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
}
"@
$ErrorActionPreference = "Stop"
$word = $null
$documents = $null
$document = $null
$window = $null
$wordProcessId = [uint32]0
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.AutomationSecurity = 3
    if ($null -ne $word.Hwnd) {
        [void][PipWordNativeMethods]::GetWindowThreadProcessId(
            [IntPtr]$word.Hwnd,
            [ref]$wordProcessId
        )
    }
    $documents = $word.Documents
    $document = $documents.Open($SourcePath, $false, $true)
    $window = $document.ActiveWindow
    if ($null -ne $window -and $null -ne $window.Hwnd) {
        [void][PipWordNativeMethods]::GetWindowThreadProcessId(
            [IntPtr]$window.Hwnd,
            [ref]$wordProcessId
        )
    }
    $document.ExportAsFixedFormat($OutputPath, 17)
    Write-Output $word.Version
} finally {
    if ($null -ne $window) {
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($window) } catch {}
        $window = $null
    }
    if ($null -ne $document) {
        try { $document.Close(0) } catch {}
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($document) } catch {}
        $document = $null
    }
    if ($null -ne $documents) {
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($documents) } catch {}
        $documents = $null
    }
    if ($null -ne $word) {
        try { $word.Quit(0) } catch {}
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word) } catch {}
        $word = $null
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
    if ($wordProcessId -gt 0) {
        $wordProcess = Get-Process -Id $wordProcessId -ErrorAction SilentlyContinue
        if ($null -ne $wordProcess) {
            try { [void]$wordProcess.WaitForExit(2000) } catch {}
        }
        $wordProcess = Get-Process -Id $wordProcessId -ErrorAction SilentlyContinue
        if ($null -ne $wordProcess) {
            Stop-Process -Id $wordProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}
'''


def _word_docx_script() -> str:
    """Return the fixed, read-only legacy Word-to-DOCX conversion script."""

    return r'''param(
    [Parameter(Mandatory=$true)][string]$SourcePath,
    [Parameter(Mandatory=$true)][string]$OutputPath
)
$ErrorActionPreference = "Stop"
$word = $null
$document = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.AutomationSecurity = 3
    $document = $word.Documents.Open($SourcePath, $false, $true)
    $document.SaveAs2($OutputPath, 16)
    Write-Output $word.Version
} finally {
    if ($null -ne $document) {
        $discardChanges = 0
        $document.Close([ref]$discardChanges)
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($document)
    }
    if ($null -ne $word) {
        $discardChanges = 0
        $word.Quit([ref]$discardChanges)
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word)
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
'''


def convert_legacy_doc_to_docx(
    renderer_source: Path,
    output_root: Path,
    converted_root: Path,
    profile_root: Path,
) -> tuple[Path, str, str]:
    """Convert one staged, read-only legacy Word document to private OOXML.

    The original ``.doc`` remains the authoritative linked supplement.  The
    converted package exists only as a parser input in the caller's private
    temporary directory and is never exposed as a replacement source.
    """

    renderer_environment = os.environ.copy()
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "_CE_CONDA",
        "_CE_M",
    ):
        renderer_environment.pop(name, None)

    # Prefer the native converter for legacy Word fields and Symbol runs,
    # as for the native-layout rendering path below.
    native_word = _word_executable()
    native_powershell = _powershell_executable()
    soffice = None
    native_failure = ""
    native_succeeded = False
    if native_word is not None and native_powershell is not None:
        script_path = output_root / "convert-word-docx.ps1"
        script_path.write_text(_word_docx_script(), encoding="utf-8", newline="\n")
        converted_target = converted_root / f"{renderer_source.stem}.docx"
        command = [
            str(native_powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-SourcePath",
            str(renderer_source),
            "-OutputPath",
            str(converted_target),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=DOCX_RENDER_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=renderer_environment,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            native_failure = (
                "PowerShell executable is unavailable"
                if isinstance(exc, FileNotFoundError)
                else "Microsoft Word legacy document conversion timed out"
            )
        else:
            if completed.returncode == 0:
                native_succeeded = True
                renderer_name = "Microsoft Word legacy Word converter"
                renderer_version = _normalize_inline(completed.stdout) or "unknown"
            else:
                detail = _normalize_inline(
                    f"{completed.stdout or ''} {completed.stderr or ''}"
                )[-600:]
                native_failure = (
                    "Microsoft Word could not convert the reviewed legacy document"
                )
                if detail:
                    native_failure += f": {detail}"

    if not native_succeeded:
        soffice = _libreoffice_executable()
    if not native_succeeded and soffice is not None:
        for stale_docx in converted_root.glob("*.docx"):
            stale_docx.unlink()
        command = [
            str(soffice),
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile_root.resolve().as_uri()}",
            "--convert-to",
            "docx:Office Open XML Text",
            "--outdir",
            str(converted_root),
            str(renderer_source),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=DOCX_RENDER_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=renderer_environment,
            )
        except FileNotFoundError as exc:
            raise DocxFigureRenderError(
                "LibreOffice executable is unavailable"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DocxFigureRenderError(
                "LibreOffice legacy Word conversion timed out"
            ) from exc
        if completed.returncode != 0:
            detail = _normalize_inline(
                f"{completed.stdout or ''} {completed.stderr or ''}"
            )[-600:]
            message = "LibreOffice could not convert the reviewed legacy Word document"
            if detail:
                message += f": {detail}"
            raise DocxFigureRenderError(message)
        try:
            version_result = subprocess.run(
                [str(soffice), "--version"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=renderer_environment,
            )
            renderer_version = (
                _normalize_inline(version_result.stdout or version_result.stderr)
                or "unknown"
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            renderer_version = "unknown"
        renderer_name = "LibreOffice legacy Word converter"
        if native_failure:
            renderer_name += " (Microsoft Word fallback)"
    elif not native_succeeded:
        if native_failure:
            raise DocxFigureRenderError(native_failure)
        raise DocxFigureRenderError(
            "LibreOffice or Microsoft Word is required for legacy Word extraction"
        )

    docx_files = sorted(converted_root.glob("*.docx"))
    if len(docx_files) != 1 or not docx_files[0].is_file():
        raise DocxFigureRenderError(
            f"{renderer_name} did not create exactly one DOCX parser input"
        )
    converted_docx = docx_files[0].resolve(strict=True)
    if is_reparse_point(converted_docx):
        raise DocxFigureRenderError("legacy Word conversion output is a reparse point")
    try:
        with zipfile.ZipFile(converted_docx) as package:
            names = set(package.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise DocxFigureRenderError(
            "legacy Word conversion did not create readable OOXML"
        ) from exc
    if not {"word/document.xml", "word/_rels/document.xml.rels"}.issubset(names):
        raise DocxFigureRenderError(
            "legacy Word conversion output lacks required OOXML parts"
        )
    return converted_docx, renderer_name, renderer_version


def _render_docx_pdf(
    renderer_source: Path,
    output_root: Path,
    converted_root: Path,
    profile_root: Path,
) -> tuple[Path, str, str]:
    """Render one staged DOCX copy to PDF without saving the document."""

    # Native Word drawing objects can reflow under LibreOffice even when the
    # conversion succeeds. Prefer Word for DOCX layout fidelity when both its
    # read-only automation path and PowerShell host are available; keep
    # LibreOffice as the cross-platform fallback.
    word = _word_executable()
    powershell = _powershell_executable()
    soffice = _libreoffice_executable()
    renderer_environment = os.environ.copy()
    # The extraction process may use a bundled Python dependency path.  Those
    # Python variables can break an independent document compositor.
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "_CE_CONDA",
        "_CE_M",
    ):
        renderer_environment.pop(name, None)

    def run_libreoffice() -> tuple[str, str]:
        if soffice is None:
            raise DocxFigureRenderError("LibreOffice executable is unavailable")
        command = [
            str(soffice),
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile_root.resolve().as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(converted_root),
            str(renderer_source),
        ]
        timeout_message = "LibreOffice DOCX rendering timed out"
        failure_message = "LibreOffice could not render the reviewed DOCX"
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=DOCX_RENDER_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=renderer_environment,
            )
        except FileNotFoundError as exc:
            raise DocxFigureRenderError("LibreOffice executable is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise DocxFigureRenderError(timeout_message) from exc
        if completed.returncode != 0:
            detail = _normalize_inline(
                f"{completed.stdout or ''} {completed.stderr or ''}"
            )[-600:]
            if detail:
                failure_message += f": {detail}"
            raise DocxFigureRenderError(failure_message)
        try:
            version_result = subprocess.run(
                [str(soffice), "--version"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=renderer_environment,
            )
            renderer_version = (
                _normalize_inline(version_result.stdout or version_result.stderr)
                or "unknown"
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            renderer_version = "unknown"
        return "LibreOffice PDF compositor", renderer_version

    if word is None or powershell is None:
        renderer_name, renderer_version = run_libreoffice()
    else:
        script_path = output_root / "render-word-pdf.ps1"
        script_path.write_text(_word_pdf_script(), encoding="utf-8", newline="\n")
        rendered_target = converted_root / f"{renderer_source.stem}.pdf"
        command = [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-SourcePath",
            str(renderer_source),
            "-OutputPath",
            str(rendered_target),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=DOCX_RENDER_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=renderer_environment,
            )
        except FileNotFoundError as exc:
            raise DocxFigureRenderError("PowerShell executable is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise DocxFigureRenderError("Microsoft Word DOCX rendering timed out") from exc
        if completed.returncode != 0:
            detail = _normalize_inline(
                f"{completed.stdout or ''} {completed.stderr or ''}"
            )[-600:]
            message = "Microsoft Word could not render the reviewed DOCX"
            if detail:
                message += f": {detail}"
            if soffice is None:
                raise DocxFigureRenderError(message)
            rendered_target.unlink(missing_ok=True)
            renderer_name, renderer_version = run_libreoffice()
            renderer_name += " (Microsoft Word fallback)"
        else:
            renderer_name = "Microsoft Word PDF compositor"
            renderer_version = _normalize_inline(completed.stdout) or "unknown"

    pdfs = sorted(converted_root.glob("*.pdf"))
    if len(pdfs) != 1 or not pdfs[0].is_file():
        raise DocxFigureRenderError(
            f"{renderer_name} did not create exactly one PDF for the reviewed DOCX"
        )
    rendered_pdf = pdfs[0].resolve(strict=True)
    if is_reparse_point(rendered_pdf):
        raise DocxFigureRenderError("DOCX PDF output is a reparse point")
    return rendered_pdf, renderer_name, renderer_version


def render_docx_figure_crops(
    docx_path: Path,
    output_directory: Path,
    requests: tuple[DocxFigureCropRequest, ...],
    dpi: int,
) -> list[RenderedDocxFigureCrop]:
    """Render reviewed DOCX page crops through an installed document compositor.

    Word figures can combine raster chart panels with native text boxes.  OOXML
    media extraction preserves the component images but cannot reproduce those
    positioned labels.  This renderer opens only the candidate copy, converts
    it to a temporary PDF without saving the document, and crops the reviewed
    authored layout.  It performs no OCR.
    """

    if not requests:
        return []
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not 72 <= dpi <= 1200:
        raise DocxFigureRenderError("DOCX figure-render DPI is outside policy")
    if len({request.asset_id for request in requests}) != len(requests):
        raise DocxFigureRenderError("DOCX crop requests contain duplicate asset IDs")
    expected_page_counts = {request.expected_page_count for request in requests}
    if len(expected_page_counts) != 1:
        raise DocxFigureRenderError("DOCX crop requests disagree on page count")

    source = Path(docx_path).resolve(strict=True)
    if is_reparse_point(source) or not stat.S_ISREG(source.lstat().st_mode):
        raise DocxFigureRenderError("DOCX renderer source is not a regular file")
    output_root = Path(output_directory)
    output_root.mkdir(parents=True, exist_ok=True)
    output_root = output_root.resolve(strict=True)
    if is_reparse_point(output_root) or any(output_root.iterdir()):
        raise DocxFigureRenderError(
            "DOCX renderer requires an empty non-reparse output directory"
        )

    # Keep the legacy format for native composition. Renaming DOC bytes to
    # DOCX or rendering the converted parser package can lose embedded art.
    renderer_source = output_root / ("input.doc" if source.suffix.lower() == ".doc" else "input.docx")
    shutil.copyfile(source, renderer_source)
    if renderer_source.read_bytes() != source.read_bytes():
        raise DocxFigureRenderError("DOCX renderer input copy failed verification")
    profile_root = output_root / ".libreoffice-profile"
    converted_root = output_root / "converted"
    rendered_root = output_root / "rendered"
    profile_root.mkdir()
    converted_root.mkdir()
    rendered_pdf, renderer_name, renderer_version = _render_docx_pdf(
        renderer_source,
        output_root,
        converted_root,
        profile_root,
    )

    try:
        document = pypdfium2.PdfDocument(str(rendered_pdf))
        try:
            page_count = len(document)
        finally:
            document.close()
    except Exception as exc:
        raise DocxFigureRenderError(
            "document compositor created an unreadable PDF for the reviewed DOCX"
        ) from exc
    expected_page_count = next(iter(expected_page_counts))
    if page_count != expected_page_count:
        raise DocxFigureRenderError(
            "rendered DOCX page count changed: "
            f"expected {expected_page_count}, found {page_count}"
        )

    rendered: list[RenderedDocxFigureCrop] = []
    for index, request in enumerate(requests, 1):
        output_name = f"{index:03d}-{_safe_identifier(request.asset_id)}.png"
        try:
            result = render_pdf_crop(
                converted_root,
                rendered_root,
                {
                    "source_path": rendered_pdf.name,
                    "asset_id": request.asset_id,
                    "page": request.page,
                    "box": list(request.box),
                    "output_path": output_name,
                },
                dpi=dpi,
            )
        except Exception as exc:
            raise DocxFigureRenderError(
                f"could not crop reviewed DOCX figure {request.asset_id!r}: {exc}"
            ) from exc
        dimensions = result.get("dimensions_pixels", {})
        rendered.append(
            RenderedDocxFigureCrop(
                asset_id=request.asset_id,
                path=rendered_root / output_name,
                pixel_width=int(dimensions.get("width", 0)),
                pixel_height=int(dimensions.get("height", 0)),
                page_count=page_count,
                page=request.page,
                box=request.box,
                dpi=dpi,
                renderer=renderer_name,
                renderer_version=renderer_version,
            )
        )
    return rendered


def _escape_run_text(value: str) -> str:
    escaped = html.escape(value, quote=False)
    return escaped.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")


def _run_typeface(run: ET.Element) -> str:
    fonts = run.find("w:rPr/w:rFonts", NS)
    for name in ("ascii", "hAnsi", "eastAsia", "cs"):
        value = _attr(fonts, "w", name)
        if value:
            return value
    return ""


def _decode_run_text(value: str, typeface: str) -> str:
    # Office stores classic Symbol glyphs either as Latin codes (b/g/m) or as
    # U+F0xx private-use values.  Legacy Word conversion can relabel the
    # Symbol-font space U+F020 as Arial/MS PGothic even though it remains a
    # spacing character; canonicalize that one unambiguous code point before
    # applying the font-gated glyph map.  Decode all other private-use glyphs
    # only in runs explicitly marked Symbol.
    value = value.replace("\uf020", " ").replace("＋", "+")
    if typeface.strip().casefold() == "symbol":
        return value.translate(_SYMBOL_TEXT)
    if typeface.strip().casefold() == "wingdings":
        # Word's Wingdings right-arrow symbol is not a Unicode PUA glyph.
        # Decode only the verified font/code pair; other fonts must retain it.
        return value.replace("\uf0e0", "→")
    return value


def _styled_run(run: ET.Element) -> tuple[str, str]:
    properties = run.find("w:rPr", NS)
    typeface = _run_typeface(run)
    markdown_parts: list[str] = []
    plain_parts: list[str] = []
    for child in list(run):
        if child.tag == _tag("w", "t"):
            value = _decode_run_text(child.text or "", typeface)
            markdown_parts.append(_escape_run_text(value))
            plain_parts.append(value)
        elif child.tag in {_tag("w", "br"), _tag("w", "cr")}:
            markdown_parts.append("<br>")
            plain_parts.append("\n")
        elif child.tag == _tag("w", "tab"):
            markdown_parts.append(" ")
            plain_parts.append(" ")
        elif child.tag == _tag("w", "sym"):
            font = str(child.get(_tag("w", "font")) or "")
            raw = str(child.get(_tag("w", "char")) or "")
            try:
                value = chr(int(raw, 16))
            except ValueError:
                value = ""
            value = _decode_run_text(value, font)
            markdown_parts.append(_escape_run_text(value))
            plain_parts.append(value)

    markdown = "".join(markdown_parts)
    plain = "".join(plain_parts)
    if not plain:
        return "", ""

    vertical = _attr(
        properties.find("w:vertAlign", NS) if properties is not None else None,
        "w",
        "val",
    )
    # Word can retain a superscript/subscript property on a run whose only
    # visible content is spacing (for example, an empty author-affiliation
    # marker).  That spacing has no scientific script meaning and wrapping it
    # would manufacture plain-text notation such as ``^{ }``. Preserve the
    # visible delimiter as ordinary whitespace without script markup.
    if vertical in {"superscript", "subscript"} and not plain.strip():
        return markdown, plain
    if vertical == "superscript":
        markdown = f"<sup>{markdown}</sup>"
        plain = f"^{{{plain}}}"
    elif vertical == "subscript":
        markdown = f"<sub>{markdown}</sub>"
        plain = f"_{{{plain}}}"

    italic = properties.find("w:i", NS) if properties is not None else None
    bold = properties.find("w:b", NS) if properties is not None else None
    if italic is not None and _truthy(_attr(italic, "w", "val") or "1"):
        markdown = f"<em>{markdown}</em>"
    if bold is not None and _truthy(_attr(bold, "w", "val") or "1"):
        markdown = f"<strong>{markdown}</strong>"
    underline = properties.find("w:u", NS) if properties is not None else None
    if underline is not None and _attr(underline, "w", "val") not in {"none", "0", "false", "off"}:
        markdown = f"<u>{markdown}</u>"
    color = properties.find("w:color", NS) if properties is not None else None
    color_value = _attr(color, "w", "val")
    if color is not None and re.fullmatch(r"[0-9A-Fa-f]{6}", color_value or ""):
        markdown = f'<span style="color:#{color_value.lower()}">{markdown}</span>'
    return markdown, plain


def _paragraph_text(paragraph: ET.Element) -> tuple[str, str]:
    markdown_parts: list[str] = []
    plain_parts: list[str] = []
    # A nested text-box paragraph has a visible block boundary even when it
    # is anchored in the outer prose paragraph.
    owners = {}
    for nested in paragraph.iter(_tag("w", "p")):
        for run in _effective_descendants(nested, _tag("w", "r")):
            owners[run] = nested
    previous_owner = None
    for run in _effective_descendants(paragraph, _tag("w", "r")):
        markdown, plain = _styled_run(run)
        owner = owners.get(run, paragraph)
        if plain and plain_parts and owner is not previous_owner:
            markdown_parts.append(" ")
            plain_parts.append(" ")
        markdown_parts.append(markdown)
        plain_parts.append(plain)
        if plain:
            previous_owner = owner
    markdown = _normalize_inline("".join(markdown_parts))
    plain = _normalize_inline("".join(plain_parts))
    while _ADJACENT_SCRIPT_NOTATION.search(plain):
        plain = _ADJACENT_SCRIPT_NOTATION.sub(
            lambda match: (
                f"{match.group('marker')}"
                f"{{{match.group('left')}{match.group('right')}}}"
            ),
            plain,
        )
    return markdown, plain


def _is_standalone_bold_paragraph(markdown: str, plain: str) -> bool:
    """Recognize a DOCX paragraph whose complete visible content is bold."""

    if not markdown or not plain:
        return False
    remainder = re.sub(
        r"<strong>(?:(?!</?strong>).)*</strong>",
        "",
        markdown,
        flags=re.DOTALL,
    )
    # Color/underline wrappers around bold text carry no unbolded letters.
    remainder = html.unescape(re.sub(r"<[^>]+>", "", remainder))
    # Word can place punctuation between otherwise bold runs outside each
    # run's ``w:b`` properties. Such punctuation does not turn a visually
    # complete bold heading into a bold-lead paragraph. Continue to reject
    # any unbolded lexical content.
    return bool(
        re.fullmatch(r"[\s.,:;!?()\[\]{}\-‐‑‒–—−/\\]*", remainder)
    )


def _paragraph_has_heading_style(paragraph: ET.Element) -> bool:
    """Recognize Word's built-in numbered heading paragraph styles."""

    style = paragraph.find("w:pPr/w:pStyle", NS)
    value = _attr(style, "w", "val").strip()
    # A legacy file may reuse a heading style for body prose while explicitly
    # switching its paragraph-mark bold property off. Respect that direct
    # formatting instead of manufacturing a bold subsection from the style.
    bold = paragraph.find("w:pPr/w:rPr/w:b", NS)
    if bold is not None and not _truthy(_attr(bold, "w", "val") or "1"):
        return False
    return bool(re.fullmatch(r"(?:heading|h)[ _-]*[1-9]\d*", value, re.IGNORECASE))


def _inherit_paragraph_emphasis(document: ET.Element, styles: ET.Element) -> None:
    """Resolve paragraph-style b/i for runs, honoring direct run cancellation.

    This mutates only the in-memory parsing tree, never the native package.
    Paragraph-mark rPr formats the paragraph mark, not all contained runs.
    """
    definitions = {_attr(s, "w", "styleId"): s for s in styles.findall("w:style", NS)}

    def resolve(style_id: str, seen: set[str]) -> dict[str, ET.Element]:
        if style_id in seen or style_id not in definitions:
            return {}
        style = definitions[style_id]
        parent = _attr(style.find("w:basedOn", NS), "w", "val")
        values = resolve(parent, seen | {style_id})
        for name in ("b", "i"):
            element = style.find(f"w:rPr/w:{name}", NS)
            if element is not None:
                values[name] = element
        return values

    for paragraph in document.iter(_tag("w", "p")):
        style_id = _attr(paragraph.find("w:pPr/w:pStyle", NS), "w", "val")
        values = resolve(style_id, set())
        for run in paragraph.findall("w:r", NS):
            properties = run.find("w:rPr", NS)
            if properties is None and values:
                properties = ET.Element(_tag("w", "rPr"))
                run.insert(0, properties)
            for name, element in values.items():
                if properties.find(f"w:{name}", NS) is None:
                    properties.append(copy.deepcopy(element))


def _decimal_list_labels(body: ET.Element, numbering: ET.Element) -> dict[int, str]:
    """Materialize explicit decimal Word lists, including authored starts.

    Leave unsupported numbering formats untouched rather than guessing glyphs.
    Native labels are absent from w:t, including bibliography entry numbers.
    """
    abstracts = {_attr(x, "w", "abstractNumId"): x for x in numbering.findall("w:abstractNum", NS)}
    nums = {_attr(x, "w", "numId"): x for x in numbering.findall("w:num", NS)}
    counters: dict[tuple[str, int], int] = {}
    labels = {}
    for index, paragraph in enumerate(list(body), 1):
        props = paragraph.find("w:pPr/w:numPr", NS)
        num_id = _attr(props.find("w:numId", NS) if props is not None else None, "w", "val")
        num = nums.get(num_id)
        if num is None:
            continue
        level = int(_attr(props.find("w:ilvl", NS), "w", "val") or "0")
        abstract = abstracts.get(_attr(num.find("w:abstractNumId", NS), "w", "val"))
        if abstract is None:
            continue
        lvl = next((x for x in abstract.findall("w:lvl", NS) if _attr(x,"w","ilvl") == str(level)), None)
        override = next((x for x in num.findall("w:lvlOverride", NS) if _attr(x,"w","ilvl") == str(level)), None)
        if override is not None and override.find("w:lvl", NS) is not None:
            lvl = override.find("w:lvl", NS)
        if lvl is None or _attr(lvl.find("w:numFmt", NS), "w", "val") != "decimal":
            continue
        pattern = _attr(lvl.find("w:lvlText", NS), "w", "val")
        # Resolve only a level's own counter; do not invent parent counters.
        if set(re.findall(r"%\d+", pattern)) != {f"%{level+1}"}:
            continue
        start = _attr(lvl.find("w:start", NS), "w", "val") or "1"
        if override is not None and override.find("w:startOverride", NS) is not None:
            start = _attr(override.find("w:startOverride", NS), "w", "val")
        key = (num_id, level)
        counters[key] = counters[key] + 1 if key in counters else int(start)
        for other in list(counters):
            if other[0] == num_id and other[1] > level:
                del counters[other]
        labels[index] = pattern.replace(f"%{level+1}", str(counters[key]))
    return labels


def _paragraph_has_toc_style(paragraph: ET.Element) -> bool:
    """Return whether a paragraph is a generated Word contents entry."""

    style = paragraph.find("w:pPr/w:pStyle", NS)
    value = _attr(style, "w", "val").strip()
    return bool(re.fullmatch(r"toc[ _-]*\d+", value, re.IGNORECASE))


def _heading_markdown(markdown: str) -> str:
    """Remove redundant outer bold tags from a DOCX subsection heading."""

    return _normalize_inline(
        re.sub(r"</?strong>", "", markdown, flags=re.IGNORECASE)
    )


def _is_caption_only_document_boundary(markdown: str, plain: str) -> bool:
    """Recognize an explicit document section after a caption-only figure.

    Some publishers put figure captions in a DOCX and the corresponding
    pixels in a separate PDF.  Such a caption has no drawing relationship, so
    the general drawing-aware boundary cannot fire.  Limit the fallback to
    unmistakable authored section labels; ordinary bold panel text remains
    part of the caption.
    """

    value = _normalize_inline(plain).rstrip(".:").casefold()
    # Statistical-method run-in headings retain ordinary paragraph styles.
    # Their bold lead marks a return to methods after an intervening drawing;
    # panel leads and ordinary bold legend prose are deliberately excluded.
    if re.match(
        r"^<strong>(?:graphical\s+activity\s+display(?:\s*\(scatter\s+plot\))?|"
        r"clustering|experimental\s+quality\s+assessment)[.:]</strong>\s*\S",
        re.sub(r"</strong>\s*<strong>", "", markdown, flags=re.IGNORECASE),
        re.IGNORECASE,
    ):
        return True
    # An underlined methods/synthesis run-in heading can start the next
    # paragraph without a Word heading style or a wholly bold paragraph.
    # Keep that paragraph intact, but do not absorb it into a figure legend.
    if re.match(
        r"^<u>(?:materials?\s+and\s+methods|experimental\s+(?:procedures?|section)|"
        r"synthesis(?:\s+and\s+characterization)?|characterization)\b"
        r"[^<]*[.:]\s*</u>\s*\S",
        re.sub(r"</u>\s*<u>", "", markdown, flags=re.IGNORECASE),
        re.IGNORECASE,
    ):
        return True
    # A following media legend is a sibling supplementary item, not a
    # continuation of the preceding caption-only figure.  These paragraphs
    # commonly use only a bold lead, so they cannot rely on the complete-bold
    # section-heading check below.
    if re.match(
        r"^(?:supplement(?:al|ary)|supporting)\s+"
        r"(?:movies?|videos?|audio|datasets?|files?)\s+"
        r"(?:[a-z]*\s*)?\d+(?:\s*[-–—]\s*[a-z]*\s*\d+)?\b",
        value,
    ):
        return True
    # An authored References label is an unambiguous structural boundary even
    # when the legacy document did not style it in bold. Without this check, a
    # drawn figure can absorb the entire bibliography into its caption.
    if value in {"reference", "references"}:
        return True
    if not _is_standalone_bold_paragraph(markdown, plain):
        return False
    return bool(
        re.fullmatch(
            r"(?:supplement(?:al|ary)\s+)?(?:materials?\s+and\s+)?methods"
            r"|supplement(?:al|ary)\s+(?:materials?|information)"
            r"|supplement(?:al|ary)\s+text"
            r"|supplement(?:al|ary)\s+(?:figures?|tables?)"
            r"|supporting\s+information"
            r"|experimental\s+(?:procedures?|section)"
            r"|references?",
            value,
        )
    )


def _is_numbered_references_heading(plain: str) -> bool:
    """Recognize decimal-numbered Word bibliography headings.

    Legacy Word supplements can print a section label such as ``3.0
    References`` without bold or a heading style.  After a native table this
    must end table-footnote collection; otherwise the complete bibliography
    is incorrectly attached to the table.  Restrict this fallback to an exact
    References heading so authored numbered table notes remain footnotes.
    """

    return bool(
        re.fullmatch(
            r"\s*\d+(?:\.\d+)+\s+references?[.:]?\s*",
            plain,
            flags=re.IGNORECASE,
        )
    )


def _is_decimal_numbered_heading(plain: str) -> bool:
    """Recognize short unstyled decimal section labels such as ``2.7 FRET``.

    A number followed by an uppercase title is narrow enough to avoid treating
    ordinary measurements (for example ``1.5 mg ...``) as headings.  Word TOC
    entries are rejected separately using their paragraph style.
    """

    value = _normalize_inline(plain)
    return bool(
        len(value) <= 160
        and re.fullmatch(r"\d+(?:\.\d+)+\s+[A-Z][^\n]*", value)
    )


def _split_numbered_reference_paragraph(
    markdown: str, plain: str
) -> list[tuple[str, str]]:
    """Split multiple bracket-numbered references in one safe OOXML paragraph.

    Legacy Word files sometimes encode visually separate bibliography entries
    in one paragraph.  Split only at repeated ``[n]`` labels and only when each
    resulting rich-text fragment has balanced inline tags; otherwise preserve
    the original paragraph unchanged.
    """

    labels = re.findall(r"\[(\d+)\]", plain)
    if len(labels) < 2:
        return [(markdown, plain)]
    plain_parts = [
        part.strip()
        for part in re.split(r"(?=\[\d+\])", plain)
        if part.strip()
    ]
    markdown_parts = [
        part.strip()
        for part in re.split(r"(?=\[\d+\])", markdown)
        if part.strip()
    ]
    if (
        len(plain_parts) != len(labels)
        or len(markdown_parts) != len(labels)
        or [re.match(r"\[(\d+)\]", part).group(1) for part in plain_parts]
        != labels
        or [re.match(r"\[(\d+)\]", part).group(1) for part in markdown_parts]
        != labels
    ):
        return [(markdown, plain)]
    for part in markdown_parts:
        for tag in ("strong", "em", "sup", "sub"):
            if part.count(f"<{tag}>") != part.count(f"</{tag}>"):
                return [(markdown, plain)]
    return list(zip(markdown_parts, plain_parts, strict=True))


def _paragraph_relationship_ids(paragraph: ET.Element) -> list[str]:
    values: list[str] = []
    for blip in _effective_descendants(paragraph, _tag("a", "blip")):
        value = str(blip.get(_tag("r", "embed")) or "")
        if value and value not in values:
            values.append(value)
    for image in _effective_descendants(paragraph, _tag("v", "imagedata")):
        value = str(image.get(_tag("r", "id")) or "")
        if value and value not in values:
            values.append(value)
    return values


def _paragraph_relationship_widths(paragraph: ET.Element) -> dict[str, int]:
    """Return authored DrawingML display widths keyed by relationship ID."""

    values: dict[str, int] = {}
    for drawing in _effective_descendants(paragraph, _tag("w", "drawing")):
        blips = list(_effective_descendants(drawing, _tag("a", "blip")))
        relationship_ids = {
            str(blip.get(_tag("r", "embed")) or "")
            for blip in blips
            if str(blip.get(_tag("r", "embed")) or "")
        }
        # A grouped DrawingML object can contain several raster panels under
        # one outer wp:extent.  That extent describes the complete group, not
        # its first image.  Assigning it to the first blip makes the remaining
        # panels collapse during fallback composition, so retain authored
        # media dimensions unless the drawing has exactly one image.
        if len(relationship_ids) != 1:
            continue
        relationship_id = next(iter(relationship_ids))
        extent = drawing.find(".//wp:extent", NS)
        try:
            width = int(str(extent.get("cx") if extent is not None else ""))
        except ValueError:
            width = 0
        if relationship_id and width > 0:
            values[relationship_id] = width
    return values


def _paragraph_relationship_crops(
    paragraph: ET.Element,
) -> dict[str, tuple[int, int, int, int]]:
    """Return authored DrawingML source crops keyed by relationship ID.

    ``a:srcRect`` values are thousandths of a percent removed from the source
    image's left, top, right, and bottom edges.  The original package media is
    retained separately; these values apply only to the display derivative.
    """

    values: dict[str, tuple[int, int, int, int]] = {}
    for container in _effective_descendants(paragraph, _tag("pic", "blipFill")):
        blip = container.find("a:blip", NS)
        source_crop = container.find("a:srcRect", NS)
        if blip is None or source_crop is None:
            continue
        relationship_id = str(blip.get(_tag("r", "embed")) or "")
        if not relationship_id:
            continue
        crop: list[int] = []
        for edge in ("l", "t", "r", "b"):
            try:
                value = int(str(source_crop.get(edge) or "0"))
            except ValueError:
                value = 0
            crop.append(max(0, min(100000, value)))
        if crop[0] + crop[2] >= 100000 or crop[1] + crop[3] >= 100000:
            continue
        if any(crop):
            values[relationship_id] = tuple(crop)  # type: ignore[assignment]
    return values


def _apply_authored_crop(
    png_bytes: bytes,
    crop: tuple[int, int, int, int] | None,
) -> tuple[bytes, int, int]:
    """Apply an OOXML ``a:srcRect`` crop to a lossless PNG derivative."""

    from PIL import Image

    # Publisher-authored vector artwork can legitimately rasterize above
    # Pillow's default decompression-bomb threshold at the required lossless
    # review resolution.  Permit those images only within a deliberate hard
    # ceiling, and restore Pillow's process-wide setting immediately after the
    # header has been decoded.  The explicit pixel check keeps malformed or
    # unexpectedly enormous package media from allocating without bound.
    max_docx_raster_pixels = 250_000_000
    previous_limit = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = max_docx_raster_pixels
        image = Image.open(io.BytesIO(png_bytes))
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
    with image:
        pixels = image.width * image.height
        if pixels > max_docx_raster_pixels:
            raise ValueError(
                "DOCX display derivative exceeds the safe raster ceiling: "
                f"{image.width}x{image.height} ({pixels} pixels)"
            )
        image.load()
        if crop is None and max(image.width, image.height) <= DOCX_DISPLAY_MAX_EDGE:
            return png_bytes, image.width, image.height
        rendered = image
        if crop is not None:
            left, top, right, bottom = crop
            box = (
                round(image.width * left / 100000),
                round(image.height * top / 100000),
                image.width - round(image.width * right / 100000),
                image.height - round(image.height * bottom / 100000),
            )
            rendered = image.crop(box)
        if max(rendered.width, rendered.height) > DOCX_DISPLAY_MAX_EDGE:
            scale = DOCX_DISPLAY_MAX_EDGE / max(rendered.width, rendered.height)
            target = (
                max(1, round(rendered.width * scale)),
                max(1, round(rendered.height * scale)),
            )
            rendered = rendered.resize(target, Image.Resampling.LANCZOS)
        output = io.BytesIO()
        rendered.convert("RGBA" if "A" in rendered.getbands() else "RGB").save(
            output, format="PNG", optimize=False, compress_level=6
        )
        return output.getvalue(), rendered.width, rendered.height


def _visual_caption_textbox(
    paragraph: ET.Element,
) -> tuple[str, str] | None:
    """Return one unambiguous figure/scheme caption stored in a text box.

    Some Word supplements group chart drawings, native chart-label text boxes,
    and the authored caption in one outer paragraph.  The paragraph's combined
    text therefore does not begin with ``Figure S...`` even though one distinct
    text box does.  Accept only exactly one labeled text box so unrelated chart
    labels cannot be mistaken for a caption.
    """

    candidates: list[tuple[str, str]] = []
    for textbox in _effective_descendants(paragraph, _tag("w", "txbxContent")):
        markdown, plain = _paragraph_text(textbox)
        if _FIGURE_LABEL.match(plain) is not None or _SCHEME_LABEL.match(plain) is not None:
            candidates.append((markdown, plain))
    return candidates[0] if len(candidates) == 1 else None


def _safe_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"[^A-Za-z0-9]+", "_", ascii_value).strip("_").casefold() or "item"


def _relationship_targets(archive: zipfile.ZipFile) -> dict[str, str]:
    rels = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
    result: dict[str, str] = {}
    for relationship in rels.findall("rel:Relationship", NS):
        relationship_id = str(relationship.get("Id") or "")
        if not relationship_id or str(relationship.get("TargetMode") or "").casefold() == "external":
            continue
        target = str(relationship.get("Target") or "").replace("\\", "/")
        resolved = posixpath.normpath(posixpath.join("word", target))
        if resolved.startswith("../") or resolved.startswith("/"):
            raise ValueError(f"unsafe DOCX relationship target: {target!r}")
        result[relationship_id] = resolved
    return result


def _write_asset(extraction_root: Path, relative_path: str, data: bytes) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative_path:
        raise ValueError(f"unsafe DOCX asset path: {relative_path!r}")
    destination = ensure_within(
        extraction_root.joinpath(*pure.parts), extraction_root, require_exists=False
    )
    reject_reparse_chain(destination.parent, extraction_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    reject_reparse_chain(destination.parent, extraction_root)
    if destination.exists():
        if is_reparse_point(destination) or not stat.S_ISREG(destination.lstat().st_mode):
            raise ValueError(f"unsafe existing DOCX asset destination: {destination}")
        if destination.read_bytes() != data:
            raise FileExistsError(f"refusing to replace a different DOCX asset: {destination}")
        return destination

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
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise FileExistsError(
                f"DOCX asset destination appeared during copy: {destination}"
            ) from exc
        temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if destination.read_bytes() != data:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"DOCX asset copy failed verification: {destination}")
    return destination


def _single_bitmap_emf(data: bytes) -> bytes | None:
    """Recover an untransformed bitmap from a zero-frame EMF wrapper.

    Some legacy OLE previews wrap one DIB in an EMF with zero physical bounds.
    Decode only the exact single-bitmap form; vector compositions still require
    a reviewed native-layout render.
    """
    if len(data) < 88 or data[40:44] != b" EMF" or any(data[8:40]):
        return None
    offset = 0
    bitmap = None
    while offset + 8 <= len(data):
        kind, size = struct.unpack_from('<II', data, offset)
        if size < 8 or offset + size > len(data) or kind not in {1, 9, 11, 14, 17, 81}:
            return None
        if kind == 81:
            if bitmap is not None or size < 80:
                return None
            record = data[offset:offset + size]
            x, y, sx, sy, sw, sh, bi, cb, bits, nb, usage, rop, dw, dh = struct.unpack_from('<14i', record, 24)
            if (x, y, sx, sy, usage, rop) != (0, 0, 0, 0, 0, 0xCC0020) or (sw, sh) != (dw, dh) or min(sw, sh) <= 0:
                return None
            if bi < 80 or cb < 40 or bits < bi + cb or nb < 1 or bits + nb > size:
                return None
            dib = record[bi:bi + cb] + record[bits:bits + nb]
            bitmap = b'BM' + struct.pack('<IHHI', 14 + len(dib), 0, 0, 14 + cb) + dib
        offset += size
    return bitmap if offset == len(data) else None


def _windows_metafile_png(data: bytes, width: int, height: int) -> bytes:
    """Render native metafile text with GDI+, avoiding Pillow GDI font drift.

    Bytes pass through stdin/stdout only, so private embedded art never needs
    a shared temporary file. The fixed script performs no external lookup.
    """
    import base64
    executable = _powershell_executable()
    if executable is None:
        raise UnsupportedDocxImageError("PowerShell is required for native metafile rendering")
    script = r'''Add-Type -AssemblyName System.Drawing
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$inputStream = New-Object System.IO.MemoryStream(,[Convert]::FromBase64String($payload.data))
$metafile = [System.Drawing.Imaging.Metafile]::FromStream($inputStream)
$bitmap = New-Object System.Drawing.Bitmap([int]$payload.width,[int]$payload.height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$outputStream = New-Object System.IO.MemoryStream
try {
  $graphics.Clear([System.Drawing.Color]::White)
  $graphics.DrawImage($metafile,[System.Drawing.Rectangle]::new(0,0,[int]$payload.width,[int]$payload.height))
  $bitmap.Save($outputStream,[System.Drawing.Imaging.ImageFormat]::Png)
  [Console]::Out.Write([Convert]::ToBase64String($outputStream.ToArray()))
} finally {
  $graphics.Dispose(); $bitmap.Dispose(); $metafile.Dispose()
  $inputStream.Dispose(); $outputStream.Dispose()
}'''
    payload = json.dumps({"data": base64.b64encode(data).decode("ascii"),
                          "width": width, "height": height})
    result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command", script],
                            input=payload, text=True, capture_output=True, timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise UnsupportedDocxImageError("Native metafile rendering failed")
    try:
        return base64.b64decode(result.stdout.strip(), validate=True)
    except ValueError as exc:
        raise UnsupportedDocxImageError("Native metafile renderer returned invalid data") from exc


def _png_derivative(data: bytes) -> tuple[bytes, int, int]:
    try:
        from PIL import Image, UnidentifiedImageError
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency validation owns this
        raise RuntimeError("Pillow is required for DOCX figure extraction") from exc

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.seek(0)
            if image.format == "WMF" and os.name == "nt":
                source_dpi = image.info.get("dpi", 72)
                if isinstance(source_dpi, tuple):
                    source_dpi = source_dpi[0]
                width, height = (max(1, round(value * DOCX_FIGURE_RENDER_DPI / source_dpi))
                                 for value in image.size)
                rendered = _windows_metafile_png(data, width, height)
                with Image.open(io.BytesIO(rendered)) as verified:
                    verified.load()
                    if verified.size != (width, height):
                        raise UnsupportedDocxImageError("Native metafile dimensions changed")
                return rendered, width, height
            try:
                # Pillow's WMF/EMF loader accepts a render DPI; raster loaders do
                # not.  Preserve vector detail at the same reviewed-document DPI.
                image.load(dpi=DOCX_FIGURE_RENDER_DPI)
            except TypeError:
                image.load()
            if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            width, height = image.size
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=False, compress_level=6)
    except (UnidentifiedImageError, OSError, ZeroDivisionError) as exc:
        bitmap = _single_bitmap_emf(data)
        if bitmap is not None:
            return _png_derivative(bitmap)
        # Legacy Word conversions can retain authored WMF or PICT objects that
        # Pillow cannot decode losslessly (including EMF zero frame extents).
        # The caller still preserves those
        # native bytes and exposes a captioned, asset-less figure so the
        # hash-pinned native-page crop workflow can materialize the complete
        # composed visual without inventing or partially dropping content.
        raise UnsupportedDocxImageError(
            "DOCX image requires a reviewed native-layout render"
        ) from exc
    return output.getvalue(), width, height


def _compose_vertical_png(
    components: list[tuple[bytes, int, int, int | None]],
) -> tuple[bytes, int, int]:
    """Compose authored consecutive DOCX drawings at their relative widths.

    A Word figure can be split across multiple image-only paragraphs while one
    following caption identifies the group as a single scientific figure.  The
    OOXML drawing extents preserve the author's relative display widths.  Use
    a common no-upscale raster scale, center narrower components, and retain
    each original image separately as a downloadable source asset.
    """

    if len(components) < 2:
        raise ValueError("a composed DOCX figure requires at least two components")
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency validation owns this
        raise RuntimeError("Pillow is required for DOCX figure extraction") from exc

    effective_widths = [display_width or width for _, width, _, display_width in components]
    maximum_effective = max(effective_widths)
    ratios = [width / maximum_effective for width in effective_widths]
    canvas_width = max(
        1,
        int(
            min(
                native_width / ratio
                for (_, native_width, _, _), ratio in zip(components, ratios)
            )
        ),
    )
    prepared: list[Any] = []
    for (png_bytes, native_width, native_height, _), ratio in zip(
        components, ratios
    ):
        target_width = max(1, int(round(canvas_width * ratio)))
        target_height = max(1, int(round(native_height * target_width / native_width)))
        with Image.open(io.BytesIO(png_bytes)) as image:
            image.load()
            converted = image.convert("RGBA")
            if converted.size != (target_width, target_height):
                converted = converted.resize(
                    (target_width, target_height), Image.Resampling.LANCZOS
                )
            prepared.append(converted.copy())

    gap = max(8, canvas_width // 150)
    canvas_height = sum(image.height for image in prepared) + gap * (len(prepared) - 1)
    canvas = Image.new("RGBA", (canvas_width, canvas_height), (255, 255, 255, 255))
    y = 0
    for image in prepared:
        x = (canvas_width - image.width) // 2
        canvas.alpha_composite(image, (x, y))
        y += image.height + gap
    output = io.BytesIO()
    canvas.convert("RGB").save(
        output, format="PNG", optimize=False, compress_level=6
    )
    return output.getvalue(), canvas_width, canvas_height


@dataclass
class _PendingFigure:
    kind: str
    number: str
    label: str
    body_indices: list[int]
    paragraphs: list[tuple[str, str]] = field(default_factory=list)
    relationship_ids: list[str] = field(default_factory=list)


@dataclass
class _PendingTable:
    number: str
    label: str
    markdown: str
    plain: str
    body_index: int


def _image_before_figure_label_map(
    body_children: list[ET.Element],
) -> dict[int, tuple[list[int], list[str]]]:
    """Map an unambiguous image-before-caption DOCX layout.

    Some Word supplements place one or more inline drawings immediately before
    a ``Supplementary Fig. N`` or ``Scheme S1`` label.  The normal parser
    supports label-first documents.  Recognize each unambiguous preceding visual
    group independently so mixed documents remain supported.  Blank spacers,
    separate short panel markers (``a)``), and short text attached to a drawing
    are part of that visual group.  A drawing paragraph that carries its own
    unambiguous caption text box remains self-contained.
    """

    labels: list[int] = []
    drawings: dict[int, list[str]] = {}
    for body_index, child in enumerate(body_children, 1):
        if child.tag != _tag("w", "p"):
            continue
        _, plain = _paragraph_text(child)
        relationship_ids = _paragraph_relationship_ids(child)
        self_contained_visual = bool(
            relationship_ids and _visual_caption_textbox(child) is not None
        )
        if not self_contained_visual and (
            _FIGURE_LABEL.match(plain) is not None
            or _SCHEME_LABEL.match(plain) is not None
        ) and child.find(".//w:lastRenderedPageBreak", NS) is None:
            # A label beginning on a new rendered page cannot describe a
            # drawing left on the preceding page.  Treat it as label-first so
            # a long run of authored spacer paragraphs cannot make the
            # backwards matcher steal the previous figure's image.
            labels.append(body_index)
        if relationship_ids and not self_contained_visual:
            concise_visual_text = _normalize_inline(plain)
            floating_in_prose = bool(child.findall('.//v:shape', NS)) and all(
                'position:absolute' in (shape.get('style') or '').replace(' ', '')
                for shape in child.findall('.//v:shape', NS)
                if shape.find('v:imagedata', NS) is not None
            )
            if plain and not floating_in_prose and (
                len(concise_visual_text) > 80
                or "\n" in plain
                or _FIGURE_LABEL.match(plain) is not None
                or _SCHEME_LABEL.match(plain) is not None
                or _TABLE_LABEL.match(plain) is not None
            ):
                continue
            drawings[body_index] = relationship_ids
    if not labels or not drawings:
        return {}

    result: dict[int, tuple[list[int], list[str]]] = {}
    used: set[int] = set()
    for label_index in labels:
        candidate = label_index - 1
        owned: list[int] = []
        owned_drawings: list[int] = []
        preceding_caption_boundary = False
        while candidate >= 1:
            node = body_children[candidate - 1]
            if node.tag != _tag("w", "p"):
                break
            # A hard page break at the end of an authored paragraph is an
            # exact boundary before a following drawing.  Do not let the
            # backwards image-before-caption matcher absorb contents-list or
            # other prose from the preceding page into the visual group.
            if node.find('.//w:br[@w:type="page"]', NS) is not None:
                break
            _, plain = _paragraph_text(node)
            relationship_ids = _paragraph_relationship_ids(node)
            if relationship_ids:
                if candidate not in drawings:
                    break
                owned.append(candidate)
                owned_drawings.append(candidate)
                if plain and len(_normalize_inline(plain)) > 80:
                    # Floating artwork is anchored to prose but remains part
                    # of the following visual. Keep that prose in body order.
                    break
                candidate -= 1
                continue
            if plain:
                # A drawing between two authored visual labels is not an
                # unambiguous image-before-caption group: it may instead be
                # the normal label-first image owned by the earlier caption.
                # Leave that sequence to the forward parser, which can pair
                # both labels and drawings without stealing the first image.
                if (
                    _FIGURE_LABEL.match(plain) is not None
                    or _SCHEME_LABEL.match(plain) is not None
                    or _TABLE_LABEL.match(plain) is not None
                ):
                    preceding_caption_boundary = bool(owned_drawings) and (
                        candidate not in result
                    )
                    break
                concise_visual_label = _normalize_inline(plain)
                # A postal-address continuation is front matter, even though
                # it is short and can precede an image-before-caption group.
                if re.search(r",\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*$", concise_visual_label):
                    break
                panel_marker = re.fullmatch(
                    r"\(?[A-Za-z0-9]\)?[.)]?", concise_visual_label
                )
                if not panel_marker and (
                    _paragraph_has_heading_style(node)
                    or (
                        _is_decimal_numbered_heading(plain)
                        and not _paragraph_has_toc_style(node)
                    )
                    or _is_standalone_bold_paragraph(*_paragraph_text(node))
                ):
                    break
                if not panel_marker and not (
                    owned_drawings
                    and len(concise_visual_label) <= 80
                    and "\n" not in plain
                    and not re.search(r"[.!?;]$", concise_visual_label)
                    and _FIGURE_LABEL.match(plain) is None
                    and _SCHEME_LABEL.match(plain) is None
                    and _TABLE_LABEL.match(plain) is None
                ):
                    break
                owned.append(candidate)
            candidate -= 1
        owned.reverse()
        owned_drawings.reverse()
        if (
            preceding_caption_boundary
            or not owned_drawings
            or any(index in used for index in owned_drawings)
        ):
            continue
        relationship_ids = [
            relationship_id
            for index in owned_drawings
            for relationship_id in drawings[index]
        ]
        used.update(owned_drawings)
        result[label_index] = (owned, relationship_ids)
    return result


def _cell_paragraph_content(
    cell: ET.Element,
    *,
    direct_only: bool = False,
) -> list[tuple[str, str]]:
    paragraphs = cell.findall("w:p", NS) if direct_only else cell.findall(".//w:p", NS)
    return [
        pair
        for paragraph in paragraphs
        if any(pair := _paragraph_text(paragraph))
    ]


def _cell_content(
    cell: ET.Element,
    *,
    direct_only: bool = False,
) -> tuple[str, str]:
    pairs = _cell_paragraph_content(cell, direct_only=direct_only)
    return "<br>".join(pair[0] for pair in pairs), "\n".join(
        pair[1] for pair in pairs
    )


def _native_table(
    element: ET.Element,
    source: SourceFile,
    supplement_id: str,
    table_number: int,
    pending: _PendingTable | None,
    body_index: int,
    *,
    direct_cell_paragraphs: bool = False,
    locator_suffix: str = "",
) -> TableItem:
    table_rows = element.findall("w:tr", NS)
    internal_title: tuple[str, str, str, str] | None = None
    if pending is None and table_rows:
        title_cells = table_rows[0].findall("w:tc", NS)
        if len(title_cells) == 1:
            title_markdown, title_plain = _cell_content(
                title_cells[0], direct_only=direct_cell_paragraphs
            )
            title_match = _TABLE_LABEL.fullmatch(title_plain)
            if title_match is not None:
                if title_match.group("number"):
                    title_number = title_match.group("number").upper().replace(":", "")
                    title_label = f"Table {title_number}"
                else:
                    authored_number = title_match.group("long_number")
                    title_number = authored_number.upper()
                    if not title_number.startswith("S"):
                        title_number = f"S{title_number}"
                    authored_kind = re.sub(
                        r"\s+", " ", title_match.group("long_kind")
                    )
                    title_label = f"{authored_kind} {authored_number}"
                internal_title = (
                    title_number,
                    title_label,
                    title_markdown,
                    title_plain,
                )

    number = (
        pending.number
        if pending is not None
        else internal_title[0]
        if internal_title is not None
        else str(table_number)
    )
    label = (
        pending.label
        if pending is not None
        else internal_title[1]
        if internal_title is not None
        else f"Table {number}"
    )
    table_id = f"{supplement_id}_table_{_safe_identifier(number)}"
    rows: list[list[TableCell]] = []
    active_vertical: dict[int, TableCell] = {}
    data_rows = table_rows[1:] if internal_title is not None else table_rows
    # An explicitly counted list of gene identifiers is a data grid, not a
    # column-headed table. Keep the first identifiers as data when every cell
    # fits that structure and the native document marks no repeating header.
    # Ordinary tables retain the established first-row header fallback.
    table_title = pending.plain if pending is not None else internal_title[3] if internal_title is not None else ""
    gene_count = re.search(r"\ball\s+(\d+)\s+genes\b", table_title, re.IGNORECASE)
    grid_cells = [cell for row in data_rows for cell in row.findall("w:tc", NS)]
    headerless_gene_grid = bool(
        gene_count
        and len(grid_cells) == int(gene_count.group(1))
        and len(data_rows) > 1
        and all(row.find("w:trPr/w:tblHeader", NS) is None for row in data_rows)
        and all(re.fullmatch(r"[A-Z][A-Z0-9-]{1,19}", _cell_content(cell, direct_only=direct_cell_paragraphs)[1]) for cell in grid_cells)
    )
    for row_number, row_element in enumerate(data_rows, 1):
        row: list[TableCell] = []
        cell_paragraphs: list[list[tuple[str, str]]] = []
        column = 0
        row_has_merge = False
        explicit_header = row_element.find("w:trPr/w:tblHeader", NS) is not None
        for cell_element in row_element.findall("w:tc", NS):
            properties = cell_element.find("w:tcPr", NS)
            grid_span = properties.find("w:gridSpan", NS) if properties is not None else None
            try:
                colspan = max(1, int(_attr(grid_span, "w", "val") or "1"))
            except ValueError:
                colspan = 1
            vertical = properties.find("w:vMerge", NS) if properties is not None else None
            vertical_value = _attr(vertical, "w", "val").casefold()
            row_has_merge = row_has_merge or colspan != 1 or vertical is not None
            if vertical is not None and vertical_value != "restart":
                prior = active_vertical.get(column)
                if prior is not None:
                    prior.rowspan += 1
                column += colspan
                continue

            paragraphs = _cell_paragraph_content(
                cell_element, direct_only=direct_cell_paragraphs
            )
            markdown = "<br>".join(pair[0] for pair in paragraphs)
            plain = "\n".join(pair[1] for pair in paragraphs)
            cell = TableCell(
                text=plain,
                markdown=markdown,
                header=(row_number == 1 and not headerless_gene_grid) or explicit_header,
                colspan=colspan,
            )
            row.append(cell)
            cell_paragraphs.append(paragraphs)
            for covered in range(column, column + colspan):
                if vertical is not None and vertical_value == "restart":
                    active_vertical[covered] = cell
                else:
                    active_vertical.pop(covered, None)
            column += colspan

        # Empty layout rows can carry a closing border in Word but contain no
        # table data. Preserve merge-continuation rows; omit only independent
        # empty rows.
        if not row_has_merge and row and not any(cell.text for cell in row):
            continue

        # Some authored Word tables encode several visual data rows as
        # synchronized paragraphs inside one XML row. When every unmerged
        # data cell has the same multi-paragraph cardinality, emit those
        # parallel values as semantic rows. Header rows stay intact because
        # multi-line headings are common.
        paragraph_counts = {len(paragraphs) for paragraphs in cell_paragraphs}
        if (
            not row_has_merge
            and not ((row_number == 1 and not headerless_gene_grid) or explicit_header)
            and len(paragraph_counts) == 1
            and next(iter(paragraph_counts), 0) > 1
        ):
            for paragraph_index in range(next(iter(paragraph_counts))):
                rows.append(
                    [
                        TableCell(
                            text=paragraphs[paragraph_index][1],
                            markdown=paragraphs[paragraph_index][0],
                            header=False,
                            colspan=1,
                        )
                        for paragraphs in cell_paragraphs
                    ]
                )
            continue

        rows.append(row)

    title_markdown = (
        pending.markdown
        if pending is not None
        else internal_title[2]
        if internal_title is not None
        else _escape_run_text(label)
    )
    title_plain = (
        pending.plain
        if pending is not None
        else internal_title[3]
        if internal_title is not None
        else label
    )
    return TableItem(
        table_id=table_id,
        source_id=label,
        label=label,
        title_markdown=title_markdown,
        title_plain=title_plain,
        parts=[TablePart(part_id=f"{table_id}_part_001", rows=rows)],
        footnotes_markdown=[],
        footnotes_plain=[],
        source_path=source.relative_path,
        source_locator=(
            f"docx-part=word/document.xml;body-child={body_index};table={table_number}"
            f"{locator_suffix}"
        ),
        source_kind="document",
    )


def _split_authored_combined_tables(table: TableItem) -> list[TableItem]:
    """Split one Word table containing later full-width authored table titles."""

    rows = table.parts[0].rows
    markers: list[tuple[int, re.Match[str]]] = []
    for row_index, row in enumerate(rows):
        if len(row) != 1 or row[0].colspan <= 1:
            continue
        match = _TABLE_LABEL.fullmatch(row[0].text)
        if match is not None:
            markers.append((row_index, match))
    if not markers:
        return [table]

    boundaries = [0, *(row_index for row_index, _ in markers), len(rows)]
    result: list[TableItem] = []
    for segment_index in range(len(boundaries) - 1):
        start = boundaries[segment_index]
        end = boundaries[segment_index + 1]
        if segment_index == 0:
            title_markdown = table.title_markdown
            title_plain = table.title_plain
            label = table.label
            number = table.source_id.removeprefix("Table ")
            segment_rows = rows[start:end]
        else:
            marker_row = rows[start]
            marker_match = markers[segment_index - 1][1]
            authored_number = (
                marker_match.group("number") or marker_match.group("long_number")
            )
            number = authored_number.upper().replace(":", "")
            if not number.startswith("S"):
                number = f"S{number}"
            label = f"Table {number}"
            title_markdown = marker_row[0].markdown
            title_plain = marker_row[0].text
            segment_rows = rows[start + 1 : end]
        if not segment_rows:
            continue
        for cell in segment_rows[0]:
            cell.header = True
        table_id = (
            f"{table.table_id.rsplit('_table_', 1)[0]}_table_"
            f"{_safe_identifier(number)}"
        )
        result.append(
            TableItem(
                table_id=table_id,
                source_id=label,
                label=label,
                title_markdown=title_markdown,
                title_plain=title_plain,
                parts=[
                    TablePart(
                        part_id=f"{table_id}_part_001",
                        rows=segment_rows,
                    )
                ],
                footnotes_markdown=(
                    table.footnotes_markdown
                    if segment_index == len(boundaries) - 2
                    else []
                ),
                footnotes_plain=(
                    table.footnotes_plain
                    if segment_index == len(boundaries) - 2
                    else []
                ),
                source_path=table.source_path,
                source_locator=(
                    f"{table.source_locator};authored-table-segment={segment_index + 1}"
                ),
                source_kind=table.source_kind,
            )
        )
    return result


def extract_docx_supplement(
    source: SourceFile,
    supplement_id: str,
    *,
    docx_path: Path,
    extraction_root: Path,
) -> tuple[
    list[ContentBlock],
    list[FigureItem],
    list[TableItem],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Return native DOCX content and materialized figure assets."""

    if source.detected_format != DOCX_MEDIA_TYPE:
        raise ValueError(f"not a DOCX supplement: {source.detected_format}")
    extraction_root = extraction_root.resolve(strict=True)
    blocks: list[ContentBlock] = []
    figures: list[FigureItem] = []
    tables: list[TableItem] = []
    assets: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    pending_figure: _PendingFigure | None = None
    pending_table: _PendingTable | None = None
    last_table: TableItem | None = None
    block_number = 0
    table_number = 0
    in_numbered_reference_section = False

    with zipfile.ZipFile(docx_path) as archive:
        names = set(archive.namelist())
        required = {"word/document.xml", "word/_rels/document.xml.rels"}
        missing = sorted(required - names)
        if missing:
            raise ValueError(f"DOCX package is missing {', '.join(missing)}")
        relationships = _relationship_targets(archive)
        document = ET.fromstring(archive.read("word/document.xml"))
        if "word/styles.xml" in names:
            _inherit_paragraph_emphasis(document, ET.fromstring(archive.read("word/styles.xml")))
        # Preserve reachable native OLE/package objects without executing them.
        # Their preview drawing is a separate visual, not a replacement for
        # the embedded source bytes.
        embedded_targets: set[str] = set()
        for element in document.iter():
            relationship_id = _attr(element, "r", "id")
            package_path = relationships.get(relationship_id, "")
            if not package_path.startswith("word/embeddings/") or package_path in embedded_targets:
                continue
            if package_path not in names:
                raise ValueError(f"DOCX embedded object is missing: {package_path!r}")
            embedded_targets.add(package_path)
            data = archive.read(package_path)
            relative = (PurePosixPath("supplementary") / supplement_id / package_path).as_posix()
            _write_asset(extraction_root, relative, data)
            assets.append({
                "asset_id": f"{supplement_id}_embedded_{len(embedded_targets):03d}",
                "category": "supplement_data",
                "label": f"Embedded DOCX object ({PurePosixPath(package_path).name})",
                "media_type": "application/octet-stream",
                "output_path": relative,
                "parent_id": supplement_id,
                "source_locator": f"docx-part={package_path};relationship={relationship_id}",
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "ocr_performed": False,
            })
        body = document.find("w:body", NS)
        if body is None:
            raise ValueError("DOCX document.xml has no body")
        body_children = list(body)
        native_list_labels = (
            _decimal_list_labels(body, ET.fromstring(archive.read("word/numbering.xml")))
            if "word/numbering.xml" in names else {}
        )
        claimed_relationship_ids: set[str] = set()
        relationship_widths = {
            relationship_id: width
            for child in body_children
            if child.tag == _tag("w", "p")
            for relationship_id, width in _paragraph_relationship_widths(child).items()
        }
        relationship_crops = {
            relationship_id: crop
            for child in body_children
            if child.tag == _tag("w", "p")
            for relationship_id, crop in _paragraph_relationship_crops(child).items()
        }

        def materialize_figure(
            pending: _PendingFigure,
            body_index: int,
        ) -> None:
            caption_markdown = "<br>".join(
                pair[0] for pair in pending.paragraphs if pair[0]
            )
            caption_plain = "\n".join(
                pair[1] for pair in pending.paragraphs if pair[1]
            )
            base_item_stem = f"{pending.kind}_{_safe_identifier(pending.number)}"
            base_figure_id = f"{supplement_id}_{base_item_stem}"
            prior_drawn_occurrences = sum(
                item.output_path is not None
                and (
                    item.figure_id == base_figure_id
                    or item.figure_id.startswith(f"{base_figure_id}_occurrence_")
                )
                for item in figures
            )
            # Authored Word supplements occasionally reuse one printed figure
            # number for two different drawings. Preserve that source label,
            # but disambiguate only the internal identity and asset path for
            # the later drawn occurrence. Caption-only contents entries keep
            # the base identity so the post-pass can still consolidate them
            # with their one matching rendered figure.
            occurrence = (
                prior_drawn_occurrences + 1
                if pending.relationship_ids and prior_drawn_occurrences
                else 1
            )
            item_stem = (
                base_item_stem
                if occurrence == 1
                else f"{base_item_stem}_occurrence_{occurrence:02d}"
            )
            figure_id = f"{supplement_id}_{item_stem}"
            locator = (
                "docx-part=word/document.xml;body-children="
                f"{pending.body_indices[0]}-{body_index}"
            )
            if occurrence > 1:
                locator += f";authored-identity-occurrence={occurrence}"
            output_path: str | None = None
            component_rows: list[tuple[bytes, int, int, int | None]] = []
            component_locators: list[str] = []
            unsupported_component_locators: list[str] = []
            is_composed = len(pending.relationship_ids) > 1
            for media_number, relationship_id in enumerate(
                pending.relationship_ids, 1
            ):
                claimed_relationship_ids.add(relationship_id)
                package_path = relationships.get(relationship_id)
                if package_path is None or package_path not in names:
                    raise ValueError(
                        f"DOCX drawing has unresolved relationship {relationship_id!r}"
                    )
                if not package_path.startswith("word/media/"):
                    raise ValueError(
                        f"DOCX drawing relationship is not package media: {package_path!r}"
                    )
                source_bytes = archive.read(package_path)
                source_name = PurePosixPath(package_path).name
                suffix = PurePosixPath(source_name).suffix.casefold()
                source_relative = (
                    PurePosixPath("supplementary")
                    / supplement_id
                    / "assets"
                    / source_name
                ).as_posix()
                source_path = _write_asset(extraction_root, source_relative, source_bytes)
                source_asset_id = (
                    f"{figure_id}_source"
                    if not is_composed
                    else f"{figure_id}_source_{media_number:02d}"
                )
                assets.append(
                    {
                        "asset_id": source_asset_id,
                        "category": "supplement_source_image",
                        "label": f"{pending.label} source {source_name}",
                        "media_type": _MEDIA_TYPES.get(
                            suffix, "application/octet-stream"
                        ),
                        "output_path": source_relative,
                        "parent_id": figure_id,
                        "source_locator": (
                            f"docx-part={package_path};relationship={relationship_id}"
                        ),
                        "sha256": hashlib.sha256(source_bytes).hexdigest(),
                        "bytes": source_path.stat().st_size,
                        "ocr_performed": False,
                    }
                )

                try:
                    png_bytes, _, _ = _png_derivative(source_bytes)
                except UnsupportedDocxImageError:
                    unsupported_component_locators.append(
                        f"relationship={relationship_id};media={package_path}"
                    )
                    continue
                authored_crop = relationship_crops.get(relationship_id)
                png_bytes, width, height = _apply_authored_crop(
                    png_bytes, authored_crop
                )
                component_rows.append(
                    (
                        png_bytes,
                        width,
                        height,
                        relationship_widths.get(relationship_id),
                    )
                )
                crop_locator = (
                    ";srcRect=" + ",".join(str(value) for value in authored_crop)
                    if authored_crop is not None
                    else ""
                )
                component_locators.append(
                    f"relationship={relationship_id};media={package_path}"
                    f"{crop_locator}"
                )
                display_relative = (
                    PurePosixPath("figures")
                    / supplement_id
                    / (
                        f"{item_stem}.png"
                        if not is_composed
                        else (
                            f"{item_stem}_"
                            f"component_{media_number:02d}.png"
                        )
                    )
                ).as_posix()
                display_path = _write_asset(extraction_root, display_relative, png_bytes)
                display_asset_id = (
                    figure_id
                    if not is_composed
                    else f"{figure_id}_component_{media_number:02d}"
                )
                assets.append(
                    {
                        "asset_id": display_asset_id,
                        "category": pending.kind if not is_composed else "supplement_image",
                        "label": pending.label,
                        "media_type": "image/png",
                        "output_path": display_relative,
                        "parent_id": figure_id if is_composed else None,
                        "source_locator": (
                            f"docx-part={package_path};relationship={relationship_id};"
                            "derivative=lossless-png"
                            f"{crop_locator}"
                        ),
                        "sha256": hashlib.sha256(png_bytes).hexdigest(),
                        "bytes": display_path.stat().st_size,
                        "width": width,
                        "height": height,
                        "ocr_performed": False,
                    }
                )
                if not is_composed:
                    output_path = display_relative
                    locator += (
                        f";relationship={relationship_id};media={package_path}"
                    )

            if unsupported_component_locators:
                locator += (
                    ";requires-reviewed-native-layout-render="
                    + "|".join(unsupported_component_locators)
                )
            elif is_composed:
                png_bytes, width, height = _compose_vertical_png(component_rows)
                display_relative = (
                    PurePosixPath("figures") / supplement_id / f"{item_stem}.png"
                ).as_posix()
                display_path = _write_asset(extraction_root, display_relative, png_bytes)
                assets.append(
                    {
                        "asset_id": figure_id,
                        "category": pending.kind,
                        "label": pending.label,
                        "media_type": "image/png",
                        "output_path": display_relative,
                        "source_locator": (
                            "docx-composition="
                            + "|".join(component_locators)
                            + ";derivative=authored-vertical-composite-png"
                        ),
                        "sha256": hashlib.sha256(png_bytes).hexdigest(),
                        "bytes": display_path.stat().st_size,
                        "width": width,
                        "height": height,
                        "ocr_performed": False,
                    }
                )
                output_path = display_relative
                locator += ";" + "|".join(component_locators)

            figures.append(
                FigureItem(
                    figure_id=figure_id,
                    source_id=pending.label,
                    label=pending.label,
                    kind=pending.kind,
                    caption_markdown=caption_markdown,
                    caption_plain=caption_plain,
                    source_path=source.relative_path,
                    source_locator=locator,
                    output_path=output_path,
                )
            )

        image_before_labels = _image_before_figure_label_map(body_children)
        leading_image_indices = {
            image_index
            for image_indices, _ in image_before_labels.values()
            for image_index in image_indices
        }

        for body_index, child in enumerate(body_children, 1):
            if child.tag == _tag("w", "p"):
                markdown, plain = _paragraph_text(child)
                heading_style = _paragraph_has_heading_style(child)
                native_label = native_list_labels.get(body_index)
                if native_label and plain:
                    markdown = f"{_escape_run_text(native_label)} {markdown}"
                    plain = f"{native_label} {plain}"
                relationship_ids = _paragraph_relationship_ids(child)
                figure_match = _FIGURE_LABEL.match(plain)
                scheme_match = _SCHEME_LABEL.match(plain)
                if figure_match is None and scheme_match is None and relationship_ids:
                    textbox_caption = _visual_caption_textbox(child)
                    if textbox_caption is not None:
                        markdown, plain = textbox_caption
                        figure_match = _FIGURE_LABEL.match(plain)
                        scheme_match = _SCHEME_LABEL.match(plain)
                table_match = _TABLE_LABEL.match(plain)

                if body_index in leading_image_indices:
                    if pending_figure is not None:
                        materialize_figure(pending_figure, body_index - 1)
                        pending_figure = None
                    if len(_normalize_inline(plain)) <= 80:
                        continue
                    relationship_ids = []

                visual_match = figure_match if figure_match is not None else scheme_match
                visual_kind = "figure" if figure_match is not None else "scheme"
                if visual_match is not None:
                    if visual_match.group("number"):
                        number = visual_match.group("number").upper()
                        label = f"{visual_kind.title()} {number}"
                    else:
                        authored_number = visual_match.group("long_number")
                        number = authored_number.upper()
                        if not number.startswith("S"):
                            number = f"S{number}"
                        authored_kind = re.sub(
                            r"\s+", " ", visual_match.group("long_kind")
                        )
                        label = f"{authored_kind} {authored_number}"
                    if pending_figure is not None:
                        prior_plain = next(
                            (
                                item_plain
                                for _, item_plain in pending_figure.paragraphs
                                if item_plain
                            ),
                            "",
                        )
                        prior_match = (
                            _FIGURE_LABEL.match(prior_plain)
                            if pending_figure.kind == "figure"
                            else _SCHEME_LABEL.match(prior_plain)
                        )
                        prior_title = (
                            _normalize_inline(prior_match.group("title"))
                            if prior_match is not None
                            else ""
                        )
                        current_title = _normalize_inline(
                            visual_match.group("title")
                        )
                        # Word documents sometimes repeat a visual label: a
                        # bare authored heading appears before the drawing and
                        # the full caption repeats the same label afterward.
                        # Coalesce only that exact same-identity, label-only
                        # pattern. Distinct substantive captions with a reused
                        # number remain separate and will still fail duplicate
                        # identity validation instead of being hidden.
                        if (
                            pending_figure.kind == visual_kind
                            and pending_figure.number == number
                            and pending_figure.relationship_ids
                            and prior_match is not None
                            and (not prior_title or not current_title)
                        ):
                            pending_figure.body_indices.append(body_index)
                            for relationship_id in relationship_ids:
                                if relationship_id not in pending_figure.relationship_ids:
                                    pending_figure.relationship_ids.append(relationship_id)
                            if current_title and not prior_title:
                                pending_figure.paragraphs = [(markdown, plain)]
                            continue
                        materialize_figure(pending_figure, body_index - 1)
                    leading_image = image_before_labels.get(body_index)
                    pending_figure = _PendingFigure(
                        kind=visual_kind,
                        number=number,
                        label=label,
                        body_indices=(
                            [*leading_image[0], body_index]
                            if leading_image is not None
                            else [body_index]
                        ),
                        paragraphs=[(markdown, plain)],
                        relationship_ids=(
                            list(leading_image[1])
                            if leading_image is not None
                            else list(relationship_ids)
                        ),
                    )
                    pending_table = None
                    last_table = None
                    continue

                if pending_figure is not None:
                    # A figure legend may continue after its inline drawing. Keep the
                    # drawing pending until an authored structural boundary instead of
                    # emitting later legend paragraphs as unrelated supplement prose.
                    # Native tables and explicit table labels are unambiguous bounds;
                    # complete bold paragraphs are treated as a new subsection only
                    # after at least one drawing has been encountered.
                    caption_index = next((index for index in pending_figure.body_indices
                                          if index in image_before_labels), None)
                    caption_node = body_children[caption_index - 1] if caption_index else None
                    caption_size = (caption_node.find('./w:pPr/w:rPr/w:sz', NS)
                                    if caption_node is not None else None)
                    prose_after_caption = bool(
                        plain and caption_size is not None
                        and child.find('.//w:lastRenderedPageBreak', NS) is not None
                        and child.find('./w:pPr/w:spacing[@w:line="360"]', NS) is not None
                        and caption_node.find('./w:pPr/w:spacing[@w:line="360"]', NS) is None
                    )
                    if prose_after_caption or table_match is not None or (
                        plain
                        and (
                            (
                                pending_figure.relationship_ids
                                and (
                                    _is_standalone_bold_paragraph(markdown, plain)
                                    or heading_style
                                    or (
                                        _is_decimal_numbered_heading(plain)
                                        and not _paragraph_has_toc_style(child)
                                    )
                                    or _is_caption_only_document_boundary(
                                        markdown, plain
                                    )
                                )
                            )
                            or (
                                not pending_figure.relationship_ids
                                and pending_figure.paragraphs
                                and (
                                    heading_style
                                    or _is_caption_only_document_boundary(
                                        markdown, plain
                                    )
                                )
                            )
                        )
                    ):
                        materialize_figure(pending_figure, body_index - 1)
                        pending_figure = None
                    else:
                        pending_figure.body_indices.append(body_index)
                        if plain:
                            pending_figure.paragraphs.append((markdown, plain))
                        pending_figure.relationship_ids.extend(relationship_ids)
                        continue

                if table_match is not None:
                    if table_match.group("number"):
                        number = table_match.group("number").upper()
                        label = f"Table {number}"
                    else:
                        authored_number = table_match.group("long_number")
                        number = authored_number.upper()
                        if not number.startswith("S"):
                            number = f"S{number}"
                        authored_kind = re.sub(
                            r"\s+", " ", table_match.group("long_kind")
                        )
                        label = f"{authored_kind} {authored_number}"
                    pending_table = _PendingTable(
                        number=number,
                        label=label,
                        markdown=markdown,
                        plain=plain,
                        body_index=body_index,
                    )
                    last_table = None
                    continue

                if not plain:
                    continue
                if pending_table is not None:
                    pending_table.markdown = "<br>".join(
                        (pending_table.markdown, markdown)
                    )
                    pending_table.plain = "\n".join((pending_table.plain, plain))
                    continue
                if last_table is not None:
                    if (
                        _is_caption_only_document_boundary(markdown, plain)
                        or _is_numbered_references_heading(plain)
                        or _is_standalone_bold_paragraph(markdown, plain)
                        or heading_style
                        or re.match(r"^\d+\.\s+\S", plain)
                    ):
                        last_table = None
                    else:
                        last_table.footnotes_markdown.append(markdown)
                        last_table.footnotes_plain.append(plain)
                        continue

                is_heading = (
                    _is_standalone_bold_paragraph(markdown, plain)
                    or heading_style
                    or (
                        not native_label and _is_decimal_numbered_heading(plain)
                        and not _paragraph_has_toc_style(child)
                    )
                    or _is_numbered_references_heading(plain)
                    or _normalize_inline(plain).rstrip(".:").casefold()
                    in {"reference", "references"}
                )
                normalized_heading = _normalize_inline(plain).rstrip(".:").casefold()
                if is_heading and (
                    _is_numbered_references_heading(plain)
                    or normalized_heading in {"reference", "references"}
                ):
                    in_numbered_reference_section = True
                block_pairs = (
                    _split_numbered_reference_paragraph(markdown, plain)
                    if in_numbered_reference_section and not is_heading
                    else [(markdown, plain)]
                )
                for block_markdown, block_plain in block_pairs:
                    block_number += 1
                    blocks.append(
                        ContentBlock(
                            block_id=(
                                f"{supplement_id}-docx-block-{block_number:03d}"
                            ),
                            kind="subsection_heading" if is_heading else "text",
                            markdown=(
                                _heading_markdown(block_markdown)
                                if is_heading
                                else block_markdown
                            ),
                            plain_text=block_plain,
                            source_path=source.relative_path,
                            source_locator=(
                                "docx-part=word/document.xml;"
                                f"body-child={body_index}"
                            ),
                        )
                    )
            elif child.tag == _tag("w", "tbl"):
                if pending_figure is not None:
                    materialize_figure(pending_figure, body_index - 1)
                    pending_figure = None
                table_number += 1
                nested_tables = [
                    nested
                    for nested in child.findall("./w:tr/w:tc/w:tbl", NS)
                    if (nested_rows := nested.findall("w:tr", NS))
                    and len(nested_rows[0].findall("w:tc", NS)) == 1
                    and _TABLE_LABEL.fullmatch(
                        _cell_content(nested_rows[0].findall("w:tc", NS)[0])[1]
                    )
                    is not None
                ]
                for nested_number, nested in enumerate(nested_tables, 1):
                    nested_table = _native_table(
                        nested,
                        source,
                        supplement_id,
                        table_number,
                        None,
                        body_index,
                        locator_suffix=f";nested-table={nested_number}",
                    )
                    tables.extend(_split_authored_combined_tables(nested_table))
                table = _native_table(
                    child,
                    source,
                    supplement_id,
                    table_number,
                    pending_table,
                    body_index,
                    direct_cell_paragraphs=bool(nested_tables),
                )
                split_tables = _split_authored_combined_tables(table)
                tables.extend(split_tables)
                last_table = split_tables[-1]
                pending_table = None

        if pending_figure is not None:
            materialize_figure(pending_figure, len(list(body)))

        # Preserve every authored drawing that is not owned by a formal figure
        # caption as a generic, non-OCR visual asset.  Chemistry schemes,
        # spectra, and instrument plots in supplementary Word files are often
        # introduced by subsection headings rather than "Figure S#" labels.
        # Do not infer figure semantics; expose their pixels and exact OOXML
        # provenance through supplement.asset_ids instead.
        referenced_relationships: list[tuple[int, str]] = []
        seen_relationships: set[str] = set()
        for body_index, child in enumerate(body_children, 1):
            if child.tag != _tag("w", "p"):
                continue
            for relationship_id in _paragraph_relationship_ids(child):
                if relationship_id in seen_relationships:
                    continue
                seen_relationships.add(relationship_id)
                referenced_relationships.append((body_index, relationship_id))
        seen_package_paths: set[str] = {
            relationships[relationship_id]
            for relationship_id in claimed_relationship_ids
            if relationship_id in relationships
        }
        embedded_number = 0
        for body_index, relationship_id in referenced_relationships:
            if relationship_id in claimed_relationship_ids:
                continue
            package_path = relationships.get(relationship_id)
            if (
                package_path is None
                or package_path in seen_package_paths
                or package_path not in names
                or not package_path.startswith("word/media/")
            ):
                continue
            seen_package_paths.add(package_path)
            source_bytes = archive.read(package_path)
            source_name = PurePosixPath(package_path).name
            suffix = PurePosixPath(source_name).suffix.casefold()
            if suffix not in _MEDIA_TYPES:
                warnings.append(
                    {
                        "schema_version": "1.0",
                        "code": "docx_embedded_visual_format_unsupported",
                        "severity": "structural",
                        "message": (
                            "An uncaptioned DOCX visual could not be exposed as a "
                            f"renderable asset: {source_name}."
                        ),
                        "source_path": source.relative_path,
                        "source_locator": (
                            f"docx-part={package_path};relationship={relationship_id};"
                            f"body-child={body_index}"
                        ),
                    }
                )
                continue
            try:
                png_bytes, width, height = _png_derivative(source_bytes)
            except UnsupportedDocxImageError:
                warnings.append(
                    {
                        "schema_version": "1.0",
                        "code": "docx_embedded_visual_format_unsupported",
                        "severity": "structural",
                        "message": (
                            "An uncaptioned DOCX visual requires a reviewed "
                            f"native-layout render: {source_name}."
                        ),
                        "source_path": source.relative_path,
                        "source_locator": (
                            f"docx-part={package_path};relationship={relationship_id};"
                            f"body-child={body_index}"
                        ),
                    }
                )
                continue
            embedded_number += 1
            asset_id = f"{supplement_id}_embedded_visual_{embedded_number:03d}"
            output_path = (
                PurePosixPath("figures")
                / supplement_id
                / f"embedded_visual_{embedded_number:03d}.png"
            ).as_posix()
            rendered_path = _write_asset(extraction_root, output_path, png_bytes)
            visual_label = f"Embedded DOCX visual {embedded_number} ({source_name})"
            # Keep an adjacent authored label attached to an otherwise unnamed
            # image. This is a structural association, not inferred figure text.
            if body_index > 1:
                preceding = body_children[body_index - 2]
                _, preceding_text = _paragraph_text(preceding)
                _, current_text = _paragraph_text(body_children[body_index - 1])
                if (preceding.tag == _tag("w", "p") and not current_text
                        and not _paragraph_relationship_ids(preceding)
                        and 0 < len(preceding_text) <= 200):
                    visual_label = f"{preceding_text} ({source_name})"
            assets.append(
                {
                    "asset_id": asset_id,
                    "category": "supplement_image",
                    "label": visual_label,
                    "media_type": "image/png",
                    "output_path": output_path,
                    "source_locator": (
                        f"docx-part={package_path};relationship={relationship_id};"
                        f"body-child={body_index};derivative=non-ocr-png"
                    ),
                    "sha256": hashlib.sha256(png_bytes).hexdigest(),
                    "bytes": rendered_path.stat().st_size,
                    "width": width,
                    "height": height,
                    "ocr_performed": False,
                }
            )

    blocks = _merge_wrapped_docx_text_blocks(blocks)

    # A Word supplement can begin with an authored contents list whose entries
    # use the same ``Figure S#`` syntax as the later full figure captions.  The
    # contents entries have no drawing, while the actual figure records do.
    # Once the complete document has been parsed, discard only those
    # caption-only duplicates for which exactly one same-ID record owns a
    # rendered visual or an explicitly retained undecodable drawing. Distinct
    # duplicate drawn figures remain visible to the
    # normal duplicate-ID validator instead of being hidden here.
    figures_by_id: dict[str, list[FigureItem]] = {}
    def owns_drawing(item: FigureItem) -> bool:
        return item.output_path is not None or ";requires-reviewed-native-layout-render=" in item.source_locator

    for figure in figures:
        figures_by_id.setdefault(figure.figure_id, []).append(figure)
    superseded_caption_only_ids = {
        figure_id
        for figure_id, matching in figures_by_id.items()
        if len(matching) > 1
        and sum(owns_drawing(item) for item in matching) == 1
    }
    figures = [
        figure
        for figure in figures
        if not (
            figure.figure_id in superseded_caption_only_ids
            and not owns_drawing(figure)
        )
    ]

    # Remove internal null parent values before generic asset normalization.
    for asset in assets:
        if asset.get("parent_id") is None:
            asset.pop("parent_id", None)
    return blocks, figures, tables, assets, warnings


__all__ = [
    "DOCX_FIGURE_RENDER_DPI",
    "DOCX_MEDIA_TYPE",
    "DocxFigureCropRequest",
    "DocxFigureRenderer",
    "DocxFigureRenderError",
    "RenderedDocxFigureCrop",
    "extract_docx_supplement",
    "render_docx_figure_crops",
]
