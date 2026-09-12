"""M5 local delivery utilities. Never initialize/seed Store or overwrite a restore.

Backups are private, unsigned snapshots, not regulated/tamper-proof archives.
Only SQLite's backup API reads the live WAL database. Completed run directories
are selected from the snapshot and the frozen report, not a live directory glob.
"""
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import time
from uuid import UUID, uuid4
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

MODEL = 'qwen3:4b-instruct-2507-q4_K_M'
APP_URL = 'http://127.0.0.1:8911'
MODEL_URL = 'http://127.0.0.1:11434'
TABLES = ('users', 'cases', 'bundles', 'settings', 'jobs', 'runs', 'reviews',
          'drafts', 'publications', 'audit', 'alerts', 'schema_migrations')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_path(root, relative):
    rel = PurePosixPath(relative)
    if not relative or rel.is_absolute() or '..' in rel.parts or '\\' in relative or str(rel) != relative:
        raise ValueError('非法相对路径。')
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('不接受符号链接：' + relative)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('路径越界。')
    return path


def read_db(path):
    if not path.is_file() or path.is_symlink():
        raise ValueError('数据库不存在或是符号链接；不会创建新库。')
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def database_summary(path):
    db = read_db(path)
    try:
        db.execute('BEGIN')
        if [r[0] for r in db.execute('PRAGMA quick_check')] != ['ok'] or db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('数据库完整性检查失败。')
        counts, fingerprints = {}, {}
        for table in TABLES:
            # Table identifiers come only from this fixed constant, never input.
            rows = sorted(canonical(list(r)) for r in db.execute(f'SELECT * FROM {table}'))
            counts[table] = len(rows)
            fingerprints[table] = hashlib.sha256('\n'.join(rows).encode()).hexdigest()
        active = db.execute("SELECT value FROM settings WHERE key='active_bundle'").fetchone()
        if not active or not db.execute('SELECT 1 FROM bundles WHERE id=?', (active[0],)).fetchone():
            raise ValueError('没有有效的已发布规则快照。')
        for table in ('runs', 'bundles'):
            for row in db.execute(f'SELECT payload, sha256 FROM {table}'):
                if hashlib.sha256(canonical(json.loads(row[0])).encode()).hexdigest() != row[1]:
                    raise ValueError('已保存内容摘要不匹配：' + table)
        run_ids = [r[0] for r in db.execute('SELECT run_id FROM runs ORDER BY run_id')]
        busy = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
        schema = '\n'.join(r[0] for r in db.execute('SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name'))
        return dict(counts=counts, table_sha256=fingerprints, active_bundle=active[0],
                    run_ids=run_ids, active_jobs=busy, schema_sha256=hashlib.sha256(schema.encode()).hexdigest())
    finally:
        db.close()


def check_credentials(root):
    passwords = json.loads(safe_path(root, 'runtime/web/local_accounts.json').read_text())
    db = read_db(safe_path(root, 'runtime/web/qc.sqlite3'))
    try:
        users = list(db.execute('SELECT name,salt,password_hash FROM users'))
        if set(passwords) != {r[0] for r in users}:
            raise ValueError('账户清单与数据库不匹配。')
        for name, salt, wanted in users:
            actual = hashlib.pbkdf2_hmac('sha256', passwords[name].encode(), bytes.fromhex(salt), 200000).hex()
            if actual != wanted:
                raise ValueError('账户凭证校验失败；未输出密码。')
    finally:
        db.close()


def application_schema_sha(path):
    """Exclude SQLite's own query-planner metadata, never user tables/triggers."""
    db = read_db(path)
    try:
        sql = '\n'.join(r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND substr(name,1,7) != 'sqlite_' ORDER BY type,name"))
        return hashlib.sha256(sql.encode()).hexdigest()
    finally:
        db.close()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('健康检查拒绝重定向。')


def local_json(url):
    if url not in {APP_URL + '/api/health', MODEL_URL + '/api/version', MODEL_URL + '/api/tags'}:
        raise ValueError('仅允许已知本机健康检查。')
    with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=3) as response:
        raw = response.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError('健康检查响应过大。')
    return json.loads(raw)


def frozen_check(root):
    manifest = json.loads((root / 'data/evaluation/m4_v1/manifest.json').read_text())
    mismatches = [p for p, wanted in manifest['files'].items()
                  if not safe_path(root, p).is_file() or sha(safe_path(root, p)) != wanted]
    return manifest, mismatches


