"""人工判定追加式审计；文件归属运行根，不修改冻结题库。"""
import json
import os
import re
from tempfile import NamedTemporaryFile
from pathlib import Path

from db.evaluation_run_repository import root


def append(record):
    if not re.fullmatch(r'evr_[a-f0-9]{32}',record['review_id']):
        raise ValueError('核验编号无效')
    folder=root()/'reviews'
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/(record['review_id']+'.json')
    if path.exists():
        raise FileExistsError('核验记录不能覆盖')
    temp=None
    try:
        with NamedTemporaryFile(mode='w',encoding='utf-8',dir=folder,suffix='.tmp',delete=False) as file:
            temp=Path(file.name)
            json.dump(record,file,ensure_ascii=False,indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp,path)
    finally:
        if temp and temp.exists():
            temp.unlink()


def list_reviews():
    records=[json.loads(p.read_text('utf-8')) for p in (root()/'reviews').glob('evr_*.json')]
    return sorted(records,key=lambda r:(r['recorded_at'],r['review_id']),reverse=True)
