from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts.extraction.coordination import (
    check_owner, close_batch, import_owner_runs, prepare_batch,
)
from scripts.extraction.discovery import discover_sources, source_fingerprint
from scripts.extraction.finalization import finalize_extraction
from scripts.extraction.locking import OWNER_WORKSPACE_MARKER, repository_lock
from scripts.extraction.paths import atomic_write_json, sha256_file
from scripts.extraction.promotion import promote_extraction
from scripts.extraction.reporting import _pipeline_code_sha256


class ExtractionCoordinationTests(unittest.TestCase):
    def fixture(self, directory: str) -> Path:
        root = Path(directory)
        for name, content in {
            'scripts/extraction/worker.py': '# frozen runtime\n',
            'tests/test_example.py': '# frozen tests\n',
            'database/schema/example.json': '{}\n',
            'EXTRACTION_PROTOCOL.md': 'Frozen protocol.\n',
            '.github/workflows/check.yml': 'name: frozen\n',
            'tmp/EXTRACTION_QUEUE.md': ''.join(
                f'{number}. **`{number:05d}`** — Ready for extraction.\n'
                for number in (1, 2, 3)
            ),
        }.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        for record_id in ('00001', '00002', '00003'):
            source = root / 'papers (private)' / record_id / 'html/main.html'
            source.parent.mkdir(parents=True)
            source.write_text('<html><body><h1>Synthetic paper</h1></body></html>', encoding='utf-8')
            metadata = root / 'database/records' / f'{record_id}.yaml'
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text('title: Synthetic paper\njamies_human_only_notes:\n'
                                '  - synthetic_human_tag\npip_litdb_status: ready_for_extraction\n',
                                encoding='utf-8')
        return root

    def prepared(self, directory: str) -> tuple[Path, Path, Path]:
        root = self.fixture(directory)
        result = prepare_batch(root, 'trial', ['00001', '00002'])
        return root, Path(result['workspaces']['00001']), Path(result['workspaces']['00002'])

    def runs(self, workspace: Path, record_id: str = '00001', *, override: bool = False) -> None:
        override_bytes = b'schema_version: "1.0"\ntext_repairs: []\n'
        if override:
            (workspace / 'papers (private)' / record_id / 'extraction_overrides.yaml').write_bytes(override_bytes)
        sources = discover_sources(workspace, record_id)
        override_path = workspace / 'papers (private)' / record_id / 'extraction_overrides.yaml'
        override_hash = sha256_file(override_path) if override else None
        fingerprint = source_fingerprint(sources, override_hash)
        for run_id in ('reviewed', 'clean'):
            run = workspace / 'papers (private)/staging' / record_id / run_id
            diagnostic = run / 'extraction_diagnostic'
            diagnostic.mkdir(parents=True)
            (run / 'extraction').mkdir()
            (run / 'extraction/record.md').write_text('Reviewed synthetic content.\n', encoding='utf-8')
            manifest = {'record_id': record_id, 'run_id': run_id,
                        'source_fingerprint': fingerprint,
                        'pipeline_code_sha256': _pipeline_code_sha256()}
            if override:
                (diagnostic / 'overrides.yaml').write_bytes(override_bytes)
                manifest.update(override_snapshot_sha256=override_hash,
                                override_snapshot_bytes=len(override_bytes))
            atomic_write_json(diagnostic / 'manifest.json', manifest)
            atomic_write_json(diagnostic / 'sources.json', {
                'record_id': record_id, 'source_fingerprint': fingerprint,
                'sources': [source.as_dict() for source in sources],
            })

    def test_workspaces_are_distinct_copies_with_only_the_assigned_private_record(self):
        with TemporaryDirectory() as temporary:
            root, first, second = self.prepared(temporary)
            original = root / 'scripts/extraction/worker.py'
            self.assertFalse(original.samefile(first / 'scripts/extraction/worker.py'))
            self.assertFalse((first / 'papers (private)/00002').exists())
            self.assertFalse((second / 'papers (private)/00001').exists())
            for record_id in ('00001', '00002'):
                self.assertEqual(check_owner(root, 'trial', record_id)['status'], 'inputs_unchanged')
            (first / 'scripts/extraction/worker.py').write_text('local change', encoding='utf-8')
            self.assertEqual(original.read_text(), '# frozen runtime\n')
            self.assertEqual((second / 'scripts/extraction/worker.py').read_text(), '# frozen runtime\n')
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                check_owner(root, 'trial', '00001')
            self.assertEqual(check_owner(root, 'trial', '00002')['status'], 'inputs_unchanged')

    def test_active_batch_prevents_duplicate_owners_and_abort_preserves_work(self):
        with TemporaryDirectory() as temporary:
            root, first, _ = self.prepared(temporary)
            with self.assertRaisesRegex(RuntimeError, 'active batch'):
                prepare_batch(root, 'second', ['00001', '00003'])
            with self.assertRaises(RuntimeError):
                check_owner(root, 'trial', '00003')
            close_batch(root, 'trial', abort_reason='Both owners stopped for a shared fix.')
            self.assertTrue(first.is_dir())
            self.assertEqual(prepare_batch(root, 'second', ['00001'])['status'], 'prepared')

    def test_input_changes_are_rejected_before_import(self):
        changes = [
            ('primary', 'scripts/extraction/worker.py'),
            ('primary', 'papers (private)/00001/html/main.html'),
            ('primary', 'papers (private)/00001/extraction_overrides.yaml'),
            ('owner', 'database/records/00001.yaml'),
            ('owner', 'papers (private)/00001/html/added.html'),
            ('owner', 'tests/test_added.py'),
            ('owner', OWNER_WORKSPACE_MARKER),
        ]
        for location, name in changes:
            with self.subTest(location=location, name=name), TemporaryDirectory() as temporary:
                root, first, _ = self.prepared(temporary)
                self.runs(first)
                ((root if location == 'primary' else first) / name).write_text('changed', encoding='utf-8')
                with self.assertRaises((RuntimeError, OSError)):
                    import_owner_runs(root, 'trial', '00001', 'reviewed', 'clean')
                self.assertFalse((root / 'papers (private)/staging/00001').exists())

    def test_another_records_status_update_does_not_invalidate_the_owner(self):
        with TemporaryDirectory() as temporary:
            root, _, _ = self.prepared(temporary)
            other = root / 'database/records/00002.yaml'
            other.write_text(other.read_text().replace('ready_for_extraction', 'extracted_approved'), encoding='utf-8')
            self.assertEqual(check_owner(root, 'trial', '00001')['status'], 'inputs_unchanged')
            with self.assertRaisesRegex(RuntimeError, 'metadata changed'):
                check_owner(root, 'trial', '00002')

    def test_import_keeps_exact_runs_and_override_without_publishing(self):
        with TemporaryDirectory() as temporary:
            root, first, second = self.prepared(temporary)
            self.runs(first, override=True)
            metadata = (root / 'database/records/00001.yaml').read_bytes()
            queue = (root / 'tmp/EXTRACTION_QUEUE.md').read_bytes()
            result = import_owner_runs(root, 'trial', '00001', 'reviewed', 'clean')
            self.assertEqual(result['status'], 'staged_for_primary_review')
            self.assertEqual((root / 'database/records/00001.yaml').read_bytes(), metadata)
            self.assertEqual((root / 'tmp/EXTRACTION_QUEUE.md').read_bytes(), queue)
            self.assertFalse((root / 'papers (private)/00001/extraction').exists())
            self.assertEqual((root / 'papers (private)/00001/extraction_overrides.yaml').read_bytes(),
                             (first / 'papers (private)/00001/extraction_overrides.yaml').read_bytes())
            for run_id in ('reviewed', 'clean'):
                relative = f'papers (private)/staging/00001/{run_id}'
                for source in (first / relative).rglob('*'):
                    if source.is_file():
                        self.assertEqual((root / relative / source.relative_to(first / relative)).read_bytes(), source.read_bytes())
            self.assertFalse((second / 'papers (private)/staging/00001').exists())
            self.assertEqual(check_owner(root, 'trial', '00002')['status'], 'inputs_unchanged')

    def test_import_rejects_stale_code_and_different_reproduction(self):
        for change in ('pipeline', 'canonical'):
            with self.subTest(change=change), TemporaryDirectory() as temporary:
                root, first, _ = self.prepared(temporary)
                self.runs(first)
                clean = first / 'papers (private)/staging/00001/clean'
                if change == 'pipeline':
                    path = clean / 'extraction_diagnostic/manifest.json'
                    value = json.loads(path.read_text())
                    value['pipeline_code_sha256'] = 'stale'
                    atomic_write_json(path, value)
                else:
                    (clean / 'extraction/record.md').write_text('Different content', encoding='utf-8')
                with self.assertRaises(RuntimeError):
                    import_owner_runs(root, 'trial', '00001', 'reviewed', 'clean')
                self.assertFalse((root / 'papers (private)/staging/00001').exists())

    def test_import_refuses_to_overwrite_a_conflicting_run(self):
        with TemporaryDirectory() as temporary:
            root, first, _ = self.prepared(temporary)
            self.runs(first)
            collision = root / 'papers (private)/staging/00001/reviewed'
            collision.mkdir(parents=True)
            (collision / 'sentinel').write_text('Unrelated staged work.', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'overwrite'):
                import_owner_runs(root, 'trial', '00001', 'reviewed', 'clean')
            self.assertEqual((collision / 'sentinel').read_text(), 'Unrelated staged work.')

    def test_mutating_candidate_during_copy_does_not_complete_handoff(self):
        with TemporaryDirectory() as temporary:
            root, first, _ = self.prepared(temporary)
            self.runs(first)
            original = shutil.copytree
            def changed_copy(source, destination, *args, **kwargs):
                result = original(source, destination, *args, **kwargs)
                record = Path(source) / 'extraction/record.md'
                if record.is_file():
                    record.write_text('Changed during handoff.', encoding='utf-8')
                return result
            with mock.patch('scripts.extraction.coordination.shutil.copytree', side_effect=changed_copy):
                with self.assertRaisesRegex(RuntimeError, 'changed during handoff'):
                    import_owner_runs(root, 'trial', '00001', 'reviewed', 'clean')
            self.assertFalse((root / 'papers (private)/parallel/trial/import-00001.json').exists())
            self.assertFalse((root / 'papers (private)/00001/extraction').exists())

    def test_owner_workspace_cannot_promote_or_finalize(self):
        with TemporaryDirectory() as temporary:
            _, first, _ = self.prepared(temporary)
            with self.assertRaisesRegex(RuntimeError, 'isolated owners'):
                finalize_extraction(first, '00001', run_id='reviewed', reproducibility_run_id='clean')
            with self.assertRaisesRegex(RuntimeError, 'isolated owners'):
                promote_extraction(first, '00001', run_id='reviewed', approval_mode='standing_policy', reproducibility_run_id='clean')

    def test_batch_completion_requires_matching_live_approval(self):
        with TemporaryDirectory() as temporary:
            root, first, _ = self.prepared(temporary)
            self.runs(first)
            import_owner_runs(root, 'trial', '00001', 'reviewed', 'clean')
            with self.assertRaises(RuntimeError):
                close_batch(root, 'trial')
            self.assertTrue((root / 'papers (private)/parallel/active.json').exists())

    def test_assignment_rejects_unsafe_ids_duplicates_and_excess_owners(self):
        for ids in (['../01'], ['00001', '00001'], ['00001', '00002', '00003', '00004'], []):
            with self.subTest(ids=ids), TemporaryDirectory() as temporary:
                root = self.fixture(temporary)
                with self.assertRaises((RuntimeError, ValueError)):
                    prepare_batch(root, 'trial', ids)
                self.assertFalse((root / 'papers (private)/parallel').exists())

    def test_three_owner_batch_releases_slots_after_all_matching_live_checks(self):
        with TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            prepared = prepare_batch(root, 'trial', ['00001', '00002', '00003'])
            manifest = json.loads((root / 'papers (private)/parallel/trial/batch.json').read_text())
            self.assertEqual((manifest['owner_model'], manifest['owner_effort']), ('gpt-6-astra', 'high'))
            for record_id, assigned in prepared['workspaces'].items():
                workspace = Path(assigned)
                self.assertEqual(check_owner(root, 'trial', record_id)['status'], 'inputs_unchanged')
                self.assertFalse((workspace / 'papers (private)' / ('00002' if record_id == '00001' else '00001')).exists())
                self.runs(workspace, record_id)
                import_owner_runs(root, 'trial', record_id, 'reviewed', 'clean')
                diagnostic = root / 'papers (private)' / record_id / 'extraction_diagnostic'
                diagnostic.mkdir()
                atomic_write_json(diagnostic / 'approval.json', {'run_id': 'reviewed'})
            with mock.patch('scripts.extraction.coordination.cleanup_approved_staging') as live_check:
                result = close_batch(root, 'trial')
                self.assertEqual(live_check.call_args_list, [
                    mock.call(root, '00001', remove=False),
                    mock.call(root, '00002', remove=False),
                    mock.call(root, '00003', remove=False),
                ])
            self.assertEqual(result['status'], 'completed')
            self.assertFalse((root / 'papers (private)/parallel/active.json').exists())
            for workspace in prepared['workspaces'].values():
                self.assertTrue(Path(workspace).is_dir())

    def test_repository_lock_excludes_another_process_and_releases_after_failure(self):
        with TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            with self.assertRaisesRegex(ValueError, 'simulated failure'):
                with repository_lock(root):
                    process = subprocess.run([
                        sys.executable, '-c',
                        'from pathlib import Path; import sys; '
                        'from scripts.extraction.locking import repository_lock; '
                        'repository_lock(Path(sys.argv[1])).__enter__()', str(root),
                    ], capture_output=True, text=True)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn('coordinator is busy', process.stderr)
                    raise ValueError('simulated failure')
            with repository_lock(root):
                self.assertTrue((root / 'papers (private)/.extraction-coordinator.lock').is_dir())
            self.assertFalse((root / 'papers (private)/.extraction-coordinator.lock').exists())

    def test_two_finalizers_preserve_both_queue_updates_and_human_metadata(self):
        with TemporaryDirectory() as temporary:
            root = self.fixture(temporary)
            entered, release = threading.Event(), threading.Event()
            def promotion(_root, record_id, **kwargs):
                if record_id == '00001':
                    entered.set()
                    if not release.wait(timeout=10):
                        raise RuntimeError('test release timed out')
                return SimpleNamespace(record_id=record_id)
            with mock.patch('scripts.extraction.finalization.promote_extraction', side_effect=promotion), \
                 mock.patch('scripts.extraction.finalization.cleanup_approved_staging', return_value=SimpleNamespace(removed=True)), \
                 ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(finalize_extraction, root, '00001', run_id='r', reproducibility_run_id='c')
                try:
                    self.assertTrue(entered.wait(timeout=10))
                    with self.assertRaisesRegex(RuntimeError, 'coordinator is busy'):
                        finalize_extraction(root, '00002', run_id='r', reproducibility_run_id='c')
                finally:
                    release.set()
                future.result(timeout=10)
                finalize_extraction(root, '00002', run_id='r', reproducibility_run_id='c')
            queue = (root / 'tmp/EXTRACTION_QUEUE.md').read_text(encoding='utf-8')
            for record_id in ('00001', '00002'):
                self.assertIn(f'**`{record_id}`** — Extraction completed and approved.', queue)
                text = (root / 'database/records' / f'{record_id}.yaml').read_text()
                self.assertIn('pip_litdb_status: extracted_approved', text)
                self.assertIn('jamies_human_only_notes:\n  - synthetic_human_tag\n', text)
            self.assertIn('**`00003`** — Ready for extraction.', queue)


if __name__ == '__main__':
    unittest.main()
