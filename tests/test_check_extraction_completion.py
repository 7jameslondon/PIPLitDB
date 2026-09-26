from __future__ import annotations

import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import check_extraction_completion as checks


class CompletionCheckTests(unittest.TestCase):
    def test_viewer_omits_null_and_default_browser_channels(self):
        node = checks._node_executable(None)
        adapter = Path(checks.__file__).with_name("check_extraction_viewer.cjs").resolve()
        program = """
          const assert = require('node:assert/strict');
          const {browserChannel} = require(process.argv[1]);
          assert.equal(browserChannel(null), undefined);
          assert.equal(browserChannel(undefined), undefined);
          assert.equal(browserChannel('chromium'), undefined);
          assert.equal(browserChannel('chrome'), 'chrome');
          assert.equal(browserChannel('msedge'), 'msedge');
        """
        result = subprocess.run([node, "-e", program, str(adapter)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_element_capture_aligns_top_and_left_before_screenshot_without_layout_mutation(self):
        node = checks._node_executable(None)
        adapter = Path(checks.__file__).with_name("check_extraction_viewer.cjs").resolve()
        program = """
          const assert = require('node:assert/strict');
          const {captureElement} = require(process.argv[1]);
          (async () => {
            for (const top of [59, -800, 0]) {
              const events = [];
              const view = {scrollX:7, scrollY:1000, scrollTo:options => events.push(options)};
              const element = {ownerDocument:{defaultView:view}, getBoundingClientRect:() => ({top})};
              const locator = {
                evaluate:async callback => callback(element),
                screenshot:async options => {events.push(options); return 'captured';}
              };
              assert.equal(await captureElement(locator, 'private/section.png'), 'captured');
              assert.deepEqual(events, [
                {top:1000+top,left:0,behavior:'instant'},
                {path:'private/section.png',style:'.topbar { visibility: hidden !important; }'}
              ]);
              assert.deepEqual(Object.keys(element), ['ownerDocument','getBoundingClientRect']);
            }
          })().catch(error => {console.error(error); process.exitCode=1;});
        """
        result = subprocess.run([node, "-e", program, str(adapter)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_element_screenshots_hide_only_sticky_toolbar_during_capture(self):
        node = checks._node_executable(None)
        adapter = Path(checks.__file__).with_name("check_extraction_viewer.cjs").resolve()
        program = """
          const assert = require('node:assert/strict');
          const {screenshotOptions} = require(process.argv[1]);
          assert.deepEqual(screenshotOptions('private/table.png'), {
            path:'private/table.png', style:'.topbar { visibility: hidden !important; }'
          });
        """
        result = subprocess.run([node, "-e", program, str(adapter)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.private = self.root / "papers (private)"
        self.record = self.private / "00001"
        self.diagnostic = self.record / "extraction_diagnostic"
        self.reviews = self.diagnostic / "reviews"
        self.reviews.mkdir(parents=True)
        (self.record / "extraction").mkdir()
        (self.record / "extraction/record.json").write_text('{"record":{"title":"Synthetic","record_id":"00001"}}')
        (self.root / "extraction_viewer.html").write_text("Synthetic viewer fixture")
        (self.record / "html").mkdir()
        (self.record / "html/main.html").write_text("Synthetic source")
        (self.root / "database/records").mkdir(parents=True)
        self.metadata = self.root / "database/records/00001.yaml"
        self.metadata.write_text("pip_litdb_status: extracted_approved\njamies_human_only_notes:\n  - exact_human_tag\n")
        (self.root / "tmp").mkdir()
        self.queue = self.root / "tmp/EXTRACTION_QUEUE.md"
        self.queue.write_text("1. **`00001`** — Extraction completed and approved.\n", encoding="utf-8")
        for name in ("text_reading.json", "scientific_notation.json", "figures.json",
                     "ai_readiness.json", "adversarial.json", "adjudication.json"):
            (self.reviews / name).write_text('{"status":"pass"}')
        evidence = checks._review_evidence(self.diagnostic, [])
        evidence["reproducibility"] = {"run_id": "clean", "manifest_sha256": "a" * 64}
        self.approval = {"approval_mode": "standing_policy", "accepted_finding_codes": [],
                         "finalization_evidence": evidence}
        self.save_approval()
        self.logs = self.private / "diagnostics"
        self.logs.mkdir()
        self.test_log = self.logs / "unittest.log"
        self.test_log.write_text("Complete synthetic unittest log\n")
        self.test_report = self.logs / "tests.json"
        self.test_data = {
            "status": "passed", "exit_code": 0, "issues": [],
            "counts": {"tests_run": 3, "passed": 2, "skipped": 1, "errors": 0, "failures": 0,
                       "expected_failures": 0, "unexpected_successes": 0},
            "command": ["python", "run_tests.py", "--worker", str(self.test_report), "discover", "-s", "tests", "-v"],
            "working_directory": str(self.root),
            "log": checks._file(self.test_log),
        }
        self.save_tests()

    def save_approval(self):
        (self.diagnostic / "approval.json").write_text(json.dumps(self.approval))

    def save_tests(self):
        self.test_report.write_text(json.dumps(self.test_data))

    def run_check(self, viewer=None):
        cleanup = SimpleNamespace(approved_run_id="reviewed", staging_root=self.private / "staging/00001")
        with patch.object(checks, "cleanup_approved_staging", return_value=cleanup) as guard:
            with patch.object(checks, "_viewer", side_effect=viewer) as browser:
                browser.return_value = {"report": {}, "counts": {}, "screenshots": 2, "downloads": 0,
                                        "duration_seconds": 1, "visual_inspection": "pending"}
                summary, code = checks.check_completion(self.root, "00001", test_report=self.test_report,
                                                        log_dir=self.logs / "runs", node="synthetic-node")
        guard.assert_called_once_with(self.root, "00001", remove=False)
        report = checks._json(Path(summary["report_path"]))
        self.assertLess(len(json.dumps(summary)), 6000)
        return summary, code, report, browser.call_count

    def test_success_preserves_inputs_and_requires_real_visual_review(self):
        before = checks._snapshot(self.root, "00001")
        summary, code, report, calls = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(calls, 1)
        self.assertEqual(summary["status"], "machine_checks_passed")
        self.assertEqual(summary["visual_inspection"], "pending")
        self.assertEqual(report["checks"]["read_only_inputs"], "passed")
        self.assertEqual(checks._snapshot(self.root, "00001"), before)
        self.assertEqual(len(report["evidence"]["review_roles"]), 5)
        self.assertEqual(report["tests"]["log"]["sha256"], checks.sha256_file(self.test_log))
        self.assertTrue(report["remaining_agent_checks"])

    def test_changed_test_log_blocks_browser(self):
        self.test_log.write_text("Altered after test completion")
        _, code, report, calls = self.run_check()
        self.assertEqual(code, 2)
        self.assertEqual(calls, 0)
        self.assertIn("log bytes", report["error"])

    def test_filtered_test_run_cannot_satisfy_full_suite_evidence(self):
        self.test_data["command"] += ["-p", "test_one.py"]
        self.save_tests()
        _, code, report, calls = self.run_check()
        self.assertEqual((code, calls), (2, 0))
        self.assertIn("filtered", report["error"])

    def test_full_suite_report_from_another_repository_is_rejected(self):
        self.test_data["working_directory"] = str(self.root / "other")
        self.save_tests()
        _, code, report, calls = self.run_check()
        self.assertEqual((code, calls), (2, 0))
        self.assertIn("this repository", report["error"])

    def test_empty_failed_or_inconsistent_test_results_fail(self):
        original = json.loads(json.dumps(self.test_data))
        variants = [
            {"status": "incomplete"},
            {"counts": {**original["counts"], "passed": 0}},
            {"counts": {**original["counts"], "tests_run": 4}},
            {"counts": {**original["counts"], "errors": 1}},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.test_data = {**original, **variant}
                self.save_tests()
                _, code, _, calls = self.run_check()
                self.assertEqual((code, calls), (2, 0))

    def test_missing_review_role_blocks_browser(self):
        (self.reviews / "adversarial.json").unlink()
        _, code, report, calls = self.run_check()
        self.assertEqual((code, calls), (2, 0))
        self.assertIn("all five", report["error"])

    def test_changed_review_inventory_blocks_browser(self):
        (self.reviews / "extra-review.json").write_text('{}')
        _, code, report, calls = self.run_check()
        self.assertEqual((code, calls), (2, 0))
        self.assertIn("differs from approval", report["error"])

    def test_missing_distinct_rebuild_evidence_blocks_browser(self):
        self.approval["finalization_evidence"]["reproducibility"]["run_id"] = "reviewed"
        self.save_approval()
        _, code, report, calls = self.run_check()
        self.assertEqual((code, calls), (2, 0))
        self.assertIn("clean-rebuild", report["error"])

    def test_wrong_or_duplicate_queue_entry_blocks_browser(self):
        for text in ("1. **`00001`** — Pending\n", self.queue.read_text(encoding="utf-8") * 2):
            self.queue.write_text(text, encoding="utf-8")
            _, code, report, calls = self.run_check()
            self.assertEqual((code, calls), (2, 0))
            self.assertIn("queue", report["error"])

    def test_browser_failure_is_saved_with_bounded_console_output(self):
        summary, code, report, _ = self.run_check(RuntimeError("Browser failure " + "x" * 20000))
        self.assertEqual(code, 2)
        self.assertEqual(report["checks"]["viewer"], "failed")
        self.assertLessEqual(len(summary["error_preview"]), 500)
        self.assertGreater(len((Path(summary["report_path"]).parent / "error.log").read_text()), 20000)

    def test_inputs_changed_during_browser_check_fail(self):
        def mutate(*args):
            self.metadata.write_text("Unexpected concurrent change")
            return {"report": {}, "counts": {}, "screenshots": 0, "downloads": 0, "duration_seconds": 1}
        _, code, report, _ = self.run_check(mutate)
        self.assertEqual(code, 2)
        self.assertEqual(report["checks"]["read_only_inputs"], "failed")

    def test_existing_staging_is_preserved_and_blocks_completion(self):
        staging = self.private / "staging/00001"
        staging.mkdir(parents=True)
        sentinel = staging / "keep.txt"
        sentinel.write_text("Preserve")
        _, code, _, calls = self.run_check()
        self.assertEqual((code, calls), (2, 0))
        self.assertEqual(sentinel.read_text(), "Preserve")

    def test_output_outside_private_diagnostics_is_rejected(self):
        public = self.root / "public-results"
        with self.assertRaises(ValueError):
            checks.check_completion(self.root, "00001", test_report=self.test_report, log_dir=public)
        self.assertFalse(public.exists())

    def test_browser_uses_private_temporary_directories_without_changing_parent_environment(self):
        output = self.logs / "browser-test"
        (output / "viewer").mkdir(parents=True)
        (output / "viewer/report.json").write_text(json.dumps({
            "status": "passed", "counts": {}, "screenshots": [], "downloads": [], "duration_seconds": 1,
        }))
        environment_before = dict(checks.os.environ)
        with patch.object(checks.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as process:
            checks._viewer(output / "config.json", output, "node", 10)
        environment = process.call_args.kwargs["env"]
        for key in ("TEMP", "TMP", "TMPDIR"):
            self.assertEqual(Path(environment[key]), output / "browser-runtime")
            self.assertTrue(Path(environment[key]).is_dir())
        self.assertEqual(dict(checks.os.environ), environment_before)


class BrowserAdapterRegressionTests(unittest.TestCase):
    def test_file_tree_is_path_only_and_does_not_embed_asset_bytes(self):
        try:
            node = checks._node_executable(None)
        except ValueError as error:
            self.skipTest(str(error))
        adapter = Path(checks.__file__).with_name("check_extraction_viewer.cjs").resolve()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "figures").mkdir()
            (root / "record.json").write_bytes(b'{"record":true}')
            (root / "figures/large.bin").write_bytes(b"not-serialized" * 1000)
            program = r'''
                const assert = require('node:assert/strict');
                const {tree} = require(process.argv[1]);
                const value=tree(process.argv[2]);
                assert.equal(value.children['record.json'].relative,'record.json');
                assert.equal(value.children.figures.children['large.bin'].relative,'figures/large.bin');
                assert.equal(value.children.figures.children['large.bin'].type,'application/octet-stream');
                assert(!JSON.stringify(value).includes('not-serialized'));
                assert(!Object.hasOwn(value.children['record.json'],'base64'));
            '''
            result = subprocess.run([node, "-e", program, str(adapter), str(root)],
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_source_omission_requires_exact_diagnostics_and_card_identity(self):
        try:
            node = checks._node_executable(None)
        except ValueError as error:
            self.skipTest(str(error))
        adapter = Path(checks.__file__).with_name("check_extraction_viewer.cjs").resolve()
        program = r'''
            const assert = require('node:assert/strict');
            const {documentedSourceOmissions: classify, checkAssetPlaceholders: check,
                sourceOmissionEvidence: load}
                = require(process.argv[1]);
            const record = {record:{record_id:'00001'}, figures:[
                {figure_id:'synopsis',kind:'graphical_abstract'},
                {figure_id:'figure_1',kind:'figure',asset_id:'image_1'}],
                assets:[{asset_id:'image_1'}]};
            const source = 'papers (private)/00001/html/main.html';
            const evidence = {coverage:[
                {output_id:'synopsis',output_path:'record.json',output_locator:{json_pointer:'/figures/0'},
                 content_kind:'graphical_abstract',status:'included',source_path:source,source_locator:'/article/figure'},
                {coverage_id:'unresolved-graphical-abstract-image',content_kind:'graphical_abstract_image',
                 status:'intentionally_excluded',source_path:source,source_locator:'/article/figure',reason:'Confirmed empty source'}],
                warnings:[{code:'graphical_abstract_image_unavailable',source_path:source,message:'No source pixels'}],
                anomalies:[{anomaly_id:'empty-artwork',coverage_id:'unresolved-graphical-abstract-image',
                    source_path:source,source_locator:'/article/figure',observed:'Empty figure',
                    assessment:'Archive omission',disposition:'Preserve synopsis'}],
                sources:{record_id:'00001',sources:[{path:source,role:'main_html',sha256:'a'.repeat(64)}]}};
            const allowed = classify(record,evidence);
            assert.equal(allowed.length,1);
            assert.equal(allowed[0].figure_id,'synopsis');
            assert.equal(allowed[0].anomaly_id,'empty-artwork');
            check(0,['figure-synopsis'],allowed);
            assert.deepEqual(classify({figures:[],assets:[]}),[]);
            check(0,[],[]);
            assert.throws(()=>classify(record));
            for (const mutate of [
                (r,e)=>r.figures[0].kind='figure',
                (r,e)=>r.figures[0].asset_id='',
                (r,e)=>r.figures[0].asset_id=null,
                (r,e)=>r.figures[0].asset_id='missing',
                (r,e)=>r.figures[1].asset_id='missing',
                (r,e)=>r.figures.push({figure_id:'another',kind:'graphical_abstract'}),
                (r,e)=>r.figures[1].figure_id='synopsis',
                (r,e)=>e.coverage[0].output_locator.json_pointer='/figures/1',
                (r,e)=>e.coverage[0].output_id='another',
                (r,e)=>e.coverage[1].status='unresolved',
                (r,e)=>e.coverage[1].source_path='another.html',
                (r,e)=>e.coverage.push(structuredClone(e.coverage[1])),
                (r,e)=>e.anomalies[0].coverage_id='unrelated',
                (r,e)=>e.anomalies[0].source_path='another.html',
                (r,e)=>e.anomalies[0].assessment='',
                (r,e)=>e.warnings=[],
                (r,e)=>e.sources.record_id='00002',
                (r,e)=>e.sources.sources[0].role='public_metadata',
                (r,e)=>e.sources.sources[0].sha256='invalid',
            ]) {
                const r=structuredClone(record), e=structuredClone(evidence);
                mutate(r,e); assert.throws(()=>classify(r,e));
            }
            for (const [errors,cards] of [[1,['figure-synopsis']], [0,[]],
                [0,['figure-other']], [0,['figure-synopsis','figure-synopsis']],
                [0,['figure-synopsis',null]]]) {
                assert.throws(()=>check(errors,cards,allowed));
            }
            const fs=require('node:fs'), path=require('node:path'), crypto=require('node:crypto');
            const digest=value=>crypto.createHash('sha256').update(value).digest('hex');
            const root=process.argv[2], extraction=path.join(root,'papers (private)/00001/extraction');
            const diagnostic=path.join(path.dirname(extraction),'extraction_diagnostic');
            fs.mkdirSync(extraction,{recursive:true}); fs.mkdirSync(diagnostic);
            fs.mkdirSync(path.dirname(path.join(root,source)),{recursive:true});
            const sourceBytes=Buffer.from('<article><figure></figure></article>');
            fs.writeFileSync(path.join(root,source),sourceBytes);
            const recordBytes=Buffer.from(JSON.stringify(record));
            fs.writeFileSync(path.join(extraction,'record.json'),recordBytes);
            const bound=structuredClone(evidence);
            bound.sources.sources[0].sha256=digest(sourceBytes);
            bound.sources.source_fingerprint='b'.repeat(64);
            const manifest={record_id:'00001',source_fingerprint:'b'.repeat(64),
                files:[{path:'record.json',sha256:digest(recordBytes),bytes:recordBytes.length}]};
            const write=()=>{
                for (const [name,rows] of [['coverage',bound.coverage],['warnings',bound.warnings],
                    ['source_anomalies',bound.anomalies]]) {
                    fs.writeFileSync(path.join(diagnostic,name+'.jsonl'),rows.map(x=>JSON.stringify(x)).join('\n'));
                }
                fs.writeFileSync(path.join(diagnostic,'sources.json'),JSON.stringify(bound.sources));
                fs.writeFileSync(path.join(diagnostic,'manifest.json'),JSON.stringify(manifest));
            };
            write();
            const loaded=load(root,extraction,record,recordBytes);
            assert.equal(loaded.limitations[0].figure_id,'synopsis');
            assert.equal(loaded.files.length,5);
            assert(loaded.files.every(file=>/^[a-f0-9]{64}$/.test(file.sha256)));
            assert.throws(()=>load(root,extraction,record,Buffer.from('{}')));
            fs.writeFileSync(path.join(root,source),'changed source');
            assert.throws(()=>load(root,extraction,record,recordBytes));
            fs.writeFileSync(path.join(root,source),sourceBytes);
            manifest.source_fingerprint='c'.repeat(64); write();
            assert.throws(()=>load(root,extraction,record,recordBytes));
            manifest.source_fingerprint='b'.repeat(64); write();
            fs.unlinkSync(path.join(diagnostic,'source_anomalies.jsonl'));
            assert.throws(()=>load(root,extraction,record,recordBytes));
            write();
            bound.coverage.forEach(row=>row.source_path='../outside.html');
            bound.anomalies[0].source_path='../outside.html';
            bound.warnings[0].source_path='../outside.html';
            bound.sources.sources[0].path='../outside.html'; write();
            assert.throws(()=>load(root,extraction,record,recordBytes));
        '''
        with TemporaryDirectory() as directory:
            result = subprocess.run([node, "-e", program, str(adapter), directory],
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_canonical_title_and_download_path_contract(self):
        try:
            node = checks._node_executable(None)
        except ValueError as error:
            self.skipTest(str(error))
        adapter = Path(checks.__file__).with_name("check_extraction_viewer.cjs").resolve()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "local.pdf").write_bytes(b"Synthetic supplement")
            program = r'''
                const assert = require('node:assert/strict');
                const {expectedTitle, within} = require(process.argv[1]);
                assert.equal(expectedTitle({record:{title:'Canonical title'}}),'Canonical title');
                assert.equal(expectedTitle({record:{title:{plain_text:'Rich title'}}}),'Rich title');
                assert.throws(()=>expectedTitle({metadata:{title:'Wrong field'}}));
                assert.throws(()=>expectedTitle({record:{title:''}}));
                assert(within(process.argv[2],'local.pdf').endsWith('local.pdf'));
                for (const value of ['../outside.pdf','/outside.pdf','C:/outside.pdf','a\\b.pdf','a/../local.pdf']) {
                    assert.throws(()=>within(process.argv[2],value));
                }
            '''
            result = subprocess.run([node, "-e", program, str(adapter), str(root)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
