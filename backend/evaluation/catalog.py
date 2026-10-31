"""版本化评测题库；只读取资料，不导入或执行题库附带脚本。"""
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

PACK_ROOT = Path(__file__).resolve().parents[2] / 'docs/evaluation-datasets/v1'
VERSION = 'sb-eval-data-1'
AREA_LABELS = {'protocol':'流程与故障','search':'网络搜索','report':'报告质量',
               'robustness':'鲁棒性与恢复','end_to_end':'完整任务'}


class CatalogError(RuntimeError):
    def __init__(self, code, message, status=503):
        super().__init__(message)
        self.code, self.status = code, status


class CatalogCase(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    case_id: str
    collection: Literal['golden','regression','challenge','legacy']
    area: Literal['protocol','search','report','robustness','end_to_end']
    title: str
    split: str
    human_review: str
    run_status: Literal['not_run'] = 'not_run'
    source_count: int
    has_fixture: bool


class CatalogArea(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    area: str
    label: str
    case_count: int
    runnable: bool
    profile: str | None
    availability: str
    score: float | None = None
    supported_case_ids: list[str] = []
    experiment_case_ids: list[str] = []


class EvaluationCatalog(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: str
    dataset_hash: str
    cutoff: str
    total_cases: int
    collections: dict[str,int]
    areas: list[CatalogArea]
    cases: list[CatalogCase]


def _read(path):
    return json.loads(path.read_text('utf-8'))


def _load():
    try:
        manifest = _read(PACK_ROOT / 'manifest.json')
        if manifest['version'] != VERSION:
            raise ValueError('版本不一致')
        for name, digest in manifest['files'].items():
            relative = PurePosixPath(name)
            path = (PACK_ROOT / name).resolve()
            if relative.is_absolute() or '..' in relative.parts or ':' in name or not path.is_relative_to(PACK_ROOT.resolve()):
                raise ValueError('越界文件')
            content = path.read_bytes()
            # Git在Windows和Linux之间转换文本行尾，语义相同不应损坏题库。
            lf = content.replace(b'\r\n', b'\n')
            if digest not in {hashlib.sha256(c).hexdigest() for c in (content, lf, lf.replace(b'\n',b'\r\n'))}:
                raise ValueError('资料指纹不一致')
        sources = {p.stem:_read(p) for p in (PACK_ROOT / 'sources').glob('*.json')}
        facts = {f['fact_id']:s['id'] for s in sources.values() for f in s['facts']}
        rubrics, fixtures = _read(PACK_ROOT/'rubrics.json'), _read(PACK_ROOT/'fixtures.json')
        cases = []
        for collection in ('golden','regression','challenge'):
            for case in _read(PACK_ROOT/f'private/{collection}.json')['cases']:
                if case['collection'] != collection:
                    raise ValueError('集合错误')
                ids = case['input']['source_ids']
                if any(s not in sources for s in ids):
                    raise ValueError('来源引用错误')
                if any(facts.get(f) not in ids for f in case['oracle']['required_fact_ids']):
                    raise ValueError('事实引用错误')
                if any(a['rubric_id'] not in rubrics for a in case['oracle']['assertions']):
                    raise ValueError('评分引用错误')
                if case['input']['fixture_id'] and case['input']['fixture_id'] not in fixtures:
                    raise ValueError('场景引用错误')
                cases.append(case)
        legacy = _read(PACK_ROOT/'legacy/protocol-1.json')
        if legacy != _read(Path(__file__).resolve().parents[1]/'data/evaluation_cases.json'):
            raise ValueError('旧协议变化')
        for old in legacy['cases']:
            cases.append(dict(case_id='P-'+old['case_id'],collection='legacy',area='protocol',
                              title=old['topic'],split='legacy',input=dict(user_request=old['topic'],source_ids=[],fixture_id=None),
                              provenance=dict(human_review='pending'),legacy=old))
        if len({c['case_id'] for c in cases}) != len(cases) or len(cases) != manifest['total_new_cases']+manifest['legacy_cases']:
            raise ValueError('案例数量错误')
        return manifest,cases,sources,rubrics,fixtures
    except (OSError,ValueError,KeyError,TypeError):
        raise CatalogError('CATALOG_UNAVAILABLE','题库缺失、损坏或引用不一致，请先校验数据包') from None


def summary(version=VERSION):
    if version != VERSION:
        raise CatalogError('INVALID_CATALOG_VERSION','未知题库版本',422)
    manifest,cases,_,_,_ = _load()
    rows = [CatalogCase(case_id=c['case_id'],collection=c['collection'],area=c['area'],title=c['title'],
                        split=c['split'],human_review=c['provenance']['human_review'],
                        source_count=len(c['input']['source_ids']),has_fixture=bool(c['input']['fixture_id'])) for c in cases]
    areas = [CatalogArea(area=key,label=label,case_count=sum(c.area==key for c in rows),
                         runnable=key=='protocol',profile='protocol-1' if key=='protocol' else None,
                         availability='原20例合成协议可运行' if key=='protocol' else '题目已准备，执行器待接通')
             for key,label in AREA_LABELS.items()]
    from services.evaluation_run_service import capabilities
    mechanism=capabilities()
    robust=next(a for a in areas if a.area=='robustness')
    robust.runnable=mechanism['available']
    robust.profile='mechanism_regression'
    robust.supported_case_ids=mechanism['supported_case_ids']
    robust.availability=f'本地机制回归已接通{len(robust.supported_case_ids)}个映射案例；其他场景待接通' if robust.runnable else '本地pytest环境不可用'
    from evaluation.search_replay import SUPPORTED
    search=next(a for a in areas if a.area=='search')
    search.runnable=True
    search.profile='search_replay'
    search.supported_case_ids=list(SUPPORTED)
    search.availability='12例冻结响应回放已接通；真实联网待授权，语义评分另列'
    from evaluation.report_grading import SUPPORTED as REPORT_CASES
    report=next(a for a in areas if a.area=='report')
    report.runnable=True
    report.profile='report_grading'
    report.supported_case_ids=list(REPORT_CASES)
    report.availability='固定资料报告程序检查与独立判分已接通；未启用语义模型时为未知'
    from evaluation.full_task import SUPPORTED as FULL_CASES
    full=next(a for a in areas if a.area=='end_to_end')
    full.runnable=True
    full.profile='full_task_local'
    full.supported_case_ids=list(FULL_CASES)
    full.availability='固定资料完整图和三组主备配置已接通；默认供应商替身，真实调用需单独授权'
    from evaluation.full_task import DEVELOPMENT_CASES
    full.experiment_case_ids=list(DEVELOPMENT_CASES)
    return EvaluationCatalog(version=VERSION,
        dataset_hash=hashlib.sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        cutoff=manifest['cutoff'],total_cases=len(rows),
        collections={key:sum(c.collection==key for c in rows) for key in ('golden','regression','challenge','legacy')},
        areas=areas,cases=rows).model_dump()


def detail(case_id):
    _,cases,sources,rubrics,fixtures = _load()
    case = next((c for c in cases if c['case_id']==case_id),None)
    if case is None:
        raise CatalogError('CASE_NOT_FOUND','评测案例不存在',404)
    return dict(case=case,sources=[sources[s] for s in case['input']['source_ids']],
                rubrics=[rubrics[r] for r in dict.fromkeys(a['rubric_id'] for a in case.get('oracle',{}).get('assertions',[]))],
                fixture=fixtures.get(case['input']['fixture_id']))
