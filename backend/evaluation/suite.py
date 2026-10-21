"""版本化离线协议案例集；材料为合成夹具，不是真实市场事实。"""
import hashlib
import json
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

SUITE_VERSION = 'protocol-1'
DATASET = Path(__file__).resolve().parents[1] / 'data/evaluation_cases.json'


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    case_id: str = Field(pattern=r'^C[0-9]{2}$')
    category: Literal['regular', 'uncovered', 'evidence', 'fault']
    topic: str = Field(min_length=1, max_length=200)
    keywords: list[str] = Field(min_length=1)
    scenario: Literal['normal', 'no_sources', 'conflict', 'unknown_usage', 'model_timeout', 'review_invalid', 'budget', 'cancel']
    source_note: str = Field(min_length=1)


def load_suite(version=SUITE_VERSION, path=DATASET):
    if version != SUITE_VERSION:
        raise ValueError('未知评测集版本')
    raw = json.loads(Path(path).read_text('utf-8'))
    if raw['suite_version'] != version:
        raise ValueError('评测集版本不匹配')
    cases = [EvaluationCase.model_validate(c).model_dump() for c in raw['cases']]
    if len(cases) != 20 or len({c['case_id'] for c in cases}) != 20:
        raise ValueError('评测集必须恰好20个唯一案例')
    if Counter(c['category'] for c in cases) != dict(regular=10, uncovered=3, evidence=3, fault=4):
        raise ValueError('案例分类数量不符合协议')
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return cases, hashlib.sha256(canonical).hexdigest()
