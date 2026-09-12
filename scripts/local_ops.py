"""Private local status/backup/isolated restore. No delete, reset or live restore."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from clinical_qc_demo.delivery import backup, status, verify_backup, restore_isolated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    sub.add_parser('backup')
    verify = sub.add_parser('verify')
    verify.add_argument('backup', type=Path)
    restore = sub.add_parser('restore-isolated')
    restore.add_argument('backup', type=Path)
    restore.add_argument('--to', required=True, type=Path, help='必须是尚不存在的目录，其父目录需已存在')
    args = parser.parse_args()
    try:
        if args.command == 'status':
            result = status(ROOT)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['ok'] else 1
        if args.command == 'backup':
            dest = backup(ROOT)
            print(f'私有备份完成：{dest}\n含演示密码与原文，不上传。未备份模型权重/依赖；未删除任何文件。')
        elif args.command == 'verify':
            result = verify_backup(args.backup)
            print(json.dumps(dict(ok=True, files=len(result['files']), counts=result['database']['counts'],
                                  active_bundle=result['database']['active_bundle']), ensure_ascii=False, indent=2))
        elif args.command == 'restore-isolated':
            dest = restore_isolated(args.backup.absolute(), args.to.absolute())
            print(f'隔离恢复并核对成功：{dest}\n没有替换在线目录；没有启动应用或模型。')
        return 0
    except Exception as exc:
        print(f'未完成：{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
