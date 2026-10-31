"""授权后的EXA固定查询适配；语义质量不由HTTP成功推断。"""
from datetime import datetime, timezone
import math
import os
from time import perf_counter

from evaluation import catalog
from evaluation.search_replay import LiveSearchMetrics, LiveSearchResult, canonical_url
from services.exa_evaluation_adapter import post_search
from services.exa_search import _snippet


async def run_case(case_id, grant):
    if case_id not in grant.case_ids:
        raise ValueError('搜索案例不属于本次授权')
    start = perf_counter()
    query = catalog.detail(case_id)['case']['input']['user_request']
    metrics = LiveSearchMetrics(returned_count=None, valid_url_count=None, unique_sources=None,
        exact_duplicate_count=None, dated_documents=None, unknown_period_documents=None,
        stale_documents=None, unavailable_content_count=None, network_calls=0)
    trace, evidence, error = [], [], None
    key = os.getenv('EXA_API_KEY', '').strip()
    try:
        if not key or datetime.fromisoformat(grant.expires_at) <= datetime.now(timezone.utc):
            raise PermissionError
        metrics.network_calls = 1
        trace.append(dict(kind='search_request', provider='exa', query=query, recorded_at=datetime.now(timezone.utc).isoformat()))
        raw = await post_search(dict(query=query, numResults=grant.max_results, type=grant.search_type, contents=dict(highlights=True)), key)
        items = raw['results']
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError('供应商响应结构无效')
        for rank, item in enumerate(items[:grant.max_results], 1):
            url = canonical_url(item.get('url', ''))
            if url and key in url:
                url = None
            date = item.get('publishedDate')
            try:
                datetime.fromisoformat(date)
            except (ValueError, TypeError):
                date = None
            snippet = _snippet(item).replace(key, '[已脱敏]')
            evidence.append(dict(rank=rank, title=str(item.get('title') or '').replace(key, '[已脱敏]'), url=url,
                snippet=snippet, published_at=date, data_period=None, origin='exa_live', content_scope='provider_excerpt',
                content_available=bool(snippet), retrieved_at=datetime.now(timezone.utc).isoformat()))
        urls = [row['url'] for row in evidence if row['url']]
        metrics.returned_count = len(evidence)
        metrics.valid_url_count = len(urls)
        metrics.unique_sources = len(set(urls))
        metrics.exact_duplicate_count = len(urls) - len(set(urls))
        metrics.dated_documents = sum(row['published_at'] is not None for row in evidence)
        metrics.unknown_period_documents = len(evidence)
        metrics.unavailable_content_count = sum(not row['content_available'] for row in evidence)
        cost = raw.get('costDollars', {})
        value = cost.get('total') if isinstance(cost, dict) else None
        metrics.provider_estimated_cost_usd = value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
        trace.extend(dict(kind='search_result', title=row['title'], published_at=row['published_at'],
                          data_period=None, origin='exa_live', recorded_at=row['retrieved_at']) for row in evidence)
    except Exception:
        error = '搜索请求未获有效响应；已请求/未请求以network_calls为准，不推断质量或零费用'
        trace.append(dict(kind='search_error', error_code='SEARCH_UNAVAILABLE', recorded_at=datetime.now(timezone.utc).isoformat()))
    return LiveSearchResult(case_id=case_id, check_status='unknown', test_count=0, passed=0, failed=0, skipped=0,
        test_names=[], execution_status='failed' if error else 'completed', exit_code=None,
        duration_seconds=round(perf_counter()-start, 4), error=error,
        scope='当次EXA提供商摘要；不证明正文已核验、语义相关性或统计期符合，费用仅供应商估计',
        trace=trace, metrics=metrics, evidence=evidence).model_dump()