def status(root):
    checks = {}
    def check(name, fn):
        try:
            value = fn()
            checks[name] = dict(ok=True, detail=value)
        except Exception as exc:
            checks[name] = dict(ok=False, error=f'{type(exc).__name__}: {exc}')
    def application():
        result = local_json(APP_URL + '/api/health')
        if result.get('ok') is not True or result.get('scope') != 'm4_six_families_explicit_grammar' or result.get('synthetic') is not True:
            raise ValueError('响应不是本项目的健康服务。')
        return result
    def model():
        tags = local_json(MODEL_URL + '/api/tags')
        found = [m for m in tags.get('models', []) if m.get('name') == MODEL]
        frozen, _ = frozen_check(root)
        if len(found) != 1 or found[0].get('digest') != frozen['model']['digest']:
            raise ValueError('需要的Qwen不存在或与冻结版本不同；不会自动下载/替换。')
        return dict(name=MODEL, digest=found[0]['digest'], ollama=local_json(MODEL_URL + '/api/version'))
    def files():
        for p in ('frontend/dist/index.html', 'runtime/web/local_accounts.json'):
            if not safe_path(root, p).is_file():
                raise ValueError('缺少：' + p)
        return '本地静态页面与账户文件存在；未输出密码。'
    def db():
        info = database_summary(safe_path(root, 'runtime/web/qc.sqlite3'))
        return {k: info[k] for k in ('counts', 'active_bundle', 'active_jobs')}
    def frozen():
        manifest, mismatches = frozen_check(root)
        if mismatches:
            raise ValueError('冻结文件改变：' + ', '.join(mismatches))
        return f"{len(manifest['files'])}个冻结文件摘要一致；未重跑评测。"
    check('application', application)
    check('qwen', model)
    check('files', files)
    check('database', db)
    check('frozen_m4', frozen)
    check('python_packages', lambda: {name: importlib.metadata.version(name) for name in ('fastapi','uvicorn','pydantic')})
    return dict(ok=all(c['ok'] for c in checks.values()), checks=checks,
                note='只读检查；模型存在不等于已完成推理。应用与模型检查分别报告。')


