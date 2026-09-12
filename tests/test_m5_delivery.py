"""Delivery tests use disposable synthetic databases, never the live runtime."""
import json
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import runpy
import unittest
from unittest.mock import patch, MagicMock
from uuid import uuid4

from clinical_qc_demo.delivery import (backup, database_summary, frozen_check, restore_isolated,
                                      safe_path, status, verify_backup, MODEL, application_schema_sha)
from clinical_qc_demo.web_store import Store

ROOT = Path(__file__).resolve().parents[1]


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / 'project'
        self.root.mkdir()
        for folder in ('src', 'scripts', 'tests', 'docs', 'data', 'examples', 'frontend/src'):
            shutil.copytree(ROOT / folder, self.root / folder, ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('README.md', '.gitignore', 'pyproject.toml', 'requirements-m3.lock',
                     'frontend/package.json', 'frontend/package-lock.json', 'frontend/tsconfig.json',
                     'frontend/vite.config.ts', 'frontend/index.html'):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, self.root / name)
        (self.root / 'frontend/dist').mkdir()
        (self.root / 'frontend/dist/index.html').write_text('<p>TEST FIXTURE ONLY</p>')
        folder = self.root / 'runtime/evaluations/m4-v1'
        folder.mkdir(parents=True)
        (folder / 'report.json').write_text(json.dumps({'rows': [], 'test_double_report': True}))
        self.store = Store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_preserves_all_tables_and_private_accounts(self):
        original = database_summary(self.store.path)
        folder = backup(self.root)
        info = verify_backup(folder)
        self.assertEqual(original, info['database'])
        dest = Path(self.tmp.name).resolve() / 'restored'
        restore_isolated(folder, dest)
        self.assertEqual(original, database_summary(dest / 'runtime/web/qc.sqlite3'))
        self.assertEqual(original, database_summary(self.store.path))
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o700)
        for p in dest.rglob('*'):
            if p.is_file():
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_wal_committed_row_is_included(self):
        con = sqlite3.connect(self.store.path)
        try:
            con.execute('PRAGMA wal_autocheckpoint=0')
            con.execute("INSERT INTO cases SELECT 'SYN-WEB-WAL',payload,created_at,actor FROM cases LIMIT 1")
            con.commit()
            self.assertTrue(Path(str(self.store.path) + '-wal').exists())
            info = verify_backup(backup(self.root))
            self.assertEqual(info['database']['counts']['cases'], 15)
        finally:
            con.close()

    def test_busy_snapshot_is_not_a_completed_backup(self):
        self.store.create_job('QC002', 'reviewer', 'test-m5-busy')
        with self.assertRaisesRegex(ValueError, '运行中'):
            backup(self.root)
        self.assertEqual(list((self.root / 'runtime/backups').glob('*/backup-manifest.json')), [])

    def test_restore_never_overwrites_existing_directory(self):
        folder = backup(self.root)
        with self.assertRaisesRegex(ValueError, '已存在'):
            restore_isolated(folder, self.root)

    def test_nested_restore_refused_without_polluting_backup(self):
        folder = backup(self.root)
        with self.assertRaisesRegex(ValueError, '备份目录内'):
            restore_isolated(folder, folder / 'nested')
        self.assertFalse((folder / 'nested').exists())
        verify_backup(folder)

    def add_trace_fixture(self):
        rid = str(uuid4())
        result = dict(run_id=rid, status='analysis_failed', mode='test_double', synthetic=True)
        folder = self.root / 'runtime/runs' / rid
        folder.mkdir(parents=True)
        (folder / 'result.json').write_text(json.dumps(result))
        jid, _ = self.store.create_job('QC002', 'reviewer', 'test-m5-trace')
        self.store.finish_job(jid, result)
        return folder

    def test_referenced_trace_is_restored(self):
        trace = self.add_trace_fixture()
        folder = backup(self.root)
        dest = Path(self.tmp.name).resolve() / 'restored'
        restore_isolated(folder, dest)
        self.assertEqual((trace / 'result.json').read_bytes(),
                         (dest / trace.relative_to(self.root) / 'result.json').read_bytes())

    def test_missing_completed_trace_refused(self):
        trace = self.add_trace_fixture()
        (trace / 'result.json').unlink()
        with self.assertRaises(FileNotFoundError):
            backup(self.root)

    def test_mismatched_completed_trace_refused(self):
        trace = self.add_trace_fixture()
        result = json.loads((trace / 'result.json').read_text())
        result['extra'] = 'not the saved DB result'
        (trace / 'result.json').write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, '轨迹与数据库'):
            backup(self.root)

    def test_corrupt_backup_refused(self):
        folder = backup(self.root)
        (folder / 'README.md').write_text('broken')
        with self.assertRaisesRegex(ValueError, '损坏'):
            verify_backup(folder)

    def test_extra_file_refused(self):
        folder = backup(self.root)
        (folder / 'extra.txt').write_text('extra')
        with self.assertRaisesRegex(ValueError, '清单'):
            verify_backup(folder)

    def test_missing_file_refused(self):
        folder = backup(self.root)
        (folder / 'README.md').unlink()
        with self.assertRaisesRegex(ValueError, '清单'):
            verify_backup(folder)

    def test_symlink_backup_entry_refused(self):
        folder = backup(self.root)
        (folder / 'extra-link').symlink_to(self.root / 'README.md')
        with self.assertRaisesRegex(ValueError, '符号链接'):
            verify_backup(folder)

    def test_unsafe_paths_refused(self):
        for rel in ('../outside', '/absolute', 'a/../../b', 'a\\b', './a', 'a//b'):
            with self.subTest(rel=rel), self.assertRaises(ValueError):
                safe_path(self.root, rel)

    def test_wrong_credentials_refused(self):
        self.store.credentials.write_text('{"reviewer":"wrong"}')
        with self.assertRaisesRegex(ValueError, '账户清单'):
            backup(self.root)

    def test_modified_frozen_source_refused(self):
        (self.root / 'src/clinical_qc_demo/m4_engine.py').write_text('# changed')
        with self.assertRaisesRegex(ValueError, '冻结文件'):
            backup(self.root)

    def test_app_ready_does_not_hide_model_down(self):
        def request(url):
            if url.endswith('/api/health'):
                return {'ok': True, 'synthetic': True, 'scope': 'm4_six_families_explicit_grammar'}
            raise OSError('TEST: model unavailable')
        with patch('clinical_qc_demo.delivery.local_json', side_effect=request):
            result = status(self.root)
        self.assertTrue(result['checks']['application']['ok'])
        self.assertFalse(result['checks']['qwen']['ok'])
        self.assertFalse(result['ok'])

    def test_status_model_digest_mismatch(self):
        def request(url):
            if url.endswith('/api/health'):
                return {'ok': True, 'synthetic': True, 'scope': 'm4_six_families_explicit_grammar'}
            return {'models': [{'name': MODEL, 'digest': 'not-frozen'}]}
        with patch('clinical_qc_demo.delivery.local_json', side_effect=request):
            self.assertFalse(status(self.root)['checks']['qwen']['ok'])

    def test_occupied_port_prevents_any_app_initialization(self):
        listener = MagicMock()
        listener.__enter__.return_value = listener
        listener.bind.side_effect = OSError('TEST port busy')
        with patch('socket.socket', return_value=listener), patch('clinical_qc_demo.web_api.create_app') as create:
            with self.assertRaisesRegex(SystemExit, '未初始化数据库'):
                runpy.run_path(str(ROOT / 'scripts/serve_m3.py'), run_name='__main__')
            create.assert_not_called()

    def test_restart_sqlite_statistics_not_business_schema_change(self):
        before = database_summary(self.store.path)
        schema = application_schema_sha(self.store.path)
        Store(self.root)
        after = database_summary(self.store.path)
        self.assertEqual(schema, application_schema_sha(self.store.path))
        self.assertEqual(before['table_sha256'], after['table_sha256'])


if __name__ == '__main__':
    unittest.main()
