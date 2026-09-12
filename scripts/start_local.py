"""Start the existing loopback app independently of a temporary terminal session.

Manual start only: no login item, launchd registration, or automatic restart.
Existing records/passwords and the frozen inference/evaluation stay unchanged.
"""
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime/web'
URL = 'http://127.0.0.1:8911'
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def healthy():
    try:
        with HTTP.open(URL + '/api/health', timeout=1) as response:
            info = json.load(response)
        return isinstance(info, dict) and info.get('ok') is True and info.get('synthetic') is True and info.get('scope') == 'm4_six_families_explicit_grammar'
    except (OSError, ValueError):
        return False


def start():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    # Serialize this launcher; the app still binds one port and one worker.
    lock_fd = os.open(RUNTIME / 'start.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with socket.socket() as probe:
            probe.settimeout(1)
            occupied = probe.connect_ex(('127.0.0.1', 8911)) == 0
        if occupied:
            if healthy():
                print(f'本地工作台已在运行，不重复启动：{URL}')
                return
            raise SystemExit('8911已有其他或未就绪服务；未停止任何进程。')
        python = ROOT / '.venv/bin/python'
        if not python.exists() or not (ROOT / 'frontend/dist/index.html').exists():
            raise SystemExit('缺少已有Python环境或前端构建；请先按README准备。')
        log_path = RUNTIME / 'server.log'
        log_fd = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(log_fd, 'ab', buffering=0) as log:
            child = subprocess.Popen(
                [str(python), str(ROOT / 'scripts/serve_m3.py')],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True,
            )
        # Diagnostic PID only; never trust this file to kill an arbitrary PID.
        state_fd = os.open(RUNTIME / 'server-process.json', os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(state_fd, 'w') as state:
            json.dump({'pid': child.pid, 'url': URL, 'log': str(log_path)}, state)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise SystemExit(f'服务启动失败，日志：{log_path}')
            if healthy():
                print(f'本地工作台已启动：{URL}\nPID：{child.pid}\n日志：{log_path}')
                return
            time.sleep(0.2)
        raise SystemExit(f'尚未通过健康检查，未声称启动成功；请检查PID {child.pid}，日志：{log_path}')


if __name__ == '__main__':
    start()
