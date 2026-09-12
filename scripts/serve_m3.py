"""One process, one local port; build frontend before using the production URL."""
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    import uvicorn
    # Reserve the port BEFORE Store initialization or restart recovery can write.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", 8911))
            listener.listen(128)
        except OSError:
            raise SystemExit("8911不可用；未初始化数据库，也未停止现有服务。")
        from clinical_qc_demo.web_api import create_app
        print("合成演示/非临床用途。M4（兼容M3历史）: http://127.0.0.1:8911", flush=True)
        print(f"本机随机演示账户：{ROOT / 'runtime/web/local_accounts.json'}", flush=True)
        config = uvicorn.Config(create_app(ROOT), host="127.0.0.1", port=8911, access_log=False)
        uvicorn.Server(config).run(sockets=[listener])
