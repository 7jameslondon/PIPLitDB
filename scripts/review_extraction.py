#!/usr/bin/env python3
"""Prepare, freeze and check a staged extraction; never approve scientific work.

This opt-in runner composes the existing extractor, validator, full test runner,
PDF renderer, viewer checker and promotion evidence checks. Original sources and
canonical outputs remain available. All additional output stays in diagnostics.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

if __package__:
    from . import check_extraction_completion as completion
    from .extraction import promotion
    from .extraction.coordination import _files, _runtime, _safe
    from .extraction.discovery import discover_sources
    from .extraction.metadata import load_record_metadata
    from .extraction.paths import atomic_write_json, sha256_file, validate_record_id, validate_run_id
    from .extraction.pdf_extractor import inspect_pdf_native_text, render_pdf_crops
    from .extraction.pipeline import extract_record
    from .extraction.validation import validate_candidate
    from .private_directory import create_unique_private_directory
else:
    import check_extraction_completion as completion
    from extraction import promotion
    from extraction.coordination import _files, _runtime, _safe
    from extraction.discovery import discover_sources
    from extraction.metadata import load_record_metadata
    from extraction.paths import atomic_write_json, sha256_file, validate_record_id, validate_run_id
    from extraction.pdf_extractor import inspect_pdf_native_text, render_pdf_crops
    from extraction.pipeline import extract_record
    from extraction.validation import validate_candidate
    from private_directory import create_unique_private_directory


STAGED_VIEWER_TIMEOUT_SECONDS = 600


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _json(path):
    return completion._json(path)


def _locations(root, record_id, run_id):
    root = root.resolve(strict=True)
    validate_record_id(record_id)
    validate_run_id(run_id)
    private = _safe(root / "papers (private)", root)
    run = _safe(private / "staging" / record_id / run_id, private, exists=False)
    package = _safe(private / "diagnostics/review-runner" / record_id / run_id, private, exists=False)
    return root, private, run, package


def _source_key(root, record_id, dpi):
    return {"sources": [source.as_dict() for source in discover_sources(root, record_id)],
            "page_dpi": dpi, "runner_sha256": sha256_file(Path(__file__)),
            "renderer_sha256": sha256_file(Path(__file__).parent / "extraction/pdf_extractor.py"),
            "pdfium": importlib.metadata.version("pypdfium2"),
            "pillow": importlib.metadata.version("Pillow")}


def _source_views(root, record_id, output, dpi, reuse=None):
    key = _source_key(root, record_id, dpi)
    if reuse:
        receipt_path = _safe(reuse.absolute(), root / "papers (private)/diagnostics")
        receipt = _json(receipt_path)
        directory = receipt_path.parent
        if receipt["inputs"] != key:
            raise ValueError("source views have different source bytes, generator or settings")
        actual = _files(directory, directory)
        actual.pop(receipt_path.name)
        if actual != receipt["artifacts"]:
            raise ValueError("saved source views changed; regenerate before inspecting them")
        return receipt_path
    output.mkdir()
    index = []
    for number, source in enumerate(key["sources"], 1):
        row = {"source": source["path"], "format": source["detected_format"], "views": []}
        if source["detected_format"] == "application/pdf":
            document = inspect_pdf_native_text(root, source["path"])
            text_name = f"source-{number:03d}-native.json"
            atomic_write_json(output / text_name, document)
            specs = [{"source_path": source["path"], "asset_id": f"source-{number}-page-{page['page']}",
                      "page": page["page"], "box": [0, 0, page["width_points"], page["height_points"]],
                      "output_path": f"source-{number:03d}-page-{page['page']:03d}.png"}
                     for page in document["pages"]]
            render_pdf_crops(root, output, specs, dpi=dpi)
            row["native_text"] = text_name
            row["views"] = [spec["output_path"] for spec in specs]
            row["caution"] = "Native text is uncorrected PDFium output. Inspect every original page; use raw sources for ambiguities."
        else:
            row["inspection"] = "Inspect the original file and native package/HTML structure with format-appropriate tools."
        index.append(row)
    atomic_write_json(output / "index.json", {"sources": index, "source_inspection": "pending"})
    if key != _source_key(root, record_id, dpi):
        raise ValueError("source inputs changed during preparation")
    receipt_path = output / "receipt.json"
    atomic_write_json(receipt_path, {"inputs": key, "artifacts": _files(output, output)})
    return receipt_path


def prepare(root, record_id, run_id, *, page_dpi=150, crop_dpi=300, reuse_sources=None):
    root, private, run, package = _locations(root, record_id, run_id)
    if run.exists():
        raise ValueError("candidate already exists; use a new unique run ID")
    if not 72 <= page_dpi <= 600 or not 72 <= crop_dpi <= 1200:
        raise ValueError("page DPI must be 72-600 and crop DPI 72-1200")
    package.parent.mkdir(parents=True, exist_ok=True)
    package.mkdir()
    sources = _source_views(root, record_id, package / "sources", page_dpi, reuse_sources)
    _safe(private / "staging", private, exists=False).mkdir(exist_ok=True)
    result = extract_record(root, record_id, run_id=run_id, dpi=crop_dpi)
    canonical = _json(result.extraction_root / "record.json")
    # A lossless compact JSON view, not a prose summary or filtered content set.
    (package / "candidate-reading.json").write_text(
        json.dumps(canonical, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    receipt = {"record_id": record_id, "run_id": run_id, "created_utc": _stamp(),
               "crop_dpi": crop_dpi, "page_dpi": page_dpi,
               "source_receipt": completion._file(sources),
               "candidate": completion._file(result.extraction_root / "record.json"),
               "reading_view": completion._file(package / "candidate-reading.json"),
               "run_root": str(run), "source_index": str(sources.parent / "index.json"),
               "finding_count": result.finding_count, "scientific_review": "pending"}
    atomic_write_json(package / "prepare.json", receipt)
    return {"status": "prepared_for_inspection", "receipt": str(package / "prepare.json"),
            "source_index": receipt["source_index"], "reading_view": receipt["reading_view"]["path"],
            "finding_count": result.finding_count, "run_root": str(run)}


def _state(root, record_id, run):
    diagnostic = run / "extraction_diagnostic"
    promotion._validate_tree(run, root / "papers (private)", "candidate")
    manifest = _json(diagnostic / "manifest.json")
    if manifest.get("run_id") != run.name or manifest.get("record_id") != record_id:
        raise ValueError("candidate manifest identifies another run or record")
    promotion._verify_current_inputs(root, root / "papers (private)" / record_id, record_id,
                                    diagnostic, manifest, _json(diagnostic / "sources.json"))
    core = _files(run, diagnostic)
    core = {name: value for name, value in core.items()
            if not name.startswith(("extraction_diagnostic/reviews/", "extraction_diagnostic/test-logs/"))}
    return {"runtime": _runtime(root), "canonical": _files(run, run / "extraction"),
            "diagnostics": core, "source_fingerprint": manifest["source_fingerprint"]}


def _validation(root, record_id, run):
    metadata = load_record_metadata(root / "database/records" / f"{record_id}.yaml", record_id)
    report = validate_candidate(run / "extraction", run / "extraction_diagnostic",
                                expected_title=metadata.title, expected_metadata=metadata.as_dict())
    promotion._validate_stored_result(run / "extraction_diagnostic", report)
    allowed = promotion._standing_policy_acceptances(report)
    return report.as_dict(), list(allowed)


def _tests(root, output):
    command = [sys.executable, "-X", "utf8", str(root / "scripts/run_tests.py"), "--log-dir", str(output)]
    with (output.parent / "test-runner.log").open("xb") as log:
        process = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT, check=False)
    paths = list(output.glob("*/report.json"))
    if process.returncode or len(paths) != 1:
        raise ValueError(f"full tests did not pass; inspect {output.parent / 'test-runner.log'}")
    return completion._test_evidence(paths[0], root / "papers (private)")


def _staged_viewer(root, record_id, run, output, browser_channel):
    config = output / "viewer-config.json"
    atomic_write_json(config, {"repository_root": str(root), "record_id": record_id,
                              "staged_run_id": run.name, "output": str(output / "viewer"),
                              "browser_channel": browser_channel})
    # The existing read-only browser adapter mounts the exact staged files;
    # no live promotion or deeply nested temporary repository is necessary.
    # Large lossless scientific figures and embedded supplement packages can
    # take several minutes to marshal into the browser's read-only file-handle
    # adapter. Keep a finite limit, but allow the same complete checks to run
    # without misclassifying a slow, asset-heavy record as a browser failure.
    result = completion._viewer(
        config,
        output,
        completion._node_executable(None),
        STAGED_VIEWER_TIMEOUT_SECONDS,
                                runtime_parent=_safe(root / "papers (private)/diagnostics", root))
    return result, _files(output, output / "viewer")


def freeze(root, record_id, run_id, *, browser_channel=None):
    root, private, run, package = _locations(root, record_id, run_id)
    prepared = _json(_safe(package / "prepare.json", private))
    for key in ("candidate", "reading_view", "source_receipt"):
        path = _safe(Path(prepared[key]["path"]), private)
        if completion._file(path) != prepared[key]:
            raise ValueError(f"prepared {key} changed; rebuild with a new run ID")
    _source_views(root, record_id, package / "unused", prepared["page_dpi"],
                  Path(prepared["source_receipt"]["path"]))
    before = _state(root, record_id, run)
    output = create_unique_private_directory(package, "freeze-")
    validation, accepted = _validation(root, record_id, run)
    atomic_write_json(output / "validation.json", validation)
    tests = _tests(root, output / "tests")
    viewer, artifacts = _staged_viewer(root, record_id, run, output, browser_channel)
    if before != _state(root, record_id, run):
        raise ValueError("inputs, code or candidate changed during freeze")
    receipt = {"record_id": record_id, "run_id": run_id, "created_utc": _stamp(),
               "status": "frozen_for_review", "state": before, "tests": tests,
               "preparation": completion._file(package / "prepare.json"),
               "viewer": viewer, "viewer_artifacts": artifacts,
               "accepted_finding_codes": accepted, "scientific_review": "pending",
               "visual_inspection": "pending"}
    atomic_write_json(output / "freeze.json", receipt)
    return {"status": receipt["status"], "freeze": str(output / "freeze.json"),
            "test_counts": tests["counts"], "viewer_report": viewer["report"]["path"],
            "accepted_finding_codes": accepted, "scientific_review": "pending"}


def handoff(root, record_id, run_id, *, freeze_path, repro_run_id):
    root, private, run, package = _locations(root, record_id, run_id)
    validate_run_id(repro_run_id)
    frozen_path = _safe(freeze_path.absolute(), package)
    frozen = _json(frozen_path)
    if (frozen["record_id"], frozen["run_id"]) != (record_id, run_id):
        raise ValueError("freeze belongs to another candidate")
    before = _state(root, record_id, run)
    if before != frozen["state"]:
        raise ValueError("frozen candidate, code or inputs changed; repeat affected reviews")
    prepare_path = _safe(Path(frozen["preparation"]["path"]), package)
    if completion._file(prepare_path) != frozen["preparation"]:
        raise ValueError("preparation receipt changed after freeze")
    prepared = _json(prepare_path)
    for key in ("candidate", "reading_view", "source_receipt"):
        path = _safe(Path(prepared[key]["path"]), private)
        if completion._file(path) != prepared[key]:
            raise ValueError(f"prepared {key} changed after freeze")
    _source_views(root, record_id, package / "unused", prepared["page_dpi"],
                  Path(prepared["source_receipt"]["path"]))
    tests = completion._test_evidence(Path(frozen["tests"]["report"]["path"]), private)
    if tests != frozen["tests"]:
        raise ValueError("test evidence changed after freeze")
    if _files(frozen_path.parent, frozen_path.parent / "viewer") != frozen["viewer_artifacts"]:
        raise ValueError("viewer evidence changed after freeze")
    _, accepted = _validation(root, record_id, run)
    reviews = promotion._review_evidence(run / "extraction_diagnostic", accepted)
    extract_record(root, record_id, run_id=repro_run_id, dpi=prepared["crop_dpi"])
    _validation(root, record_id, private / "staging" / record_id / repro_run_id)
    reproducibility = promotion._verify_reproducibility_run(
        private / "staging", record_id, run_id, repro_run_id, run / "extraction",
        _json(run / "extraction_diagnostic/manifest.json"))
    if before != _state(root, record_id, run):
        raise ValueError("inputs or candidate changed during reproduction")
    output = create_unique_private_directory(package, "handoff-")
    receipt = {"record_id": record_id, "run_id": run_id, "created_utc": _stamp(),
               "status": "machine_checks_passed", "freeze": completion._file(frozen_path),
               "reviews": reviews, "review_files": _files(run, run / "extraction_diagnostic/reviews"),
               "reproducibility": reproducibility, "accepted_finding_codes": accepted,
               "primary_source_and_visual_judgment": "required", "promoted": False}
    atomic_write_json(output / "handoff.json", receipt)
    return {key: receipt[key] for key in ("status", "record_id", "run_id", "reproducibility", "promoted")} | {
        "handoff": str(output / "handoff.json"), "primary_source_and_visual_judgment": "required"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "freeze", "handoff"):
        item = sub.add_parser(name)
        item.add_argument("record_id")
        item.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
        item.add_argument("--run-id", required=True)
        if name == "prepare":
            item.add_argument("--page-dpi", type=int, default=150)
            item.add_argument("--crop-dpi", type=int, default=300)
            item.add_argument("--reuse-sources", type=Path)
        elif name == "freeze":
            item.add_argument("--browser-channel", choices=("chromium", "msedge", "chrome"))
        else:
            item.add_argument("--freeze", dest="freeze_path", type=Path, required=True)
            item.add_argument("--repro-run-id", required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    args["root"] = args.pop("repository_root")
    try:
        result = globals()[command](**args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)[:1200]}, ensure_ascii=True))
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
