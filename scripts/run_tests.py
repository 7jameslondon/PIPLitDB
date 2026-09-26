#!/usr/bin/env python3
"""Run unittest with complete on-disk logs and a concise console report.

The child process writes structured outcomes through unittest's result API;
printed test output is never parsed to decide whether the suite passed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

if __package__:
    from .private_directory import create_unique_private_directory
else:
    from private_directory import create_unique_private_directory


DEFAULT_LOG_DIR = Path("papers (private)/diagnostics/test-runs")
DEFAULT_TEST_ARGS = ["discover", "-s", "tests", "-v"]
MAX_ISSUE_PREVIEWS = 3


class ReportingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = 0

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed += 1


class ReportingRunner(unittest.TextTestRunner):
    resultclass = ReportingResult


class PreflightTestProgram(unittest.TestProgram):
    def runTests(self):
        # Discovery imports every selected test module before running any tests.
        # Do not spend a full run on a suite that already has import failures.
        if self.testLoader.errors:
            self.result = None
        else:
            super().runTests()


def _configure_test_environment() -> dict:
    added_paths = []
    if importlib.util.find_spec("reportlab") is None:
        bundled = (
            Path.home() / ".cache/codex-runtimes/codex-primary-runtime"
            / "dependencies/python/Lib/site-packages"
        )
        if (bundled / "reportlab/__init__.py").is_file() and (
            bundled / "reportlab/pdfgen/canvas.py"
        ).is_file():
            # Keep this interpreter's installed libraries ahead of the fallback,
            # especially compiled packages such as Pillow. Configure the worker
            # itself: changing the parent's sys.path does not reach subprocesses.
            if str(bundled) not in sys.path:
                sys.path.append(str(bundled))
                added_paths.append(str(bundled))
    return {"python_executable": sys.executable, "added_package_paths": added_paths}


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _worker(report_path: Path, test_args: list[str]) -> int:
    # Match `python -m unittest`: imports start from the caller's directory.
    sys.path[0] = os.getcwd()
    environment = _configure_test_environment()
    program = PreflightTestProgram(
        module=None,
        argv=["python -m unittest", *test_args],
        testLoader=unittest.TestLoader(),
        testRunner=ReportingRunner,
        exit=False,
    )
    if program.testLoader.errors:
        details = program.testLoader.errors
        for detail in details:
            print(detail, file=sys.stderr)
        _write_json(report_path, {
            "status": "discovery_failed",
            "counts": {
                "tests_run": 0, "passed": 0, "failures": 0, "errors": len(details),
                "skipped": 0, "expected_failures": 0, "unexpected_successes": 0,
            },
            "issues": [{"kind": "error", "test": detail.splitlines()[0], "detail": detail}
                       for detail in details],
            "skipped_tests": [], "expected_failures": [], "environment": environment,
            "message": "Test discovery/imports failed before any tests ran. Inspect every error; "
                       "for missing packages, install requirements.txt with the reported Python "
                       "executable before retrying. No packages were installed automatically.",
        })
        return 1
    result = program.result
    issues = [
        {"kind": kind, "test": test.id(), "detail": detail}
        for kind, entries in (("failure", result.failures), ("error", result.errors))
        for test, detail in entries
    ]
    issues.extend(
        {"kind": "unexpected_success", "test": test.id(),
         "detail": "A test marked expectedFailure unexpectedly passed."}
        for test in result.unexpectedSuccesses
    )
    # An empty selection must not be presented as a passing full suite.
    if not result.wasSuccessful():
        status, exit_code = "failed", 1
    elif result.shouldStop:
        status, exit_code = "incomplete", 1
    elif result.testsRun == 0 and not result.skipped:
        status, exit_code = "no_tests", 5
    else:
        status, exit_code = "passed", 0
    report = {
        "status": status,
        "environment": environment,
        "counts": {
            "tests_run": result.testsRun,
            "passed": result.passed,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
        },
        "issues": issues,
        "skipped_tests": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
        "expected_failures": [{"test": test.id(), "detail": detail} for test, detail in result.expectedFailures],
    }
    if status == "incomplete":
        report["message"] = "Test execution stopped early; this does not satisfy the full-suite gate."
    _write_json(report_path, report)
    return exit_code


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n[... excerpt; read the full report ...]\n"
    remaining = limit - len(marker)
    return text[:remaining // 2] + marker + text[-(remaining - remaining // 2):]


def _console_report(report: dict, report_path: Path) -> dict:
    issues = report.get("issues", [])
    previews = []
    for index, issue in enumerate(issues[:MAX_ISSUE_PREVIEWS]):
        previews.append({
            "kind": issue["kind"],
            "test": _excerpt(issue["test"], 200),
            "detail": _excerpt(issue["detail"], 400),
            "excerpted": len(issue["test"]) > 200 or len(issue["detail"]) > 400,
            "report_pointer": f"/issues/{index}",
        })
    summary = {key: report[key] for key in ("status", "exit_code", "counts", "duration_seconds", "log")}
    summary.update({
        "report_path": str(report_path),
        "issue_previews": previews,
        "issues_not_shown": len(issues) - len(previews),
    })
    if issues:
        summary["next_step"] = "Inspect every issue in the full report; previews are not complete diagnostics."
    if "message" in report:
        summary["message"] = report["message"]
    if "environment" in report:
        summary["environment"] = report["environment"]
    # Account for JSON escaping as well as preview count (e.g. long Unicode
    # assertion messages). Complete issues remain available in report.json.
    while previews and len(json.dumps(summary, ensure_ascii=True, indent=2)) + 1 > 6000:
        previews.pop()
        summary["issues_not_shown"] += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Internal invocation only; the parent owns capture and final status.
    if argv[:1] == ["--worker"]:
        return _worker(Path(argv[1]), argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR,
                        help="parent for a new log directory (use the assigned private diagnostics area)")
    parser.add_argument("test_args", nargs=argparse.REMAINDER,
                        help="unittest arguments after --; default: discover -s tests -v")
    args = parser.parse_args(argv)
    test_args = args.test_args
    if test_args[:1] == ["--"]:
        test_args = test_args[1:]
    test_args = test_args or DEFAULT_TEST_ARGS
    # unittest buffering discards successful-test output, defeating full capture.
    if any(arg == "--buffer" or (arg.startswith("-") and not arg.startswith("--") and "b" in arg[1:])
           for arg in test_args):
        parser.error("unittest --buffer/-b is incompatible with complete logs; omit it")

    log_parent = args.log_dir.resolve()
    try:
        log_parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = create_unique_private_directory(log_parent, f"tests-{stamp}-")
    except OSError as exc:
        parser.error(f"cannot create test log directory: {exc}")
    log_path, report_path = run_dir / "unittest.log", run_dir / "report.json"
    command = [sys.executable, "-X", "utf8", "-u", str(Path(__file__).resolve()),
               "--worker", str(report_path), *test_args]
    start = time.perf_counter()
    interrupted = False
    with log_path.open("xb") as log:
        try:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                return_code = process.wait()
            except KeyboardInterrupt:
                interrupted = True
                process.terminate()
                process.wait()
                return_code = 130
        except OSError as exc:
            log.write(f"Could not start unittest: {exc}\n".encode("utf-8"))
            return_code = 2
    duration = round(time.perf_counter() - start, 3)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {"status": "incomplete", "counts": None, "issues": [],
                  "message": "No complete unittest result was written. Inspect the full log."}
        return_code = return_code or 1
    if interrupted:
        report["status"] = "interrupted"
    elif return_code and report["status"] == "passed":
        report["status"] = "incomplete"
        report["message"] = "The process failed after reporting results. Inspect the full log."
    exit_code = return_code if return_code >= 0 else 1
    with log_path.open("rb") as log:
        digest = hashlib.file_digest(log, "sha256").hexdigest()
    report.update({
        "exit_code": exit_code,
        "duration_seconds": duration,
        "command": command,
        "working_directory": str(Path.cwd()),
        "python_version": sys.version,
        "log": {"path": str(log_path), "sha256": digest, "bytes": log_path.stat().st_size},
    })
    _write_json(report_path, report)
    print(json.dumps(_console_report(report, report_path), ensure_ascii=True, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
