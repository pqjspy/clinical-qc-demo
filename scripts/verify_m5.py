"""Isolated-data/lifespan delivery drill with one real local QC002.

Never points Store at production. Blocks non-loopback Python socket connections
inside this process, not OS-wide egress or Ollama's separate process. Uses the
real FastAPI lifespan via TestClient, not a second public/listening server.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import socket
import sys
import time
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from clinical_qc_demo.delivery import database_summary, frozen_check, restore_isolated, verify_backup, sha, application_schema_sha


def loopback_only(stack, calls):
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    original_dns = socket.getaddrinfo
    def address_ok(sock, address):
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return
        host = address[0]
        if host not in ('127.0.0.1', '::1'):
            raise OSError('M5 drill denies non-loopback connection')
        calls.append(dict(host=host, port=address[1]))
    def connect(sock, address):
        address_ok(sock, address)
        return original_connect(sock, address)
    def connect_ex(sock, address):
        address_ok(sock, address)
        return original_connect_ex(sock, address)
    def dns(host, *args, **kwargs):
        if host not in ('127.0.0.1', '::1', None):
            raise OSError('M5 drill denies external DNS/hostnames')
        return original_dns(host, *args, **kwargs)
    stack.enter_context(patch.object(socket.socket, 'connect', connect))
    stack.enter_context(patch.object(socket.socket, 'connect_ex', connect_ex))
    stack.enter_context(patch.object(socket, 'getaddrinfo', dns))


def run(backup_path):
    # Full integrity verification BEFORE app initialization makes intentional changes.
    frozen, bad = frozen_check(ROOT)
    if bad:
        raise ValueError('Frozen M4 source differs; do not relabel results.')
    backup_manifest = verify_backup(backup_path)
    for rel, identity in backup_manifest['files'].items():
        if rel.startswith(('src/', 'frontend/dist/')) and sha(ROOT / rel) != identity['sha256']:
            raise ValueError('当前源代码/构建与备份不同；本演练仅验证匹配版本。')
    live_before = database_summary(ROOT / 'runtime/web/qc.sqlite3')
    base = ROOT / 'runtime/delivery_drills'
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = base / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8])
    folder.mkdir(mode=0o700)
    report = dict(synthetic=True, kind='m5_delivery_drill_not_model_benchmark', ok=False,
                  folder=str(folder), tests={}, backup=str(backup_path),
                  limitations=['进程级Python socket约束；未断开整机网络，未封锁Ollama独立进程。',
                               'TestClient测试真实应用启动/关闭；不是浏览器测试，也不是重启在线8911。',
                               '新目录使用本机已有依赖；没有在一台新电脑重新安装。',
                               '恢复测试使用已核对摘要的当前源码+恢复数据，不从备份目录重新导入源码。',
                               'QC002为基础设施/L3冒烟验证，不是解释语义正确率。'])
    try:
        restored = restore_isolated(backup_path, folder / 'restored')
        report['tests']['restored_snapshot'] = dict(ok=True, counts=backup_manifest['database']['counts'])
        from fastapi.testclient import TestClient
        from clinical_qc_demo.web_api import create_app
        with TestClient(create_app(restored), base_url='http://127.0.0.1:8911') as client:
            assert client.get('/api/health').json()['ok'] is True
            assert client.get('/').status_code == 200
            accounts = json.loads((restored / 'runtime/web/local_accounts.json').read_text())
            response = client.post('/api/login', json={'username':'viewer','password':accounts['viewer']},
                                   headers={'Origin':'http://127.0.0.1:8911'})
            assert response.status_code == 200
            assert len(client.get('/api/jobs').json()) == backup_manifest['database']['counts']['jobs']
            assert client.get('/api/evaluation').json()['available'] is True
        after = database_summary(restored / 'runtime/web/qc.sqlite3')
        for table in ('cases','bundles','runs','reviews','drafts','publications','alerts','users','settings'):
            assert after['table_sha256'][table] == backup_manifest['database']['table_sha256'][table], table
        report['tests']['restored_app_boot_and_saved_history'] = dict(ok=True)

        fresh = folder / 'fresh'
        fresh.mkdir(mode=0o700)
        for path in ('data/knowledge', 'data/inputs', 'examples', 'frontend/dist'):
            shutil.copytree(ROOT / path, fresh / path)
        # No reference labels or frozen result files are present in the fresh root.
        report['tests']['fresh_has_no_reference_answers'] = dict(ok=not (fresh / 'data/evaluation').exists())
        connections = []
        with ExitStack() as stack:
            loopback_only(stack, connections)
            with socket.socket() as probe:
                try:
                    probe.connect(('203.0.113.1', 443))
                except OSError as exc:
                    assert 'denies non-loopback' in str(exc)
                else:
                    raise AssertionError('Outbound guard did not block')
            with TestClient(create_app(fresh), base_url='http://127.0.0.1:8911') as client:
                assert client.get('/').status_code == 200
                accounts = json.loads((fresh / 'runtime/web/local_accounts.json').read_text())
                auth = client.post('/api/login', json={'username':'reviewer','password':accounts['reviewer']},
                                   headers={'Origin':'http://127.0.0.1:8911'})
                assert auth.status_code == 200
                headers = {'Origin':'http://127.0.0.1:8911','X-QC-CSRF':auth.json()['csrf']}
                request = client.post('/api/jobs', json={'case_id':'QC002','request_key':str(uuid4())}, headers=headers)
                assert request.status_code == 202, request.text
                jid = request.json()['id']
                print('隔离新应用已启动，正在实际调用本机Qwen一次（不重跑18条评测）……', flush=True)
                deadline = time.monotonic() + 300
                while True:
                    detail = client.get('/api/jobs/' + jid).json()
                    if detail['job']['state'] not in ('queued','running'):
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError('交付演练超时；不自动重试。')
                    time.sleep(.2)
                result = detail['result']
                assert result and result['mode'] == 'live_local' and result['model_calls']
                assert result['model_identity'] == frozen['model'], '模型身份与冻结版本不一致'
                assert result['status'] != 'analysis_failed', result.get('error')
                l3 = [f['l3_id'] for issue in result['issues'] for f in issue['findings']]
                report['tests']['real_qwen_QC002'] = dict(ok=l3 == ['L3-PK-001'], run_id=result['run_id'],
                    status=result['status'], l3=l3, elapsed_ms=result['elapsed_ms'],
                    explanation_error=result.get('explanation_error'), job_id=jid)
                report['model_identity'] = result['model_identity']
                assert l3 == ['L3-PK-001'], 'QC002现场未得到预期L3；保留本次错误，不自动重试。'
            before_restart = database_summary(fresh / 'runtime/web/qc.sqlite3')
            schema_before = application_schema_sha(fresh / 'runtime/web/qc.sqlite3')
            with TestClient(create_app(fresh), base_url='http://127.0.0.1:8911') as client:
                assert client.get('/api/health').json()['ok'] is True
            after_restart = database_summary(fresh / 'runtime/web/qc.sqlite3')
            # PRAGMA optimize may create sqlite_stat1 during a later initialization.
            # Require every business table AND app-defined schema to remain exact.
            assert application_schema_sha(fresh / 'runtime/web/qc.sqlite3') == schema_before
            assert {k:v for k,v in before_restart.items() if k != 'schema_sha256'} == {k:v for k,v in after_restart.items() if k != 'schema_sha256'}
        report['tests']['fresh_restart_persistence'] = dict(ok=True,
            sqlite_internal_schema_changed=before_restart['schema_sha256'] != after_restart['schema_sha256'])
        report['tests']['python_outbound_guard'] = dict(ok=True, connections=connections)
        live_after = database_summary(ROOT / 'runtime/web/qc.sqlite3')
        report['tests']['live_database_unchanged'] = dict(ok=live_before == live_after)
        if live_before != live_after:
            raise ValueError('在线数据在演练期间发生变化；不声称未变化，需检查是否为用户操作。')
        assert not frozen_check(ROOT)[1]
        report['tests']['frozen_m4_unchanged'] = dict(ok=True)
        report['ok'] = True
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    output = folder / 'verification.json'
    with output.open('x', encoding='utf-8') as handle:
        output.chmod(0o600)
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print('演练报告：', output)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.backup.resolve()))
