from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import inspect_extraction
from scripts.inspect_extraction import main


class EvidenceInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "source.txt"

    def write(self, text: str, encoding: str = "utf-8") -> None:
        self.path.write_bytes(text.encode(encoding))

    def run_tool(self, operation: str, *args: str, expected: int = 0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([operation, str(self.path), *args])
        self.assertEqual(code, expected, stderr.getvalue())
        if expected:
            self.assertEqual(stdout.getvalue(), "")
            return stderr.getvalue()
        return json.loads(stdout.getvalue()), stdout.getvalue()

    def test_minified_html_search_is_bounded_and_keeps_every_occurrence(self) -> None:
        source = "<html>" + ("x" * 50_000 + "α-value" + "y" * 50_000) * 7 + "</html>"
        self.write(source)
        before = self.path.read_bytes()
        start, positions = 0, []
        while True:
            page, output = self.run_tool("find", "α-value", "--start", str(start),
                                         "--max-output-chars", "2000", "--context", "80")
            self.assertLessEqual(len(output), 2000)
            for match in page["matches"]:
                self.assertEqual(match["excerpt"], source[match["excerpt_start"]:match["excerpt_end"]])
                positions.append(match["match_start"])
            if page["next_start"] is None:
                break
            self.assertGreater(page["next_start"], start)
            start = page["next_start"]
        self.assertEqual(len(positions), 7)
        self.assertEqual(len(set(positions)), 7)
        self.assertEqual(before, self.path.read_bytes())

    def test_paged_raw_reads_reconstruct_unicode_controls_and_line_endings(self) -> None:
        source = ('α β \"quotes\" \\ slash\r\n\t' * 1000) + "last character Ω"
        self.write(source)
        pieces, start = [], 0
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        while True:
            page, output = self.run_tool("read", "--start", str(start),
                                         "--expect-sha256", digest, "--max-output-chars", "2000")
            self.assertLessEqual(len(output), 2000)
            self.assertEqual(page["content"], source[page["start"]:page["end"]])
            pieces.append(page["content"])
            if page["next_start"] is None:
                break
            self.assertGreater(page["next_start"], start)
            start = page["next_start"]
        self.assertEqual("".join(pieces), source)

    def test_json_pointer_preserves_original_numbers_escapes_and_spacing(self) -> None:
        value = '[ 1.234567890123456789012345, 1e-1000, "\\u03b1", -0.00 ]'
        self.write('{"a/b": {"~key": ' + value + '}, "": true}')
        page, _ = self.run_tool("read", "--pointer", "/a~1b/~0key")
        self.assertEqual(page["content"], value)
        self.assertEqual(page["representation"], "original_json_value")
        page, _ = self.run_tool("read", "--pointer", "/")
        self.assertEqual(page["content"], "true")

    def test_outline_pagination_retains_every_pointer(self) -> None:
        self.write(json.dumps({"sections": [{"heading": {"plain_text": "Title " + str(i)}} for i in range(31)]}))
        start, pointers = 0, []
        while True:
            page, output = self.run_tool("outline", "--pointer", "/sections",
                                         "--start", str(start), "--max-output-chars", "2000")
            self.assertLessEqual(len(output), 2000)
            pointers.extend(entry["pointer"] for entry in page["entries"])
            if page["next_start"] is None:
                break
            start = page["next_start"]
        self.assertEqual(pointers, [f"/sections/{i}" for i in range(31)])

    def test_changed_source_is_refused_before_content_is_printed(self) -> None:
        self.write("original")
        page, _ = self.run_tool("stat")
        self.write("changed")
        error = self.run_tool("read", "--expect-sha256", page["source_sha256"], expected=2)
        self.assertIn("SHA-256 changed", error)

    def test_ambiguous_json_requires_raw_read(self) -> None:
        self.write('{"value": 1, "value": 2}')
        self.assertIn("duplicate JSON keys", self.run_tool("read", "--pointer", "/value", expected=2))
        page, _ = self.run_tool("read")
        self.assertEqual(page["content"], '{"value": 1, "value": 2}')

    def test_invalid_pointers_and_out_of_range_offsets_fail(self) -> None:
        self.write('{"items": [1, 2]}')
        for pointer in ("items", "/items/01", "/items/-1", "/items/2", "/items/~2"):
            self.run_tool("read", "--pointer", pointer, expected=2)
        self.run_tool("read", "--start", "100000", expected=2)

    def test_overlapping_literal_matches_are_not_skipped(self) -> None:
        self.write("aaa")
        first, _ = self.run_tool("find", "aa", "--limit", "1")
        second, _ = self.run_tool("find", "aa", "--start", str(first["next_start"]))
        self.assertEqual(first["matches"][0]["match_start"], 0)
        self.assertEqual(second["matches"][0]["match_start"], 1)
        self.assertIsNone(second["next_start"])

    def test_encoding_is_strict_and_can_be_selected_explicitly(self) -> None:
        self.write("café", "cp1252")
        self.run_tool("read", expected=2)
        page, _ = self.run_tool("read", "--encoding", "cp1252")
        self.assertEqual(page["content"], "café")

    def test_empty_views_and_absent_matches_have_explicit_end(self) -> None:
        self.write("")
        page, _ = self.run_tool("read")
        self.assertEqual(page["total_chars"], 0)
        self.assertIsNone(page["next_start"])
        page, _ = self.run_tool("find", "missing")
        self.assertEqual(page["matches"], [])
        self.assertIsNone(page["next_start"])
        self.write("{}")
        page, _ = self.run_tool("outline")
        self.assertEqual(page["entries"], [])
        self.assertIsNone(page["next_start"])

    def test_identical_retained_view_can_be_omitted_without_claiming_new_coverage(self) -> None:
        self.write("diagnostic detail " * 4000)
        first, _ = self.run_tool("read", "--max-output-chars", "2000")
        again, output = self.run_tool("read", "--max-output-chars", "2000",
                                      "--if-view-token", first["view_token"])
        self.assertTrue(again["view_unchanged"])
        self.assertTrue(again["content_omitted"])
        self.assertNotIn("content", again)
        self.assertNotIn("next_start", again)  # Not an end-of-file/coverage claim.
        self.assertLess(len(output), 1000)
        continuation, _ = self.run_tool("read", "--max-output-chars", "2000",
                                       "--start", str(first["next_start"]),
                                       "--if-view-token", first["view_token"])
        self.assertFalse(continuation["view_unchanged"])
        self.assertTrue(continuation["content"])

    def test_changed_bytes_return_a_new_view_and_keep_hash_pinning_strict(self) -> None:
        self.write("original")
        first, _ = self.run_tool("read")
        self.write("modified")
        changed, _ = self.run_tool("read", "--if-view-token", first["view_token"])
        self.assertFalse(changed["view_unchanged"])
        self.assertEqual(changed["content"], "modified")
        self.run_tool("read", "--if-view-token", first["view_token"],
                      "--expect-sha256", first["source_sha256"], expected=2)

    def test_view_tokens_do_not_skip_other_paths_pointers_or_budgets(self) -> None:
        self.write('{"a": "alpha", "b": "beta"}')
        first, _ = self.run_tool("read", "--pointer", "/a")
        for extra in (("--pointer", "/b"), ("--pointer", "/a", "--max-output-chars", "2000")):
            with self.subTest(extra=extra):
                page, _ = self.run_tool("read", *extra, "--if-view-token", first["view_token"])
                self.assertFalse(page["view_unchanged"])
                self.assertIn("content", page)
        self.path = Path(self.directory.name) / "another.json"
        self.write('{"a": "alpha", "b": "beta"}')
        page, _ = self.run_tool("read", "--pointer", "/a", "--if-view-token", first["view_token"])
        self.assertFalse(page["view_unchanged"])

    def test_search_and_outline_tokens_bind_their_operation_and_options(self) -> None:
        self.write('{"files": [{"path": "alpha.txt"}, {"path": "beta.txt"}]}')
        first, _ = self.run_tool("find", "alpha", "--context", "10")
        again, _ = self.run_tool("find", "alpha", "--context", "10", "--if-view-token", first["view_token"])
        self.assertTrue(again["view_unchanged"])
        for query, context in (("beta", "10"), ("alpha", "20")):
            changed, _ = self.run_tool("find", query, "--context", context, "--if-view-token", first["view_token"])
            self.assertFalse(changed["view_unchanged"])
            self.assertTrue(changed["matches"])
        outline, _ = self.run_tool("outline", "--pointer", "/files", "--limit", "1")
        self.assertEqual(outline["entries"][0]["preview"], "alpha.txt")
        again, _ = self.run_tool("outline", "--pointer", "/files", "--limit", "1",
                                 "--if-view-token", outline["view_token"])
        self.assertTrue(again["view_unchanged"])
        read, _ = self.run_tool("read", "--pointer", "/files", "--if-view-token", outline["view_token"])
        self.assertFalse(read["view_unchanged"])
        self.run_tool("read", "--if-view-token", "invalid", expected=2)


class BatchEvidenceInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest = self.root / "requests.json"

    def source(self, name: str, content: str) -> Path:
        path = self.root / name
        path.write_bytes(content.encode("utf-8"))
        return path

    def requests(self, values) -> None:
        self.manifest.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")

    def run_batch(self, *, cursor=None, budget=2000, expected=0):
        args = ["batch", str(self.manifest), "--max-output-chars", str(budget)]
        if cursor is not None:
            args += ["--cursor", cursor]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(args)
        self.assertEqual(code, expected, stderr.getvalue())
        if expected:
            self.assertEqual(stdout.getvalue(), "")
            return stderr.getvalue()
        self.assertEqual(stderr.getvalue(), "")
        self.assertLessEqual(len(stdout.getvalue()), budget)
        return json.loads(stdout.getvalue())

    def all_pages(self, *, budget=2000):
        pages, cursors, cursor = [], set(), None
        for _ in range(200):
            page = self.run_batch(cursor=cursor, budget=budget)
            self.assertTrue(page["results"])
            pages.append(page)
            cursor = page["next_cursor"]
            if cursor is None:
                return pages
            self.assertNotIn(cursor, cursors, "continuation made no progress")
            cursors.add(cursor)
        self.fail("batch never reached an explicit end")

    def test_small_requests_share_one_response_and_sources_are_read_once(self):
        path = self.source("shared.txt", "complete small source")
        self.requests([{"command": "read", "path": str(path)} for _ in range(4)])
        with patch("scripts.inspect_extraction._load_source", wraps=inspect_extraction._load_source) as load:
            page = self.run_batch(budget=6000)
        self.assertIsNone(page["next_cursor"])
        self.assertEqual([r["request_index"] for r in page["results"]], list(range(4)))
        self.assertTrue(all(r["content"] == "complete small source" for r in page["results"]))
        self.assertEqual(sum(call.args[0] == path for call in load.call_args_list), 1)

    def test_one_budget_reconstructs_every_source_without_gaps_or_rewrites(self):
        contents = [(f"source {i}: " + 'α β "quotes" \\ slash\r\n\t\x01' * 120 + "end Ω") for i in range(5)]
        paths = [self.source(f"source-{i}.txt", text) for i, text in enumerate(contents)]
        before = {p: p.read_bytes() for p in paths}
        self.requests([{"command": "read", "path": str(p)} for p in paths])
        manifest_before = self.manifest.read_bytes()
        assembled = ["" for _ in paths]
        pages = self.all_pages()
        self.assertGreater(len(pages), 5)
        for page in pages:
            for result in page["results"]:
                index = result["request_index"]
                self.assertEqual(result["start"], len(assembled[index]))
                assembled[index] += result["content"]
                self.assertEqual(result["end"], len(assembled[index]))
                self.assertEqual(result["source_sha256"], hashlib.sha256(before[paths[index]]).hexdigest())
        self.assertEqual(assembled, contents)
        self.assertEqual({p: p.read_bytes() for p in paths}, before)
        self.assertEqual(self.manifest.read_bytes(), manifest_before)

    def test_json_selections_outline_and_overlapping_search_keep_exact_evidence(self):
        value = '[ 1.234567890123456789, 1e-1000, "\\u03b1", -0.00 ]'
        json_path = self.source("source.json", '{"a/b": ' + value + ', "items": [0,1,2,3,4,5]}')
        text_path = self.source("matches.txt", "aaaaa")
        self.requests([
            {"command": "read", "path": str(json_path), "pointer": "/a~1b"},
            {"command": "outline", "path": str(json_path), "pointer": "/items", "limit": 2},
            {"command": "find", "path": str(text_path), "query": "aa", "limit": 1},
        ])
        results = [r for page in self.all_pages(budget=3000) for r in page["results"]]
        self.assertEqual("".join(r["content"] for r in results if r["request_index"] == 0), value)
        self.assertEqual([e["pointer"] for r in results if r["request_index"] == 1 for e in r["entries"]],
                         [f"/items/{i}" for i in range(6)])
        self.assertEqual([m["match_start"] for r in results if r["request_index"] == 2 for m in r["matches"]],
                         [0, 1, 2, 3])

    def test_changed_later_source_or_manifest_invalidates_continuation(self):
        first = self.source("first.txt", "long source " * 1000)
        later = self.source("later.txt", "not returned yet")
        self.requests([{"command": "read", "path": str(p)} for p in (first, later)])
        page = self.run_batch()
        self.assertEqual([r["request_index"] for r in page["results"]], [0])
        later.write_text("changed later source", encoding="utf-8")
        self.assertIn("changed", self.run_batch(cursor=page["next_cursor"], expected=2))
        fresh = self.run_batch()
        with self.manifest.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        self.assertIn("changed", self.run_batch(cursor=fresh["next_cursor"], expected=2))

    def test_reader_change_invalidates_continuation(self):
        path = self.source("long.txt", "content " * 1000)
        self.requests([{"command": "read", "path": str(path)}])
        first = self.run_batch()
        changed_reader = self.source("changed_reader.py", "different reader implementation")
        with patch("scripts.inspect_extraction.__file__", str(changed_reader)):
            self.assertIn("changed", self.run_batch(cursor=first["next_cursor"], expected=2))

    def test_budget_can_change_while_cursor_keeps_the_exact_position(self):
        content = "precise continuation " * 200
        path = self.source("long.txt", content)
        self.requests([{"command": "read", "path": str(path), "start": 7}])
        first = self.run_batch()
        second = self.run_batch(cursor=first["next_cursor"], budget=12000)
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(first["results"][0]["content"] + second["results"][0]["content"], content[7:])

    def test_empty_views_and_absent_matches_complete_explicitly(self):
        empty = self.source("empty.txt", "")
        obj = self.source("empty.json", "{}")
        self.requests([
            {"command": "read", "path": str(empty)},
            {"command": "find", "path": str(empty), "query": "absent"},
            {"command": "outline", "path": str(obj)},
            {"command": "stat", "path": str(empty)},
        ])
        results = [r for page in self.all_pages() for r in page["results"]]
        self.assertEqual(len(results), 4)
        self.assertEqual(results[0]["content"], "")
        self.assertEqual(results[1]["matches"], [])
        self.assertEqual(results[2]["entries"], [])
        self.assertEqual(results[3]["source_bytes"], 0)

    def test_later_error_cannot_be_hidden_by_earlier_success(self):
        path = self.source("source.json", "{}")
        self.requests([{"command": "stat", "path": str(path)},
                       {"command": "read", "path": str(path), "pointer": "/absent"}])
        self.assertIn("batch request 1", self.run_batch(budget=6000, expected=2))

    def test_expected_hashes_are_checked_even_for_later_requests(self):
        path = self.source("long.txt", "source " * 1000)
        self.requests([{"command": "read", "path": str(path)},
                       {"command": "stat", "path": str(path), "expect_sha256": "0" * 64}])
        self.assertIn("request 1: source SHA-256 changed", self.run_batch(expected=2))

    def test_invalid_requests_and_cursors_fail_without_partial_output(self):
        path = self.source("source.txt", "a source")
        valid = {"command": "read", "path": str(path)}
        invalid = [[], {}, [None], [{"command": []}], [dict(valid, start=True)],
                   [dict(valid, max_output_chars=2000)], [dict(valid, if_view_token="0" * 64)],
                   [dict(valid, unknown="field")], [dict(valid, expect_sha256="bad")],
                   [{"command": "outline", "path": str(path), "pointer": None}],
                   [{"command": "find", "path": str(path), "query": ""}]]
        for value in invalid:
            with self.subTest(value=value):
                self.requests(value)
                self.run_batch(expected=2)
        self.requests([valid])
        for cursor in ("", "not-a-cursor", "!", "a" * 513):
            with self.subTest(cursor=cursor):
                self.run_batch(cursor=cursor, expected=2)

    def test_unfittable_request_stays_pending_instead_of_being_skipped(self):
        path = self.source("source.txt", "a" * 500 + "needle" + "z" * 500)
        self.requests([{"command": "stat", "path": str(path)},
                       {"command": "find", "path": str(path), "query": "needle", "context": 500}])
        first = self.run_batch()
        self.assertEqual([r["request_index"] for r in first["results"]], [0])
        self.assertIsNotNone(first["next_cursor"])
        self.assertIn("budget", self.run_batch(cursor=first["next_cursor"], expected=2))
        larger = self.run_batch(cursor=first["next_cursor"], budget=6000)
        self.assertIsNone(larger["next_cursor"])
        self.assertEqual(larger["results"][0]["request_index"], 1)
        self.assertEqual(larger["results"][0]["matches"][0]["match_start"], 500)

    def test_ambiguous_or_oversized_request_lists_are_rejected(self):
        path = self.source("source.txt", "content")
        request = {"command": "read", "path": str(path)}
        self.requests([request] * 101)
        self.assertIn("1 to 100", self.run_batch(expected=2))
        self.manifest.write_text('[{"command":"read","command":"stat","path":'
                                 + json.dumps(str(path)) + '}]', encoding="utf-8")
        self.assertIn("duplicate JSON keys", self.run_batch(expected=2))

    def test_batch_view_token_cannot_suppress_a_different_standalone_view(self):
        path = self.source("source.txt", "retained source " * 500)
        self.requests([{"command": "read", "path": str(path)}])
        first = self.run_batch()["results"][0]
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["read", str(path), "--max-output-chars", "2000",
                         "--if-view-token", first["view_token"]])
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertFalse(result["view_unchanged"])
        self.assertTrue(result["content"])


if __name__ == "__main__":
    unittest.main()
