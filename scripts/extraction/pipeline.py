"""Orchestrate one offline, staged record extraction."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import math
import os
import re
import secrets
import shutil
import unicodedata
from dataclasses import dataclass, replace
from importlib.util import find_spec
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - CLI dependency check
    yaml = None

from .discovery import discover_sources, source_fingerprint
from .html_extractor import extract_html
from .metadata import RecordMetadata, load_record_metadata
from .models import (
    ContentBlock,
    EmbeddedAsset,
    FigureItem,
    Repair,
    SourceFile,
    TableCell,
    TableItem,
    TablePart,
)
from .ocr import (
    OcrDependencyError,
    OcrModelFile,
    RapidOcrEngine,
    RapidOcrModelSet,
    ocr_pdf_from_config,
    parse_pdf_ocr_config,
)
from .pdf_article_extractor import extract_pdf_article
from .pdf_ocr_text import (
    ocr_run_to_pdf_text_document,
    ocr_text_extraction_details,
)
from .paths import (
    WINDOWS_DEVICE_NAMES,
    UnsafePathError,
    atomic_write_bytes,
    ensure_within,
    is_reparse_point,
    reject_reparse_chain,
    sha256_file,
    validate_record_id,
    validate_run_id,
)
from .pdf_extractor import inspect_pdf_native_text, render_pdf_crops
from .record_json import write_record_json, _canonical_plain_for_rich
from .reporting import write_diagnostics, write_validation_result
from .rich_text import (
    UnsafeRichTextError,
    block_markup_to_safe_html,
    rich_text_matches_plain,
)
from .supplements import extract_supplements
from .reviewed_html_assets import select_reviewed_html_assets
from .validation import validate_candidate


class ExtractionError(RuntimeError):
    """A controlled extraction failure."""


@dataclass(frozen=True)
class ExtractionResult:
    record_id: str
    run_id: str
    run_root: Path
    extraction_root: Path
    diagnostic_root: Path
    source_fingerprint: str
    status: str
    finding_count: int


_TITLE_PRESENTATION_EQUIVALENTS = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2032": "'",
        "\u2033": '"',
    }
)
_TITLE_SCRIPT_MARKER = re.compile(r"(?:_|\^)\{([^{}]+)\}")


def _title_identity_text(value: str) -> str:
    """Return the public-metadata form of a source title for identity checks.

    Rich publisher titles retain subscripts and superscripts as ``_{...}``
    and ``^{...}``, while the public YAML title is deliberately plain text.
    Collapsing only those extractor-generated semantic markers lets ``pK_{a}``
    identify the curated plain-text ``pKa`` without tolerating different
    characters, punctuation, or wording.
    """

    normalized = unicodedata.normalize("NFC", value).translate(
        _TITLE_PRESENTATION_EQUIVALENTS
    )
    return _TITLE_SCRIPT_MARKER.sub(r"\1", normalized)


def _reconcile_article_title(
    article: Any, metadata: RecordMetadata, *, source_html: Path | None = None
) -> None:
    """Require source-title identity across safe presentation differences.

    Publisher HTML commonly substitutes ASCII punctuation for typographic
    hyphens and quotation marks preserved in curated metadata, or vice versa.
    Treat only those presentation code points as identity-equivalent. The
    rich-text extractor also writes source subscripts/superscripts as explicit
    script markers, whereas public metadata is plain text; compare their
    marker-free visible text. Retain the exact metadata title in the canonical
    record. Other punctuation and wording differences remain blocking identity
    mismatches. A single terminal full stop or a dash/minus used in place of an
    ASCII hyphen may differ only when archived HTML explicitly identifies the
    same DOI. The DOI-gated comparison also accepts the DOI in a legacy OUP or
    ACS Cite card.
    """

    source_title = _title_identity_text(article.title)
    metadata_title = _title_identity_text(metadata.title)
    terminal_stop_only = (
        source_title + "." == metadata_title
        or metadata_title + "." == source_title
    ) and not source_title.endswith("..") and not metadata_title.endswith("..")
    dash_only = (
        source_title.replace("\u2013", "-").replace("\u2212", "-")
        == metadata_title.replace("\u2013", "-").replace("\u2212", "-")
    )
    if source_title != metadata_title and (terminal_stop_only or dash_only) and source_html is not None:
        from lxml import html as lxml_html

        document = lxml_html.parse(str(source_html))
        doi_values = document.xpath(
            '//meta[@name="citation_doi"]/@content | //a[@property="sameAs"]/@href'
        )
        # Wiley article headers expose the canonical DOI as an ordinary link
        # followed by this explicit label rather than as citation metadata.
        # Restrict recognition to that label so an arbitrary DOI link in the
        # article body or reference list cannot establish identity.
        doi_values += document.xpath(
            '//a[.//span[normalize-space()="Digital Object Identifier (DOI)"]]/@href'
        )
        if terminal_stop_only or dash_only:
            # Legacy OUP and ACS archives expose article identity in the Cite
            # card. Do not accept arbitrary DOI links from the reference list.
            doi_values += document.xpath('//*[@id="getCitation"]//a/@href')
        normalized_dois = {
            re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value.strip(), flags=re.I).lower()
            for value in doi_values
            if re.match(r"^(?:10\.|https?://(?:dx\.)?doi\.org/)", value.strip(), re.I)
        }
        if metadata.doi and normalized_dois == {metadata.doi.lower()}:
            article.title = metadata.title
            return
    if source_title != metadata_title:
        raise ExtractionError(
            "article title does not match public metadata: "
            f"{article.title!r} != {metadata.title!r}"
        )
    article.title = metadata.title


def _create_build_root(record_staging: Path, run_id: str) -> Path:
    """Create a unique build directory with the private root's permissions.

    ``tempfile.mkdtemp`` deliberately uses mode ``0o700``.  On Windows that
    suppresses ACL inheritance, which made runs created by the Codex sandbox
    unreadable to the human user's browser.  A normal Windows directory keeps
    the parent private ACL; POSIX builds retain the original owner-only mode.
    """

    mode = 0o777 if os.name == "nt" else 0o700
    for _ in range(128):
        candidate = record_staging / f".{run_id}.building-{secrets.token_hex(8)}"
        try:
            candidate.mkdir(mode=mode)
        except FileExistsError:
            continue
        return candidate
    raise ExtractionError("could not allocate a unique staged build directory")


def _recover_missing_html_body(article, html_source, pdf_source, metadata, config, crops, ocr_config=None):
    """Recover a reviewed PDF body for an authenticated abstract-only HTML archive.

    Retain the available HTML abstract, references and bibliographic fields.
    Require exact source hashes and the precise non-article preview placeholder;
    a populated or changed HTML body must never be silently replaced.
    """
    if config.get("recover_truncated_html_tail") is not None:
        return _recover_truncated_html_tail(article, html_source, pdf_source, metadata, config)
    recovery = config.get("recover_missing_html_body")
    if recovery is None:
        return article
    headings = [section.heading for section in article.sections]
    preview = (
        headings == ["Abstract", "Article PDF"]
        and [block.plain_text for block in article.sections[1].blocks]
        == ["Download to read the full article text"]
    )
    empty_nature_preview = (
        isinstance(recovery, dict)
        and recovery.get("html_layout") == "nature_empty_pdf_preview"
        and headings == ["Abstract", "Article PDF"]
        and not article.sections[1].blocks
    )
    if empty_nature_preview:
        from lxml import html as lxml_html
        document = lxml_html.parse(str(html_source.path))
        preview_nodes = document.xpath('//section[@aria-labelledby="preview"]//*[@id="preview-content"]')
        empty_nature_preview = (
            len(preview_nodes) == 1
            and not ''.join(preview_nodes[0].itertext()).strip()
            and not preview_nodes[0].xpath('.//img | .//table | .//figure')
            and len(document.xpath('//section[@aria-labelledby="Abs1"]')) == 1
            and not document.xpath('//*[@id="body"] | //article//figure | //article//table | //article//img')
        )
    abstract_keywords_only = (
        isinstance(recovery, dict)
        and recovery.get("html_layout") == "sciencedirect_abstract_keywords_only"
        and headings == ["Abstract", "Keywords"]
    )
    if abstract_keywords_only:
        from lxml import html as lxml_html
        document = lxml_html.parse(str(html_source.path))
        abstract_keywords_only = (
            len(document.xpath('//article//h1[@id="screen-reader-main-title"]')) == 1
            and len(document.xpath('//article//div[@id="abstracts"]')) == 1
            and len(document.xpath('//article//div[starts-with(@id,"aep-keywords-id")]')) == 1
            and len(document.xpath('//article//section[starts-with(@id,"aep-bibliography-id")]')) == 1
            and not document.xpath('//*[@id="body"] | //article//figure | //article//table | //article//img')
        )
    abstract_abbreviations_only = (
        isinstance(recovery, dict)
        and recovery.get("html_layout")
        == "sciencedirect_abstract_abbreviations_only"
        and headings == ["Abstract", "Abbreviations"]
    )
    if abstract_abbreviations_only:
        from lxml import html as lxml_html
        document = lxml_html.parse(str(html_source.path))
        abbreviation_groups = document.xpath(
            '//article//div[starts-with(@id,"aep-keywords-id")]'
            '[h2[normalize-space()="Abbreviations"]]'
        )
        abstract_abbreviations_only = (
            len(document.xpath('//article//h1[@id="screen-reader-main-title"]')) == 1
            and len(document.xpath('//article//div[@id="abstracts"]')) == 1
            and len(abbreviation_groups) == 1
            and len(
                document.xpath(
                    '//article//section[starts-with(@id,"aep-bibliography-id")]'
                )
            ) == 1
            and not document.xpath(
                '//*[@id="body"] | //article//figure | //article//table | '
                '//article//img'
            )
        )
    abstract_graphical_only = (
        isinstance(recovery, dict)
        and recovery.get("html_layout") == "sciencedirect_abstract_graphical_only"
        and headings == ["Abstract"]
    )
    if abstract_graphical_only:
        from lxml import html as lxml_html
        document = lxml_html.parse(str(html_source.path))
        abstract_graphical_only = (
            len(document.xpath('//h1[@id="screen-reader-main-title"]')) == 1
            and len(document.xpath('//div[@id="abstracts"]')) == 1
            and len(document.xpath('//*[starts-with(@id,"aep-bibliography-id")]')) == 1
            and not document.xpath('//*[@id="body"] | //table | //figure[not(ancestor::div[@id="abstracts"])] | //img[not(ancestor::div[@id="abstracts"])]')
            and all(item.kind == "graphical_abstract" for item in article.figures)
        )
    publisher_summary_only = (
        isinstance(recovery, dict)
        and recovery.get("html_layout")
        == "sciencedirect_publisher_summary_only"
        and headings == ["Publisher Summary"]
    )
    if publisher_summary_only:
        from lxml import html as lxml_html
        document = lxml_html.parse(str(html_source.path))
        groups = document.xpath(
            '//article/div[@id="abstracts"]/div['
            'starts-with(@id,"aep-abstract-id")]'
        )
        publisher_summary_only = (
            len(document.xpath('//article/h1[@id="screen-reader-main-title"]')) == 1
            and len(groups) == 1
            and len(
                groups[0].xpath(
                    './h2[normalize-space()="Publisher Summary"]'
                )
            ) == 1
            and len(
                groups[0].xpath(
                    './div[starts-with(@id,"aep-abstract-sec-id")]/'
                    'div[starts-with(@id,"fsabs")]'
                )
            ) == 1
            and len(
                document.xpath(
                    '//article//section[starts-with(@id,"bibliography.")]'
                    '[h2[normalize-space()="References"]]'
                )
            ) == 1
            and not document.xpath(
                '//*[@id="body"] | //article//figure | //article//table | '
                '//article//img'
            )
        )
    if (
        not isinstance(recovery, dict)
        or recovery.get("html_sha256") != html_source.sha256
        or config.get("source_sha256") != pdf_source.sha256
        or not recovery.get("reason")
        or not recovery.get("evidence")
        or not (
            preview
            or abstract_keywords_only
            or abstract_abbreviations_only
            or empty_nature_preview
            or abstract_graphical_only
            or publisher_summary_only
        )
        or (article.figures and not abstract_graphical_only) or article.tables or article.supporting_information
    ):
        raise ExtractionError("PDF body recovery requires a pinned abstract-only HTML preview")
    _reconcile_article_title(article, metadata, source_html=html_source.path)
    pdf_repairs = list(config.get("text_repairs", []))
    _validate_text_repair_sources(pdf_repairs, [pdf_source], pdf_source)
    text_document = text_extraction = None
    if ocr_config is not None:
        text_document, text_extraction = _run_pdf_ocr(
            pdf_source.path, pdf_source.relative_path, ocr_config, crops
        )
    recovered = extract_pdf_article(
        pdf_source.path, pdf_source.relative_path, metadata, config,
        crop_specs=crops, text_repairs=pdf_repairs,
        text_document=text_document, text_extraction=text_extraction,
    )
    retained_sections = (
        article.sections
        if abstract_keywords_only or abstract_abbreviations_only
        else article.sections[:1]
    )
    for section in retained_sections:
        for block in section.blocks:
            block.block_id = "html-" + block.block_id
    recovered.sections = retained_sections + [
        section for section in recovered.sections if section.heading.casefold() != "abstract"
    ]
    if len(recovered.sections) < 2:
        raise ExtractionError("PDF recovery did not produce an article body")
    reference_recovery = recovery.get("use_reviewed_pdf_references")
    if reference_recovery is not None and (
        not isinstance(reference_recovery, dict)
        or not reference_recovery.get("reason")
        or not reference_recovery.get("evidence")
        or not recovered.references
    ):
        raise ExtractionError("PDF bibliography recovery requires reviewed evidence and nonempty references")
    recovered.references = recovered.references if reference_recovery else (article.references or recovered.references)
    if abstract_graphical_only:
        recovered.figures = article.figures + recovered.figures
        recovered.embedded_assets = article.embedded_assets + recovered.embedded_assets
    recovered.bibliographic.update(article.bibliographic)
    recovered.repairs = article.repairs + recovered.repairs
    recovered.warnings = article.warnings + recovered.warnings
    recovered.text_extraction["supporting_source"] = html_source.relative_path
    recovered.page_diagnostics.append({
        "kind": "reviewed_html_body_omission", "status": "reviewed",
        "source_path": html_source.relative_path,
        "source_sha256": html_source.sha256,
        "reason": recovery["reason"], "evidence": recovery["evidence"],
        "retained_html_components": ["abstract", "bibliographic"] + ([] if reference_recovery else ["references"]) + (["graphical_abstract"] if abstract_graphical_only else []),
        "pdf_reference_recovery": reference_recovery,
    })
    return recovered


def _recover_truncated_html_tail(article, html_source, pdf_source, metadata, config):
    """Replace one source-pinned truncated final paragraph and append PDF tail.

    The existing reviewed PDF region configuration supplies only the missing
    tail. Both the exact HTML boundary and the recovered PDF opening are pinned;
    figures, tables, preceding prose and HTML front matter remain authoritative.
    """
    spec = config.get('recover_truncated_html_tail')
    if (not isinstance(spec, dict) or spec.get('html_sha256') != html_source.sha256
            or config.get('source_sha256') != pdf_source.sha256
            or not spec.get('reason') or not spec.get('evidence')
            or not spec.get('last_html_block_id') or not spec.get('last_html_text')
            or not spec.get('pdf_opening_text') or article.references):
        raise ExtractionError('truncated HTML tail recovery requires pinned sources and exact boundaries')
    sections = [section for section in article.sections if section.blocks]
    if not sections:
        raise ExtractionError('truncated HTML recovery needs existing body text')
    last = sections[-1].blocks[-1]
    if last.block_id != spec['last_html_block_id'] or last.plain_text != spec['last_html_text']:
        raise ExtractionError('truncated HTML final block differs from reviewed boundary')
    _reconcile_article_title(article, metadata, source_html=html_source.path)
    repairs = list(config.get('text_repairs', []))
    _validate_text_repair_sources(repairs, [pdf_source], pdf_source)
    recovered = extract_pdf_article(pdf_source.path, pdf_source.relative_path, metadata, config,
                                    crop_specs=[], text_repairs=repairs)
    if (not recovered.sections or recovered.sections[0].heading != 'Article Text'
            or not recovered.sections[0].blocks
            or not recovered.sections[0].blocks[0].plain_text.startswith(spec['pdf_opening_text'])
            or recovered.figures or recovered.tables or recovered.front_matter):
        raise ExtractionError('recovered PDF tail differs from reviewed scope')
    for section in recovered.sections:
        section.section_id = 'pdf-tail-' + section.section_id
        for block in section.blocks:
            block.block_id = 'pdf-tail-' + block.block_id
    sections[-1].blocks.pop()
    sections[-1].blocks.extend(recovered.sections[0].blocks)
    article.sections.extend(recovered.sections[1:])
    article.references = recovered.references
    article.repairs.extend(recovered.repairs)
    article.warnings.extend(recovered.warnings)
    article.page_diagnostics.extend(recovered.page_diagnostics)
    article.page_diagnostics.append({'kind':'reviewed_html_tail_omission','status':'reviewed',
        'source_path':html_source.relative_path,'source_sha256':html_source.sha256,
        'reason':spec['reason'],'evidence':spec['evidence'],
        'replaced_html_block':spec['last_html_block_id']})
    return article


def _bundled_rapidocr_models() -> RapidOcrModelSet:
    """Locate the pinned wheel's three local PP-OCRv6 model files.

    Runtime model URLs are never followed.  OCR refuses to start unless every
    expected wheel-bundled file is already present and readable; the adapter
    records their hashes in the private diagnostics.
    """

    spec = find_spec("rapidocr")
    locations = tuple(spec.submodule_search_locations or ()) if spec else ()
    if len(locations) != 1:
        raise OcrDependencyError(
            "RapidOCR 3.9.2 is not installed as one local package; install "
            "requirements.txt"
        )
    model_root = Path(locations[0]).resolve() / "models"
    return RapidOcrModelSet(
        detector=OcrModelFile(
            model_root / "PP-OCRv6_det_small.onnx",
            "PP-OCRv6-det-small",
        ),
        classifier=OcrModelFile(
            model_root / "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
            "ch-ppocr-mobile-v2.0-cls-mobile",
        ),
        recognizer=OcrModelFile(
            model_root / "PP-OCRv6_rec_small.onnx",
            "PP-OCRv6-rec-small",
        ),
    )


def _run_pdf_ocr(
    source: Path,
    source_path: str,
    config: dict[str, Any],
    crop_specs: list[dict[str, Any]],
) -> tuple[Any, dict[str, Any]]:
    ocr_crop_specs = _crop_specs_for_pdf_source(crop_specs, source_path)
    plan = parse_pdf_ocr_config(config, crop_specs=ocr_crop_specs)
    if plan.requested_engine.casefold() != "rapidocr":
        raise ExtractionError(
            f"unsupported local OCR engine: {plan.requested_engine!r}"
        )
    engine = RapidOcrEngine(
        _bundled_rapidocr_models(),
        use_cls=plan.use_cls,
        text_score=plan.text_score,
        box_thresh=plan.box_thresh,
        unclip_ratio=plan.unclip_ratio,
    )
    run = ocr_pdf_from_config(
        source,
        source_path,
        config,
        engine,
        crop_specs=ocr_crop_specs,
    )
    document = ocr_run_to_pdf_text_document(source, source_path, run, config)
    return document, ocr_text_extraction_details(run)


def _crop_specs_for_pdf_source(
    crop_specs: Iterable[Mapping[str, Any]],
    source_path: str,
) -> list[dict[str, Any]]:
    """Return only visual crops belonging to the PDF currently being OCRed.

    One record-level override can describe crops from the main PDF and several
    supplementary PDFs.  The OCR planner must see only the current PDF's crops;
    otherwise a valid multi-page supplementary crop is misread as a malformed
    single-page main-PDF exclusion.
    """

    normalized_source = source_path.replace("\\", "/")
    return [
        dict(spec)
        for spec in crop_specs
        if str(spec.get("source_path", spec.get("source", ""))).replace("\\", "/")
        == normalized_source
    ]


def _load_override(path: Path | None, record_id: str) -> tuple[dict[str, Any], str | None, str | None]:
    if path is None or not path.exists():
        return {}, None, None
    if yaml is None:
        raise ExtractionError("PyYAML is required to load extraction overrides")
    # The diagnostic snapshot must retain the exact reviewed override bytes,
    # including CRLF on Windows; universal-newline reads silently change them.
    text = path.read_bytes().decode("utf-8", errors="strict")
    value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ExtractionError(f"override must be a mapping: {path}")
    if str(value.get("record_id", "")) != record_id:
        raise ExtractionError(f"override record_id does not match {record_id}: {path}")
    if str(value.get("schema_version", "")) != "1.0":
        raise ExtractionError(f"unsupported override schema_version: {path}")
    for key in (
        "text_repairs",
        "rich_text_overrides",
        "pdf_crops",
        "supplement_exclusions",
        "front_matter",
        "section_equation_additions",
        "reference_entries",
        "supporting_information_additions",
        "supplement_block_additions",
        "supplement_figure_overrides",
        "source_anomalies",
        "standalone_image_figures",
        "standalone_image_tables",
        "docx_figure_crops",
        "supplement_table_overrides",
        "table_overrides",
        "reviewed_html_assets",
    ):
        if key in value and not isinstance(value[key], list):
            raise ExtractionError(f"override {key} must be a list: {path}")
    for key in ("pdf_text", "pdf_ocr", "expected_counts"):
        if key in value and not isinstance(value[key], dict):
            raise ExtractionError(f"override {key} must be a mapping: {path}")
    return value, text, sha256_file(path)


def _validate_text_repair_sources(
    specs: list[dict[str, Any]],
    sources: list[SourceFile],
    primary_source: SourceFile,
) -> None:
    """Fail closed for text repairs that declare an exact source snapshot.

    Older reviewed overrides remain compatible. New repairs can opt into a
    source-path/hash gate and an enforced match count by declaring all three
    fields together.
    """

    source_by_path = {source.relative_path: source for source in sources}
    pin_fields = {"source_path", "source_sha256", "expected_matches"}
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise ExtractionError(f"text_repairs item {index} must be a mapping")
        if not pin_fields.intersection(spec):
            continue
        source_path = str(spec.get("source_path", "")).replace("\\", "/")
        source_sha256 = str(spec.get("source_sha256", "")).strip().casefold()
        expected_matches = spec.get("expected_matches")
        source = source_by_path.get(source_path)
        if (
            source is None
            or source.relative_path != primary_source.relative_path
            or source.sha256.casefold() != source_sha256
            or isinstance(expected_matches, bool)
            or not isinstance(expected_matches, int)
            or expected_matches < 1
            or not str(spec.get("pattern", ""))
            or not str(spec.get("reason", "")).strip()
            or not str(spec.get("evidence", "")).strip()
        ):
            raise ExtractionError(f"invalid source-pinned text_repairs item {index}")


def _apply_front_matter_overrides(
    article: Any,
    specs: list[dict[str, Any]],
    sources: list[SourceFile],
) -> None:
    """Add or replace reviewed front matter from stronger source evidence.

    A matching label replaces one publisher-HTML block.  This lets an exact
    PDF-backed override complete a partially archived author/affiliation row
    without leaving two contradictory blocks in canonical content.
    """

    allowed_sources = {source.relative_path: source for source in sources}
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise ExtractionError(f"front_matter item {index} must be a mapping")
        label = str(spec.get("label", "")).strip()
        value = str(spec.get("value", "")).strip()
        source_path = str(spec.get("source_path", "")).replace("\\", "/")
        source_sha256 = str(spec.get("source_sha256", "")).strip().casefold()
        source_locator = str(spec.get("source_locator", "")).strip()
        source_geometry = _validated_override_source_geometry(
            spec.get("source_geometry", []),
            context=f"front_matter item {index}",
        )
        if not label or not value or "\n" in label or "\n" in value:
            raise ExtractionError(
                f"front_matter item {index} requires single-line label and value"
            )
        source = allowed_sources.get(source_path)
        if (
            source is None
            or (source_sha256 and source_sha256 != source.sha256.casefold())
            or not source_locator
        ):
            raise ExtractionError(
                f"front_matter item {index} requires discovered source evidence"
            )
        replacement = ContentBlock(
            block_id=f"front-matter-{index:03d}",
            kind="front_matter",
            markdown=f"**{label}:** {value}",
            plain_text=f"{label}: {value}",
            source_path=source_path,
            source_locator=source_locator,
            source_geometry=source_geometry,
        )
        matching = [
            position
            for position, block in enumerate(article.front_matter)
            if block.plain_text.startswith(f"{label}:")
        ]
        if len(matching) > 1:
            raise ExtractionError(
                f"front_matter item {index} label matches multiple existing blocks"
            )
        if matching:
            article.front_matter[matching[0]] = replacement
        else:
            article.front_matter.append(replacement)


def _apply_rich_text_overrides(
    article: Any,
    specs: list[dict[str, Any]],
    sources: list[SourceFile],
) -> None:
    """Apply exact, source-pinned formatting without changing plain text.

    Each override targets one stable main-article block or figure identifier,
    pins both of its existing text projections, and supplies a replacement only
    for the rich projection. Visible-text parity is independently checked
    before mutation and again by canonical record construction.
    """

    if not specs:
        return

    targets: dict[str, list[tuple[Any, str, str, str]]] = {}

    def add_target(
        target_id: str,
        owner: Any,
        markdown_field: str,
        plain_field: str,
        kind: str,
    ) -> None:
        targets.setdefault(target_id, []).append(
            (owner, markdown_field, plain_field, kind)
        )

    for block in article.front_matter:
        add_target(block.block_id, block, "markdown", "plain_text", block.kind)
    for section in article.sections:
        for block in section.blocks:
            add_target(block.block_id, block, "markdown", "plain_text", block.kind)
    for block in article.references:
        add_target(block.block_id, block, "markdown", "plain_text", block.kind)
    for block in article.supporting_information:
        add_target(block.block_id, block, "markdown", "plain_text", block.kind)
    for figure in article.figures:
        add_target(
            figure.figure_id,
            figure,
            "caption_markdown",
            "caption_plain",
            "figure_caption",
        )

    source_by_path = {source.relative_path: source for source in sources}
    seen_targets: set[str] = set()
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise ExtractionError(
                f"rich_text_overrides item {index} must be a mapping"
            )
        target_id = str(spec.get("target_id", "")).strip()
        expected_plain = str(spec.get("expected_plain_text", ""))
        expected_markdown = str(spec.get("expected_markdown", expected_plain))
        replacement_markdown = str(spec.get("replacement_markdown", ""))
        source_path = str(spec.get("source_path", "")).replace("\\", "/")
        source_sha256 = str(spec.get("source_sha256", "")).strip().casefold()
        source_locator = str(spec.get("source_locator", "")).strip()
        reason = str(spec.get("reason", "")).strip()
        evidence = str(spec.get("evidence", "")).strip()
        source = source_by_path.get(source_path)
        matching = targets.get(target_id, [])
        if (
            not target_id
            or target_id in seen_targets
            or len(matching) != 1
            or not expected_plain
            or not expected_markdown
            or not replacement_markdown
            or replacement_markdown == expected_markdown
            or source is None
            or source.sha256.casefold() != source_sha256
            or not source_locator
            or not reason
            or not evidence
        ):
            raise ExtractionError(f"invalid rich_text_overrides item {index}")
        owner, markdown_field, plain_field, kind = matching[0]
        if (
            getattr(owner, plain_field) != expected_plain
            or getattr(owner, markdown_field) != expected_markdown
        ):
            raise ExtractionError(
                f"rich_text_overrides item {index} did not match target {target_id!r}"
            )
        try:
            rendered = block_markup_to_safe_html(
                replacement_markdown,
                kind=kind,
            )
        except UnsafeRichTextError as exc:
            raise ExtractionError(
                f"rich_text_overrides item {index} contains unsafe markup: {exc}"
            ) from exc
        native_pdf_parity = (
            source.role == "main_pdf"
            and _canonical_plain_for_rich(expected_plain, rendered) is not None
        )
        if not rich_text_matches_plain(expected_plain, rendered) and not native_pdf_parity:
            raise ExtractionError(
                f"rich_text_overrides item {index} changes visible plain text"
            )
        setattr(owner, markdown_field, replacement_markdown)
        article.repairs.append(
            Repair(
                repair_id=f"rich-text-override-{index:03d}",
                pattern=expected_markdown,
                replacement=replacement_markdown,
                occurrences=1,
                reason=reason,
                evidence=f"{source_locator.rstrip('.')}: {evidence}",
            )
        )
        seen_targets.add(target_id)


def _project_pdf_scientific_markup(article: Any, pdf_source_path: str) -> None:
    """Render explicit canonical PDF scripts without changing plain text.

    Reviewed PDF/OCR repairs use the repository's canonical ``_{...}`` and
    ``^{...}`` notation in their plain projection. Mirror only those explicit
    tokens into safe ``<sub>``/``<sup>`` markup. Existing publisher or reviewed
    markup remains intact, including in blocks that join native and reviewed
    PDF regions.
    """

    script_pattern = re.compile(r"(?P<marker>[_^])\{(?P<body>[^{}]+)\}")

    def project(value: str) -> str:
        return script_pattern.sub(
            lambda match: (
                f"<sub>{match.group('body')}</sub>"
                if match.group("marker") == "_"
                else f"<sup>{match.group('body')}</sup>"
            ),
            value,
        )

    for section in article.sections:
        if section.source_path == pdf_source_path:
            section.heading = project(section.heading)
        for block in section.blocks:
            if block.source_path == pdf_source_path:
                block.markdown = project(block.markdown)
    for block in (*article.front_matter, *article.references, *article.supporting_information):
        if block.source_path == pdf_source_path:
            block.markdown = project(block.markdown)
    for figure in article.figures:
        if figure.source_path == pdf_source_path:
            figure.caption_markdown = project(figure.caption_markdown)


def _validated_override_source_geometry(
    value: Any,
    *,
    context: str,
) -> list[dict[str, Any]]:
    """Validate explicit PDF block geometry supplied by a reviewed override."""

    if not isinstance(value, list):
        raise ExtractionError(f"{context} source_geometry must be a list")
    result: list[dict[str, Any]] = []
    for position, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise ExtractionError(
                f"{context} source_geometry item {position} must be a mapping"
            )
        page = item.get("page")
        bbox = item.get("bbox")
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or not isinstance(bbox, list)
            or len(bbox) != 4
            or any(
                isinstance(number, bool) or not isinstance(number, (int, float))
                for number in bbox
            )
            or any(not math.isfinite(float(number)) for number in bbox)
            or any(float(number) < 0 for number in bbox)
            or float(bbox[0]) >= float(bbox[2])
            or float(bbox[1]) >= float(bbox[3])
        ):
            raise ExtractionError(
                f"{context} source_geometry item {position} is invalid"
            )
        result.append({"page": page, "bbox": list(bbox)})
    return result


def _override_pdf_locator(source_geometry: list[dict[str, Any]]) -> str:
    """Return the validator-supported locator for reviewed PDF geometry."""

    pages = sorted({int(item["page"]) for item in source_geometry})
    if not pages:
        raise ExtractionError("reviewed PDF geometry cannot be empty")
    if len(pages) > 1:
        return (
            "PDF pages "
            + ", ".join(str(page) for page in pages)
            + "; exact per-line page/bbox geometry is stored in source_geometry"
        )
    boxes = [item["bbox"] for item in source_geometry]
    box = (
        min(float(value[0]) for value in boxes),
        min(float(value[1]) for value in boxes),
        max(float(value[2]) for value in boxes),
        max(float(value[3]) for value in boxes),
    )
    return (
        f"PDF page {pages[0]}, box "
        f"[{box[0]:.2f}, {box[1]:.2f}, {box[2]:.2f}, {box[3]:.2f}]"
    )


def _apply_section_equation_additions(
    article: Any,
    specs: list[dict[str, Any]],
    sources: list[SourceFile],
) -> None:
    """Insert exact, source-reviewed equations omitted by a primary HTML archive.

    Some publisher HTML snapshots retain references to a numbered display
    equation while omitting the equation node itself. The override is pinned
    to a discovered source digest and to an existing section/block anchor so a
    stale or ambiguously positioned transcription fails closed.
    """

    source_by_path = {source.relative_path: source for source in sources}
    existing_block_ids = {
        block.block_id for section in article.sections for block in section.blocks
    }
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise ExtractionError(
                f"section_equation_additions item {index} must be a mapping"
            )
        section_id = str(spec.get("section_id", "")).strip()
        after_block_id = str(spec.get("after_block_id", "")).strip()
        block_id = str(spec.get("block_id", "")).strip()
        plain_text = str(spec.get("plain_text", "")).strip()
        markdown = str(spec.get("markdown", plain_text)).strip()
        source_path = str(spec.get("source_path", "")).replace("\\", "/")
        source_sha256 = str(spec.get("source_sha256", "")).strip().casefold()
        source_locator = str(spec.get("source_locator", "")).strip()
        reason = str(spec.get("reason", "")).strip()
        evidence = str(spec.get("evidence", "")).strip()
        source_geometry = _validated_override_source_geometry(
            spec.get("source_geometry", []),
            context=f"section_equation_additions item {index}",
        )
        source = source_by_path.get(source_path)
        sections = [
            section for section in article.sections if section.section_id == section_id
        ]
        if (
            len(sections) != 1
            or not after_block_id
            or not block_id
            or block_id in existing_block_ids
            or not plain_text
            or not markdown
            or source is None
            or source.sha256.casefold() != source_sha256
            or not source_locator
            or not reason
            or not evidence
        ):
            raise ExtractionError(
                f"section_equation_additions item {index} requires a unique "
                "anchor and complete discovered-source evidence"
            )
        section = sections[0]
        anchors = [
            position
            for position, block in enumerate(section.blocks)
            if block.block_id == after_block_id
        ]
        if len(anchors) != 1:
            raise ExtractionError(
                f"section_equation_additions item {index} anchor did not match once"
            )
        section.blocks.insert(
            anchors[0] + 1,
            ContentBlock(
                block_id=block_id,
                kind="equation",
                markdown=markdown,
                plain_text=plain_text,
                source_path=source_path,
                source_locator=source_locator,
                source_geometry=source_geometry,
            ),
        )
        existing_block_ids.add(block_id)


def _apply_main_table_overrides(
    article: Any,
    specs: list[dict[str, Any]],
    sources: list[SourceFile],
) -> None:
    """Add or replace exact-PDF-reviewed main-article tables.

    Publisher HTML archives sometimes retain a caption and footnotes but omit
    the table body or contain a source-specific semantic-markup error that a
    stronger archived source resolves. A hash-pinned override recovers that
    body without weakening the default preference for native HTML tables.
    """

    source_by_path = {source.relative_path: source for source in sources}
    tables_by_number: dict[int, TableItem] = {}
    for table in article.tables:
        match = re.fullmatch(r"table_(\d{3})", table.table_id)
        if match is None:
            raise ExtractionError(
                "table_overrides requires canonical existing table identifiers"
            )
        number = int(match.group(1))
        if number in tables_by_number:
            raise ExtractionError("table_overrides found duplicate existing table numbers")
        tables_by_number[number] = table

    previous_number = 0
    seen_numbers: set[int] = set()
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise ExtractionError(f"table_overrides item {index} must be a mapping")
        raw_number = spec.get("number")
        if isinstance(raw_number, bool) or not isinstance(raw_number, int):
            raise ExtractionError(f"table_overrides item {index} has invalid number")
        number = raw_number
        source_path = str(spec.get("source_path", "")).replace("\\", "/")
        source_sha256 = str(spec.get("source_sha256", "")).strip().casefold()
        source_locator = str(spec.get("source_locator", "")).strip()
        source_kind = str(spec.get("source_kind", "")).strip().casefold()
        supporting_source_path = str(
            spec.get("supporting_source_path", "")
        ).replace("\\", "/")
        supporting_source_sha256 = str(
            spec.get("supporting_source_sha256", "")
        ).strip().casefold()
        supporting_source_locator = str(
            spec.get("supporting_source_locator", "")
        ).strip()
        supporting_fields_present = any(
            (supporting_source_path, supporting_source_sha256, supporting_source_locator)
        )
        supporting_source = source_by_path.get(supporting_source_path)
        title_plain = str(spec.get("title_plain", "")).strip()
        title_markdown = str(spec.get("title_markdown", title_plain)).strip()
        label = str(spec.get("label", f"Table {number}")).strip()
        reason = str(spec.get("reason", "")).strip()
        evidence = str(spec.get("evidence", "")).strip()
        source = source_by_path.get(source_path)
        if (
            number < 1
            or number in seen_numbers
            or number <= previous_number
            or source is None
            or source.sha256.casefold() != source_sha256
            or not source_locator
            or source_kind not in {"document", "html", "image", "pdf"}
            or (
                supporting_fields_present
                and (
                    supporting_source is None
                    or supporting_source.sha256.casefold()
                    != supporting_source_sha256
                    or not supporting_source_locator
                )
            )
            or not title_plain
            or not title_markdown
            or not label or "\n" in label
            or not reason
            or not evidence
        ):
            raise ExtractionError(f"invalid table_overrides item {index}")
        seen_numbers.add(number)
        previous_number = number
        table_id = f"table_{number:03d}"

        raw_parts = spec.get("parts")
        if not isinstance(raw_parts, list) or not raw_parts:
            raise ExtractionError(
                f"table_overrides item {index} requires nonempty parts"
            )
        parts: list[TablePart] = []
        for part_index, raw_part in enumerate(raw_parts, start=1):
            if not isinstance(raw_part, dict):
                raise ExtractionError(
                    f"table_overrides item {index} part {part_index} must be a mapping"
                )
            raw_rows = raw_part.get("rows")
            if not isinstance(raw_rows, list) or not raw_rows:
                raise ExtractionError(
                    f"table_overrides item {index} part {part_index} needs rows"
                )
            rows: list[list[TableCell]] = []
            for row_index, raw_row in enumerate(raw_rows, start=1):
                if not isinstance(raw_row, list) or not raw_row:
                    raise ExtractionError(
                        f"table_overrides item {index} part {part_index} row "
                        f"{row_index} must be nonempty"
                    )
                row: list[TableCell] = []
                for cell_index, raw_cell in enumerate(raw_row, start=1):
                    if not isinstance(raw_cell, dict):
                        raise ExtractionError(
                            f"table_overrides item {index} part {part_index} row "
                            f"{row_index} cell {cell_index} must be a mapping"
                        )
                    text = str(raw_cell.get("text", "")).strip()
                    markdown = str(raw_cell.get("markdown", text)).strip()
                    header = raw_cell.get("header", False)
                    rowspan = raw_cell.get("rowspan", 1)
                    colspan = raw_cell.get("colspan", 1)
                    if (
                        not isinstance(header, bool)
                        or isinstance(rowspan, bool)
                        or not isinstance(rowspan, int)
                        or rowspan < 1
                        or isinstance(colspan, bool)
                        or not isinstance(colspan, int)
                        or colspan < 1
                    ):
                        raise ExtractionError(
                            f"table_overrides item {index} part {part_index} row "
                            f"{row_index} cell {cell_index} has invalid metadata"
                        )
                    row.append(
                        TableCell(
                            text=text,
                            markdown=markdown,
                            header=header,
                            rowspan=rowspan,
                            colspan=colspan,
                        )
                    )
                rows.append(row)
            part_id = str(raw_part.get("part_id", "")).strip()
            parts.append(
                TablePart(
                    part_id=part_id or f"{table_id}_part_{part_index:03d}",
                    rows=rows,
                )
            )

        raw_footnotes = spec.get("footnotes", [])
        if not isinstance(raw_footnotes, list):
            raise ExtractionError(
                f"table_overrides item {index} footnotes must be a list"
            )
        footnotes_plain: list[str] = []
        footnotes_markdown: list[str] = []
        for footnote_index, raw_footnote in enumerate(raw_footnotes, start=1):
            if not isinstance(raw_footnote, dict):
                raise ExtractionError(
                    f"table_overrides item {index} footnote {footnote_index} "
                    "must be a mapping"
                )
            plain = str(raw_footnote.get("plain_text", "")).strip()
            markdown = str(raw_footnote.get("markdown", plain)).strip()
            if not plain or not markdown:
                raise ExtractionError(
                    f"table_overrides item {index} footnote {footnote_index} is incomplete"
                )
            footnotes_plain.append(plain)
            footnotes_markdown.append(markdown)

        existing = tables_by_number.get(number)
        tables_by_number[number] = TableItem(
            table_id=table_id,
            source_id=existing.source_id if existing is not None else f"Table {number}",
            label=label,
            title_markdown=title_markdown,
            title_plain=title_plain,
            parts=parts,
            footnotes_markdown=footnotes_markdown,
            footnotes_plain=footnotes_plain,
            source_path=source_path,
            source_locator=source_locator,
            source_kind=source_kind,
        )

    if tables_by_number and sorted(tables_by_number) != list(
        range(1, max(tables_by_number) + 1)
    ):
        raise ExtractionError("table_overrides would create a table-number gap")
    article.tables = [tables_by_number[number] for number in sorted(tables_by_number)]


def _apply_supplement_table_overrides(
    supplements: list[Any],
    specs: list[dict[str, Any]],
    sources: list[SourceFile],
) -> None:
    """Apply exact, hash-pinned metadata repairs to parsed supplement tables.

    Some native office documents position a visual table title after the table
    in OOXML reading order.  The generic parser conservatively keeps those
    paragraphs as footnotes.  A reviewed override can relabel the table only
    when the source snapshot and every parser-produced value match exactly;
    near-misses fail rather than rewriting unrelated authored notes.
    """

    if not specs:
        return
    source_by_path = {source.relative_path: source for source in sources}
    supplement_by_path = {
        supplement.source.relative_path: supplement for supplement in supplements
    }
    repair_required = {
        "source_path",
        "source_sha256",
        "table_index",
        "expected_label",
        "expected_title_plain",
        "expected_title_markdown",
        "expected_footnotes_plain",
        "expected_footnotes_markdown",
        "label",
        "title_plain",
        "title_markdown",
        "footnotes_plain",
        "footnotes_markdown",
        "source_locator",
        "reason",
        "evidence",
    }
    image_required = {
        "operation",
        "source_path",
        "source_sha256",
        "title_source_path",
        "title_source_sha256",
        "slide_number",
        "expected_slide_count",
        "source_media_sha256",
        "table_id",
        "label",
        "title_plain",
        "title_markdown",
        "parts",
        "footnotes",
        "source_locator",
        "reason",
        "evidence",
    }
    document_add_required = {
        "operation",
        "source_path",
        "source_sha256",
        "table_id",
        "label",
        "title_plain",
        "title_markdown",
        "parts",
        "footnotes",
        "source_locator",
        "reason",
        "evidence",
    }
    document_image_add_required = {
        *document_add_required,
        "source_media_sha256",
        "insert_index",
    }
    cell_repair_required = {
        "operation",
        "source_path",
        "source_sha256",
        "table_index",
        "expected_table_id",
        "part_index",
        "row_index",
        "cell_index",
        "expected_text",
        "expected_markdown",
        "text",
        "markdown",
        "reason",
        "evidence",
    }
    seen: set[tuple[str, int]] = set()
    seen_cells: set[tuple[str, int, int, int, int]] = set()
    seen_table_ids = {
        table.table_id for supplement in supplements for table in supplement.tables
    }

    def string_list(value: Any, field: str, index: int) -> list[str]:
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise ExtractionError(
                f"supplement_table_overrides item {index} {field} must be a string list"
            )
        return list(value)

    for index, spec in enumerate(specs, 1):
        if not isinstance(spec, dict):
            raise ExtractionError(
                f"supplement_table_overrides item {index} has invalid fields"
            )
        if spec.get("operation") == "repair_cell":
            if set(spec) != cell_repair_required:
                raise ExtractionError(
                    f"supplement_table_overrides item {index} has invalid fields"
                )
            source_path = str(spec["source_path"]).replace("\\", "/")
            source_sha256 = str(spec["source_sha256"]).strip().casefold()
            source = source_by_path.get(source_path)
            supplement = supplement_by_path.get(source_path)
            table_index = spec["table_index"]
            part_index = spec["part_index"]
            row_index = spec["row_index"]
            cell_index = spec["cell_index"]
            coordinates = (source_path, table_index, part_index, row_index, cell_index)
            expected_table_id = str(spec["expected_table_id"]).strip()
            expected_text = str(spec["expected_text"])
            expected_markdown = str(spec["expected_markdown"])
            text = str(spec["text"])
            markdown = str(spec["markdown"])
            reason = str(spec["reason"]).strip()
            evidence = str(spec["evidence"]).strip()
            indexes = (table_index, part_index, row_index, cell_index)
            if (
                source is None
                or supplement is None
                or source.sha256.casefold() != source_sha256
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 1
                    for value in indexes
                )
                or coordinates in seen_cells
                or not all((expected_table_id, text, markdown, reason, evidence))
            ):
                raise ExtractionError(
                    f"invalid supplement_table_overrides item {index}"
                )
            try:
                table = supplement.tables[table_index - 1]
                part = table.parts[part_index - 1]
                cell = part.rows[row_index - 1][cell_index - 1]
            except IndexError as error:
                raise ExtractionError(
                    f"supplement_table_overrides item {index} did not match parsed table cell"
                ) from error
            if (
                table.source_path != source_path
                or table.table_id != expected_table_id
                or cell.text != expected_text
                or cell.markdown != expected_markdown
            ):
                raise ExtractionError(
                    f"supplement_table_overrides item {index} did not match parsed table cell"
                )
            seen_cells.add(coordinates)
            cell.text = text
            cell.markdown = markdown
            continue

        if spec.get("operation") in {
            "add_document_table",
            "add_document_image_table",
        }:
            image_table = spec.get("operation") == "add_document_image_table"
            required_fields = (
                document_image_add_required if image_table else document_add_required
            )
            if set(spec) != required_fields:
                raise ExtractionError(
                    f"supplement_table_overrides item {index} has invalid fields"
                )
            source_path = str(spec["source_path"]).replace("\\", "/")
            source_sha256 = str(spec["source_sha256"]).strip().casefold()
            source = source_by_path.get(source_path)
            supplement = supplement_by_path.get(source_path)
            table_id = str(spec["table_id"]).strip()
            label = str(spec["label"]).strip()
            title_plain = str(spec["title_plain"]).strip()
            title_markdown = str(spec["title_markdown"]).strip()
            source_locator = str(spec["source_locator"]).strip()
            reason = str(spec["reason"]).strip()
            evidence = str(spec["evidence"]).strip()
            source_media_sha256 = (
                str(spec.get("source_media_sha256", "")).strip().casefold()
            )
            insert_index = spec.get("insert_index")
            if (
                source is None
                or supplement is None
                or source.sha256.casefold() != source_sha256
                or table_id in seen_table_ids
                or (image_table and len(source_media_sha256) != 64)
                or (
                    image_table
                    and (
                        isinstance(insert_index, bool)
                        or not isinstance(insert_index, int)
                        or insert_index < 1
                        or insert_index > len(supplement.tables) + 1
                    )
                )
                or not all(
                    (
                        table_id,
                        label,
                        title_plain,
                        title_markdown,
                        source_locator,
                        reason,
                        evidence,
                    )
                )
            ):
                raise ExtractionError(
                    f"invalid supplement_table_overrides item {index}"
                )
            image_assets = (
                [
                    asset
                    for asset in supplement.assets
                    if str(asset.get("category", "")).casefold()
                    == "supplement_image"
                    and str(asset.get("sha256", "")).casefold()
                    == source_media_sha256
                ]
                if image_table
                else []
            )
            if image_table and len(image_assets) != 1:
                raise ExtractionError(
                    f"supplement_table_overrides item {index} did not match "
                    "exactly one reviewed document image"
                )

            raw_parts = spec["parts"]
            if not isinstance(raw_parts, list) or not raw_parts:
                raise ExtractionError(
                    f"supplement_table_overrides item {index} requires nonempty parts"
                )
            parts: list[TablePart] = []
            for part_index, raw_part in enumerate(raw_parts, 1):
                if not isinstance(raw_part, dict) or set(raw_part) - {"part_id", "rows"}:
                    raise ExtractionError(
                        f"supplement_table_overrides item {index} part {part_index} is invalid"
                    )
                raw_rows = raw_part.get("rows")
                if not isinstance(raw_rows, list) or not raw_rows:
                    raise ExtractionError(
                        f"supplement_table_overrides item {index} part {part_index} needs rows"
                    )
                rows: list[list[TableCell]] = []
                for row_index, raw_row in enumerate(raw_rows, 1):
                    if not isinstance(raw_row, list) or not raw_row:
                        raise ExtractionError(
                            f"supplement_table_overrides item {index} part {part_index} "
                            f"row {row_index} must be nonempty"
                        )
                    row: list[TableCell] = []
                    for cell_index, raw_cell in enumerate(raw_row, 1):
                        if not isinstance(raw_cell, dict) or set(raw_cell) - {
                            "text",
                            "markdown",
                            "header",
                            "rowspan",
                            "colspan",
                        }:
                            raise ExtractionError(
                                f"supplement_table_overrides item {index} part {part_index} "
                                f"row {row_index} cell {cell_index} is invalid"
                            )
                        text = str(raw_cell.get("text", "")).strip()
                        markdown = str(raw_cell.get("markdown", text)).strip()
                        header = raw_cell.get("header", False)
                        rowspan = raw_cell.get("rowspan", 1)
                        colspan = raw_cell.get("colspan", 1)
                        if (
                            not isinstance(header, bool)
                            or isinstance(rowspan, bool)
                            or not isinstance(rowspan, int)
                            or rowspan < 1
                            or isinstance(colspan, bool)
                            or not isinstance(colspan, int)
                            or colspan < 1
                        ):
                            raise ExtractionError(
                                f"supplement_table_overrides item {index} part {part_index} "
                                f"row {row_index} cell {cell_index} has invalid metadata"
                            )
                        row.append(
                            TableCell(
                                text=text,
                                markdown=markdown,
                                header=header,
                                rowspan=rowspan,
                                colspan=colspan,
                            )
                        )
                    rows.append(row)
                part_id = str(raw_part.get("part_id", "")).strip()
                parts.append(
                    TablePart(
                        part_id=part_id or f"{table_id}_part_{part_index:03d}",
                        rows=rows,
                    )
                )

            raw_footnotes = spec["footnotes"]
            if not isinstance(raw_footnotes, list):
                raise ExtractionError(
                    f"supplement_table_overrides item {index} footnotes must be a list"
                )
            footnotes_plain: list[str] = []
            footnotes_markdown: list[str] = []
            for footnote_index, raw_footnote in enumerate(raw_footnotes, 1):
                if not isinstance(raw_footnote, dict) or set(raw_footnote) - {
                    "plain_text",
                    "markdown",
                }:
                    raise ExtractionError(
                        f"supplement_table_overrides item {index} footnote "
                        f"{footnote_index} is invalid"
                    )
                plain = str(raw_footnote.get("plain_text", "")).strip()
                markdown = str(raw_footnote.get("markdown", plain)).strip()
                if not plain or not markdown:
                    raise ExtractionError(
                        f"supplement_table_overrides item {index} footnote "
                        f"{footnote_index} is incomplete"
                    )
                footnotes_plain.append(plain)
                footnotes_markdown.append(markdown)

            if image_table:
                image_asset = image_assets[0]
                previous_asset_id = str(image_asset.get("asset_id", ""))
                image_asset["asset_id"] = table_id
                image_asset["category"] = "supplement_table"
                image_asset["label"] = label
                image_asset["source_locator"] = source_locator
                supplement.asset_ids = [
                    table_id if asset_id == previous_asset_id else asset_id
                    for asset_id in supplement.asset_ids
                ]

            table_item = TableItem(
                    table_id=table_id,
                    source_id=label,
                    label=label,
                    title_markdown=title_markdown,
                    title_plain=title_plain,
                    parts=parts,
                    footnotes_markdown=footnotes_markdown,
                    footnotes_plain=footnotes_plain,
                    source_path=source_path,
                    source_locator=source_locator,
                    source_kind="image" if image_table else "document",
                )
            if image_table:
                supplement.tables.insert(insert_index - 1, table_item)
            else:
                supplement.tables.append(table_item)
            seen_table_ids.add(table_id)
            continue

        if spec.get("operation") == "add_image_table":
            if set(spec) != image_required:
                raise ExtractionError(
                    f"supplement_table_overrides item {index} has invalid fields"
                )
            source_path = str(spec["source_path"]).replace("\\", "/")
            source_sha256 = str(spec["source_sha256"]).strip().casefold()
            title_source_path = str(spec["title_source_path"]).replace("\\", "/")
            title_source_sha256 = str(spec["title_source_sha256"]).strip().casefold()
            source = source_by_path.get(source_path)
            title_source = source_by_path.get(title_source_path)
            supplement = supplement_by_path.get(source_path)
            slide_number = spec["slide_number"]
            expected_slide_count = spec["expected_slide_count"]
            source_media_sha256 = str(spec["source_media_sha256"]).strip().casefold()
            table_id = str(spec["table_id"]).strip()
            label = str(spec["label"]).strip()
            title_plain = str(spec["title_plain"]).strip()
            title_markdown = str(spec["title_markdown"]).strip()
            source_locator = str(spec["source_locator"]).strip()
            reason = str(spec["reason"]).strip()
            evidence = str(spec["evidence"]).strip()
            if (
                source is None
                or title_source is None
                or supplement is None
                or source.sha256.casefold() != source_sha256
                or title_source.sha256.casefold() != title_source_sha256
                or isinstance(slide_number, bool)
                or not isinstance(slide_number, int)
                or slide_number < 1
                or isinstance(expected_slide_count, bool)
                or not isinstance(expected_slide_count, int)
                or expected_slide_count < slide_number
                or len(source_media_sha256) != 64
                or table_id in seen_table_ids
                or not all(
                    (
                        table_id,
                        label,
                        title_plain,
                        title_markdown,
                        source_locator,
                        reason,
                        evidence,
                    )
                )
            ):
                raise ExtractionError(
                    f"invalid supplement_table_overrides item {index}"
                )

            renders = [
                asset
                for asset in supplement.assets
                if str(asset.get("category", "")).casefold()
                == "supplement_slide_render"
                and asset.get("presentation_slide_number") == slide_number
                and asset.get("presentation_slide_count") == expected_slide_count
            ]
            media = []
            for asset in supplement.assets:
                if (
                    str(asset.get("category", "")).casefold()
                    != "supplement_image"
                    or str(asset.get("sha256", "")).casefold()
                    != source_media_sha256
                ):
                    continue
                raw_slides = asset.get("presentation_slide_numbers")
                try:
                    slides = [int(value) for value in raw_slides]
                except (TypeError, ValueError):
                    slides = []
                if slides == [slide_number]:
                    media.append(asset)
            if (
                len(renders) != 1
                or str(renders[0].get("asset_id", "")) != table_id
                or len(media) != 1
            ):
                raise ExtractionError(
                    f"supplement_table_overrides item {index} did not match "
                    "the reviewed slide render and embedded source image"
                )

            raw_parts = spec["parts"]
            if not isinstance(raw_parts, list) or not raw_parts:
                raise ExtractionError(
                    f"supplement_table_overrides item {index} requires nonempty parts"
                )
            parts: list[TablePart] = []
            for part_index, raw_part in enumerate(raw_parts, 1):
                if not isinstance(raw_part, dict) or set(raw_part) - {"part_id", "rows"}:
                    raise ExtractionError(
                        f"supplement_table_overrides item {index} part {part_index} is invalid"
                    )
                raw_rows = raw_part.get("rows")
                if not isinstance(raw_rows, list) or not raw_rows:
                    raise ExtractionError(
                        f"supplement_table_overrides item {index} part {part_index} needs rows"
                    )
                rows: list[list[TableCell]] = []
                for row_index, raw_row in enumerate(raw_rows, 1):
                    if not isinstance(raw_row, list) or not raw_row:
                        raise ExtractionError(
                            f"supplement_table_overrides item {index} part {part_index} "
                            f"row {row_index} must be nonempty"
                        )
                    row: list[TableCell] = []
                    for cell_index, raw_cell in enumerate(raw_row, 1):
                        if not isinstance(raw_cell, dict) or set(raw_cell) - {
                            "text",
                            "markdown",
                            "header",
                            "rowspan",
                            "colspan",
                        }:
                            raise ExtractionError(
                                f"supplement_table_overrides item {index} part {part_index} "
                                f"row {row_index} cell {cell_index} is invalid"
                            )
                        text = str(raw_cell.get("text", "")).strip()
                        markdown = str(raw_cell.get("markdown", text)).strip()
                        header = raw_cell.get("header", False)
                        rowspan = raw_cell.get("rowspan", 1)
                        colspan = raw_cell.get("colspan", 1)
                        if (
                            not isinstance(header, bool)
                            or isinstance(rowspan, bool)
                            or not isinstance(rowspan, int)
                            or rowspan < 1
                            or isinstance(colspan, bool)
                            or not isinstance(colspan, int)
                            or colspan < 1
                        ):
                            raise ExtractionError(
                                f"supplement_table_overrides item {index} part {part_index} "
                                f"row {row_index} cell {cell_index} has invalid metadata"
                            )
                        row.append(
                            TableCell(
                                text=text,
                                markdown=markdown,
                                header=header,
                                rowspan=rowspan,
                                colspan=colspan,
                            )
                        )
                    rows.append(row)
                part_id = str(raw_part.get("part_id", "")).strip()
                parts.append(
                    TablePart(
                        part_id=part_id or f"{table_id}_part_{part_index:03d}",
                        rows=rows,
                    )
                )

            raw_footnotes = spec["footnotes"]
            if not isinstance(raw_footnotes, list):
                raise ExtractionError(
                    f"supplement_table_overrides item {index} footnotes must be a list"
                )
            footnotes_plain: list[str] = []
            footnotes_markdown: list[str] = []
            for footnote_index, raw_footnote in enumerate(raw_footnotes, 1):
                if not isinstance(raw_footnote, dict) or set(raw_footnote) - {
                    "plain_text",
                    "markdown",
                }:
                    raise ExtractionError(
                        f"supplement_table_overrides item {index} footnote "
                        f"{footnote_index} is invalid"
                    )
                plain = str(raw_footnote.get("plain_text", "")).strip()
                markdown = str(raw_footnote.get("markdown", plain)).strip()
                if not plain or not markdown:
                    raise ExtractionError(
                        f"supplement_table_overrides item {index} footnote "
                        f"{footnote_index} is incomplete"
                    )
                footnotes_plain.append(plain)
                footnotes_markdown.append(markdown)

            renders[0]["category"] = "supplement_table"
            renders[0]["label"] = label
            renders[0]["source_locator"] = source_locator
            media[0]["parent_id"] = table_id
            supplement.tables.append(
                TableItem(
                    table_id=table_id,
                    source_id=label,
                    label=label,
                    title_markdown=title_markdown,
                    title_plain=title_plain,
                    parts=parts,
                    footnotes_markdown=footnotes_markdown,
                    footnotes_plain=footnotes_plain,
                    source_path=source_path,
                    source_locator=source_locator,
                    source_kind="image",
                )
            )
            seen_table_ids.add(table_id)
            continue

        if set(spec) != repair_required:
            raise ExtractionError(
                f"supplement_table_overrides item {index} has invalid fields"
            )
        source_path = str(spec["source_path"]).replace("\\", "/")
        source_sha256 = str(spec["source_sha256"]).strip().casefold()
        table_index = spec["table_index"]
        source = source_by_path.get(source_path)
        supplement = supplement_by_path.get(source_path)
        expected_label = str(spec["expected_label"])
        expected_title_plain = str(spec["expected_title_plain"])
        expected_title_markdown = str(spec["expected_title_markdown"])
        label = str(spec["label"]).strip()
        title_plain = str(spec["title_plain"]).strip()
        title_markdown = str(spec["title_markdown"]).strip()
        source_locator = str(spec["source_locator"]).strip()
        reason = str(spec["reason"]).strip()
        evidence = str(spec["evidence"]).strip()
        expected_footnotes_plain = string_list(
            spec["expected_footnotes_plain"], "expected_footnotes_plain", index
        )
        expected_footnotes_markdown = string_list(
            spec["expected_footnotes_markdown"], "expected_footnotes_markdown", index
        )
        footnotes_plain = string_list(
            spec["footnotes_plain"], "footnotes_plain", index
        )
        footnotes_markdown = string_list(
            spec["footnotes_markdown"], "footnotes_markdown", index
        )
        if (
            source is None
            or supplement is None
            or source.sha256.casefold() != source_sha256
            or isinstance(table_index, bool)
            or not isinstance(table_index, int)
            or table_index < 1
            or table_index > len(supplement.tables)
            or (source_path, table_index) in seen
            or not all((label, title_plain, title_markdown, source_locator, reason, evidence))
            or len(footnotes_plain) != len(footnotes_markdown)
        ):
            raise ExtractionError(
                f"invalid supplement_table_overrides item {index}"
            )
        seen.add((source_path, table_index))
        table = supplement.tables[table_index - 1]
        if (
            table.source_path != source_path
            or table.label != expected_label
            or table.title_plain != expected_title_plain
            or table.title_markdown != expected_title_markdown
            or table.footnotes_plain != expected_footnotes_plain
            or table.footnotes_markdown != expected_footnotes_markdown
        ):
            raise ExtractionError(
                f"supplement_table_overrides item {index} did not match parsed table"
            )
        table.source_id = label
        table.label = label
        table.title_plain = title_plain
        table.title_markdown = title_markdown
        table.footnotes_plain = footnotes_plain
        table.footnotes_markdown = footnotes_markdown
        table.source_locator = source_locator


def _apply_reference_entries(
    article: Any,
    specs: list[dict[str, Any]],
    sources: list[SourceFile],
) -> None:
    """Replace or extend an incomplete HTML bibliography from exact evidence.

    This is deliberately record-specific. Existing entries may be replaced by
    number, and new entries must extend the current bibliography contiguously;
    gaps, reordering, duplicate specifications, and undiscovered sources fail
    closed.
    """

    if not specs:
        return

    allowed_sources = {source.relative_path: source for source in sources}
    label_style: str | None = None
    for expected_number, block in enumerate(article.references, start=1):
        label = re.match(
            r"^\s*(?:(?P<plain>\d+)\.(?:[•*]{1,2})?|"
            r"(?P<close_paren>\d+)[)）]|\[(?P<bracketed>\d+)\]\.)\s+",
            block.plain_text,
        )
        if label is None:
            number = None
            current_style = "unlabeled"
            identity_is_contiguous = (
                block.block_id == f"reference-{expected_number:03d}"
            )
        else:
            number = (
                label.group("plain")
                or label.group("close_paren")
                or label.group("bracketed")
            )
            current_style = (
                "bracketed"
                if label.group("bracketed")
                else "close_paren"
                if label.group("close_paren")
                else "plain"
            )
            identity_is_contiguous = (
                number is not None and int(number) == expected_number
            )
        if not identity_is_contiguous or (
            label_style is not None and current_style != label_style
        ):
            raise ExtractionError(
                "reference_entries requires an existing contiguous numbered bibliography"
            )
        label_style = current_style

    seen: set[int] = set()
    previous_number = 0
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise ExtractionError(f"reference_entries item {index} must be a mapping")
        raw_number = spec.get("number")
        if isinstance(raw_number, bool):
            raise ExtractionError(f"reference_entries item {index} has invalid number")
        try:
            number = int(raw_number)
        except (TypeError, ValueError) as error:
            raise ExtractionError(
                f"reference_entries item {index} has invalid number"
            ) from error
        value = str(spec.get("value", "")).strip()
        source_path = str(spec.get("source_path", "")).replace("\\", "/")
        source_sha256 = str(spec.get("source_sha256", "")).strip().casefold()
        source_locator = str(spec.get("source_locator", "")).strip()
        override_source_geometry = _validated_override_source_geometry(
            spec.get("source_geometry", []),
            context=f"reference_entries item {index}",
        )
        source = allowed_sources.get(source_path)
        if (
            number < 1
            or number in seen
            or number <= previous_number
            or not value
            or "\n" in value
            or re.match(r"^\s*(?:\d+[.)]|\[\d+\]\.?)\s+", value)
            or source is None
            or (source_sha256 and source_sha256 != source.sha256.casefold())
            or not source_locator
        ):
            raise ExtractionError(f"invalid reference_entries item {index}")
        seen.add(number)
        previous_number = number
        label_prefix = (
            ""
            if label_style == "unlabeled"
            else f"{number})"
            if label_style == "close_paren"
            else f"[{number}]."
            if label_style == "bracketed"
            else f"{number}."
        )
        rendered_value = f"{label_prefix} {value}" if label_prefix else value
        existing = (
            article.references[number - 1]
            if number <= len(article.references)
            else None
        )
        source_geometry = override_source_geometry or (
            list(existing.source_geometry)
            if existing is not None and existing.source_path == source_path
            else []
        )
        output_locator = (
            _override_pdf_locator(override_source_geometry)
            if override_source_geometry
            else existing.source_locator
            if existing is not None
            and existing.source_path == source_path
            and source_geometry
            else source_locator
        )
        block = ContentBlock(
            block_id=f"reference-{number:03d}",
            kind="reference",
            markdown=rendered_value,
            plain_text=rendered_value,
            source_path=source_path,
            source_locator=output_locator,
            source_geometry=source_geometry,
        )
        if number <= len(article.references):
            article.references[number - 1] = block
        elif number == len(article.references) + 1:
            article.references.append(block)
        else:
            raise ExtractionError(
                f"reference_entries item {index} would create a bibliography gap"
            )


def _apply_supporting_information_additions(
    article: Any,
    specs: list[dict[str, Any]],
    sources: list[SourceFile],
) -> None:
    allowed_sources = {source.relative_path: source for source in sources}
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise ExtractionError(
                f"supporting_information_additions item {index} must be a mapping"
            )
        value = str(spec.get("value", "")).strip()
        source_path = str(spec.get("source_path", "")).replace("\\", "/")
        source_sha256 = str(spec.get("source_sha256", "")).casefold()
        source_locator = str(spec.get("source_locator", "")).strip()
        reason = str(spec.get("reason", "")).strip()
        evidence = str(spec.get("evidence", "")).strip()
        source = allowed_sources.get(source_path)
        # Frozen reviewed inputs created before per-entry evidence fields were
        # introduced remain reproducible.  Their source path still resolves
        # only inside the extraction-wide immutable discovery snapshot.  Any
        # entry that opts into the newer fields must provide the complete set
        # and an exact digest; partial or stale evidence fails closed.
        legacy_source_snapshot = not source_sha256 and not reason and not evidence
        explicit_source_evidence = bool(source_sha256 and reason and evidence)
        if (
            not value
            or "\n" in value
            or source is None
            or not (legacy_source_snapshot or explicit_source_evidence)
            or (
                explicit_source_evidence
                and source_sha256 != source.sha256.casefold()
            )
            or not source_locator
        ):
            raise ExtractionError(
                f"invalid supporting_information_additions item {index}"
            )
        article.supporting_information.append(
            ContentBlock(
                block_id=f"supporting-addition-{index:03d}",
                kind="supporting_information",
                markdown=value,
                plain_text=value,
                source_path=source_path,
                source_locator=source_locator,
            )
        )


def _apply_supplement_block_additions(
    supplements: list[Any],
    specs: list[dict[str, Any]],
    sources: list[SourceFile],
) -> None:
    """Attach reviewed publisher descriptions to their preserved supplement.

    Download-only supplements such as videos can carry their authored label and
    description solely in the main HTML.  Hash-pin both that evidence source and
    the target supplement so a stale filename/order assumption fails closed.
    """

    source_by_path = {source.relative_path: source for source in sources}
    supplement_by_path = {
        supplement.source.relative_path: supplement for supplement in supplements
    }
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise ExtractionError(
                f"supplement_block_additions item {index} must be a mapping"
            )
        supplement_path = str(spec.get("supplement_path", "")).replace("\\", "/")
        supplement_sha256 = str(spec.get("supplement_sha256", "")).casefold()
        value = str(spec.get("value", "")).strip()
        source_path = str(spec.get("source_path", "")).replace("\\", "/")
        source_sha256 = str(spec.get("source_sha256", "")).casefold()
        source_locator = str(spec.get("source_locator", "")).strip()
        reason = str(spec.get("reason", "")).strip()
        evidence = str(spec.get("evidence", "")).strip()
        supplement = supplement_by_path.get(supplement_path)
        evidence_source = source_by_path.get(source_path)
        target_source = source_by_path.get(supplement_path)
        if (
            supplement is None
            or target_source is None
            or supplement.source.relative_path != target_source.relative_path
            or not supplement_sha256
            or supplement_sha256 != target_source.sha256.casefold()
            or evidence_source is None
            or not source_sha256
            or source_sha256 != evidence_source.sha256.casefold()
            or not value
            or "\n" in value
            or not all((source_locator, reason, evidence))
        ):
            raise ExtractionError(f"invalid supplement_block_additions item {index}")
        supplement.blocks.append(
            ContentBlock(
                block_id=f"{supplement.supplement_id}-addition-{index:03d}",
                kind="supplement_description",
                markdown=value,
                plain_text=value,
                source_path=source_path,
                source_locator=source_locator,
            )
        )


def _apply_supplement_figure_overrides(
    supplements: list[Any],
    specs: list[dict[str, Any]],
    sources: list[SourceFile],
) -> None:
    """Promote one exact uncaptioned supplement visual to a reviewed figure.

    Legacy Word supplements can consist solely of a single embedded drawing.
    The generic document parser correctly preserves that visual as an opaque
    asset because pixels and filename order alone do not establish figure
    semantics.  A record-specific, hash-pinned override may classify the
    already extracted asset only after source review has established its
    authored supplementary-figure identity.  No pixels or source files are
    changed, and stale parser output fails closed.
    """

    if not specs:
        return
    source_by_path = {source.relative_path: source for source in sources}
    supplement_by_path = {
        supplement.source.relative_path: supplement for supplement in supplements
    }
    required = {
        "source_path",
        "source_sha256",
        "asset_id",
        "expected_asset_category",
        "expected_asset_output_path",
        "label",
        "caption_plain",
        "caption_markdown",
        "source_locator",
        "reason",
        "evidence",
    }
    seen_labels = {
        figure.label.casefold()
        for supplement in supplements
        for figure in supplement.figures
    }
    seen_ids = {
        figure.figure_id
        for supplement in supplements
        for figure in supplement.figures
    }
    for index, spec in enumerate(specs, 1):
        if not isinstance(spec, dict) or set(spec) != required:
            raise ExtractionError(
                f"supplement_figure_overrides item {index} has invalid fields"
            )
        source_path = str(spec["source_path"]).replace("\\", "/").strip()
        source_sha256 = str(spec["source_sha256"]).strip().casefold()
        asset_id = str(spec["asset_id"]).strip()
        expected_category = str(spec["expected_asset_category"]).strip()
        expected_output = str(spec["expected_asset_output_path"]).replace(
            "\\", "/"
        ).strip()
        label = str(spec["label"]).strip()
        caption_plain = str(spec["caption_plain"]).strip()
        caption_markdown = str(spec["caption_markdown"]).strip()
        source_locator = str(spec["source_locator"]).strip()
        reason = str(spec["reason"]).strip()
        evidence = str(spec["evidence"]).strip()
        source = source_by_path.get(source_path)
        supplement = supplement_by_path.get(source_path)
        matching_assets = [
            asset
            for asset in (supplement.assets if supplement is not None else [])
            if str(asset.get("asset_id", "")) == asset_id
        ]
        if (
            source is None
            or supplement is None
            or source.sha256.casefold() != source_sha256
            or len(matching_assets) != 1
            or matching_assets[0].get("category") != expected_category
            or expected_category != "supplement_image"
            or str(matching_assets[0].get("output_path", "")).replace("\\", "/")
            != expected_output
            or asset_id in seen_ids
            or label.casefold() in seen_labels
            or re.fullmatch(r"(?:Supplementary )?Figure S\d+", label, re.IGNORECASE)
            is None
            or not all(
                (
                    caption_plain,
                    caption_markdown,
                    source_locator,
                    reason,
                    evidence,
                )
            )
        ):
            raise ExtractionError(
                f"invalid supplement_figure_overrides item {index}"
            )
        asset = matching_assets[0]
        asset["category"] = "supplement_figure"
        asset["label"] = label
        asset["source_locator"] = source_locator
        supplement.figures.append(
            FigureItem(
                figure_id=asset_id,
                source_id=label,
                label=label,
                kind="figure",
                caption_markdown=caption_markdown,
                caption_plain=caption_plain,
                source_path=source_path,
                source_locator=source_locator,
            )
        )
        if asset_id not in supplement.asset_ids:
            supplement.asset_ids.append(asset_id)
        seen_ids.add(asset_id)
        seen_labels.add(label.casefold())


def _remove_redundant_supporting_figure_captions(
    article: Any, supplements: list[Any]
) -> None:
    """Drop only supporting blocks duplicated by semantic supplement figures.

    Some publisher HTML exports concatenate every supplementary figure caption
    into one supporting-information paragraph. Once the verified supplement
    figures carry those same captions, retaining the paragraph presents the
    scientific text twice. Require an exact complete sequence (or an exact
    individual caption block); near matches and blocks with any extra text are
    preserved.
    """

    duplicate_texts: set[str] = set()
    for supplement in supplements:
        captions = [
            figure.caption_plain.strip()
            for figure in supplement.figures
            if figure.caption_plain.strip()
        ]
        supplement_duplicate_texts = set(captions)
        if captions:
            supplement_duplicate_texts.add(" ".join(captions))
        duplicate_texts.update(supplement_duplicate_texts)
        if supplement_duplicate_texts and hasattr(supplement, "blocks"):
            supplement.blocks = [
                block
                for block in supplement.blocks
                if block.plain_text.strip() not in supplement_duplicate_texts
            ]
    if not duplicate_texts:
        return
    article.supporting_information = [
        block
        for block in article.supporting_information
        if block.plain_text.strip() not in duplicate_texts
    ]


def _bind_html_supplement_figures(article: Any, supplements: list[Any]) -> None:
    """Classify publisher-HTML supplementary figures without inventing ownership.

    The HTML extractor marks an exact ``Supplementary Fig. S#`` caption with
    an unmistakable provisional ID.  When precisely one supplementary source
    exists, that source association is unambiguous even if the downloadable
    legacy document contains only text while the publisher hosts the figure
    pixels in the article HTML. When multiple or no downloadable supplements
    exist, retain the cards at record scope with ``supplement_figure`` semantics
    and an HTML-supplement asset path; attaching them to any one file would be
    an unsupported source claim.
    """

    pending = [
        figure
        for figure in article.figures
        if figure.figure_id.startswith("html_supplement_figure_")
    ]
    if not pending:
        return
    supplement = supplements[0] if len(supplements) == 1 else None
    embedded_by_id = {
        asset.asset_id: asset for asset in article.embedded_assets
    }
    if len(embedded_by_id) != len(article.embedded_assets):
        raise ExtractionError("duplicate embedded asset ID before supplement binding")
    existing_ids = (
        {figure.figure_id for figure in supplement.figures}
        | set(supplement.asset_ids)
        if supplement is not None
        else set()
    )
    replacements: dict[str, tuple[FigureItem, EmbeddedAsset]] = {}
    for figure in pending:
        token = figure.figure_id.removeprefix("html_supplement_figure_")
        if not token or not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", token):
            raise ExtractionError(
                f"invalid publisher-HTML supplementary figure token: {token!r}"
            )
        asset = embedded_by_id.get(figure.figure_id)
        if asset is None:
            raise ExtractionError(
                f"publisher-HTML supplementary figure lacks embedded pixels: {figure.figure_id}"
            )
        bound_id = (
            f"{supplement.supplement_id}_figure_{token}"
            if supplement is not None
            else figure.figure_id
        )
        if bound_id in existing_ids or bound_id in replacements:
            raise ExtractionError(
                f"duplicate supplementary figure identity after HTML binding: {bound_id}"
            )
        extension = Path(asset.output_path).suffix.casefold()
        if not extension:
            raise ExtractionError(
                f"publisher-HTML supplementary figure has no asset extension: {figure.figure_id}"
            )
        bound_output = (
            f"figures/{supplement.supplement_id}/figure_{token}{extension}"
            if supplement is not None
            else f"figures/html_supplement/figure_{token}{extension}"
        )
        figure.figure_id = bound_id
        figure.kind = "figure" if supplement is not None else "supplement_figure"
        replacements[asset.asset_id] = (
            figure,
            replace(
                asset,
                asset_id=bound_id,
                category="supplement_figure",
                output_path=bound_output,
            ),
        )
        existing_ids.add(bound_id)

    if supplement is not None:
        article.figures = [
            figure for figure in article.figures if figure not in pending
        ]
    article.embedded_assets = [
        replacements[asset.asset_id][1]
        if asset.asset_id in replacements
        else asset
        for asset in article.embedded_assets
    ]
    if supplement is not None:
        for figure in pending:
            supplement.figures.append(figure)
            supplement.asset_ids.append(figure.figure_id)


def _remove_duplicate_main_figures_promoted_from_supplements(
    article: Any, supplements: list[Any]
) -> None:
    """Drop an empty-caption HTML duplicate after reviewed supplement promotion.

    Some ScienceDirect archives append standalone supplementary images to the
    article's generic figure carousel.  The HTML parser cannot safely infer
    their supplement identity from that presentation alone, so it initially
    exposes them as article figures.  Once a hash-pinned standalone-image
    review has promoted the exact source file to a semantic supplement figure,
    suppress only an article figure whose embedded bytes equal that source and
    whose caption is empty (or exactly duplicates the reviewed caption).

    A differently captioned main figure is retained even when its pixels are
    reused in a supplement; that can represent intentional authored reuse.
    """

    reviewed_by_source_sha: dict[str, set[str]] = {}
    for supplement in supplements:
        captions = {
            figure.caption_plain.strip()
            for figure in getattr(supplement, "figures", [])
            if figure.caption_plain.strip()
        }
        source = getattr(supplement, "source", None)
        source_sha256 = str(getattr(source, "sha256", "")).casefold()
        if captions and re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            reviewed_by_source_sha[source_sha256] = captions
    if not reviewed_by_source_sha:
        return

    figure_by_id = {
        figure.figure_id: figure for figure in getattr(article, "figures", [])
    }
    duplicate_ids: set[str] = set()
    for asset in getattr(article, "embedded_assets", []):
        figure = figure_by_id.get(asset.asset_id)
        if figure is None or asset.category not in {
            "figure",
            "scheme",
            "graphical_abstract",
        }:
            continue
        digest = hashlib.sha256(asset.data).hexdigest()
        reviewed_captions = reviewed_by_source_sha.get(digest)
        if reviewed_captions is None:
            continue
        caption = figure.caption_plain.strip()
        if not caption or caption in reviewed_captions:
            duplicate_ids.add(figure.figure_id)

    if not duplicate_ids:
        return
    article.figures = [
        figure
        for figure in article.figures
        if figure.figure_id not in duplicate_ids
    ]
    article.embedded_assets = [
        asset
        for asset in article.embedded_assets
        if asset.asset_id not in duplicate_ids
    ]


def _remove_redundant_supplement_table_titles(supplements: list[Any]) -> None:
    """Drop exact DOCX heading blocks represented by reviewed table titles.

    A native document can place the authored ``S# Table`` heading immediately
    before its table.  The generic DOCX parser conservatively retains that
    paragraph as a block and gives the table a local ``Table 1`` label.  Once
    a hash-pinned table override supplies the same authored heading as the
    table title, retaining the identical block renders the title twice.  Only
    an exact plain-text match on a subsection-heading block is consolidated;
    partial matches and extra prose remain untouched.
    """

    for supplement in supplements:
        represented = {
            table.title_plain.strip()
            for table in supplement.tables
            if table.title_plain.strip() and table.title_markdown.strip()
        }
        if not represented:
            continue
        supplement.blocks = [
            block
            for block in supplement.blocks
            if not (
                block.kind == "subsection_heading"
                and block.plain_text.strip() in represented
            )
        ]


def _validated_source_anomalies(
    specs: list[dict[str, Any]], sources: list[SourceFile]
) -> list[dict[str, Any]]:
    allowed_sources = {source.relative_path: source for source in sources}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            raise ExtractionError(f"source_anomalies item {index} must be a mapping")
        anomaly_id = str(spec.get("anomaly_id", "")).strip()
        source_path = str(spec.get("source_path", "")).replace("\\", "/")
        source_locator = str(spec.get("source_locator", "")).strip()
        observed = str(spec.get("observed", "")).strip()
        assessment = str(spec.get("assessment", "")).strip()
        disposition = str(spec.get("disposition", "")).strip()
        coverage_id = str(spec.get("coverage_id", "")).strip()
        if (
            not anomaly_id
            or anomaly_id in seen
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", anomaly_id)
            or source_path not in allowed_sources
            or (
                coverage_id
                and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", coverage_id)
            )
            or not all((source_locator, observed, assessment, disposition))
        ):
            raise ExtractionError(f"invalid source_anomalies item {index}")
        seen.add(anomaly_id)
        row = {
            "schema_version": "1.0",
            "anomaly_id": anomaly_id,
            "source_path": source_path,
            "source_locator": source_locator,
            "observed": observed,
            "assessment": assessment,
            "disposition": disposition,
        }
        if coverage_id:
            row["coverage_id"] = coverage_id
        rows.append(row)
    return rows


def _source(sources: list[SourceFile], role: str) -> SourceFile:
    matches = [source for source in sources if source.role == role]
    if len(matches) != 1:
        raise ExtractionError(f"expected exactly one {role} source; found {len(matches)}")
    return matches[0]


def inventory_record(repository_root: Path, record_id: str) -> dict[str, Any]:
    sources = discover_sources(repository_root, record_id)
    return {
        "schema_version": "1.0",
        "record_id": validate_record_id(record_id),
        "source_fingerprint": source_fingerprint(sources),
        "sources": [source.as_dict() for source in sources],
    }


def _default_override_path(record_root: Path) -> Path | None:
    """Return the active working override or the approved diagnostic snapshot.

    A record-root ``extraction_overrides.yaml`` is a temporary working input
    for a new or revised extraction and therefore takes precedence while it
    exists.  After approval, guarded cleanup removes that working copy and the
    manifest-bound diagnostic snapshot becomes the canonical rebuild input.
    """

    working_override = record_root / "extraction_overrides.yaml"
    approved_override = record_root / "extraction_diagnostic" / "overrides.yaml"
    for candidate in (working_override, approved_override):
        if os.path.lexists(candidate):
            return candidate
    return None


def _resolve_override_path(
    path: Path,
    *,
    repository_root: Path,
    record_root: Path,
    record_staging: Path,
) -> Path:
    """Resolve one override inside the record or its same-record staging tree.

    Temporary record-root working overrides, approved diagnostic snapshots,
    and new reviewed inputs inside ``staging/<record-id>`` are allowed;
    cross-record and repository-wide paths fail closed.
    """

    candidate = path if path.is_absolute() else repository_root / path
    for allowed_root in (record_root, record_staging):
        try:
            resolved = ensure_within(candidate, allowed_root)
        except (UnsafePathError, FileNotFoundError):
            continue
        reject_reparse_chain(resolved, allowed_root)
        if not resolved.is_file():
            raise ExtractionError(f"override is not a regular file: {candidate}")
        return resolved
    raise ExtractionError(
        "override must be inside the target record or its same-record staging tree"
    )


def _enrich_assets(
    rendered_assets: list[dict[str, Any]], crop_specs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    specs = {str(spec.get("asset_id")): spec for spec in crop_specs}
    enriched: list[dict[str, Any]] = []
    for asset in rendered_assets:
        spec = specs[asset["asset_id"]]
        merged = dict(asset)
        for key in (
            "category",
            "label",
            "parent_table_id",
            "compound_id",
            "parent_id",
            "source_role",
            "caption_page",
            "caption_box",
        ):
            if key in spec:
                merged[key] = spec[key]
        if "category" not in merged:
            merged["category"] = merged.get("kind", "figure")
        enriched.append(merged)
    return enriched


def _materialize_embedded_assets(
    pending_assets: list[EmbeddedAsset],
    extraction_root: Path,
    sources: list[SourceFile],
    *,
    reserved_assets: Iterable[Mapping[str, Any]] = (),
    reserved_paths: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Write source-embedded bytes only inside the isolated candidate root."""

    try:
        reject_reparse_chain(extraction_root, extraction_root)
        extraction_root = ensure_within(extraction_root, extraction_root)
    except (FileNotFoundError, OSError, UnsafePathError) as exc:
        raise ExtractionError(f"embedded asset output root is unsafe: {exc}") from exc
    source_hashes = {source.relative_path: source.sha256 for source in sources}
    seen_ids, seen_paths = _asset_identity_keys(
        reserved_assets, reserved_paths=reserved_paths
    )
    assets: list[dict[str, Any]] = []
    for pending in pending_assets:
        asset_id = _normalized_asset_id(pending.asset_id)
        if asset_id in seen_ids:
            raise ExtractionError(
                f"embedded asset has a duplicate asset_id: {asset_id!r}"
            )
        relative, parts = _normalized_asset_output_path(pending.output_path)
        path_key = relative.casefold()
        if path_key in seen_paths:
            raise ExtractionError(
                f"embedded asset has a duplicate output path: {relative!r}"
            )
        if not isinstance(pending.data, bytes):
            raise ExtractionError(
                f"embedded asset {asset_id!r} data must be immutable bytes"
            )
        if not isinstance(pending.ocr_performed, bool):
            raise ExtractionError(
                f"embedded asset {asset_id!r} ocr_performed must be boolean"
            )
        if pending.source_path not in source_hashes:
            raise ExtractionError(
                f"embedded asset refers to an undiscovered source: {pending.source_path!r}"
            )
        try:
            destination = ensure_within(
                extraction_root.joinpath(*parts), extraction_root, require_exists=False
            )
            reject_reparse_chain(destination, extraction_root)
            if os.path.lexists(destination):
                raise ExtractionError(
                    f"embedded asset output already exists: {relative!r}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            reject_reparse_chain(destination, extraction_root)
            atomic_write_bytes(destination, pending.data)
            reject_reparse_chain(destination, extraction_root)
            ensure_within(destination, extraction_root, require_exists=True)
            if is_reparse_point(destination) or not destination.is_file():
                raise ExtractionError(
                    f"embedded asset output is not a regular file: {relative!r}"
                )
        except ExtractionError:
            raise
        except (FileNotFoundError, OSError, UnsafePathError) as exc:
            raise ExtractionError(
                f"could not safely materialize embedded asset {asset_id!r}: {exc}"
            ) from exc
        materialized = {
                "schema_version": "1.0",
                "asset_id": asset_id,
                "category": pending.category,
                "label": pending.label,
                "source_path": pending.source_path,
                "source_locator": pending.source_locator,
                "source_sha256": source_hashes[pending.source_path],
                "output_path": relative,
                "media_type": pending.media_type,
                "sha256": sha256_file(destination),
                "bytes": destination.stat().st_size,
                "ocr_performed": pending.ocr_performed,
            }
        if pending.parent_table_id is not None:
            materialized["parent_table_id"] = pending.parent_table_id
        if pending.compound_id is not None:
            materialized["compound_id"] = pending.compound_id
        assets.append(materialized)
        seen_ids.add(asset_id)
        seen_paths.add(path_key)
    return assets


def _embedded_assets_after_pdf_overrides(
    pending_assets: Iterable[EmbeddedAsset],
    crop_specs: Iterable[Mapping[str, Any]],
) -> list[EmbeddedAsset]:
    """Let an explicit PDF crop replace the same HTML-embedded asset.

    A crop is a reviewed source-selection decision. Keeping the lower-quality
    embedded bytes as well would create duplicate IDs and force record owners
    to mutate otherwise valid HTML in memory merely to choose the PDF visual.
    """

    overridden_ids = {
        _normalized_asset_id(spec.get("asset_id")) for spec in crop_specs
    }
    return [asset for asset in pending_assets if asset.asset_id not in overridden_ids]


def _normalized_asset_id(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ExtractionError(
            f"embedded asset has an empty or non-canonical asset_id: {value!r}"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ExtractionError(
            f"embedded asset_id contains a control character: {value!r}"
        )
    return value


def _normalized_asset_output_path(value: Any) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ExtractionError(
            f"embedded asset has an empty or non-canonical output path: {value!r}"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ExtractionError(
            f"embedded asset output path contains a control character: {value!r}"
        )
    normalized = value.replace("\\", "/")
    if normalized.startswith("/"):
        raise ExtractionError(f"embedded asset output path must be relative: {value!r}")
    parts = tuple(normalized.split("/"))
    if any(not part or part in {".", ".."} for part in parts):
        raise ExtractionError(
            f"embedded asset output path contains an unsafe segment: {value!r}"
        )
    for part in parts:
        if (
            part != part.strip()
            or part.endswith(".")
            or any(character in '<>:"|?*' for character in part)
            or part.split(".", 1)[0].upper() in WINDOWS_DEVICE_NAMES
        ):
            raise ExtractionError(
                f"embedded asset output path is not portable: {value!r}"
            )
    return "/".join(parts), parts


def _asset_identity_keys(
    assets: Iterable[Mapping[str, Any]], *, reserved_paths: Iterable[str] = ()
) -> tuple[set[str], set[str]]:
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for index, asset in enumerate(assets, start=1):
        if not isinstance(asset, Mapping):
            raise ExtractionError(f"asset registry entry {index} is not an object")
        asset_id = _normalized_asset_id(asset.get("asset_id"))
        relative, _ = _normalized_asset_output_path(asset.get("output_path"))
        path_key = relative.casefold()
        if asset_id in seen_ids:
            raise ExtractionError(f"duplicate asset_id across extraction sources: {asset_id!r}")
        if path_key in seen_paths:
            raise ExtractionError(
                f"duplicate output path across extraction sources: {relative!r}"
            )
        seen_ids.add(asset_id)
        seen_paths.add(path_key)
    for value in reserved_paths:
        relative, _ = _normalized_asset_output_path(value)
        path_key = relative.casefold()
        if path_key in seen_paths:
            raise ExtractionError(
                f"asset output conflicts with a preserved supplementary file: {relative!r}"
            )
        seen_paths.add(path_key)
    return seen_ids, seen_paths


def _reject_existing_asset_outputs(
    assets: Iterable[Mapping[str, Any]], extraction_root: Path
) -> None:
    for asset in assets:
        asset_id = _normalized_asset_id(asset.get("asset_id"))
        relative, parts = _normalized_asset_output_path(asset.get("output_path"))
        try:
            destination = ensure_within(
                extraction_root.joinpath(*parts), extraction_root, require_exists=False
            )
            reject_reparse_chain(destination, extraction_root)
        except (FileNotFoundError, OSError, UnsafePathError) as exc:
            raise ExtractionError(
                f"asset output for {asset_id!r} is unsafe: {relative!r}"
            ) from exc
        if os.path.lexists(destination):
            raise ExtractionError(
                f"asset output for {asset_id!r} would overwrite an existing file: {relative!r}"
            )


def _prepare_supplement_assets(
    supplements: Iterable[Any], extraction_root: Path
) -> list[dict[str, Any]]:
    """Normalize parser assets and bind them to the files actually written."""

    prepared: list[dict[str, Any]] = []
    for supplement in supplements:
        normalized_for_supplement: list[dict[str, Any]] = []
        for index, raw in enumerate(supplement.assets, start=1):
            if not isinstance(raw, Mapping):
                raise ExtractionError(
                    f"supplement {supplement.supplement_id!r} asset {index} is not an object"
                )
            value = dict(raw)
            asset_id = _normalized_asset_id(value.get("asset_id"))
            relative, parts = _normalized_asset_output_path(
                value.get("output_path", value.get("path"))
            )
            try:
                destination = ensure_within(
                    extraction_root.joinpath(*parts), extraction_root
                )
                reject_reparse_chain(destination, extraction_root)
            except (FileNotFoundError, OSError, UnsafePathError) as exc:
                raise ExtractionError(
                    f"supplement asset {asset_id!r} is missing or unsafe: {relative!r}"
                ) from exc
            if is_reparse_point(destination) or not destination.is_file():
                raise ExtractionError(
                    f"supplement asset {asset_id!r} is not a regular file: {relative!r}"
                )
            actual_sha256 = sha256_file(destination)
            declared_sha256 = value.get("sha256")
            if declared_sha256 is not None and declared_sha256 != actual_sha256:
                raise ExtractionError(
                    f"supplement asset {asset_id!r} hash does not match its materialized file"
                )
            ocr_performed = value.get("ocr_performed", False)
            if not isinstance(ocr_performed, bool):
                raise ExtractionError(
                    f"supplement asset {asset_id!r} ocr_performed must be boolean"
                )
            value.pop("path", None)
            value.update(
                {
                    "schema_version": "1.0",
                    "asset_id": asset_id,
                    "category": str(value.get("category") or value.get("kind") or "asset"),
                    "output_path": relative,
                    "supplement_id": supplement.supplement_id,
                    "source_path": supplement.source.relative_path,
                    "source_sha256": supplement.source.sha256,
                    "sha256": actual_sha256,
                    "bytes": destination.stat().st_size,
                    "ocr_performed": ocr_performed,
                }
            )
            normalized_for_supplement.append(value)
            prepared.append(value)
        supplement.assets = normalized_for_supplement
    return prepared


def _attach_assets(
    article: Any, supplements: list[Any], assets: list[dict[str, Any]]
) -> None:
    output_by_id = {asset["asset_id"]: asset["output_path"] for asset in assets}

    def attach_table_assets(table: Any) -> None:
        table.image_path = (
            output_by_id.get(table.table_id)
            if table.requires_source_image
            else None
        )
        table.structure_assets = {
            str(asset["compound_id"]): str(asset["output_path"])
            for asset in assets
            if asset.get("category") == "table_cell"
            and asset.get("parent_table_id") == table.table_id
            and asset.get("compound_id")
        }

    for figure in article.figures:
        figure.output_path = output_by_id.get(figure.figure_id)
    for table in article.tables:
        attach_table_assets(table)
    for supplement in supplements:
        for figure in supplement.figures:
            figure.output_path = output_by_id.get(figure.figure_id)
        for table in supplement.tables:
            attach_table_assets(table)


def _record_missing_asset_warnings(article: Any, supplements: list[Any]) -> None:
    for figure in article.figures:
        if figure.kind in {"figure", "scheme"} and not figure.output_path:
            article.warnings.append(
                {
                    "schema_version": "1.0",
                    "code": "main_figure_asset_missing",
                    "severity": "scientific",
                    "message": f"No local asset is attached for {figure.label}.",
                    "source_path": figure.source_path,
                    "source_locator": figure.source_locator,
                }
            )
    for table in article.tables:
        if table.requires_source_image and not table.image_path:
            article.warnings.append(
                {
                    "schema_version": "1.0",
                    "code": "main_table_source_image_missing",
                    "severity": "scientific",
                    "message": f"No source rendering is attached for {table.label}.",
                    "source_path": table.source_path,
                    "source_locator": table.source_locator,
                }
            )
    for supplement in supplements:
        for figure in supplement.figures:
            if not figure.output_path:
                article.warnings.append(
                    {
                        "schema_version": "1.0",
                        "code": "supplement_figure_asset_missing",
                        "severity": "scientific",
                        "message": f"No local asset is attached for {figure.label}.",
                        "source_path": figure.source_path,
                        "source_locator": figure.source_locator,
                    }
                )
        for table in supplement.tables:
            if table.requires_source_image and not table.image_path:
                article.warnings.append(
                    {
                        "schema_version": "1.0",
                        "code": "supplement_table_source_image_missing",
                        "severity": "scientific",
                        "message": f"No source rendering is attached for {table.label}.",
                        "source_path": table.source_path,
                        "source_locator": table.source_locator,
                    }
                )


def _verify_pdf_text(repository_root: Path, sources: list[SourceFile]) -> list[dict[str, Any]]:
    inspections: list[dict[str, Any]] = []
    for source in sources:
        if source.detected_format != "application/pdf":
            continue
        inspection = inspect_pdf_native_text(repository_root, source.relative_path)
        inspections.append(
            {
                "source_path": source.relative_path,
                "page_count": inspection["page_count"],
                "pages_with_native_text": sum(
                    1 for page in inspection["pages"] if page["has_native_text"]
                ),
                "pages_without_native_text": [
                    page["page"]
                    for page in inspection["pages"]
                    if not page["has_native_text"]
                ],
            }
        )
    return inspections


def _validate_crop_sources(
    crop_specs: list[dict[str, Any]], sources: list[SourceFile]
) -> None:
    allowed = {
        source.relative_path: source
        for source in sources
        if source.role in {"main_pdf", "supplement"}
        and source.detected_format == "application/pdf"
    }
    for index, spec in enumerate(crop_specs, start=1):
        raw = spec.get("source_path", spec.get("source", ""))
        normalized = str(raw).replace("\\", "/")
        source = allowed.get(normalized)
        if source is None:
            raise ExtractionError(
                f"PDF crop {index} source is not a discovered PDF for this record: {raw!r}"
            )
        declared_hash = spec.get("source_sha256")
        if declared_hash is not None and (
            not isinstance(declared_hash, str)
            or declared_hash.casefold() != source.sha256.casefold()
        ):
            raise ExtractionError(
                f"PDF crop {index} source_sha256 does not match {normalized!r}"
            )


def _apply_main_pdf_visual_additions(
    article: Any,
    crop_specs: list[dict[str, Any]],
    sources: list[SourceFile],
) -> None:
    """Represent reviewed main-PDF visuals omitted by a primary HTML archive.

    An HTML-primary record can legitimately have a graphical abstract or other
    authored visual that exists only in the accompanying PDF.  A main-PDF crop
    already matching an HTML figure merely replaces its pixels.  When no
    semantic figure exists, require an exact source snapshot, reviewed caption,
    and reviewer rationale before adding the visual to canonical content.
    """

    source_by_path = {
        source.relative_path: source
        for source in sources
        if source.role == "main_pdf" and source.detected_format == "application/pdf"
    }
    existing_ids = {figure.figure_id for figure in article.figures}
    for index, spec in enumerate(crop_specs, start=1):
        category = str(spec.get("category", "")).strip().casefold()
        if category not in {"figure", "scheme", "graphical_abstract"}:
            continue
        source_path = str(spec.get("source_path", spec.get("source", ""))).replace(
            "\\", "/"
        )
        if source_path not in source_by_path:
            continue
        figure_id = str(spec.get("asset_id", "")).strip()
        if figure_id in existing_ids:
            continue
        source = source_by_path[source_path]
        source_sha256 = str(spec.get("source_sha256", "")).strip().casefold()
        label = str(spec.get("label", "")).strip()
        caption_plain = str(spec.get("caption_reviewed_value", "")).strip()
        caption_markdown = str(
            spec.get("caption_reviewed_markdown", caption_plain)
        ).strip()
        caption_page = spec.get("caption_page", spec.get("page"))
        caption_box = spec.get("caption_box")
        visual_page = spec.get("page")
        visual_box = spec.get("box")
        reason = str(spec.get("reason", "")).strip()
        evidence = str(spec.get("evidence", "")).strip()
        boxes = (caption_box, visual_box)
        if (
            not figure_id
            or source.sha256.casefold() != source_sha256
            or not label
            or not caption_plain
            or not caption_markdown
            or isinstance(caption_page, bool)
            or not isinstance(caption_page, int)
            or caption_page < 1
            or isinstance(visual_page, bool)
            or not isinstance(visual_page, int)
            or visual_page < 1
            or any(
                not isinstance(box, list)
                or len(box) != 4
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    for value in box
                )
                or float(box[0]) >= float(box[2])
                or float(box[1]) >= float(box[3])
                for box in boxes
            )
            or not reason
            or not evidence
        ):
            raise ExtractionError(
                f"main-PDF visual crop {index} requires complete reviewed evidence"
            )
        figure = FigureItem(
            figure_id=figure_id,
            source_id=None,
            label=label,
            kind=category,
            caption_markdown=caption_markdown,
            caption_plain=caption_plain,
            source_path=source_path,
            source_locator=(
                f"PDF page {visual_page}, visual box "
                f"[{', '.join(f'{float(value):.2f}' for value in visual_box)}]; "
                f"page {caption_page}, caption box "
                f"[{', '.join(f'{float(value):.2f}' for value in caption_box)}]"
            ),
        )
        article.figures.append(figure)
        existing_ids.add(figure_id)


def _effective_crop_specs(
    article: Any,
    crop_specs: list[dict[str, Any]],
    supplements: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    """Apply table-output policy before any crop directories or files exist.

    A full-table rendering is useful only when pixels are the table's
    authoritative representation.  Explicit ``table_cell`` crops remain
    independent: they preserve meaningful graphical cells in an otherwise
    HTML-native table and create their subdirectory only when such crops exist.
    """

    article_tables = {table.table_id: table for table in article.tables}
    supplement_tables = {
        table.table_id: table
        for supplement in supplements
        for table in supplement.tables
    }
    duplicate_table_ids = set(article_tables) & set(supplement_tables)
    if duplicate_table_ids:
        duplicate = sorted(duplicate_table_ids)[0]
        raise ExtractionError(
            f"duplicate main/supplement table identity before PDF crops: {duplicate!r}"
        )
    tables = {**article_tables, **supplement_tables}
    selected: list[dict[str, Any]] = []
    for index, spec in enumerate(crop_specs, start=1):
        category = str(spec.get("category", "")).strip().casefold()
        asset_id = str(spec.get("asset_id") or "")
        if category == "table" or (
            category != "table_cell" and asset_id in tables
        ):
            table_id = str(spec.get("parent_table_id") or asset_id)
            table = tables.get(table_id)
            if table is None:
                raise ExtractionError(
                    f"PDF crop {index} identifies unknown table {table_id!r}"
                )
            if not table.requires_source_image:
                continue
        elif category == "table_cell":
            table_id = str(spec.get("parent_table_id") or "")
            if table_id not in tables:
                raise ExtractionError(
                    f"PDF crop {index} identifies unknown parent table {table_id!r}"
                )
        selected.append(spec)
    return selected


def _verify_source_snapshots(
    repository_root: Path,
    sources: list[SourceFile],
    override_path: Path | None,
    override_hash: str | None,
) -> None:
    """Fail closed if any authoritative input changed during extraction."""

    for source in sources:
        ensure_within(source.path, repository_root)
        reject_reparse_chain(source.path, repository_root)
        if not source.path.is_file():
            raise ExtractionError(f"source disappeared during extraction: {source.relative_path}")
        if source.path.stat().st_size != source.size or sha256_file(source.path) != source.sha256:
            raise ExtractionError(f"source changed during extraction: {source.relative_path}")
    if override_path is not None and override_hash is not None:
        ensure_within(override_path, repository_root)
        reject_reparse_chain(override_path, repository_root)
        if not override_path.is_file() or sha256_file(override_path) != override_hash:
            raise ExtractionError("private extraction override changed during extraction")


def extract_record(
    repository_root: Path,
    record_id: str,
    *,
    run_id: str,
    override_path: Path | None = None,
    dpi: int = 300,
) -> ExtractionResult:
    """Create, validate, and atomically publish one candidate inside staging.

    This never promotes into a record's live ``extraction/`` directory.
    """

    record_id = validate_record_id(record_id)
    run_id = validate_run_id(run_id)
    repository_root = repository_root.resolve(strict=True)
    private_root = ensure_within(repository_root / "papers (private)", repository_root)
    record_root = ensure_within(private_root / record_id, private_root)
    reject_reparse_chain(record_root, private_root)
    staging_root = ensure_within(private_root / "staging", private_root)
    reject_reparse_chain(staging_root, private_root)
    record_staging = staging_root / record_id
    record_staging.mkdir(parents=True, exist_ok=True)
    reject_reparse_chain(record_staging, staging_root)

    run_root = record_staging / run_id
    if run_root.exists():
        raise ExtractionError(f"staged run already exists and will not be overwritten: {run_root}")
    if override_path is None:
        override_path = _default_override_path(record_root)
    if override_path is not None:
        override_path = _resolve_override_path(
            override_path,
            repository_root=repository_root,
            record_root=record_root,
            record_staging=record_staging,
        )

    sources = discover_sources(repository_root, record_id)
    override, override_text, override_hash = _load_override(override_path, record_id)
    fingerprint = source_fingerprint(sources, override_hash)
    metadata_source = _source(sources, "public_metadata")
    html_sources = [source for source in sources if source.role == "main_html"]
    pdf_sources = [source for source in sources if source.role == "main_pdf"]
    if len(html_sources) > 1 or len(pdf_sources) != 1:
        raise ExtractionError(
            "expected exactly one main PDF and at most one main HTML source; "
            f"found {len(pdf_sources)} PDF and {len(html_sources)} HTML"
        )
    html_source = html_sources[0] if html_sources else None
    pdf_source = pdf_sources[0]
    _validate_text_repair_sources(
        list(override.get("text_repairs", [])),
        sources,
        html_source if html_source is not None else pdf_source,
    )
    metadata: RecordMetadata = load_record_metadata(metadata_source.path, record_id)
    requested_crop_specs = list(override.get("pdf_crops", []))
    _validate_crop_sources(requested_crop_specs, sources)
    if html_source is not None:
        article = extract_html(
            html_source.path,
            html_source.relative_path,
            override.get("text_repairs", []),
        )
        article = _recover_missing_html_body(
            article, html_source, pdf_source, metadata,
            override.get("pdf_text", {}), requested_crop_specs, override.get("pdf_ocr"),
        )
    else:
        ocr_config = override.get("pdf_ocr")
        text_document = None
        text_extraction = None
        if ocr_config is not None:
            text_document, text_extraction = _run_pdf_ocr(
                pdf_source.path,
                pdf_source.relative_path,
                ocr_config,
                requested_crop_specs,
            )
        article = extract_pdf_article(
            pdf_source.path,
            pdf_source.relative_path,
            metadata,
            override.get("pdf_text", {}),
            crop_specs=requested_crop_specs,
            text_repairs=override.get("text_repairs", []),
            text_document=text_document,
            text_extraction=text_extraction,
        )
    _apply_front_matter_overrides(
        article, list(override.get("front_matter", [])), sources
    )
    _apply_section_equation_additions(
        article,
        list(override.get("section_equation_additions", [])),
        sources,
    )
    _apply_main_table_overrides(
        article, list(override.get("table_overrides", [])), sources
    )
    _apply_reference_entries(
        article, list(override.get("reference_entries", [])), sources
    )
    _apply_supporting_information_additions(
        article,
        list(override.get("supporting_information_additions", [])),
        sources,
    )
    _apply_main_pdf_visual_additions(article, requested_crop_specs, sources)
    _apply_rich_text_overrides(
        article,
        list(override.get("rich_text_overrides", [])),
        sources,
    )
    _project_pdf_scientific_markup(article, pdf_source.relative_path)
    source_anomalies = _validated_source_anomalies(
        list(override.get("source_anomalies", [])), sources
    )
    _reconcile_article_title(
        article, metadata, source_html=html_source.path if html_source is not None else None
    )
    if html_source is not None:
        # HTML/PDF alignment applies to the main article PDF only.  A
        # supplementary PDF can legitimately contain image-only figure or
        # scanned-document pages; those are handled by the supplement pipeline
        # and must not make an otherwise text-native main article fail this
        # gate.
        pdf_inspections = _verify_pdf_text(repository_root, pdf_sources)
        reviewed_image_only_pages = {
            row.get("page")
            for row in article.page_diagnostics
            if row.get("kind") == "reviewed_image_only_page"
            and row.get("status") == "reviewed"
            and row.get("source_path") == pdf_source.relative_path
        }
        reviewed_ocr_pages = {
            row.get("page")
            for row in article.page_diagnostics
            if row.get("kind") == "ocr_region"
            and row.get("source_review_status") == "reviewed"
            and article.text_extraction.get("source_path")
            == pdf_source.relative_path
            and article.text_extraction.get("method")
            == "deterministic-local-region-ocr"
        }
        if any(
            set(item.get("pages_without_native_text", []))
            - reviewed_image_only_pages
            - reviewed_ocr_pages
            for item in pdf_inspections
        ):
            raise ExtractionError(
                "the HTML-primary record contains a PDF page without native text; "
                "HTML/PDF reconciliation for that case is not enabled"
            )
        article.warnings.append(
            {
                "schema_version": "1.0",
                "code": "automated_pdf_html_alignment_not_implemented",
                "severity": "structural",
                "message": (
                    "PDF native-text availability is checked and reviewed overrides are applied, "
                    "but automated element-by-element PDF/HTML alignment is not implemented yet."
                ),
                "source_path": html_source.relative_path,
            }
        )

    build_root = _create_build_root(record_staging, run_id)
    extraction_root = build_root / "extraction"
    diagnostic_root = build_root / "extraction_diagnostic"
    extraction_root.mkdir()
    try:
        supplements = extract_supplements(
            sources,
            extraction_root,
            override.get("supplement_exclusions", []),
            pdf_text_config=override.get("pdf_text", {}),
            pdf_crop_specs=requested_crop_specs,
            standalone_image_specs=override.get("standalone_image_figures", []),
            standalone_image_table_specs=override.get(
                "standalone_image_tables", []
            ),
            docx_figure_crop_specs=override.get("docx_figure_crops", []),
        )
        _bind_html_supplement_figures(article, supplements)
        _remove_duplicate_main_figures_promoted_from_supplements(
            article, supplements
        )
        _apply_supplement_table_overrides(
            supplements,
            list(override.get("supplement_table_overrides", [])),
            sources,
        )
        for spec in override.get("standalone_image_tables", []):
            asset_id = str(spec.get("asset_id", "")).strip()
            source_path = str(spec.get("source_path", "")).replace("\\", "/")
            matches = [
                (supplement, asset)
                for supplement in supplements
                if supplement.source.relative_path == source_path
                for asset in supplement.assets
                if str(asset.get("asset_id", "")) == asset_id
                and str(asset.get("category", "")).casefold()
                == "supplement_table"
                and any(table.table_id == asset_id for table in supplement.tables)
            ]
            if len(matches) != 1:
                raise ExtractionError(
                    "standalone_image_tables reviewed asset was not promoted "
                    f"exactly once: {asset_id or source_path}"
                )
        _remove_redundant_supplement_table_titles(supplements)
        _apply_supplement_block_additions(
            supplements,
            list(override.get("supplement_block_additions", [])),
            sources,
        )
        _apply_supplement_figure_overrides(
            supplements,
            list(override.get("supplement_figure_overrides", [])),
            sources,
        )
        _remove_redundant_supporting_figure_captions(article, supplements)
        supplement_assets = _prepare_supplement_assets(
            supplements, extraction_root
        )
        supplement_paths = [supplement.copied_path for supplement in supplements]
        reserved_output_paths = [*supplement_paths, "record.json"]
        crop_specs = _effective_crop_specs(
            article, requested_crop_specs, supplements
        )
        reviewed_html_assets, crop_specs = select_reviewed_html_assets(
            override.get("reviewed_html_assets", []), crop_specs, sources, supplements
        )
        embedded_assets = _materialize_embedded_assets(
            _embedded_assets_after_pdf_overrides(
                [*article.embedded_assets, *reviewed_html_assets], crop_specs
            ),
            extraction_root,
            sources,
            reserved_assets=supplement_assets,
            reserved_paths=reserved_output_paths,
        )
        _asset_identity_keys(
            [*supplement_assets, *embedded_assets, *crop_specs],
            reserved_paths=reserved_output_paths,
        )
        _reject_existing_asset_outputs(crop_specs, extraction_root)
        rendered_assets = render_pdf_crops(
            repository_root, extraction_root, crop_specs, dpi=dpi
        )
        assets = [
            *embedded_assets,
            *supplement_assets,
            *_enrich_assets(rendered_assets, crop_specs),
        ]
        _asset_identity_keys(assets, reserved_paths=reserved_output_paths)
        _attach_assets(article, supplements, assets)
        _record_missing_asset_warnings(article, supplements)
        # record.json is the sole canonical content document for new runs.
        # Tables live inside it; only binary/large assets remain as sidecars.
        rendered = write_record_json(
            metadata, article, supplements, assets, extraction_root
        )
        expected_counts = override.get("expected_counts")
        write_diagnostics(
            diagnostic_root,
            extraction_root,
            record_id=record_id,
            run_id=run_id,
            fingerprint=fingerprint,
            sources=sources,
            article=article,
            supplements=supplements,
            assets=assets,
            coverage=rendered.coverage,
            override_text=override_text,
            source_anomalies=source_anomalies,
            expected_counts=expected_counts,
        )
        validation = validate_candidate(
            extraction_root,
            diagnostic_root,
            expected_title=metadata.title,
            expected_counts=expected_counts,
            expected_metadata=metadata.as_dict(),
        )
        status = write_validation_result(
            diagnostic_root, validation.findings, validation.counts
        )
        _verify_source_snapshots(
            repository_root, sources, override_path, override_hash
        )
        os.replace(build_root, run_root)
    except BaseException:
        # The directory was created by this invocation, is beneath the fixed
        # private staging root, and has never been exposed as a completed run.
        try:
            ensure_within(build_root, record_staging)
            reject_reparse_chain(build_root, record_staging)
            shutil.rmtree(build_root)
        except (OSError, UnsafePathError):
            pass
        raise

    final_extraction = run_root / "extraction"
    final_diagnostic = run_root / "extraction_diagnostic"
    return ExtractionResult(
        record_id=record_id,
        run_id=run_id,
        run_root=run_root,
        extraction_root=final_extraction,
        diagnostic_root=final_diagnostic,
        source_fingerprint=fingerprint,
        status=status,
        finding_count=len(validation.findings),
    )
