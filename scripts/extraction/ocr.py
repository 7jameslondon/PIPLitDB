"""Deterministic, offline OCR for explicitly declared PDF text regions.

OCR is deliberately kept behind a small engine-neutral boundary.  Callers
must declare the document-text regions to recognize and every figure/scheme
region that must be excluded.  Intersecting visual regions are painted white
*before* an OCR engine receives the image, so a full-page text request cannot
accidentally recognize scientific figure pixels.

RapidOCR is optional at module-import time.  :class:`RapidOcrEngine` imports it
only when instantiated, requires explicit local model files, pins the CPU
backend configuration, and blocks socket access while the third-party engine
is initialized or invoked.  Model paths, versions, sizes, and SHA-256 hashes
are exposed for the private extraction diagnostics.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import io
import math
import re
import socket
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pypdfium2
from PIL import Image

from .paths import sha256_file


RAPIDOCR_VERSION = "3.9.2"
ONNXRUNTIME_VERSION = "1.28.0"
DEFAULT_OCR_DPI = 300
MAX_OCR_DPI = 1_200
MAX_OCR_PIXELS = 100_000_000

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OCR_ROLES = frozenset({"document_text", "textual_table", "equation", "caption"})
_VISUAL_ROLES = frozenset({"figure", "scheme"})
_MODEL_ROLES = ("detector", "classifier", "recognizer", "dictionary")
_NETWORK_LOCK = threading.RLock()

Point = tuple[float, float]
Box = tuple[float, float, float, float]


class OcrError(RuntimeError):
    """Base error for deterministic OCR operations."""


class OcrConfigurationError(OcrError, ValueError):
    """The OCR plan, local models, or PDF region is invalid."""


class OcrDependencyError(OcrError, ImportError):
    """The configured optional OCR engine is unavailable or incompatible."""


class OcrPolicyError(OcrError):
    """An OCR request would violate the figure/scheme no-OCR boundary."""


class OcrNetworkDisabledError(OcrPolicyError):
    """Third-party OCR code attempted network access at runtime."""


@dataclass(frozen=True)
class OcrModelFile:
    """One explicitly provisioned local model or language-data file."""

    path: Path
    version: str


@dataclass(frozen=True)
class RapidOcrModelSet:
    """Local ONNX files used by RapidOCR's standard pipeline.

    Current PP-OCRv6 ONNX recognizers embed their character list.  A separate
    dictionary is therefore optional, but remains supported for model variants
    that require one.
    """

    detector: OcrModelFile
    classifier: OcrModelFile
    recognizer: OcrModelFile
    dictionary: OcrModelFile | None = None

    def items(self) -> tuple[tuple[str, OcrModelFile], ...]:
        return tuple(
            (role, model)
            for role in _MODEL_ROLES
            if (model := getattr(self, role)) is not None
        )


@dataclass(frozen=True)
class OcrModelDiagnostic:
    role: str
    path: str
    version: str
    bytes: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.path,
            "version": self.version,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class OcrEngineDiagnostics:
    name: str
    version: str
    backend: str
    backend_version: str
    network_access: str
    execution_provider: str
    models: tuple[OcrModelDiagnostic, ...] = ()
    settings: tuple[tuple[str, bool | float | int | str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "backend": self.backend,
            "backend_version": self.backend_version,
            "network_access": self.network_access,
            "execution_provider": self.execution_provider,
            "models": [model.as_dict() for model in self.models],
            "settings": dict(self.settings),
        }


@dataclass(frozen=True)
class OcrDetection:
    """One engine-neutral OCR detection in rendered-image pixel coordinates."""

    text: str
    confidence: float
    polygon: tuple[Point, ...]


@runtime_checkable
class OcrEngine(Protocol):
    """Minimal interface implemented by local OCR adapters."""

    def recognize(self, image: Image.Image) -> Sequence[OcrDetection]:
        """Recognize text in one already-rendered and already-masked image."""

    def diagnostics(self) -> OcrEngineDiagnostics:
        """Describe the exact engine, backend, and model files in use."""


@dataclass(frozen=True)
class PdfOcrRegion:
    """One OCR target using PDF points with a top-left origin."""

    region_id: str
    page: int
    box: Box
    role: str = "document_text"
    asset_id: str | None = None


@dataclass(frozen=True)
class PdfVisualExclusion:
    """A figure or scheme region whose pixels must never reach OCR."""

    exclusion_id: str
    page: int
    box: Box
    role: str


@dataclass(frozen=True)
class PdfOcrPlan:
    requested_engine: str
    dpi: int
    use_cls: bool
    text_score: float
    box_thresh: float
    unclip_ratio: float
    regions: tuple[PdfOcrRegion, ...]
    visual_exclusions: tuple[PdfVisualExclusion, ...]


@dataclass(frozen=True)
class OcrObservation:
    text: str
    confidence: float
    polygon_pixels: tuple[Point, ...]
    polygon_pdf_points: tuple[Point, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "polygon_pixels": [list(point) for point in self.polygon_pixels],
            "polygon_pdf_points": [
                list(point) for point in self.polygon_pdf_points
            ],
        }


@dataclass(frozen=True)
class OcrRegionResult:
    region_id: str
    page: int
    role: str
    asset_id: str | None
    box: Box
    dpi: int
    dimensions_pixels: tuple[int, int]
    image_sha256: str
    masked_exclusions: tuple[dict[str, Any], ...]
    observations: tuple[OcrObservation, ...]

    @property
    def text(self) -> str:
        """Return engine lines in deterministic geometry order."""

        return "\n".join(item.text for item in self.observations)

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": "1.0",
            "kind": "ocr_region",
            "region_id": self.region_id,
            "page": self.page,
            "role": self.role,
            "box": list(self.box),
            "coordinate_system": "pdf-points-top-left",
            "dpi": self.dpi,
            "dimensions_pixels": {
                "width": self.dimensions_pixels[0],
                "height": self.dimensions_pixels[1],
            },
            "rendered_input_sha256": self.image_sha256,
            "masked_exclusions": list(self.masked_exclusions),
            "text": self.text,
            "observations": [item.as_dict() for item in self.observations],
            "ocr_performed": True,
        }
        if self.asset_id is not None:
            value["asset_id"] = self.asset_id
        return value


@dataclass(frozen=True)
class OcrRunResult:
    source_path: str
    source_sha256: str
    requested_engine: str
    dpi: int
    engine: OcrEngineDiagnostics
    regions: tuple[OcrRegionResult, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "method": "deterministic-local-region-ocr",
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "requested_engine": self.requested_engine,
            "dpi": self.dpi,
            "engine": self.engine.as_dict(),
            "regions": [region.as_dict() for region in self.regions],
            "ocr_performed": bool(self.regions),
        }


class RapidOcrEngine:
    """RapidOCR 3.9.2 adapter using explicit local ONNX files only.

    ``engine_factory`` and ``package_versions`` exist to support synthetic unit
    tests without importing the optional dependency.  Production callers
    should leave both unset.
    """

    def __init__(
        self,
        models: RapidOcrModelSet,
        *,
        use_cls: bool = True,
        text_score: float = 0.5,
        box_thresh: float = 0.5,
        unclip_ratio: float = 1.6,
        engine_factory: Callable[..., Any] | None = None,
        package_versions: Mapping[str, str] | None = None,
        strict_versions: bool = True,
    ) -> None:
        if not isinstance(use_cls, bool):
            raise OcrConfigurationError("RapidOCR use_cls must be a boolean")
        text_score = _unit_float(text_score, "RapidOCR text_score")
        box_thresh = _unit_float(box_thresh, "RapidOCR box_thresh")
        unclip_ratio = _positive_float(unclip_ratio, "RapidOCR unclip_ratio")
        self._model_paths, model_diagnostics, self._model_stats = (
            _validate_local_models(models)
        )
        versions = dict(package_versions or _installed_ocr_versions())
        rapidocr_version = versions.get("rapidocr", "")
        onnxruntime_version = versions.get("onnxruntime", "")
        if strict_versions and (
            rapidocr_version != RAPIDOCR_VERSION
            or onnxruntime_version != ONNXRUNTIME_VERSION
        ):
            raise OcrDependencyError(
                "OCR runtime versions do not match requirements.txt: "
                f"rapidocr={rapidocr_version or 'not-installed'} "
                f"(expected {RAPIDOCR_VERSION}), onnxruntime="
                f"{onnxruntime_version or 'not-installed'} "
                f"(expected {ONNXRUNTIME_VERSION})"
            )

        engine_type_value: Any = "onnxruntime"
        if engine_factory is None:
            try:
                with _network_disabled():
                    rapidocr_module = importlib.import_module("rapidocr")
                engine_factory = getattr(rapidocr_module, "RapidOCR")
                engine_type_value = getattr(rapidocr_module, "EngineType").ONNXRUNTIME
            except (ImportError, AttributeError) as exc:
                raise OcrDependencyError(
                    "RapidOCR is optional; install requirements.txt "
                    "and provision the local ONNX model files before OCR"
                ) from exc

        parameters = {
            "Global.use_det": True,
            "Global.use_cls": use_cls,
            "Global.use_rec": True,
            "Global.text_score": text_score,
            "Global.log_level": "error",
            "Global.model_root_dir": str(self._model_paths["detector"].parent),
            "Det.engine_type": engine_type_value,
            "Cls.engine_type": engine_type_value,
            "Rec.engine_type": engine_type_value,
            "Det.model_path": str(self._model_paths["detector"]),
            "Det.box_thresh": box_thresh,
            "Det.unclip_ratio": unclip_ratio,
            "Cls.model_path": str(self._model_paths["classifier"]),
            "Rec.model_path": str(self._model_paths["recognizer"]),
            "EngineConfig.onnxruntime.intra_op_num_threads": 1,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
            "EngineConfig.onnxruntime.use_cuda": False,
            "EngineConfig.onnxruntime.use_dml": False,
            "EngineConfig.onnxruntime.use_cann": False,
            "EngineConfig.onnxruntime.use_coreml": False,
        }
        if "dictionary" in self._model_paths:
            parameters["Rec.rec_keys_path"] = str(
                self._model_paths["dictionary"]
            )
        try:
            with _network_disabled():
                self._engine = engine_factory(params=parameters)
        except OcrError:
            raise
        except Exception as exc:
            raise OcrDependencyError(f"RapidOCR initialization failed: {exc}") from exc

        self._diagnostics = OcrEngineDiagnostics(
            name="rapidocr",
            version=rapidocr_version,
            backend="onnxruntime",
            backend_version=onnxruntime_version,
            network_access="disabled",
            execution_provider="CPUExecutionProvider",
            models=model_diagnostics,
            settings=(
                ("use_cls", use_cls),
                ("text_score", text_score),
                ("box_thresh", box_thresh),
                ("unclip_ratio", unclip_ratio),
            ),
        )

    def diagnostics(self) -> OcrEngineDiagnostics:
        return self._diagnostics

    def recognize(self, image: Image.Image) -> tuple[OcrDetection, ...]:
        self._verify_model_files_unchanged()
        png_bytes = _encode_png_rgb(image)
        try:
            with _network_disabled():
                raw_result = self._engine(png_bytes)
        except OcrError:
            raise
        except Exception as exc:
            raise OcrError(f"RapidOCR recognition failed: {exc}") from exc
        return _normalize_rapidocr_result(raw_result)

    def _verify_model_files_unchanged(self) -> None:
        for role, path in self._model_paths.items():
            try:
                stat = path.stat()
            except OSError as exc:
                raise OcrConfigurationError(
                    f"local OCR {role} file is no longer readable: {path}"
                ) from exc
            current = (stat.st_size, stat.st_mtime_ns)
            if current == self._model_stats[role]:
                continue
            expected = next(
                item.sha256 for item in self._diagnostics.models if item.role == role
            )
            if sha256_file(path) != expected:
                raise OcrConfigurationError(
                    f"local OCR {role} file changed after engine initialization"
                )
            self._model_stats[role] = current


def parse_pdf_ocr_config(
    config: Mapping[str, Any],
    *,
    crop_specs: Iterable[Mapping[str, Any]] = (),
) -> PdfOcrPlan:
    """Normalize the ``pdf_ocr`` override and mandatory crop exclusions.

    ``page_regions`` preserves the caller's page and ``reading_order`` list
    order.  Figure/scheme crop boxes are always added as visual exclusions.
    Their ``caption_page``/``caption_box`` fields become separate caption OCR
    regions, so a caption is recognized exactly once without exposing the
    scientific image to OCR.
    """

    if not isinstance(config, Mapping):
        raise OcrConfigurationError("pdf_ocr must be a mapping")
    requested_engine = _nonempty_string(config.get("engine", "rapidocr"), "engine")
    dpi = _validate_dpi(config.get("dpi", DEFAULT_OCR_DPI))
    use_cls = _boolean(config.get("use_cls", True), "pdf_ocr.use_cls")
    text_score = _unit_float(
        config.get("text_score", 0.5), "pdf_ocr.text_score"
    )
    box_thresh = _unit_float(
        config.get("box_thresh", 0.5), "pdf_ocr.box_thresh"
    )
    unclip_ratio = _positive_float(
        config.get("unclip_ratio", 1.6), "pdf_ocr.unclip_ratio"
    )
    raw_pages = config.get("page_regions", ())
    if not isinstance(raw_pages, (list, tuple)):
        raise OcrConfigurationError("pdf_ocr.page_regions must be a list")

    regions: list[PdfOcrRegion] = []
    exclusions: list[PdfVisualExclusion] = []
    region_ids: set[str] = set()
    exclusion_keys: set[tuple[int, Box, str]] = set()

    for page_index, raw_page in enumerate(raw_pages, start=1):
        if not isinstance(raw_page, Mapping):
            raise OcrConfigurationError(
                f"pdf_ocr.page_regions item {page_index} must be a mapping"
            )
        page = _positive_integer(raw_page.get("page"), "page")
        reading_order = raw_page.get("reading_order", ())
        if not isinstance(reading_order, (list, tuple)):
            raise OcrConfigurationError(
                f"page {page} reading_order must be a list"
            )
        for region_index, raw_region in enumerate(reading_order, start=1):
            if not isinstance(raw_region, Mapping):
                raise OcrConfigurationError(
                    f"page {page} reading_order item {region_index} must be a mapping"
                )
            region = _region_from_mapping(raw_region, page=page)
            _reserve_id(region.region_id, region_ids, "OCR region")
            regions.append(region)

        raw_exclusions = raw_page.get("exclusions", ())
        if not isinstance(raw_exclusions, (list, tuple)):
            raise OcrConfigurationError(f"page {page} exclusions must be a list")
        for exclusion_index, raw_exclusion in enumerate(raw_exclusions, start=1):
            if not isinstance(raw_exclusion, Mapping):
                raise OcrConfigurationError(
                    f"page {page} exclusion {exclusion_index} must be a mapping"
                )
            exclusion = _exclusion_from_mapping(raw_exclusion, page=page)
            key = (exclusion.page, exclusion.box, exclusion.role)
            if key not in exclusion_keys:
                exclusions.append(exclusion)
                exclusion_keys.add(key)

    for crop_index, raw_crop in enumerate(crop_specs, start=1):
        if not isinstance(raw_crop, Mapping):
            raise OcrConfigurationError(f"PDF crop {crop_index} must be a mapping")
        visual_role = _crop_visual_role(raw_crop)
        if visual_role is None:
            continue
        asset_id = _portable_id(raw_crop.get("asset_id"), "PDF crop asset_id")
        page = _positive_integer(raw_crop.get("page"), "PDF crop page")
        crop_box = _validate_box(raw_crop.get("box"), "PDF crop box")
        key = (page, crop_box, visual_role)
        if key not in exclusion_keys:
            exclusions.append(
                PdfVisualExclusion(
                    exclusion_id=f"crop-{asset_id}",
                    page=page,
                    box=crop_box,
                    role=visual_role,
                )
            )
            exclusion_keys.add(key)

        caption_box = raw_crop.get("caption_box")
        if caption_box is None:
            continue
        caption_page = _positive_integer(
            raw_crop.get("caption_page", page), "PDF crop caption_page"
        )
        caption_id = f"caption-{asset_id}"
        _reserve_id(caption_id, region_ids, "OCR region")
        regions.append(
            PdfOcrRegion(
                region_id=caption_id,
                page=caption_page,
                box=_validate_box(caption_box, "PDF crop caption_box"),
                role="caption",
                asset_id=asset_id,
            )
        )

    if not regions:
        raise OcrConfigurationError("pdf_ocr declares no OCR text regions")
    return PdfOcrPlan(
        requested_engine=requested_engine,
        dpi=dpi,
        use_cls=use_cls,
        text_score=text_score,
        box_thresh=box_thresh,
        unclip_ratio=unclip_ratio,
        regions=tuple(regions),
        visual_exclusions=tuple(exclusions),
    )


def ocr_pdf_from_config(
    source: Path,
    source_path: str,
    config: Mapping[str, Any],
    engine: OcrEngine,
    *,
    crop_specs: Iterable[Mapping[str, Any]] = (),
    max_output_pixels: int = MAX_OCR_PIXELS,
) -> OcrRunResult:
    """Parse a ``pdf_ocr`` plan and recognize its ordered safe regions."""

    plan = parse_pdf_ocr_config(config, crop_specs=crop_specs)
    _verify_plan_engine_settings(plan, engine.diagnostics())
    return ocr_pdf_regions(
        source,
        source_path,
        plan.regions,
        engine,
        requested_engine=plan.requested_engine,
        visual_exclusions=plan.visual_exclusions,
        dpi=plan.dpi,
        max_output_pixels=max_output_pixels,
    )


def _verify_plan_engine_settings(
    plan: PdfOcrPlan, diagnostics: OcrEngineDiagnostics
) -> None:
    """Reject a prebuilt engine whose extraction-affecting options disagree."""

    actual = dict(diagnostics.settings)
    if not actual:
        # Third-party/fake implementations can omit optional settings.  The
        # RapidOCR adapter always supplies them and is checked strictly.
        return
    expected: dict[str, bool | float] = {
        "use_cls": plan.use_cls,
        "text_score": plan.text_score,
        "box_thresh": plan.box_thresh,
        "unclip_ratio": plan.unclip_ratio,
    }
    mismatched = [
        key
        for key, value in expected.items()
        if key not in actual
        or (
            isinstance(value, float)
            and (
                isinstance(actual[key], bool)
                or not math.isclose(float(actual[key]), value, abs_tol=1e-12)
            )
        )
        or (not isinstance(value, float) and actual[key] != value)
    ]
    if mismatched:
        raise OcrConfigurationError(
            "prebuilt OCR engine settings do not match pdf_ocr: "
            + ", ".join(mismatched)
        )


def ocr_pdf_regions(
    source: Path,
    source_path: str,
    regions: Iterable[PdfOcrRegion],
    engine: OcrEngine,
    *,
    requested_engine: str | None = None,
    visual_exclusions: Iterable[PdfVisualExclusion] = (),
    dpi: int | float = DEFAULT_OCR_DPI,
    max_output_pixels: int = MAX_OCR_PIXELS,
) -> OcrRunResult:
    """Render and OCR ordered PDF regions without writing page intermediates."""

    selected_regions = tuple(regions)
    if not selected_regions:
        raise OcrConfigurationError("at least one PDF OCR region is required")
    selected_exclusions = tuple(visual_exclusions)
    render_dpi = _validate_dpi(dpi)
    _validate_pixel_limit(max_output_pixels)
    source = source.resolve(strict=True)
    if not source.is_file():
        raise OcrConfigurationError(f"OCR source is not a file: {source}")
    source_hash = sha256_file(source)
    engine_diagnostics = engine.diagnostics()

    try:
        document = pypdfium2.PdfDocument(str(source))
    except Exception as exc:
        raise OcrConfigurationError(f"could not open OCR PDF source: {exc}") from exc

    results: list[OcrRegionResult] = []
    try:
        page_count = len(document)
        for region in selected_regions:
            _validate_region(region)
            if region.page > page_count:
                raise OcrConfigurationError(
                    f"OCR region {region.region_id!r} page {region.page} exceeds "
                    f"the PDF page count ({page_count})"
                )
            page = document[region.page - 1]
            try:
                page_size = tuple(float(value) for value in page.get_size())
                image = _render_pdf_region(
                    page,
                    region,
                    page_size=page_size,
                    dpi=render_dpi,
                    max_output_pixels=max_output_pixels,
                )
            finally:
                page.close()
            try:
                results.append(
                    ocr_rendered_region(
                        image,
                        region,
                        engine,
                        visual_exclusions=selected_exclusions,
                        dpi=render_dpi,
                    )
                )
            finally:
                image.close()
    finally:
        document.close()

    if sha256_file(source) != source_hash:
        raise OcrConfigurationError("PDF source changed while OCR was running")
    return OcrRunResult(
        source_path=source_path.replace("\\", "/"),
        source_sha256=source_hash,
        requested_engine=requested_engine or engine_diagnostics.name,
        dpi=render_dpi,
        engine=engine_diagnostics,
        regions=tuple(results),
    )


def ocr_rendered_region(
    image: Image.Image,
    region: PdfOcrRegion,
    engine: OcrEngine,
    *,
    visual_exclusions: Iterable[PdfVisualExclusion] = (),
    dpi: int | float = DEFAULT_OCR_DPI,
) -> OcrRegionResult:
    """OCR one synthetic or PDF-rendered image after white-masking visuals."""

    _validate_region(region)
    render_dpi = _validate_dpi(dpi)
    if image.width < 1 or image.height < 1:
        raise OcrConfigurationError("OCR rendered image is empty")
    normalized = _to_rgb_on_white(image)
    try:
        masked_rows = _mask_visual_exclusions(
            normalized, region, tuple(visual_exclusions)
        )
        image_hash = hashlib.sha256(_encode_png_rgb(normalized)).hexdigest()
        detections = tuple(engine.recognize(normalized))
        observations = tuple(
            _observation_from_detection(detection, region, normalized.size)
            for detection in detections
        )
        observations = tuple(sorted(observations, key=_observation_sort_key))
        return OcrRegionResult(
            region_id=region.region_id,
            page=region.page,
            role=region.role,
            asset_id=region.asset_id,
            box=region.box,
            dpi=render_dpi,
            dimensions_pixels=normalized.size,
            image_sha256=image_hash,
            masked_exclusions=masked_rows,
            observations=observations,
        )
    finally:
        if normalized is not image:
            normalized.close()


def _validate_local_models(
    models: RapidOcrModelSet,
) -> tuple[
    dict[str, Path], tuple[OcrModelDiagnostic, ...], dict[str, tuple[int, int]]
]:
    paths: dict[str, Path] = {}
    diagnostics: list[OcrModelDiagnostic] = []
    stats: dict[str, tuple[int, int]] = {}
    for role, model in models.items():
        if not isinstance(model, OcrModelFile):
            raise OcrConfigurationError(f"RapidOCR {role} must be an OcrModelFile")
        version = _nonempty_string(model.version, f"RapidOCR {role} version")
        try:
            path = model.path.resolve(strict=True)
        except (AttributeError, OSError) as exc:
            raise OcrConfigurationError(
                f"RapidOCR {role} path is not a readable local file"
            ) from exc
        if not path.is_file():
            raise OcrConfigurationError(f"RapidOCR {role} is not a file: {path}")
        stat = path.stat()
        paths[role] = path
        stats[role] = (stat.st_size, stat.st_mtime_ns)
        diagnostics.append(
            OcrModelDiagnostic(
                role=role,
                path=path.as_posix(),
                version=version,
                bytes=stat.st_size,
                sha256=sha256_file(path),
            )
        )
    return paths, tuple(diagnostics), stats


def _installed_ocr_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in ("rapidocr", "onnxruntime"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


@contextmanager
def _network_disabled() -> Iterable[None]:
    """Temporarily reject socket connections made by third-party OCR code."""

    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise OcrNetworkDisabledError(
            "network access is disabled during local OCR runtime"
        )

    with _NETWORK_LOCK:
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        original_create_connection = socket.create_connection
        original_sendto = socket.socket.sendto
        socket.socket.connect = blocked  # type: ignore[method-assign]
        socket.socket.connect_ex = blocked  # type: ignore[method-assign]
        socket.create_connection = blocked  # type: ignore[assignment]
        socket.socket.sendto = blocked  # type: ignore[method-assign]
        try:
            yield
        finally:
            socket.socket.connect = original_connect  # type: ignore[method-assign]
            socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
            socket.create_connection = original_create_connection
            socket.socket.sendto = original_sendto  # type: ignore[method-assign]


def _normalize_rapidocr_result(raw_result: Any) -> tuple[OcrDetection, ...]:
    boxes = getattr(raw_result, "boxes", None)
    texts = getattr(raw_result, "txts", None)
    scores = getattr(raw_result, "scores", None)
    if boxes is None and texts is None and scores is None:
        return ()
    if boxes is None or texts is None or scores is None:
        raise OcrError("RapidOCR returned an incomplete boxes/txts/scores result")
    try:
        lengths = (len(boxes), len(texts), len(scores))
    except TypeError as exc:
        raise OcrError("RapidOCR returned non-sequence result fields") from exc
    if len(set(lengths)) != 1:
        raise OcrError(
            "RapidOCR returned different numbers of boxes, texts, and scores"
        )

    detections: list[OcrDetection] = []
    for box, raw_text, raw_score in zip(boxes, texts, scores):
        text = str(raw_text).strip()
        if not text:
            continue
        try:
            confidence = float(raw_score)
        except (TypeError, ValueError) as exc:
            raise OcrError("RapidOCR returned a non-numeric confidence") from exc
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise OcrError("RapidOCR confidence must be finite and between 0 and 1")
        polygon = _validate_polygon(box)
        detections.append(
            OcrDetection(text=text, confidence=confidence, polygon=polygon)
        )
    return tuple(detections)


def _render_pdf_region(
    page: Any,
    region: PdfOcrRegion,
    *,
    page_size: tuple[float, float],
    dpi: int,
    max_output_pixels: int,
) -> Image.Image:
    page_width, page_height = page_size
    left, top, right, bottom = region.box
    if left < 0 or top < 0 or right > page_width or bottom > page_height:
        raise OcrConfigurationError(
            f"OCR region {region.region_id!r} extends outside PDF page {region.page}"
        )
    scale = dpi / 72.0
    expected_width = max(1, math.ceil((right - left) * scale))
    expected_height = max(1, math.ceil((bottom - top) * scale))
    if expected_width * expected_height > max_output_pixels:
        raise OcrConfigurationError(
            f"OCR region {region.region_id!r} exceeds {max_output_pixels:,} pixels"
        )
    crop = (left, page_height - bottom, page_width - right, top)
    try:
        bitmap = page.render(
            scale=scale,
            crop=crop,
            may_draw_forms=False,
            fill_color=(255, 255, 255, 255),
            draw_annots=True,
            optimize_mode="print",
            rev_byteorder=True,
        )
        try:
            shared = bitmap.to_pil()
            try:
                image = shared.copy()
            finally:
                shared.close()
        finally:
            bitmap.close()
    except Exception as exc:
        raise OcrError(
            f"could not render OCR region {region.region_id!r}: {exc}"
        ) from exc
    if image.width * image.height > max_output_pixels:
        image.close()
        raise OcrConfigurationError(
            f"rendered OCR region {region.region_id!r} exceeds "
            f"{max_output_pixels:,} pixels"
        )
    return image


def _mask_visual_exclusions(
    image: Image.Image,
    region: PdfOcrRegion,
    exclusions: Sequence[PdfVisualExclusion],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    region_area = _box_area(region.box)
    for exclusion in exclusions:
        _validate_exclusion(exclusion)
        if exclusion.page != region.page:
            continue
        intersection = _box_intersection(region.box, exclusion.box)
        if intersection is None:
            continue
        if math.isclose(_box_area(intersection), region_area, rel_tol=0, abs_tol=1e-9):
            raise OcrPolicyError(
                f"OCR region {region.region_id!r} is wholly inside declared "
                f"{exclusion.role} {exclusion.exclusion_id!r}"
            )
        pixel_box = _pdf_intersection_to_pixels(intersection, region.box, image.size)
        image.paste((255, 255, 255), pixel_box)
        rows.append(
            {
                "exclusion_id": exclusion.exclusion_id,
                "role": exclusion.role,
                "box": list(exclusion.box),
                "intersection_box": list(intersection),
                "masked_pixel_box": list(pixel_box),
            }
        )
    return tuple(rows)


def _observation_from_detection(
    detection: OcrDetection,
    region: PdfOcrRegion,
    dimensions: tuple[int, int],
) -> OcrObservation:
    if not isinstance(detection, OcrDetection):
        raise OcrError("OCR engine returned a value other than OcrDetection")
    pixel_polygon = _validate_polygon(detection.polygon)
    width, height = dimensions
    left, top, right, bottom = region.box
    x_scale = (right - left) / width
    y_scale = (bottom - top) / height
    pdf_polygon: list[Point] = []
    for x, y in pixel_polygon:
        if x < 0 or y < 0 or x > width or y > height:
            raise OcrError("OCR detection polygon extends outside rendered region")
        pdf_polygon.append((left + x * x_scale, top + y * y_scale))
    confidence = float(detection.confidence)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise OcrError("OCR confidence must be finite and between 0 and 1")
    if not detection.text.strip():
        raise OcrError("OCR detection text must be non-empty")
    return OcrObservation(
        text=detection.text.strip(),
        confidence=confidence,
        polygon_pixels=pixel_polygon,
        polygon_pdf_points=tuple(pdf_polygon),
    )


def _observation_sort_key(observation: OcrObservation) -> tuple[Any, ...]:
    return (
        min(point[1] for point in observation.polygon_pixels),
        min(point[0] for point in observation.polygon_pixels),
        observation.text,
        observation.confidence,
    )


def _region_from_mapping(raw: Mapping[str, Any], *, page: int) -> PdfOcrRegion:
    region_id = _portable_id(raw.get("region_id"), "OCR region_id")
    role = _nonempty_string(raw.get("role", "document_text"), "OCR region role")
    if role not in _OCR_ROLES:
        if role in _VISUAL_ROLES:
            raise OcrPolicyError(f"OCR is forbidden for role {role!r}")
        raise OcrConfigurationError(
            f"unsupported OCR region role {role!r}; expected one of "
            f"{', '.join(sorted(_OCR_ROLES - {'caption'}))}"
        )
    asset_id_raw = raw.get("asset_id")
    asset_id = (
        _portable_id(asset_id_raw, "OCR asset_id") if asset_id_raw is not None else None
    )
    return PdfOcrRegion(
        region_id=region_id,
        page=page,
        box=_validate_box(raw.get("box"), "OCR region box"),
        role=role,
        asset_id=asset_id,
    )


def _exclusion_from_mapping(
    raw: Mapping[str, Any], *, page: int
) -> PdfVisualExclusion:
    role = _nonempty_string(raw.get("role"), "OCR exclusion role").casefold()
    if role not in _VISUAL_ROLES:
        raise OcrConfigurationError("OCR exclusions must have role figure or scheme")
    exclusion_id = _portable_id(
        raw.get("exclusion_id"), "OCR exclusion_id"
    )
    return PdfVisualExclusion(
        exclusion_id=exclusion_id,
        page=page,
        box=_validate_box(raw.get("box"), "OCR exclusion box"),
        role=role,
    )


def _crop_visual_role(raw: Mapping[str, Any]) -> str | None:
    values = (
        raw.get("kind"),
        raw.get("category"),
        raw.get("asset_type"),
    )
    for value in values:
        folded = str(value or "").strip().casefold()
        if folded in _VISUAL_ROLES:
            return folded
    asset_id = str(raw.get("asset_id", "")).strip().casefold()
    label = str(raw.get("label", "")).strip().casefold()
    if asset_id.startswith("figure") or label.startswith("figure"):
        return "figure"
    if asset_id.startswith("scheme") or label.startswith("scheme"):
        return "scheme"
    return None


def _validate_region(region: PdfOcrRegion) -> None:
    if not isinstance(region, PdfOcrRegion):
        raise OcrConfigurationError("OCR regions must be PdfOcrRegion values")
    _portable_id(region.region_id, "OCR region_id")
    _positive_integer(region.page, "OCR page")
    _validate_box(region.box, "OCR region box")
    if region.role in _VISUAL_ROLES:
        raise OcrPolicyError(f"OCR is forbidden for role {region.role!r}")
    if region.role not in _OCR_ROLES:
        raise OcrConfigurationError(f"unsupported OCR role {region.role!r}")


def _validate_exclusion(exclusion: PdfVisualExclusion) -> None:
    if not isinstance(exclusion, PdfVisualExclusion):
        raise OcrConfigurationError(
            "visual exclusions must be PdfVisualExclusion values"
        )
    _portable_id(exclusion.exclusion_id, "OCR exclusion_id")
    _positive_integer(exclusion.page, "OCR exclusion page")
    _validate_box(exclusion.box, "OCR exclusion box")
    if exclusion.role not in _VISUAL_ROLES:
        raise OcrConfigurationError("visual exclusion role must be figure or scheme")


def _reserve_id(value: str, used: set[str], label: str) -> None:
    if value in used:
        raise OcrConfigurationError(f"duplicate {label} {value!r}")
    used.add(value)


def _portable_id(value: Any, field: str) -> str:
    result = _nonempty_string(value, field)
    if not _ID_PATTERN.fullmatch(result):
        raise OcrConfigurationError(
            f"{field} must be 1-128 portable filename characters"
        )
    return result


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OcrConfigurationError(f"{field} must be a non-empty string")
    return value.strip()


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OcrConfigurationError(f"{field} must be a positive integer")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise OcrConfigurationError(f"{field} must be a boolean")
    return value


def _unit_float(value: Any, field: str) -> float:
    number = _finite_float(value, field)
    if not 0 <= number <= 1:
        raise OcrConfigurationError(f"{field} must be between 0 and 1")
    return number


def _positive_float(value: Any, field: str) -> float:
    number = _finite_float(value, field)
    if number <= 0:
        raise OcrConfigurationError(f"{field} must be positive")
    return number


def _finite_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OcrConfigurationError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise OcrConfigurationError(f"{field} must be finite")
    return number


def _validate_dpi(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OcrConfigurationError("OCR dpi must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise OcrConfigurationError("OCR dpi must be a finite integer")
    result = int(numeric)
    if not 72 <= result <= MAX_OCR_DPI:
        raise OcrConfigurationError(
            f"OCR dpi must be between 72 and {MAX_OCR_DPI}"
        )
    return result


def _validate_pixel_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OcrConfigurationError("max_output_pixels must be a positive integer")
    return value


def _validate_box(value: Any, field: str) -> Box:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise OcrConfigurationError(f"{field} must contain four numbers")
    numbers: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise OcrConfigurationError(f"{field} must contain only numbers")
        number = float(item)
        if not math.isfinite(number):
            raise OcrConfigurationError(f"{field} must contain finite numbers")
        numbers.append(number)
    left, top, right, bottom = numbers
    if left < 0 or top < 0 or left >= right or top >= bottom:
        raise OcrConfigurationError(f"{field} is empty, inverted, or negative")
    return left, top, right, bottom


def _validate_polygon(value: Any) -> tuple[Point, ...]:
    if not isinstance(value, (list, tuple)):
        try:
            value = value.tolist()
        except AttributeError as exc:
            raise OcrError("OCR polygon must be a sequence") from exc
    if len(value) < 3:
        raise OcrError("OCR polygon must contain at least three points")
    points: list[Point] = []
    for point in value:
        if not isinstance(point, (list, tuple)):
            try:
                point = point.tolist()
            except AttributeError as exc:
                raise OcrError("OCR polygon point must be a pair") from exc
        if len(point) != 2:
            raise OcrError("OCR polygon point must contain two coordinates")
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError) as exc:
            raise OcrError("OCR polygon coordinates must be numeric") from exc
        if not math.isfinite(x) or not math.isfinite(y):
            raise OcrError("OCR polygon coordinates must be finite")
        points.append((x, y))
    return tuple(points)


def _box_intersection(first: Box, second: Box) -> Box | None:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    if left >= right or top >= bottom:
        return None
    return left, top, right, bottom


def _box_area(box: Box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def _pdf_intersection_to_pixels(
    intersection: Box, region: Box, dimensions: tuple[int, int]
) -> tuple[int, int, int, int]:
    width, height = dimensions
    x_scale = width / (region[2] - region[0])
    y_scale = height / (region[3] - region[1])
    left = max(0, math.floor((intersection[0] - region[0]) * x_scale))
    top = max(0, math.floor((intersection[1] - region[1]) * y_scale))
    right = min(width, math.ceil((intersection[2] - region[0]) * x_scale))
    bottom = min(height, math.ceil((intersection[3] - region[1]) * y_scale))
    return left, top, right, bottom


def _to_rgb_on_white(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image.copy()
    if "A" in image.getbands():
        rgba = image.convert("RGBA")
        try:
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            try:
                composed = Image.alpha_composite(background, rgba)
                try:
                    return composed.convert("RGB")
                finally:
                    composed.close()
            finally:
                background.close()
        finally:
            rgba.close()
    return image.convert("RGB")


def _encode_png_rgb(image: Image.Image) -> bytes:
    normalized = _to_rgb_on_white(image)
    try:
        output = io.BytesIO()
        normalized.save(output, format="PNG", optimize=False, compress_level=9)
        return output.getvalue()
    finally:
        normalized.close()


__all__ = [
    "DEFAULT_OCR_DPI",
    "MAX_OCR_DPI",
    "MAX_OCR_PIXELS",
    "ONNXRUNTIME_VERSION",
    "RAPIDOCR_VERSION",
    "OcrConfigurationError",
    "OcrDependencyError",
    "OcrDetection",
    "OcrEngine",
    "OcrEngineDiagnostics",
    "OcrError",
    "OcrModelDiagnostic",
    "OcrModelFile",
    "OcrNetworkDisabledError",
    "OcrObservation",
    "OcrPolicyError",
    "OcrRegionResult",
    "OcrRunResult",
    "PdfOcrPlan",
    "PdfOcrRegion",
    "PdfVisualExclusion",
    "RapidOcrEngine",
    "RapidOcrModelSet",
    "ocr_pdf_from_config",
    "ocr_pdf_regions",
    "ocr_rendered_region",
    "parse_pdf_ocr_config",
]
