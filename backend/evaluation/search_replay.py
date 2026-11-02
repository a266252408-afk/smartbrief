"""冻结搜索响应回放；没有实际联网，也不凭URL或关键词给语义质量分。"""
from datetime import datetime, timezone
from time import perf_counter
from typing import Literal
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evaluation.live_consent import SearchConsent
from evaluation import catalog
from evaluation.mechanism import MechanismResult, MechanismRun
from evaluation.report_grading import ReportGradeResult, CalibrationRow
from evaluation.full_task import FullTaskResult
from evaluation.experiment import ExperimentCaseResult

GOLDEN=[f'G{i:03}' for i in range(1,17,2)]
SUPPORTED=GOLDEN+['C003','C004','C006','C007']


class SearchRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    profile: Literal['search_replay','search_live']='search_replay'
    case_ids: list[str]=Field(min_length=1,max_length=12)
    authorization_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{8,80}$')

    live_consent: SearchConsent | None = None

    @model_validator(mode='after')
    def valid_cases(self):
        allowed=GOLDEN if self.profile=='search_live' else SUPPORTED
        if len(set(self.case_ids))!=len(self.case_ids) or any(cid not in allowed for cid in self.case_ids):
            raise ValueError('未知、重复或不支持的搜索案例')
        if self.profile == 'search_replay' and (self.authorization_id is not None or self.live_consent is not None):
            raise ValueError('本地回放不接受消费授权')
        if self.authorization_id is not None and self.live_consent is not None:
            raise ValueError('不能同时使用两种授权方式')
        return self


class SearchMetrics(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    returned_count: int
    valid_url_count: int
    unique_sources: int
    exact_duplicate_count: int
    dated_documents: int
    unknown_period_documents: int
    stale_documents: int | None
    freshness_target: str | None=None
    unavailable_content_count: int
    network_calls: Literal[0]=0
    replay_queries: Literal[1]=1
    relevance_at_5: float | None=None
    key_evidence_coverage: float | None=None


class SearchResult(MechanismResult):
    engine: Literal['frozen_search_replay']='frozen_search_replay'
    metrics: SearchMetrics
    evidence: list[dict]


class LiveSearchMetrics(SearchMetrics):
    returned_count: int | None
    valid_url_count: int | None
    unique_sources: int | None
    exact_duplicate_count: int | None
    dated_documents: int | None
    unknown_period_documents: int | None
    unavailable_content_count: int | None
    network_calls: Literal[0, 1]
    replay_queries: Literal[0] = 0
    provider_estimated_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class LiveSearchResult(MechanismResult):
    engine: Literal['exa_live_search'] = 'exa_live_search'
    metrics: LiveSearchMetrics
    evidence: list[dict]


class EvaluationRun(MechanismRun):
    profile: Literal['mechanism_regression','search_replay','search_live','report_grading','grader_calibration','full_task_local','full_task_live','bounded_experiment','independent_judging']
    report_quality: Literal['unknown','passed','rejected']
    results: list[MechanismResult | SearchResult | LiveSearchResult | ReportGradeResult | CalibrationRow | FullTaskResult | ExperimentCaseResult]


def canonical_url(value):
    try:
        url=urlsplit(value)
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:
            return None
        query=urlencode(sorted((k,v) for k,v in parse_qsl(url.query,keep_blank_values=True) if not k.lower().startswith('utm_')))
        return urlunsplit((url.scheme.lower(),url.netloc.lower(),url.path.rstrip('/'),query,''))
    except (TypeError,ValueError):
        return None


async def run_case(case_id):
    if case_id not in SUPPORTED:
        raise ValueError('搜索回放案例未接通')
    start=perf_counter()
    detail=catalog.detail(case_id)
    query=detail['case']['input']['user_request']
    fixture=detail['fixture'] or {}
    required=fixture.get('required_period')
    raw=fixture.get('search_results')
    origin='synthetic_fixture' if raw is not None else 'frozen_reference'
    if raw is None:
        raw=[dict(title=s['title'],url=s['url'],snippet='\n'.join(f['statement'] for f in s['facts']),
                  published_at=s['published'],data_period=' | '.join(dict.fromkeys(f['data_period'] for f in s['facts'])))
             for s in detail['sources']]
    evidence=[]
    keys=[]
    for i,row in enumerate(raw[:5]):
        url=canonical_url(row.get('url',''))
        period=row.get('data_period')
        key=row.get('original_source') or url
        if key:
            keys.append(key)
        evidence.append(dict(rank=i+1,title=row.get('title',''),url=url,snippet=row.get('snippet',''),
                             published_at=row.get('published_at'),data_period=period,origin=origin,
                             content_scope='curated_paraphrase' if origin=='frozen_reference' else 'fixture_snippet',
                             content_available=fixture.get('fetch_http_status',200)<400,
                             stale=bool(required and period and period!=required)))
    metrics=SearchMetrics(returned_count=len(evidence),valid_url_count=sum(bool(e['url']) for e in evidence),
        unique_sources=len(set(keys)),exact_duplicate_count=len(keys)-len(set(keys)),
        dated_documents=sum(bool(e['published_at']) for e in evidence),
        unknown_period_documents=sum(not e['data_period'] for e in evidence),
        stale_documents=sum(e['stale'] for e in evidence) if required else None,freshness_target=required,
        unavailable_content_count=sum(not e['content_available'] for e in evidence))
    now=datetime.now(timezone.utc).isoformat()
    trace=[dict(kind='search_request',query=query,provider='frozen_replay',recorded_at=now)]
    trace.extend(dict(kind='search_result',title=e['title'],published_at=e['published_at'],
                      data_period=e['data_period'],origin=e['origin'],recorded_at=now) for e in evidence)
    return SearchResult(case_id=case_id,check_status='unknown',test_count=0,passed=0,failed=0,skipped=0,
        test_names=[],execution_status='completed',exit_code=0,duration_seconds=round(perf_counter()-start,4),error=None,
        scope='冻结响应解析回放；相关性和证据充分性待独立评判，不证明联网或research自主搜索能力',
        trace=trace,metrics=metrics,evidence=evidence).model_dump()
