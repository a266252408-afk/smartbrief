"""专项评测JSON归档；独立于旧protocol-1记录，写入采用原子替换。"""
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from db import database


def root():
    return Path(database.DB_PATH).parent/'evaluation-center'


def _path(run_id):
    if not re.fullmatch(r'er_[0-9a-f]{32}',run_id):
        raise ValueError('无效评测编号')
    return root()/'runs'/f'{run_id}.json'


def save(record):
    path=_path(record['run_id'])
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,suffix='.tmp',delete=False) as file:
            temporary=Path(file.name)
            json.dump(record,file,ensure_ascii=False,indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary,path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def get(run_id):
    path=_path(run_id)
    return json.loads(path.read_text('utf-8')) if path.exists() else None


def list_runs():
    folder=root()/'runs'
    return [json.loads(p.read_text('utf-8')) for p in sorted(folder.glob('er_*.json'),reverse=True)]
