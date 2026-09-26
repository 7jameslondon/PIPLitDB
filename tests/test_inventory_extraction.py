from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.inventory_extraction import main
from scripts.inspect_extraction import main as inspect_main


class InventoryExtractionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.reports = self.root / "reports"

    def write(self, name, text="data"):
        path = self.inputs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def invoke(self, *args, expected=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(args))
        self.assertEqual(code, expected, stderr.getvalue())
        if expected:
            self.assertEqual(stdout.getvalue(), "")
            return stderr.getvalue()
        return json.loads(stdout.getvalue()), stdout.getvalue()

    def run_files(self, *args, expected=0):
        return self.invoke("files", str(self.inputs), "--save-dir", str(self.reports), *args, expected=expected)

    def previous(self, summary):
        return ("--previous", summary["snapshot"]["path"],
                "--expect-previous-sha256", summary["snapshot"]["sha256"])

    def snapshot(self, summary):
        path = Path(summary["snapshot"]["path"])
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), summary["snapshot"]["sha256"])
        return json.loads(raw)

    def test_large_inventory_is_complete_bounded_and_readable_in_pages(self):
        for number in range(120):
            self.write(f"α-{number:03d}.txt", str(number))
        self.write(".hidden/data.bin", "hidden")
        (self.inputs / "empty").mkdir()
        first, output = self.run_files("--max-output-chars", "2000")
        self.assertLessEqual(len(output), 2000)
        self.assertEqual(first["counts"]["files"], 121)
        self.assertEqual(first["counts"]["directories"], 2)
        snapshot = self.snapshot(first)
        self.assertEqual(len(snapshot["entries"]), 123)
        self.assertGreater(first["preview_omitted"], 0)
        positions, start = [], 0
        while True:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = inspect_main(["outline", first["snapshot"]["path"], "--pointer", "/entries",
                                     "--expect-sha256", first["snapshot"]["sha256"],
                                     "--start", str(start), "--max-output-chars", "2000"])
            self.assertEqual(code, 0)
            self.assertLessEqual(len(stdout.getvalue()), 2000)
            page = json.loads(stdout.getvalue())
            positions.extend(entry["pointer"] for entry in page["entries"])
            if page["next_start"] is None:
                break
            start = page["next_start"]
        self.assertEqual(positions, [f"/entries/{index}" for index in range(123)])

    def test_unchanged_inventory_reuses_snapshot_without_printing_listing(self):
        self.write("diagnostic.json", '{"complete": true}')
        first, _ = self.run_files()
        before = Path(first["snapshot"]["path"]).read_bytes()
        again, output = self.run_files(*self.previous(first))
        self.assertEqual(again["status"], "unchanged")
        self.assertTrue(again["snapshot_reused"])
        self.assertEqual(again["snapshot"], first["snapshot"])
        self.assertEqual(again["preview"], [])
        self.assertEqual(again["change_counts"], {})
        self.assertEqual(len(list(self.reports.iterdir())), 1)
        self.assertEqual(Path(first["snapshot"]["path"]).read_bytes(), before)
        self.assertNotIn('"complete"', output)

    def test_added_removed_and_same_size_same_mtime_edits_are_detected(self):
        changed = self.write("changed.txt", "AAAA")
        removed = self.write("removed.txt")
        first, _ = self.run_files()
        before = changed.stat()
        changed.write_text("BBBB", encoding="utf-8")
        os.utime(changed, ns=(before.st_atime_ns, before.st_mtime_ns))
        removed.unlink()
        self.write("added.txt")
        current, _ = self.run_files(*self.previous(first), "--limit", "1")
        self.assertEqual(current["status"], "changed")
        self.assertEqual(current["change_counts"], {"added": 1, "modified": 1, "removed": 1})
        self.assertEqual(current["preview_omitted"], 2)
        diff_path = Path(current["changes_report"]["path"])
        self.assertEqual(hashlib.sha256(diff_path.read_bytes()).hexdigest(), current["changes_report"]["sha256"])
        changes = json.loads(diff_path.read_text(encoding="utf-8"))["changes"]
        edit = next(row for row in changes if row["path"] == "changed.txt")
        self.assertNotEqual(edit["before"]["sha256"], edit["after"]["sha256"])
        self.assertTrue(Path(first["snapshot"]["path"]).is_file())

    def test_filter_only_limits_preview_and_never_hides_other_changes(self):
        self.write("keep.json")
        self.write("other.txt")
        first, _ = self.run_files("--contains", "keep")
        self.assertEqual(first["matching_preview_entries"], 1)
        self.assertEqual(first["counts"]["files"], 2)
        self.assertEqual(len(self.snapshot(first)["entries"]), 2)
        self.write("other.txt", "changed")
        current, _ = self.run_files(*self.previous(first), "--contains", "keep")
        self.assertEqual(current["status"], "changed")
        self.assertEqual(current["change_counts"], {"modified": 1})
        self.assertEqual(current["matching_preview_entries"], 0)

    def test_modified_previous_snapshot_is_refused(self):
        self.write("source.txt")
        first, _ = self.run_files()
        Path(first["snapshot"]["path"]).write_text("{}", encoding="utf-8")
        error = self.run_files(*self.previous(first), expected=2)
        self.assertIn("SHA-256 changed", error)
        self.assertEqual(len(list(self.reports.iterdir())), 1)

    def test_other_root_and_changed_generator_cannot_claim_unchanged(self):
        self.write("source.txt")
        first, _ = self.run_files()
        other = self.root / "other"
        other.mkdir()
        (other / "source.txt").write_text("data", encoding="utf-8")
        error = self.invoke("files", str(other), "--save-dir", str(self.reports), *self.previous(first), expected=2)
        self.assertIn("different root", error)
        with patch("scripts.inventory_extraction._generator", return_value={"changed": True}):
            error = self.run_files(*self.previous(first), expected=2)
        self.assertIn("generator/settings changed", error)

    def test_output_inside_scanned_tree_is_refused_without_writes(self):
        self.write("source.txt")
        target = self.inputs / "reports"
        error = self.invoke("files", str(self.inputs), "--save-dir", str(target), expected=2)
        self.assertIn("outside inventoried trees", error)
        self.assertFalse(target.exists())

    def test_unreadable_entry_and_reparse_point_do_not_create_partial_success(self):
        self.write("source.txt")
        with patch("scripts.inventory_extraction._file_entry", side_effect=PermissionError("denied")):
            self.run_files(expected=2)
        self.assertFalse(self.reports.exists())
        with patch("scripts.inventory_extraction.is_reparse_point", return_value=True):
            self.assertIn("reparse", self.run_files(expected=2))
        self.assertFalse(self.reports.exists())

    def test_snapshot_directory_permission_failure_is_reported_promptly(self):
        self.write("source.txt")
        with patch(
            "scripts.inventory_extraction.create_unique_private_directory",
            side_effect=PermissionError("sandbox denied snapshot directory"),
        ) as allocate:
            error = self.run_files(expected=2)
        self.assertIn("sandbox denied snapshot directory", error)
        allocate.assert_called_once_with(self.reports.resolve(), "inventory-")
        self.assertEqual(list(self.reports.iterdir()), [])

    def test_file_changed_during_hashing_is_refused(self):
        path = self.write("source.txt")
        from scripts.inventory_extraction import _file_entry
        def changing_hash(_):
            path.write_text("changed during read", encoding="utf-8")
            return "0" * 64
        with patch("scripts.inventory_extraction.sha256_file", side_effect=changing_hash):
            with self.assertRaisesRegex(ValueError, "changed during hashing"):
                _file_entry(path, "source.txt")

    def test_source_mode_preserves_original_inventory_details_and_private_boundaries(self):
        repository = self.root / "repository"
        record = repository / "papers (private)" / "00283"
        for folder in (record / "html", record / "pdf", record / "supplementary", repository / "database/records"):
            folder.mkdir(parents=True)
        (repository / "database/records/00283.yaml").write_text("title: Test paper\n", encoding="utf-8")
        (record / "html/main.html").write_text("<html><p>Test paper</p></html>", encoding="utf-8")
        (record / "supplementary/notes.txt").write_text("Supplement", encoding="utf-8")
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(record / "pdf/main.pdf")
        save_dir = repository / "papers (private)/diagnostics"
        args = ("sources", "00283", "--repository-root", str(repository), "--save-dir", str(save_dir))
        first, _ = self.invoke(*args)
        snapshot = self.snapshot(first)
        from scripts.extraction.pipeline import inventory_record
        native = inventory_record(repository, "00283")
        self.assertEqual(snapshot["entries"], sorted(native["sources"], key=lambda item: item["path"]))
        self.assertEqual(snapshot["metadata"]["source_fingerprint"], native["source_fingerprint"])
        self.assertEqual(next(row["page_count"] for row in snapshot["entries"] if row["role"] == "main_pdf"), 1)
        again, _ = self.invoke(*args, *self.previous(first))
        self.assertEqual(again["status"], "unchanged")
        self.invoke("sources", "00283", "--repository-root", str(repository),
                    "--save-dir", str(record / "pdf/reports"), expected=2)
        self.invoke("sources", "00283", "--repository-root", str(repository),
                    "--save-dir", str(repository / "public-report"), expected=2)


if __name__ == "__main__":
    unittest.main()
