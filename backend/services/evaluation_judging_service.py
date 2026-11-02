"""真实独立评分任务装配与追加归档；从不改写原运行。"""
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4
from datetime import datetime,timezone

from evaluation import catalog
from evaluation.archive import provenance
from evaluation.report_grading import grade_report,calibrate,digest
from evaluation.real_judge import RealJudge,JUDGE_VERSION,GradingGrant,reserve_grading
from services.evaluation_service import EvaluationError
from services.evaluation_live_config import judge_config
from db import evaluation_run_repository as repository


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def prepare(body):
    material=None
    source_run=None
    source_hash=None
    if body.target=='calibration':
        case_ids=[c['id'] for c in catalog._read(catalog.PACK_ROOT/'calibration.json')['cases']]
    elif body.target=='report':
        case_ids=[body.case_id]
        material=catalog.detail(body.case_id)
        report=body.report
    else:
        source_run=repository.get(body.run_id)
        if source_run is None:
            raise EvaluationError('RUN_NOT_FOUND','原始运行不存在',404)
        if (source_run.get('execution_status')!='completed' or source_run.get('profile') not in
            ('full_task_live','full_task_local','search_live','search_replay')
            or (source_run.get('metadata') or {}).get('dataset_hash')!=body.live_consent.dataset_hash):
            raise EvaluationError('JUDGE_SOURCE_INVALID','原运行未完整结束、类型不支持或资料版本不一致',422)
        result=next((r for r in source_run['results'] if r['case_id']==body.case_id),None)
        if result is None:
            raise EvaluationError('JUDGE_SOURCE_INVALID','所选案例不属于原运行',422)
        source_hash=fingerprint(result)
        case_ids=[body.case_id]
        material=catalog.detail(body.case_id)
        refs=[dict(id=s['id'],title=s['title'],url=s['url'],facts=s['facts'],scope='冻结参考资料') for s in material['sources']]
        evidence=[dict(id=e.get('source_id') or f'E{i+1}',title=e.get('title',''),url=e.get('url'),
            excerpt=e.get('excerpt') or e.get('snippet',''),data_period=e.get('data_period'),published_at=e.get('published_at'),scope='本次取得的资料片段')
            for i,e in enumerate(result.get('evidence',[]))]
        material['sources']=evidence+refs
        material['allowed_citations']=[e['id'] for e in evidence]
        if source_run['profile'].startswith('search_'):
            report=json.dumps(evidence,ensure_ascii=False)
            rubrics=catalog._read(catalog.PACK_ROOT/'rubrics.json')
            material['rubrics']=[rubrics[k] for k in ('S_RELEVANCE','S_COVER','S_FRESH')]
            material['skip_citation_check']=True
        else:
            report=result.get('report','')
            # 完整任务的工程标准不交给语义模型，单列内容质量标准。
            rubrics=catalog._read(catalog.PACK_ROOT/'rubrics.json')
            material['rubrics']=[rubrics[k] for k in ('Q_SCOPE','Q_SUPPORT','Q_COVER','Q_GAP')]
    grant=reserve_grading(body,case_ids)
    now=datetime.now(timezone.utc).isoformat()
    return dict(run_id='er_'+uuid4().hex,profile='independent_judging',execution_status='running',case_ids=case_ids,
        results=[],report_quality='unknown',summary=dict(planned=len(case_ids),processed=0,passed=0,failed=0,unknown=0),
        error=None,created_at=now,updated_at=now,metadata=dict(provenance(grant.dataset_hash,None),dataset_hash=grant.dataset_hash,
            model=grant.model,grader_version=JUDGE_VERSION,rubric_hash=fingerprint(material['rubrics']) if material else digest('calibration-v1'),
            judge_same_as_primary=judge_config()['same_as_primary'],mode='online',target=body.target,
            source_run_id=body.run_id,source_result_sha256=source_hash,
            authorized_judge_grant=grant.model_dump(),judge_material=material,
            submitted_report=report if body.target!='calibration' else None))


async def execute(record):
    def save():
        rows=record['results']
        record['summary']=dict(planned=len(record['case_ids']),processed=len(rows),
            passed=sum(r['check_status']=='pass' for r in rows),failed=sum(r['check_status']=='fail' for r in rows),unknown=sum(r['check_status']=='unknown' for r in rows))
        record['updated_at']=datetime.now(timezone.utc).isoformat()
        repository.save(record)
    async def update_usage(usage):
        record['metadata']['judge_usage']=usage
        save()
    async def append(row):
        record['results'].append(row)
        save()
    grant=GradingGrant.model_validate(record['metadata']['authorized_judge_grant'])
    judge=RealJudge(grant,update_usage)
    try:
        if record['metadata']['target']=='calibration':
            result=await calibrate(judge,on_result=append)
            record['metadata']['calibration']={k:v for k,v in result.items() if k!='results'}
        else:
            body=SimpleNamespace(case_id=record['case_ids'][0],report=record['metadata']['submitted_report'])
            result=await grade_report(body,judge,material=record['metadata']['judge_material'])
            result['grader_version']=JUDGE_VERSION
            await append(result)
            record['report_quality']=result['report_quality']
        record['execution_status']='completed'
    finally:
        await judge.close()
        save()
