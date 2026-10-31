"""版本绑定的人工核验，原始机器结果和题库均保持不可变。"""
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from db import evaluation_review_repository as repository
from db import evaluation_run_repository as runs
from evaluation import catalog
from services.evaluation_service import EvaluationError


class EvaluationReviewRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,json_schema_extra={'allOf':[{
        'if':{'properties':{'target':{'const':'run_result'}}},
        'then':{'required':['run_id'],'properties':{'run_id':{'type':'string'}}},
        'else':{'properties':{'run_id':{'type':'null'}}}}]})
    target: Literal['case_definition','run_result']
    case_id: str=Field(pattern=r'^(?:[GRC][0-9]{3}|P-C[0-9]{2})$')
    run_id: str | None=Field(default=None,pattern=r'^er_[0-9a-f]{32}$')
    dataset_hash: str=Field(pattern=r'^[0-9a-f]{64}$')
    decision: Literal['pass','fail','unknown']
    notes: str=Field(min_length=1,max_length=2000,pattern=r'\S')

    @model_validator(mode='after')
    def validate_target(self):
        if (self.target=='run_result') != (self.run_id is not None):
            raise ValueError('运行结果需指定运行编号，题目核验不得附加运行编号')
        return self


class EvaluationReview(EvaluationReviewRequest):
    review_id: str
    actor: Literal['admin']
    recorded_at: str
    supersedes: str | None


def listing():
    try:
        return [EvaluationReview.model_validate(row).model_dump() for row in repository.list_reviews()]
    except (OSError,ValueError,KeyError):
        raise EvaluationError('REVIEW_STORAGE_FAILED','人工核验记录不可读') from None


def create(body: EvaluationReviewRequest,actor):
    if actor!='admin':
        raise EvaluationError('READ_ONLY','只读访问不能提交人工核验',403)
    pack=catalog.summary()
    catalog.detail(body.case_id)
    if body.dataset_hash!=pack['dataset_hash']:
        raise EvaluationError('REVIEW_VERSION_MISMATCH','资料版本已变化，请重新查看后核验',409)
    if body.target=='run_result':
        try:
            run=runs.get(body.run_id)
            if not run or run['metadata'].get('dataset_hash')!=body.dataset_hash or not any(
                    row['case_id']==body.case_id for row in run['results']):
                raise EvaluationError('REVIEW_RESULT_MISMATCH','运行版本不匹配或该案例尚无实际结果',409)
        except (OSError,ValueError,KeyError):
            raise EvaluationError('REVIEW_STORAGE_FAILED','被复核的运行结果不可读') from None
    previous=next((r for r in listing() if r['target']==body.target and r['case_id']==body.case_id
                   and r['run_id']==body.run_id and r['dataset_hash']==body.dataset_hash),None)
    record=EvaluationReview(**body.model_dump(),review_id='evr_'+uuid4().hex,actor='admin',
                            recorded_at=datetime.now(timezone.utc).isoformat(),
                            supersedes=previous['review_id'] if previous else None).model_dump()
    try:
        repository.append(record)
    except (OSError,ValueError):
        raise EvaluationError('REVIEW_STORAGE_FAILED','人工核验未保存，请重试') from None
    return record
