"""SmartBrief本地备份/恢复命令，不启动服务、不读取.env或调用模型。"""
import argparse
from importlib import import_module
import os
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('backup', 'restore'))
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--code-tag', required=True)
    parser.add_argument('--stopped', action='store_true', help='确认已停止后端与MCP写入；仅备份使用')
    args = parser.parse_args()
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    os.environ['DATA_DIR'] = str(args.source)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
    try:
        service = import_module('services.data_backup')
        if args.action == 'backup':
            manifest = service.create_backup(args.source, args.destination, args.code_tag, args.stopped)
        else:
            manifest = service.restore_backup(args.source, args.destination, args.code_tag)
        print(f'操作完成：{args.action}，文件{len(manifest.files)}个，数据版本{manifest.data_schema_version}；目标{args.destination}')
        return 0
    except Exception as exc:
        # 不输出配置内容、文件正文或供应商凭证。
        reason = str(exc) if type(exc) in (ValueError, RuntimeError) else {
            'ValidationError': '备份清单结构非法',
            'FileExistsError': '目标必须为全新目录，禁止覆盖',
            'JSONDecodeError': '配置或清单不是合法JSON',
            'DatabaseError': '数据库完整性校验失败',
        }.get(type(exc).__name__, '文件访问或校验失败，请检查权限、磁盘与备份')
        print(f'操作失败：{reason}（{type(exc).__name__}）；未完成校验的目标不可启用。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
