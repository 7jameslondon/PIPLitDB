#!/usr/bin/env python3
"""Check an approved extraction, its saved evidence and the live viewer read-only.

Complete reports, browser output and screenshots stay in a new private directory.
Machine success still requires the primary agent's source/evidence judgment and
visual inspection; this command neither approves nor promotes an extraction.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback

if __package__:
    from .extraction.paths import ensure_within, reject_reparse_chain, sha256_file, validate_record_id
    from .extraction.promotion import cleanup_approved_staging, _review_evidence, STANDING_POLICY_APPROVAL
    from .private_directory import create_unique_private_directory
else:
    from extraction.paths import ensure_within, reject_reparse_chain, sha256_file, validate_record_id
    from extraction.promotion import cleanup_approved_staging, _review_evidence, STANDING_POLICY_APPROVAL
    from private_directory import create_unique_private_directory


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _write(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write("\n")


def _private_path(path: Path, boundary: Path, *, exists: bool = True) -> Path:
    # Check the original spelling before resolve() hides a symlink/junction.
    reject_reparse_chain(path.absolute(), boundary)
    return ensure_within(path, boundary, require_exists=exists)


def _file(path: Path) -> dict:
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _test_evidence(path: Path, private: Path) -> dict:
    path = _private_path(path, private)
    report = _json(path)
    counts = report.get("counts", {})
    names = ("tests_run", "passed", "skipped", "failures", "errors", "expected_failures", "unexpected_successes")
    if not isinstance(counts, dict) or any(type(counts.get(name)) is not int or counts[name] < 0 for name in names):
        raise ValueError("test report has missing or invalid counts")
    if (report.get("status") != "passed" or report.get("exit_code") != 0
            or counts["passed"] == 0 or counts["failures"] or counts["errors"]
            or counts["unexpected_successes"] or report.get("issues") != []
            or counts["tests_run"] != counts["passed"] + counts["skipped"] + counts["expected_failures"]):
        raise ValueError("test evidence is not a complete passing run")
    command = report.get("command", [])
    # Accept the runner's unfiltered default discovery, not a focused selection
    # that happens to have a passing status. Report currency remains agent judgment.
    if not isinstance(command, list) or "--worker" not in command:
        raise ValueError("expected a scripts/run_tests.py full-suite report")
    worker = command.index("--worker")
    if (worker == 0 or Path(command[worker - 1]).name != "run_tests.py"
            or len(command) <= worker + 1
            or Path(command[worker + 1]).resolve() != path
            or Path(report.get("working_directory", "")).resolve() != private.parent):
        raise ValueError("test report does not identify this repository and its own runner output")
    if command[worker + 2:] not in (["discover", "-s", "tests", "-v"], ["discover", "-s", "tests"]):
        raise ValueError("test report is filtered or is not the default full-suite discovery")
    log = report.get("log", {})
    log_path = _private_path(Path(log["path"]), private)
    actual_log = _file(log_path)
    if actual_log["sha256"] != log.get("sha256") or actual_log["bytes"] != log.get("bytes"):
        raise ValueError("test log bytes do not match the saved report")
    return {"report": _file(path), "log": actual_log, "counts": counts,
            "currency": "primary agent must confirm this run covers the final code"}


def _snapshot(root: Path, record_id: str) -> dict:
    record = root / "papers (private)" / record_id
    # Read-only completion must preserve sources, live artifacts and all metadata.
    files = [p for p in record.rglob("*") if p.is_file()]
    files += [root / "database/records" / f"{record_id}.yaml", root / "tmp/EXTRACTION_QUEUE.md"]
    for path in files:
        reject_reparse_chain(path, root)
    return {p.relative_to(root).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256_file(p)}
            for p in sorted(files)}


def _node_executable(explicit: str | None) -> str:
    if explicit:
        return explicit
    bundled = (Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin"
               / ("node.exe" if os.name == "nt" else "node"))
    if bundled.is_file():
        return str(bundled)
    found = shutil.which("node")
    if not found:
        raise ValueError("Node.js is unavailable; supply --node with an installed executable")
    return found


def _viewer(config_path: Path, output: Path, node: str, timeout: float,
            *, runtime_parent: Path | None = None) -> dict:
    script = Path(__file__).with_name("check_extraction_viewer.cjs")
    # Keep Playwright's temporary profile/artifacts under the same writable
    # private boundary as downloads. Restricted Windows system-temp ACLs can
    # otherwise make graceful browser cleanup retry for over a minute.
    if runtime_parent is None:
        scratch = output / "browser-runtime"
        scratch.mkdir()
    else:
        # Nested isolated workspaces can exceed Windows browser path limits.
        # The caller supplies a checked private diagnostics parent.
        scratch = create_unique_private_directory(runtime_parent, "browser-")
    environment = dict(os.environ)
    environment.update({key: str(scratch) for key in ("TEMP", "TMP", "TMPDIR")})
    with (output / "browser.log").open("xb") as log:
        process = subprocess.run([node, str(script), str(config_path)], stdout=log,
                                 stderr=subprocess.STDOUT, timeout=timeout, check=False, env=environment)
    report_path = output / "viewer/report.json"
    if process.returncode:
        raise RuntimeError(f"viewer check exited {process.returncode}; inspect browser.log and viewer/report.json")
    report = _json(report_path)
    if report.get("status") != "passed":
        raise RuntimeError("viewer check did not pass; inspect viewer/report.json")
    return {"report": _file(report_path), "counts": report["counts"],
            "screenshots": len(report["screenshots"]), "downloads": len(report["downloads"]),
            "duration_seconds": report["duration_seconds"], "visual_inspection": "pending"}


def check_completion(root: Path, record_id: str, *, test_report: Path, log_dir: Path,
                     node: str | None = None, playwright_module: str | None = None,
                     browser_channel: str | None = None, viewer_timeout: float = 180) -> tuple[dict, int]:
    root = root.resolve(strict=True)
    record_id = validate_record_id(record_id)
    private = _private_path(root / "papers (private)", root)
    diagnostic_parent = private / "diagnostics"
    _private_path(diagnostic_parent, private, exists=False).mkdir(exist_ok=True)
    log_dir = _private_path(log_dir, diagnostic_parent, exists=False)
    log_dir.mkdir(parents=True, exist_ok=True)
    output = create_unique_private_directory(log_dir, f"completion-{record_id}-")
    report = {"schema_version": "1.0", "record_id": record_id, "status": "failed",
              "started_utc": datetime.now(timezone.utc).isoformat(), "checks": {}}
    start = time.perf_counter()
    before = None
    exit_code = 2
    phase = "input_snapshot"
    try:
        before = _snapshot(root, record_id)
        _write(output / "input-snapshot.json", before)
        report["generating_tools"] = [_file(path) for path in (
            Path(__file__).resolve(), Path(__file__).with_name("check_extraction_viewer.cjs").resolve(),
            root / "extraction_viewer.html")]
        phase = "approved_live_integrity"
        # Reuse the finalizer's existing live validation with deletion disabled.
        cleanup = cleanup_approved_staging(root, record_id, remove=False)
        report["checks"][phase] = "passed"
        report["run_id"] = cleanup.approved_run_id
        if cleanup.staging_root.exists():
            raise ValueError("record staging remains; normal guarded finalization/cleanup is not complete")
        diagnostic = private / record_id / "extraction_diagnostic"
        approval = _json(diagnostic / "approval.json")
        phase = "review_evidence"
        if approval.get("approval_mode") != STANDING_POLICY_APPROVAL:
            raise ValueError("this workflow requires a standing-policy approval with five-role evidence")
        reviews = _review_evidence(diagnostic, approval["accepted_finding_codes"])
        recorded = approval.get("finalization_evidence", {})
        if any(recorded.get(key) != reviews[key] for key in reviews):
            raise ValueError("live review inventory differs from approval evidence")
        repro = recorded.get("reproducibility", {})
        if (not repro.get("run_id") or repro["run_id"] == cleanup.approved_run_id
                or not re.fullmatch(r"[0-9a-f]{64}", str(repro.get("manifest_sha256", "")))):
            raise ValueError("approval lacks distinct clean-rebuild evidence")
        report["evidence"] = {"approval": _file(diagnostic / "approval.json"),
                              "reviews": [_file(diagnostic / "reviews" / name) for name in reviews["review_files"]],
                              "review_roles": reviews["review_roles"], "reproducibility": repro,
                              "accepted_finding_codes": approval["accepted_finding_codes"]}
        report["checks"][phase] = "passed"
        phase = "test_evidence"
        report["tests"] = _test_evidence(test_report, private)
        report["checks"][phase] = "passed"
        phase = "queue"
        queue = (root / "tmp/EXTRACTION_QUEUE.md").read_text(encoding="utf-8")
        matches = re.findall(rf"^\d+\. \*\*`{record_id}`\*\* — (.*)$", queue, re.MULTILINE)
        if len(matches) != 1 or matches[0].strip() != "Extraction completed and approved.":
            raise ValueError("queue does not contain exactly one approved completion entry")
        report["checks"][phase] = "passed"
        phase = "viewer"
        config = {"repository_root": str(root), "record_id": record_id,
                  "output": str(output / "viewer"), "playwright_module": playwright_module,
                  "browser_channel": browser_channel or ("msedge" if os.name == "nt" else "chromium")}
        config_path = output / "viewer-config.json"
        _write(config_path, config)
        report["viewer"] = _viewer(config_path, output, _node_executable(node), viewer_timeout)
        report["checks"][phase] = "passed"
        report["status"] = "machine_checks_passed"
        exit_code = 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        report["checks"][phase] = "failed"
        report["error"] = str(exc)
        (output / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        if before is not None:
            try:
                unchanged = (_snapshot(root, record_id) == before
                    and all(_file(Path(entry["path"])) == entry for entry in report.get("generating_tools", [])))
            except (OSError, ValueError):
                unchanged = False
            report["checks"]["read_only_inputs"] = "passed" if unchanged else "failed"
            if not unchanged:
                report["status"], exit_code = "failed", 2
                report["error"] = "sources, live artifacts, metadata, queue or generating tools changed during verification"
        report["duration_seconds"] = round(time.perf_counter() - start, 3)
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        report["remaining_agent_checks"] = [
            "Inspect the saved viewer screenshots at readable scale.",
            "Retain source reconciliation and primary review/adjudication judgment; summaries do not perform these reviews.",
            "Confirm the supplied full-suite report covers the final code; a passing historical report does not establish currency.",
        ]
        _write(output / "report.json", report)
    summary = {key: report[key] for key in ("record_id", "status", "checks", "duration_seconds")}
    summary["report_path"] = str(output / "report.json")
    summary["visual_inspection"] = "pending" if exit_code == 0 else "not_complete"
    if "viewer" in report:
        summary["viewer"] = report["viewer"]
    if "error" in report:
        summary["error_preview"] = report["error"][:500]
    return summary, exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record_id")
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--test-report", type=Path, required=True, help="saved default full-suite scripts/run_tests.py report")
    parser.add_argument("--log-dir", type=Path, help="output parent within papers (private)/diagnostics")
    parser.add_argument("--node", help="installed Node.js executable; bundled runtime or PATH by default")
    parser.add_argument("--playwright-module", help="installed Playwright module directory; no installation is performed")
    parser.add_argument("--browser-channel", choices=("chromium", "msedge", "chrome"))
    parser.add_argument("--viewer-timeout", type=float, default=180)
    args = parser.parse_args(argv)
    if not 0 < args.viewer_timeout <= 3600:
        parser.error("--viewer-timeout must be between 0 and 3600 seconds")
    try:
        summary, code = check_completion(args.repository_root, args.record_id,
            test_report=args.test_report,
            log_dir=args.log_dir or args.repository_root / "papers (private)/diagnostics/completion-checks",
            node=args.node, playwright_module=args.playwright_module,
            browser_channel=args.browser_channel, viewer_timeout=args.viewer_timeout)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
