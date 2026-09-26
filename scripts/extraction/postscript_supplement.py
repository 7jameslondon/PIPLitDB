"""Deterministic, OCR-free display rendering for reviewed PostScript supplements."""

from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pypdfium2

from .paths import is_reparse_point


POSTSCRIPT_MEDIA_TYPE = "application/postscript"
POSTSCRIPT_RENDER_DPI = 300
POSTSCRIPT_RENDER_TIMEOUT_SECONDS = 180
POSTSCRIPT_MAX_OUTPUT_PIXELS = 100_000_000


class PostscriptRenderError(RuntimeError):
    """Raised when a reviewed PostScript visual cannot be rendered safely."""


@dataclass(frozen=True)
class RenderedPostscript:
    png_bytes: bytes
    pixel_width: int
    pixel_height: int
    renderer: str
    renderer_version: str


class PostscriptRenderer(Protocol):
    def __call__(
        self, source_path: Path, working_root: Path, dpi: int
    ) -> RenderedPostscript: ...


def _libreoffice_executable() -> Path | None:
    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.com",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
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


def render_postscript_png(
    source_path: Path,
    working_root: Path,
    dpi: int = POSTSCRIPT_RENDER_DPI,
) -> RenderedPostscript:
    """Render one EPS/PS source through LibreOffice and PDFium without OCR.

    The source is opened read-only and copied into an isolated temporary
    compositor directory.  LibreOffice interprets the authored PostScript;
    PDFium then produces a deterministic lossless PNG for browser display.
    """

    if isinstance(dpi, bool) or not isinstance(dpi, int) or not 72 <= dpi <= 1200:
        raise PostscriptRenderError("PostScript display DPI is outside policy")
    source = Path(source_path).resolve(strict=True)
    if is_reparse_point(source) or not stat.S_ISREG(source.lstat().st_mode):
        raise PostscriptRenderError("PostScript renderer source is not a regular file")
    root = Path(working_root).resolve(strict=True)
    if is_reparse_point(root) or not root.is_dir():
        raise PostscriptRenderError("PostScript renderer root is unsafe")
    # A staged extraction root is already deeply nested.  LibreOffice on
    # Windows can fail before conversion when its isolated profile and cache
    # push a child path past legacy path-length limits, so keep the disposable
    # compositor directory at the same-record staging root.
    temporary_parent = root.parent.parent if root.name == "extraction" else root
    temporary_parent = temporary_parent.resolve(strict=True)
    if is_reparse_point(temporary_parent) or not temporary_parent.is_dir():
        raise PostscriptRenderError("PostScript temporary root is unsafe")
    soffice = _libreoffice_executable()
    if soffice is None:
        raise PostscriptRenderError(
            "LibreOffice is required for reviewed PostScript supplement rendering"
        )

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

    with tempfile.TemporaryDirectory(
        prefix=".postscript-render-", dir=temporary_parent
    ) as raw:
        temporary = Path(raw)
        renderer_source = temporary / "input.eps"
        converted_root = temporary / "converted"
        profile_root = temporary / "profile"
        converted_root.mkdir()
        profile_root.mkdir()
        shutil.copyfile(source, renderer_source)
        if renderer_source.read_bytes() != source.read_bytes():
            raise PostscriptRenderError(
                "PostScript renderer input copy failed verification"
            )
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
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=POSTSCRIPT_RENDER_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=renderer_environment,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise PostscriptRenderError(
                "LibreOffice could not render the reviewed PostScript source"
            ) from exc
        if completed.returncode != 0:
            detail = " ".join(
                f"{completed.stdout or ''} {completed.stderr or ''}".split()
            )[-600:]
            message = "LibreOffice could not render the reviewed PostScript source"
            if detail:
                message += f": {detail}"
            raise PostscriptRenderError(message)

        pdfs = sorted(converted_root.glob("*.pdf"))
        if len(pdfs) != 1 or not pdfs[0].is_file() or is_reparse_point(pdfs[0]):
            raise PostscriptRenderError(
                "LibreOffice did not create exactly one safe PostScript PDF"
            )
        try:
            document = pypdfium2.PdfDocument(str(pdfs[0]))
            try:
                if len(document) != 1:
                    raise PostscriptRenderError(
                        "reviewed PostScript supplement did not render as one page"
                    )
                page = document[0]
                try:
                    width_points, height_points = page.get_size()
                    pixel_width = max(1, round(width_points * dpi / 72.0))
                    pixel_height = max(1, round(height_points * dpi / 72.0))
                    if pixel_width * pixel_height > POSTSCRIPT_MAX_OUTPUT_PIXELS:
                        raise PostscriptRenderError(
                            "PostScript display exceeds the configured pixel limit"
                        )
                    bitmap = page.render(scale=dpi / 72.0)
                    try:
                        image = bitmap.to_pil()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            finally:
                document.close()
        except PostscriptRenderError:
            raise
        except Exception as exc:
            raise PostscriptRenderError(
                "LibreOffice created an unreadable PostScript PDF"
            ) from exc

        output = io.BytesIO()
        image.save(output, format="PNG", optimize=False, compress_level=6)
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
            renderer_version = " ".join(
                (version_result.stdout or version_result.stderr or "unknown").split()
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            renderer_version = "unknown"
        return RenderedPostscript(
            png_bytes=output.getvalue(),
            pixel_width=image.width,
            pixel_height=image.height,
            renderer="LibreOffice PostScript compositor + PDFium",
            renderer_version=renderer_version,
        )


__all__ = [
    "POSTSCRIPT_MEDIA_TYPE",
    "POSTSCRIPT_RENDER_DPI",
    "PostscriptRenderer",
    "RenderedPostscript",
    "render_postscript_png",
]
