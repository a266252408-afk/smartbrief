"""跨版本对照应用服务。"""
from db import evaluation_comparison_repository as repository
from evaluation.comparison import Comparison, compare
from services import evaluation_run_service
from services.evaluation_service import EvaluationError


def create(body):
    baseline = evaluation_run_service.get(body.baseline_id)
    current = evaluation_run_service.get(body.current_id)
    try:
        record = compare(body, baseline, current)
    except (ValueError, TypeError, AttributeError):
        raise EvaluationError('COMPARISON_STORAGE_FAILED', '被比较运行结构损坏，不能归因') from None
    try:
        repository.save(record)
    except OSError:
        raise EvaluationError('COMPARISON_STORAGE_FAILED', '对照未保存，请重试') from None
    return record


def listing():
    try:
        return [Comparison.model_validate(row).model_dump() for row in repository.listing()]
    except (OSError, ValueError):
        raise EvaluationError('COMPARISON_STORAGE_FAILED', '对照归档不可读') from None
