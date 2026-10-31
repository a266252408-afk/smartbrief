"""独立评分应用服务；每次归档新运行，不改变被测任务质量。"""
import json
from datetime import datetime, timezone
from uuid import uuid4

from db import evaluation_run_repository as repository
from evaluation import catalog
from evaluation.archive import provenance
from evaluation.report_grading import GRADER_VERSION, digest, grade_report, calibrate
from evaluation.search_replay import EvaluationRun
from services.evaluation_service import EvaluationError


async def create(body):
    result = await grade_report(body)
    pack = catalog.summary()
    metadata = provenance(pack['dataset_hash'], None)
    metadata.update(profile='report_grading', engine='independent_report_grading', model='none',
                    grader_version=GRADER_VERSION, rubric_hash=digest(json.dumps(catalog._read(catalog.PACK_ROOT / 'rubrics.json'), sort_keys=True)),
                    online_validated=False, memory_protocol='不读取或写入业务记忆')
    now = datetime.now(timezone.utc).isoformat()
    status = result['check_status']
    record = dict(run_id='er_' + uuid4().hex, profile='report_grading', execution_status='completed',
        case_ids=[body.case_id], results=[result], metadata=metadata, created_at=now, updated_at=now,
        report_quality=result['report_quality'], error=None,
        summary=dict(planned=1, processed=1, passed=int(status == 'pass'), failed=int(status == 'fail'), unknown=int(status == 'unknown')))
    record = EvaluationRun.model_validate(record).model_dump()
    _save(record)
    return record


async def calibration():
    data = await calibrate()
    now = datetime.now(timezone.utc).isoformat()
    data['run_id'] = 'er_' + uuid4().hex
    metadata = provenance(catalog.summary()['dataset_hash'], None)
    metadata.update(profile='grader_calibration', grader_version=GRADER_VERSION, model='none',
                    online_validated=False, calibration={k: v for k, v in data.items() if k != 'results'})
    record = EvaluationRun(run_id=data['run_id'], profile='grader_calibration', execution_status='completed',
        case_ids=[r['case_id'] for r in data['results']], results=data['results'], metadata=metadata,
        created_at=now, updated_at=now, error=None, report_quality='unknown',
        summary=dict(planned=data['total'], processed=data['total'], passed=0, failed=0, unknown=data['total'])).model_dump()
    _save(record)
    return data


def _save(record):
    try:
        repository.save(record)
    except OSError:
        raise EvaluationError('RUN_STORAGE_FAILED', '评分未保存，请重试') from None
