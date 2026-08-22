from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "extraction_viewer.html"


def _source_between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


class _DocumentInventory(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.tags.append((tag, dict(attrs)))


class ExtractionViewerStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = VIEWER.read_text(encoding="utf-8")
        cls.inventory = _DocumentInventory()
        cls.inventory.feed(cls.source)

    def test_viewer_is_one_self_contained_file(self) -> None:
        script_sources = [
            attrs.get("src")
            for tag, attrs in self.inventory.tags
            if tag == "script" and attrs.get("src")
        ]
        stylesheet_links = [
            attrs.get("href")
            for tag, attrs in self.inventory.tags
            if tag == "link" and attrs.get("rel") == "stylesheet"
        ]

        self.assertEqual(script_sources, [])
        self.assertEqual(stylesheet_links, [])
        self.assertIn("<style>", self.source)
        self.assertIn("<script>", self.source)

    def test_private_content_has_defense_in_depth(self) -> None:
        csp = next(
            attrs.get("content", "")
            for tag, attrs in self.inventory.tags
            if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"
        )

        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertNotRegex(self.source, r"\.innerHTML\s*=")
        self.assertIn("new DOMParser().parseFromString", self.source)
        self.assertIn("DROP_CONTENT_HTML", self.source)
        self.assertNotRegex(self.source, r"\bfetch\s*\(")

    def test_private_root_picker_is_the_only_loading_mode(self) -> None:
        file_inputs = [
            attrs
            for tag, attrs in self.inventory.tags
            if tag == "input" and attrs.get("type") == "file"
        ]

        self.assertEqual(file_inputs, [])
        self.assertNotIn("webkitdirectory", self.source)
        self.assertNotIn("URLSearchParams", self.source)
        self.assertNotIn("jsonOnlyResolver", self.source)
        self.assertNotIn("urlResolver", self.source)
        self.assertNotIn("directoryResolver", self.source)
        self.assertIn("showDirectoryPicker", self.source)
        self.assertIn('id: "pip-litdb-private"', self.source)
        self.assertIn('mode: "read"', self.source)

    def test_picker_is_feature_gated_and_user_initiated(self) -> None:
        self.assertRegex(
            self.source,
            r"(?:[\"']showDirectoryPicker[\"']\s+in\s+window|"
            r"typeof\s+window\.showDirectoryPicker\s*===\s*[\"']function[\"'])",
        )
        self.assertIn("window.isSecureContext", self.source)
        self.assertRegex(
            self.source,
            r"addEventListener\([\"']click[\"']",
        )

    def test_library_discovery_is_shallow_and_exact(self) -> None:
        self.assertIn(r"/^\d{5}$/", self.source)
        self.assertIn('getDirectoryHandle("extraction")', self.source)
        self.assertIn('getFileHandle("record.json")', self.source)
        self.assertRegex(
            self.source,
            r"rootHandle\.(?:entries|values)\(\)",
        )
        self.assertNotIn("webkitRelativePath", self.source)
        self.assertIn("scanRootHandle", self.source)

    def test_viewer_supports_legacy_and_latest_record_schemas(self) -> None:
        self.assertIn('const EXPECTED_SCHEMA_VERSION = "1.1"', self.source)
        self.assertIn('new Set(["1.0", EXPECTED_SCHEMA_VERSION])', self.source)
        schema_source = _source_between(
            self.source,
            "function schemaMessages",
            "function setView",
        )
        self.assertIn("SUPPORTED_SCHEMA_VERSIONS.has(record.schema_version)", schema_source)

    def test_library_has_search_refresh_and_record_navigation(self) -> None:
        search_inputs = [
            attrs
            for tag, attrs in self.inventory.tags
            if tag == "input" and attrs.get("type") == "search"
        ]

        self.assertEqual(len(search_inputs), 1)
        for label in (
            "Extracted records",
            "Refresh records",
            "Change private folder",
            "All records",
        ):
            self.assertIn(label, self.source)
        self.assertIn('window.addEventListener("popstate"', self.source)
        self.assertIn('"pushState"', self.source)
        self.assertIn("history.back()", self.source)
        self.assertIn("scrollIntoView", self.source)

    def test_library_sort_search_and_invalid_record_isolation_are_explicit(self) -> None:
        self.assertGreaterEqual(self.source.count(".sort("), 2)
        self.assertIn("localeCompare", self.source)
        self.assertIn("numeric: true", self.source)
        self.assertIn("searchText", self.source)
        self.assertRegex(
            self.source,
            r"state\.records\.filter\([^\n]+\.includes\(query\)",
        )
        self.assertIn("valid: false", self.source)
        self.assertIn("Record unavailable", self.source)
        self.assertIn("declaredId !== folderId", self.source)
        self.assertIn("contains record_id", self.source)

    def test_record_id_is_required_and_cannot_fall_back_to_id(self) -> None:
        summary_source = _source_between(
            self.source,
            "function metadataSummary",
            "function recordSummary",
        )

        self.assertIn("metadata.record_id", summary_source)
        self.assertNotIn("metadata.id", summary_source)
        self.assertIn("does not declare a record_id", summary_source)

    def test_discovery_reads_only_a_bounded_metadata_prefix(self) -> None:
        probe_source = _source_between(
            self.source,
            "async function probeRecordFolder",
            "async function scanRootHandle",
        )

        self.assertIn("const METADATA_PREFIX_MAX_BYTES = 1024 * 1024", self.source)
        self.assertIn("readRecordMetadataPrefix", probe_source)
        self.assertNotIn("file.text()", probe_source)
        self.assertIn("file.slice(0, limit).text()", self.source)
        self.assertIn("Math.min(limit * 2", self.source)
        self.assertIn("extractTopLevelObject", self.source)

    def test_bounded_metadata_discovery_handles_escaped_and_bad_records(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available for metadata scanner tests")

        helpers = "\n".join(
            (
                "const METADATA_PREFIX_MAX_BYTES = 1024 * 1024;",
                _source_between(self.source, "function isObject", "function asArray"),
                _source_between(self.source, "function asArray", "function firstDefined"),
                _source_between(self.source, "function firstDefined", "function richPlain"),
                _source_between(self.source, "function richPlain", "function safeContentHref"),
                _source_between(self.source, "function jsonStringEnd", "function recordSummary"),
                _source_between(self.source, "function notFound", "function setLibraryMessage"),
            )
        )
        script = helpers + r'''
const assert = require("node:assert/strict");

(async () => {
  const stats = { fullReads: 0, slices: [] };

  function fakeFile(label, raw) {
    const bytes = Buffer.from(raw, "utf8");
    return {
      size: bytes.length,
      async text() {
        stats.fullReads += 1;
        throw new Error("Discovery must not read the full file.");
      },
      slice(start, end) {
        stats.slices.push({ label, start, end });
        return {
          async text() { return bytes.subarray(start, end).toString("utf8"); }
        };
      }
    };
  }

  function recordFolder(id, raw) {
    const file = fakeFile(id, raw);
    const recordFileHandle = {
      async getFile() { return file; }
    };
    const extractionHandle = {
      async getFileHandle(name) {
        assert.equal(name, "record.json");
        return recordFileHandle;
      }
    };
    return {
      kind: "directory",
      name: id,
      async getDirectoryHandle(name) {
        assert.equal(name, "extraction");
        return extractionHandle;
      }
    };
  }

  const escapedTitle = 'Escaped "{brace}" and \\\\ slash ' + "x".repeat(70000);
  const validRaw = JSON.stringify({
    schema_version: "1.0",
    record: {
      record_id: "00005",
      title: escapedTitle,
      authors: [{ name: "Ada Example" }],
      journal: "Journal of Prefix Tests",
      publication_year: 2026,
      doi: "10.0000/example"
    },
    sections: [{ title: "Body", blocks: [] }]
  });
  const oversizedRaw = '{"padding":"' + "x".repeat(METADATA_PREFIX_MAX_BYTES)
    + '","record":{"record_id":"00001"}}';
  const malformedRaw = '{"record":{"record_id":"00002","title":"unfinished"';
  const mismatchRaw = JSON.stringify({ record: { record_id: "99999" } });
  const idOnlyRaw = JSON.stringify({ record: { id: "00004" } });

  const staging = {
    kind: "directory",
    name: "staging",
    async getDirectoryHandle() { throw new Error("staging must not be probed"); }
  };
  const folders = [
    recordFolder("00005", validRaw),
    recordFolder("00004", idOnlyRaw),
    recordFolder("00003", mismatchRaw),
    recordFolder("00002", malformedRaw),
    recordFolder("00001", oversizedRaw)
  ];
  const root = {
    async *values() {
      yield staging;
      for (const folder of folders) yield folder;
    }
  };

  const result = await scanRootHandle(root);
  assert.equal(result.candidateCount, 5);
  assert.deepEqual(result.records.map(entry => entry.id), [
    "00001", "00002", "00003", "00004", "00005"
  ]);
  assert.deepEqual(
    result.records.filter(entry => entry.valid).map(entry => entry.id),
    ["00005"]
  );
  assert.equal(result.records.find(entry => entry.id === "00005").title, escapedTitle);
  assert.match(result.records.find(entry => entry.id === "00001").error, /first 1048576 bytes/);
  assert.match(result.records.find(entry => entry.id === "00002").error, /incomplete or missing/);
  assert.match(result.records.find(entry => entry.id === "00003").error, /contains record_id 99999/);
  assert.match(result.records.find(entry => entry.id === "00004").error, /does not declare a record_id/);
  assert.equal(stats.fullReads, 0);
  assert.ok(stats.slices.some(call => call.label === "00005" && call.end > 32 * 1024));
  assert.ok(stats.slices.every(call => call.start === 0));
  assert.ok(stats.slices.every(call => call.end <= METADATA_PREFIX_MAX_BYTES));
})().catch(error => {
  console.error(error && error.stack || error);
  process.exitCode = 1;
});
'''
        result = subprocess.run(
            [node, "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_folder_operations_have_a_status_visible_on_every_screen(self) -> None:
        status_nodes = [
            attrs
            for tag, attrs in self.inventory.tags
            if tag == "div" and attrs.get("id") == "operation-status"
        ]

        self.assertEqual(len(status_nodes), 1)
        self.assertEqual(status_nodes[0].get("aria-live"), "polite")
        self.assertLess(
            self.source.index('id="operation-status"'),
            self.source.index('id="landing"'),
        )
        self.assertIn("dom.operationStatus.replaceChildren", self.source)
        self.assertIn('setOperationStatus("error", `Could not read this folder:', self.source)
        self.assertIn('setOperationStatus("error", `Folder access was not granted:', self.source)

    def test_library_test_surface_supports_deterministic_browser_smoke_tests(self) -> None:
        self.assertIn("window.ExtractionViewer = Object.freeze", self.source)
        for function_name in (
            "scanRootHandle",
            "openRecordById",
            "showLibrary",
            "renderSynthetic",
            "normalizeRelativePath",
            "latestStagedRecord",
        ):
            self.assertRegex(
                self.source,
                rf"\b{re.escape(function_name)}\b",
            )

    def test_record_switching_guards_async_work_and_releases_blob_urls(self) -> None:
        self.assertRegex(
            self.source,
            r"(?:load|selection|request)(?:Token|Generation)",
        )
        self.assertIn("recordOpenToken", self.source)
        open_source = _source_between(
            self.source,
            "async function openRecordById",
            "function setScanBusy",
        )
        root_source = _source_between(
            self.source,
            "async function loadRootHandle",
            "async function choosePrivateRoot",
        )
        self.assertIn("const openToken = ++state.recordOpenToken", open_source)
        self.assertIn("const assetToken = ++state.selectionToken", open_source)
        self.assertIn("state.recordOpenToken += 1", root_source)
        self.assertIn('showLibrary({ historyMode: "replace" })', open_source)
        self.assertIn("URL.revokeObjectURL", self.source)
        self.assertIn("clearObjectUrls", self.source)
        self.assertIn('window.addEventListener("beforeunload"', self.source)

    def test_handle_asset_resolution_remains_local_and_path_safe(self) -> None:
        self.assertIn("normalizeRelativePath", self.source)
        self.assertIn("getDirectoryHandle", self.source)
        self.assertIn("getFileHandle", self.source)
        self.assertIn("URL.createObjectURL", self.source)
        self.assertIn('raw.includes("\\\\")', self.source)
        self.assertIn('part === ".."', self.source)
        self.assertIn('part === "."', self.source)

    def test_path_and_legacy_image_mime_helpers_are_deterministic(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available for viewer helper tests")

        script = "\n".join(
            (
                'const assert = require("node:assert/strict");',
                "function firstDefined(...values) { return values.find(value => value !== undefined && value !== null && value !== \"\") || \"\"; }",
                _source_between(self.source, "function normalizeRelativePath", "function clearObjectUrls"),
                _source_between(self.source, "function assetMediaType", "function attachRuntimeFailure"),
                'assert.equal(normalizeRelativePath("figures/a.png"), "figures/a.png");',
                'for (const value of ["../a.png", "a//b.png", " a.png", "a.png ", "a\\\\b.png", "C:/a.png"]) assert.throws(() => normalizeRelativePath(value));',
                'assert.equal(assetMediaType({ path: "figure.tiff" }, "figure.tiff"), "image/tiff");',
                'assert.equal(assetMediaType({ path: "figure.emf" }, "figure.emf"), "image/emf");',
                'assert.equal(assetMediaType({ path: "figure.wdp" }, "figure.wdp"), "image/vnd.ms-photo");',
                'assert.equal(browserCanDisplayImage("image/png"), true);',
                'assert.equal(browserCanDisplayImage("image/tiff"), false);',
                'assert.equal(browserCanDisplayImage("image/emf"), false);',
                'assert.equal(browserCanDisplayImage("image/x-emf"), false);',
                'assert.equal(browserCanDisplayImage("image/vnd.ms-photo"), false);',
            )
        )
        result = subprocess.run(
            [node, "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_v1_content_and_asset_types_have_renderers(self) -> None:
        for field in (
            "front_matter",
            "sections",
            "figures",
            "tables",
            "supporting_information",
            "supplements",
            "references",
            "assets",
            "asset_ids",
        ):
            self.assertIn(field, self.source)

        for media_element in ("img", "video", "audio", "table"):
            self.assertRegex(
                self.source,
                rf'element\("{re.escape(media_element)}"',
            )
        self.assertIn('record.content_format !== "safe-html"', self.source)
        self.assertIn("Asset unavailable", self.source)

    def test_large_slide_asset_sets_are_collapsed_and_unsupported_images_download(self) -> None:
        self.assertIn("Embedded slide assets (${components.length})", self.source)
        self.assertIn("components.length > 12", self.source)
        self.assertIn('details.addEventListener("toggle"', self.source)
        self.assertIn("if (!details.open || rendered) return", self.source)
        self.assertIn("function browserCanDisplayImage", self.source)
        self.assertIn('"image/tiff"', self.source)
        for extension in ("emf", "wdp", "jxr"):
            self.assertRegex(self.source, rf"\b{extension}:\s*[\"']image/")
        display_source = _source_between(
            self.source,
            "function browserCanDisplayImage",
            "function attachRuntimeFailure",
        )
        self.assertNotIn("image/tiff", display_source)
        self.assertNotIn("image/x-emf", display_source)
        self.assertNotIn("image/vnd.ms-photo", display_source)
        table_part_source = _source_between(
            self.source,
            "function tableCellStructure",
            "function renderTablePart",
        )
        remaining_source = _source_between(
            self.source,
            "function renderRemainingTableStructures",
            "function renderTableCard",
        )
        for source in (table_part_source, remaining_source):
            self.assertIn("browserCanDisplayImage", source)
            self.assertIn("downloadCard", source)

    def test_supplement_labels_prefer_the_first_figure_label(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available for supplement label tests")

        script = "\n".join(
            (
                'const assert = require("node:assert/strict");',
                _source_between(self.source, "function asArray", "function firstDefined"),
                _source_between(self.source, "function firstDefined", "function richPlain"),
                _source_between(self.source, "function supplementDisplayLabel", "function supplementFile"),
                'assert.equal(supplementDisplayLabel({ label: "Explicit", supplement_id: "supplement_001", figures: [{ label: "Figure SI1" }] }), "Explicit");',
                'assert.equal(supplementDisplayLabel({ supplement_id: "supplement_001", figures: [{ label: "Figure SI1" }] }), "Figure SI1");',
                'assert.equal(supplementDisplayLabel({ supplement_id: "supplement_001", figures: [{}, { label: "Figure SI2" }] }), "Figure SI2");',
                'assert.equal(supplementDisplayLabel({ supplement_id: "supplement_001", figures: [] }), "supplement_001");',
            )
        )
        result = subprocess.run(
            [node, "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        supplement_file_source = _source_between(
            self.source,
            "function supplementFile",
            "function renderSupplements",
        )
        self.assertIn("supplementDisplayLabel(supplement)", supplement_file_source)

    def test_supplement_figures_and_data_render_before_blocks_and_tables(self) -> None:
        render_source = _source_between(
            self.source,
            "function renderSupplements",
            "function renderGenericAsset",
        )

        figure_index = render_source.index("for (const figure of asArray(supplement.figures))")
        blocks_index = render_source.index("renderBlocks(supplement.blocks, card)")
        data_index = render_source.index('asset.kind !== "supplement_data"')
        data_append_index = render_source.index("card.append(dataSection)")
        tables_index = render_source.index('renderTableCard(table, "supplement-table")')
        remaining_index = render_source.index("const remainingAssets = []")

        self.assertLess(figure_index, blocks_index)
        self.assertLess(blocks_index, data_index)
        self.assertLess(data_index, data_append_index)
        self.assertLess(data_append_index, tables_index)
        self.assertLess(tables_index, remaining_index)
        self.assertIn("Supplementary data files", render_source)
        self.assertIn("representedAssetIds.add(assetId)", render_source)
        self.assertIn("renderGenericAsset(asset", render_source)

    def test_staging_fallback_is_live_first_latest_and_clearly_unapproved(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available for staged discovery tests")

        helpers = "\n".join(
            (
                "const METADATA_PREFIX_MAX_BYTES = 1024 * 1024;",
                _source_between(self.source, "function isObject", "function asArray"),
                _source_between(self.source, "function asArray", "function firstDefined"),
                _source_between(self.source, "function firstDefined", "function richPlain"),
                _source_between(self.source, "function richPlain", "function safeContentHref"),
                _source_between(self.source, "function jsonStringEnd", "function recordSummary"),
                _source_between(self.source, "function notFound", "function setLibraryMessage"),
            )
        )
        script = helpers + r'''
const assert = require("node:assert/strict");

function missing(name) {
  const error = new Error(`Missing ${name}`);
  error.name = "NotFoundError";
  return error;
}

function recordFile(id, title, lastModified) {
  const bytes = Buffer.from(JSON.stringify({
    schema_version: "1.1",
    record: { record_id: id, title, authors: [], journal: "J", publication_year: 2026 }
  }), "utf8");
  return {
    size: bytes.length,
    lastModified,
    async text() { return bytes.toString("utf8"); },
    slice(start, end) {
      return { async text() { return bytes.subarray(start, end).toString("utf8"); } };
    }
  };
}

function fileHandle(name, file) {
  return { kind: "file", name, async getFile() { return file; } };
}

function directory(name, children = {}) {
  return {
    kind: "directory",
    name,
    async getDirectoryHandle(child) {
      const handle = children[child];
      if (!handle || handle.kind !== "directory") throw missing(child);
      return handle;
    },
    async getFileHandle(child) {
      const handle = children[child];
      if (!handle || handle.kind !== "file") throw missing(child);
      return handle;
    },
    async *values() { for (const handle of Object.values(children)) yield handle; }
  };
}

function run(name, id, title, modified) {
  return directory(name, {
    extraction: directory("extraction", {
      "record.json": fileHandle("record.json", recordFile(id, title, modified))
    })
  });
}

(async () => {
  const live = directory("00001", {
    extraction: directory("extraction", {
      "record.json": fileHandle("record.json", recordFile("00001", "Live", 10))
    })
  });
  const stagedOnly = directory("00002");
  const invalidLive = directory("00003", {
    extraction: directory("extraction", {
      "record.json": fileHandle("record.json", recordFile("99999", "Invalid live", 1))
    })
  });
  const staging = directory("staging", {
    "00001": directory("00001", { "run-9": run("run-9", "00001", "Staged must lose", 999) }),
    "00002": directory("00002", {
      ".run-99.building": run(".run-99.building", "00002", "Incomplete", 9999),
      "run-2": run("run-2", "00002", "Tie loses deterministically", 200),
      "run-10": run("run-10", "00002", "Latest", 200)
    }),
    "00003": directory("00003", { "run-1": run("run-1", "00003", "Fallback must lose", 500) })
  });
  const root = directory("papers (private)", { staging, "00001": live, "00002": stagedOnly, "00003": invalidLive });

  const result = await scanRootHandle(root);
  const first = result.records.find(entry => entry.id === "00001");
  const second = result.records.find(entry => entry.id === "00002");
  const third = result.records.find(entry => entry.id === "00003");
  assert.equal(first.title, "Live");
  assert.equal(first.staged, false);
  assert.equal(second.title, "Latest");
  assert.equal(second.staged, true);
  assert.equal(second.runName, "run-10");
  assert.equal(second.recordPath, "staging/00002/run-10/extraction/record.json");
  assert.equal(third.valid, false);
  assert.match(third.error, /contains record_id 99999/);
})().catch(error => {
  console.error(error && error.stack || error);
  process.exitCode = 1;
});
'''
        result = subprocess.run(
            [node, "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Staged / unapproved", self.source)
        self.assertIn("Staged preview only", self.source)
        open_source = _source_between(
            self.source,
            "async function openRecordById",
            "function setScanBusy",
        )
        self.assertIn(
            "handleResolver(entry.extractionHandle, entry.recordPath",
            open_source,
        )

    def test_staging_access_error_preserves_live_records_and_is_reported(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available for staged discovery tests")

        helpers = "\n".join(
            (
                "const METADATA_PREFIX_MAX_BYTES = 1024 * 1024;",
                _source_between(self.source, "function isObject", "function asArray"),
                _source_between(self.source, "function asArray", "function firstDefined"),
                _source_between(self.source, "function firstDefined", "function richPlain"),
                _source_between(self.source, "function richPlain", "function safeContentHref"),
                _source_between(self.source, "function jsonStringEnd", "function recordSummary"),
                _source_between(self.source, "function notFound", "function setLibraryMessage"),
            )
        )
        script = helpers + r'''
const assert = require("node:assert/strict");

function missing(name) {
  const error = new Error(`Missing ${name}`);
  error.name = "NotFoundError";
  return error;
}

function recordFile(id) {
  const bytes = Buffer.from(JSON.stringify({
    schema_version: "1.1",
    record: { record_id: id, title: "Live record", authors: [] }
  }), "utf8");
  return {
    size: bytes.length,
    lastModified: 1,
    slice(start, end) {
      return { async text() { return bytes.subarray(start, end).toString("utf8"); } };
    }
  };
}

const file = recordFile("00001");
const liveFolder = {
  kind: "directory",
  name: "00001",
  async getDirectoryHandle(name) {
    assert.equal(name, "extraction");
    return {
      async getFileHandle(fileName) {
        assert.equal(fileName, "record.json");
        return { async getFile() { return file; } };
      }
    };
  }
};
const root = {
  async getDirectoryHandle(name) {
    assert.equal(name, "staging");
    const error = new Error("Access denied by browser sandbox");
    error.name = "NotAllowedError";
    throw error;
  },
  async *values() { yield liveFolder; }
};

(async () => {
  const result = await scanRootHandle(root);
  assert.deepEqual(result.records.map(entry => entry.id), ["00001"]);
  assert.equal(result.records[0].valid, true);
  assert.match(result.stagingAccessWarning, /could not read staging\//);
  assert.match(result.stagingAccessWarning, /Live extractions are still shown/);
  assert.match(result.stagingAccessWarning, /Access denied by browser sandbox/);
})().catch(error => {
  console.error(error && error.stack || error);
  process.exitCode = 1;
});
'''
        result = subprocess.run(
            [node, "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        load_source = _source_between(
            self.source,
            "async function loadRootHandle",
            "async function choosePrivateRoot",
        )
        self.assertIn(
            'setLibraryMessage("warning", result.stagingAccessWarning)',
            load_source,
        )

    def test_inline_javascript_is_syntactically_valid(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available for JavaScript syntax validation")

        scripts = re.findall(r"<script>(.*?)</script>", self.source, flags=re.DOTALL)
        self.assertEqual(len(scripts), 1)
        result = subprocess.run(
            [node, "--check", "-"],
            input=scripts[0],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