def private_copy(source, dest):
    if not source.is_file() or source.is_symlink():
        raise ValueError('只复制普通文件。')
    before = sha(source)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with source.open('rb') as src, os.fdopen(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
        while block := src.read(1024 * 1024):
            out.write(block)
    if sha(source) != before or sha(dest) != before:
        raise ValueError('复制时文件变化，备份未完成。')


def tree_files(root, relative):
    folder = safe_path(root, relative)
    if not folder.is_dir():
        raise ValueError('缺少目录：' + relative)
    for path in sorted(folder.rglob('*')):
        rel = path.relative_to(root).as_posix()
        safe_path(root, rel)
        if path.is_file() and '__pycache__' not in path.parts:
            yield rel


def backup(root):
    root = root.resolve()
    base = safe_path(root, 'runtime/backups')
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest = base / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8])
    dest.mkdir(mode=0o700)
    web = dest / 'runtime/web'
    web.mkdir(parents=True, mode=0o700)
    snapshot = web / 'qc.sqlite3'
    fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    source = read_db(safe_path(root, 'runtime/web/qc.sqlite3'))
    target = sqlite3.connect(snapshot)
    try:
        deadline = time.monotonic() + 20
        def progress(*_):
            if time.monotonic() > deadline:
                raise TimeoutError('数据库备份超过20秒；保留未完成目录，不当成功备份。')
        source.backup(target, pages=64, progress=progress, sleep=.05)
        target.execute('PRAGMA journal_mode=DELETE')
    finally:
        target.close()
        source.close()
    info = database_summary(snapshot)
    if info['active_jobs']:
        raise ValueError('快照包含运行中任务；请等分析结束后重新备份。未完成目录保留，不用于恢复。')
    private_copy(safe_path(root, 'runtime/web/local_accounts.json'), web / 'local_accounts.json')
    check_credentials(dest)
    selected = set()
    for folder in ('src', 'scripts', 'tests', 'docs', 'data', 'examples', 'frontend/src', 'frontend/dist'):
        selected.update(tree_files(root, folder))
    selected.update(('README.md', '.gitignore', 'pyproject.toml', 'requirements-m3.lock',
                     'frontend/package.json', 'frontend/package-lock.json', 'frontend/tsconfig.json',
                     'frontend/vite.config.ts', 'frontend/index.html'))
    # Include only the completed frozen evaluation, not active/dev evaluation runs.
    report_path = safe_path(root, 'runtime/evaluations/m4-v1/report.json')
    report = json.loads(report_path.read_text())
    selected.update(tree_files(root, 'runtime/evaluations/m4-v1'))
    run_ids = set(info['run_ids']) | {r['run_id'] for r in report['rows'] if r.get('run_id')}
    for rid in run_ids:
        if str(UUID(rid)) != rid:
            raise ValueError('运行编号不是规范UUID。')
        result_path = safe_path(root, f'runtime/runs/{rid}/result.json')
        if json.loads(result_path.read_text()).get('run_id') != rid:
            raise ValueError('运行轨迹未完成或编号不匹配。')
        selected.update(tree_files(root, f'runtime/runs/{rid}'))
    for rel in sorted(selected):
        private_copy(safe_path(root, rel), safe_path(dest, rel))
    # Completed traces must agree exactly with authoritative saved web results.
    db = read_db(snapshot)
    try:
        for rid, payload in db.execute('SELECT run_id,payload FROM runs'):
            trace = json.loads((dest / f'runtime/runs/{rid}/result.json').read_text())
            if canonical(trace) != canonical(json.loads(payload)):
                raise ValueError('运行轨迹与数据库快照不一致。')
    finally:
        db.close()
    _, mismatches = frozen_check(dest)
    if mismatches:
        raise ValueError('冻结文件不一致，不能当M4原版本交付。')
    files = {p: dict(sha256=sha(dest / p), bytes=(dest / p).stat().st_size)
             for p in tree_files(dest, 'runtime')}
    for rel in selected:
        files[rel] = dict(sha256=sha(dest / rel), bytes=(dest / rel).stat().st_size)
    manifest = dict(format='qc-local-backup-v1', created_at=datetime.now(timezone.utc).isoformat(),
                    database=info, files=dict(sorted(files.items())),
                    warning='含明文演示账户和运行原文。仅本地私有备份；未加密/未签名，不上传。',
                    excluded='虚拟环境、node_modules、模型权重、日志/PID/会话、未被网页或冻结评测引用的开发运行。')
    with os.fdopen(os.open(dest / 'backup-manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as out:
        json.dump(manifest, out, ensure_ascii=False, indent=2)
    return dest


def verify_backup(folder):
    if folder.is_symlink():
        raise ValueError('不接受符号链接备份目录。')
    folder = folder.resolve()
    manifest = json.loads(safe_path(folder, 'backup-manifest.json').read_text())
    if manifest.get('format') != 'qc-local-backup-v1':
        raise ValueError('不是完整的M5备份。')
    actual = set()
    for file in folder.rglob('*'):
        rel = file.relative_to(folder).as_posix()
        safe_path(folder, rel)
        if file.is_file() and rel != 'backup-manifest.json':
            actual.add(rel)
    if actual != set(manifest['files']):
        raise ValueError('备份文件清单不一致。')
    for rel, expected in manifest['files'].items():
        file = safe_path(folder, rel)
        if file.stat().st_size != expected['bytes'] or sha(file) != expected['sha256']:
            raise ValueError('备份文件损坏：' + rel)
    if database_summary(folder / 'runtime/web/qc.sqlite3') != manifest['database']:
        raise ValueError('恢复前的数据库内容核对失败。')
    check_credentials(folder)
    if frozen_check(folder)[1]:
        raise ValueError('备份冻结摘要不一致。')
    return manifest


def restore_isolated(folder, dest):
    manifest = verify_backup(folder)
    # Exclusive NEW directory only. No replace/reset/delete command exists.
    if dest.resolve().is_relative_to(folder.resolve()):
        raise ValueError('恢复目标不能位于备份目录内，以免污染原备份。')
    if dest.exists() or dest.is_symlink():
        raise ValueError('目标已存在；不覆盖任何目录或在线数据。')
    if dest.parent.is_symlink() or dest.parent.resolve() != dest.parent.absolute():
        raise ValueError('恢复路径不能通过符号链接。')
    dest.mkdir(mode=0o700, parents=False)
    for rel in (*manifest['files'], 'backup-manifest.json'):
        private_copy(safe_path(folder, rel), safe_path(dest, rel))
    verify_backup(dest)
    return dest
