"""独立对照归档；不覆盖基线或旧协议结果。"""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from db.evaluation_run_repository import root


def save(record):
    folder = root() / 'comparisons'
    folder.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf8', dir=folder, suffix='.tmp', delete=False) as file:
            temporary = Path(file.name)
            json.dump(record, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, folder / (record['comparison_id'] + '.json'))
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def listing():
    return [json.loads(path.read_text('utf8')) for path in sorted((root() / 'comparisons').glob('ec_*.json'), reverse=True)]
