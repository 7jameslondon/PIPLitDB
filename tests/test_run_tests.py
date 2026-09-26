from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from scripts.run_tests import _configure_test_environment, main as run_tests_main


RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "run_tests.py"


class ConciseTestRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "tests").mkdir()

    def fixture(self, source: str):
        (self.root / "tests" / "test_example.py").write_text(
            textwrap.dedent(source), encoding="utf-8"
        )

    def run_suite(self, *args: str):
        process = subprocess.run(
            [sys.executable, "-X", "utf8", str(RUNNER), *args],
            cwd=self.root, capture_output=True, text=True, encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(process.stderr, "", process.stderr)
        summary = json.loads(process.stdout)
        report = json.loads(Path(summary["report_path"]).read_text(encoding="utf-8"))
        log = Path(summary["log"]["path"]).read_bytes()
        self.assertEqual(process.returncode, summary["exit_code"])
        self.assertEqual(hashlib.sha256(log).hexdigest(), summary["log"]["sha256"])
        self.assertEqual(len(log), summary["log"]["bytes"])
        self.assertLessEqual(len(process.stdout), 6000)
        return process, summary, report, log

    def test_large_python_native_and_subprocess_output_is_saved_completely(self):
        self.fixture('''
            import os, subprocess, sys, unittest
            class Example(unittest.TestCase):
                def test_output(self):
                    print("α" * 100000)
                    print("STDERR-END", file=sys.stderr)
                    os.write(1, b"NATIVE-END\\n")
                    subprocess.run([sys.executable, "-c", "print('CHILD-END')"], check=True)
        ''')
        process, summary, report, log = self.run_suite()
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(summary["counts"]["passed"], 1)
        self.assertNotIn("α", process.stdout)
        self.assertIn(("α" * 100000).encode("utf-8"), log)
        for marker in (b"STDERR-END", b"NATIVE-END", b"CHILD-END"):
            self.assertIn(marker, log)
        self.assertEqual(report["working_directory"], str(self.root))

    def test_failures_are_bounded_and_all_details_remain_in_report(self):
        self.fixture('''
            import unittest
            class Example(unittest.TestCase):
                def test_many(self):
                    for number in range(8):
                        with self.subTest(number=number):
                            self.fail("FAIL-START:" + "Ω" * 20000 + ":FAIL-END")
        ''')
        process, summary, report, log = self.run_suite()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["counts"]["tests_run"], 1)
        self.assertEqual(summary["counts"]["failures"], 8)
        self.assertEqual(summary["counts"]["passed"], 0)
        self.assertTrue(summary["issue_previews"])
        self.assertEqual(len(summary["issue_previews"]) + summary["issues_not_shown"], 8)
        for index, preview in enumerate(summary["issue_previews"]):
            self.assertTrue(preview["excerpted"])
            self.assertEqual(preview["report_pointer"], f"/issues/{index}")
        self.assertEqual(len(report["issues"]), 8)
        self.assertIn("Ω" * 20000, report["issues"][0]["detail"])
        self.assertIn(b"FAIL-END", log)

    def test_skip_expected_failure_and_unexpected_success_counts(self):
        self.fixture('''
            import unittest
            class Example(unittest.TestCase):
                def test_pass(self): pass
                @unittest.skip("intentional fixture skip")
                def test_skip(self): pass
                @unittest.expectedFailure
                def test_expected(self): self.fail("known failure")
                @unittest.expectedFailure
                def test_unexpected(self): pass
        ''')
        process, summary, report, _ = self.run_suite()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(summary["counts"], {
            "tests_run": 4, "passed": 1, "failures": 0, "errors": 0,
            "skipped": 1, "expected_failures": 1, "unexpected_successes": 1,
        })
        self.assertEqual(report["skipped_tests"][0]["reason"], "intentional fixture skip")
        self.assertEqual(summary["issue_previews"][0]["kind"], "unexpected_success")

    def test_import_error_is_reported_as_failure(self):
        self.fixture('raise RuntimeError("IMPORT-FAILURE")')
        process, summary, _, log = self.run_suite()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(summary["counts"]["errors"], 1)
        self.assertIn("IMPORT-FAILURE", summary["issue_previews"][0]["detail"])
        self.assertIn(b"IMPORT-FAILURE", log)

    def test_discovery_reports_all_missing_imports_without_running_other_tests(self):
        self.fixture('''
            from pathlib import Path
            import unittest
            class Example(unittest.TestCase):
                def test_must_not_run(self): Path("test-ran").write_text("ran")
        ''')
        for suffix in ("one", "two"):
            (self.root / "tests" / f"test_missing_{suffix}.py").write_text(
                f"import nonexistent_test_dependency_{suffix}\n", encoding="utf-8"
            )
        process, summary, report, log = self.run_suite()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(summary["status"], "discovery_failed")
        self.assertEqual(summary["counts"]["tests_run"], 0)
        self.assertEqual(summary["counts"]["errors"], 2)
        self.assertFalse((self.root / "test-ran").exists())
        self.assertEqual(len(report["issues"]), 2)
        self.assertIn(b"nonexistent_test_dependency_one", log)
        self.assertIn(b"nonexistent_test_dependency_two", log)
        self.assertEqual(report["environment"]["python_executable"], sys.executable)

    def test_reportlab_pdf_generation_works_in_the_test_worker(self):
        self.fixture('''
            from io import BytesIO
            from reportlab.pdfgen import canvas
            import unittest
            class Example(unittest.TestCase):
                def test_pdf(self):
                    output = BytesIO()
                    pdf = canvas.Canvas(output)
                    pdf.drawString(20, 20, "Test environment")
                    pdf.save()
                    self.assertTrue(output.getvalue().startswith(b"%PDF-"))
        ''')
        _, summary, report, _ = self.run_suite()
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(summary["counts"]["passed"], 1)
        self.assertEqual(report["environment"]["python_executable"], sys.executable)

    def test_bundled_reportlab_fallback_preserves_installed_package_precedence(self):
        bundled = (
            self.root / ".cache/codex-runtimes/codex-primary-runtime"
            / "dependencies/python/Lib/site-packages"
        )
        (bundled / "reportlab/pdfgen").mkdir(parents=True)
        (bundled / "reportlab/__init__.py").write_text("", encoding="utf-8")
        (bundled / "reportlab/pdfgen/canvas.py").write_text("", encoding="utf-8")
        original = list(sys.path)
        with patch("scripts.run_tests.importlib.util.find_spec", return_value=None), \
                patch("scripts.run_tests.Path.home", return_value=self.root), \
                patch.object(sys, "path", original.copy()):
            report = _configure_test_environment()
            self.assertEqual(sys.path, [*original, str(bundled)])
            self.assertEqual(report["added_package_paths"], [str(bundled)])
            _configure_test_environment()
            self.assertEqual(sys.path, [*original, str(bundled)])

    def test_installed_reportlab_does_not_use_the_bundle(self):
        original = list(sys.path)
        with patch("scripts.run_tests.importlib.util.find_spec", return_value=object()), \
                patch("scripts.run_tests.Path.home") as home:
            report = _configure_test_environment()
        home.assert_not_called()
        self.assertEqual(sys.path, original)
        self.assertEqual(report["added_package_paths"], [])

    def test_missing_bundle_leaves_the_environment_unchanged(self):
        original = list(sys.path)
        with patch("scripts.run_tests.importlib.util.find_spec", return_value=None), \
                patch("scripts.run_tests.Path.home", return_value=self.root):
            report = _configure_test_environment()
        self.assertEqual(sys.path, original)
        self.assertEqual(report["added_package_paths"], [])

    def test_fixture_error_is_not_mistaken_for_an_empty_suite(self):
        self.fixture('''
            import unittest
            def setUpModule(): raise RuntimeError("SETUP-FAILURE")
            class Example(unittest.TestCase):
                def test_unused(self): pass
        ''')
        process, summary, _, _ = self.run_suite()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["counts"]["tests_run"], 0)
        self.assertEqual(summary["counts"]["errors"], 1)

    def test_empty_selection_fails_instead_of_reporting_success(self):
        _, summary, _, _ = self.run_suite()
        self.assertEqual(summary["status"], "no_tests")
        self.assertEqual(summary["exit_code"], 5)

    def test_stopped_run_cannot_pass_with_only_partial_successes(self):
        self.fixture('''
            import unittest
            class Example(unittest.TestCase):
                def test_a_stop(self): self._outcome.result.stop()
                def test_b_not_run(self): self.fail("should not run")
        ''')
        process, summary, _, _ = self.run_suite()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(summary["counts"]["tests_run"], 1)
        self.assertEqual(summary["counts"]["passed"], 1)

    def assert_abrupt_exit(self, code):
        self.fixture(f'import os\nprint("EARLY-EXIT", flush=True)\nos._exit({code})')
        process, summary, _, log = self.run_suite()
        self.assertEqual(process.returncode, code or 1)
        self.assertEqual(summary["status"], "incomplete")
        self.assertIsNone(summary["counts"])
        self.assertIn(b"EARLY-EXIT", log)

    def test_abrupt_zero_exit_cannot_produce_a_false_pass(self):
        self.assert_abrupt_exit(0)

    def test_abrupt_nonzero_exit_code_is_preserved(self):
        self.assert_abrupt_exit(7)

    def test_focused_arguments_work_and_existing_logs_are_preserved(self):
        self.fixture('''
            import unittest
            class Example(unittest.TestCase):
                def test_selected(self): pass
                def test_other(self): self.fail("should not run")
        ''')
        args = ("--log-dir", "private logs", "--", "discover", "-s", "tests", "-k", "selected")
        _, first, _, log = self.run_suite(*args)
        _, second, _, _ = self.run_suite(*args)
        self.assertEqual(first["status"], "passed")
        self.assertEqual(first["counts"]["tests_run"], 1)
        self.assertNotEqual(first["report_path"], second["report_path"])
        self.assertEqual(Path(first["log"]["path"]).read_bytes(), log)

    def test_invalid_unittest_arguments_are_not_reported_as_success(self):
        process, summary, _, log = self.run_suite("--", "--nonexistent-option")
        self.assertEqual(process.returncode, 2)
        self.assertEqual(summary["status"], "incomplete")
        self.assertIn(b"unrecognized arguments", log)

    def test_buffering_is_rejected_because_it_discards_successful_output(self):
        for option in ("-b", "-vb", "--buffer"):
            with self.subTest(option=option):
                process = subprocess.run(
                    [sys.executable, str(RUNNER), "--", "discover", "-s", "tests", option],
                    cwd=self.root, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(process.returncode, 2)
                self.assertIn("incompatible with complete logs", process.stderr)

    def test_log_directory_permission_failure_is_reported_promptly(self):
        stderr = io.StringIO()
        log_parent = self.root / "private-logs"
        with patch(
            "scripts.run_tests.create_unique_private_directory",
            side_effect=PermissionError("sandbox denied test directory"),
        ) as allocate, redirect_stderr(stderr):
            with self.assertRaisesRegex(SystemExit, "2"):
                run_tests_main(["--log-dir", str(log_parent)])
        self.assertIn("sandbox denied test directory", stderr.getvalue())
        allocate.assert_called_once()
        self.assertEqual(list(log_parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
