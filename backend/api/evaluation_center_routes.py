"""评测中心管理入口；不把隐藏标准注入被测任务。"""
from fastapi import APIRouter, Request

from api.evaluation_routes import result
from api.memory_routes import failure
from services import evaluation_catalog_service as catalog
from services import evaluation_run_service as runs
from evaluation.mechanism import MechanismRequest
from evaluation.search_replay import SearchRequest
from services.evaluation_service import EvaluationError
from services import evaluation_review_service as reviews
from services import evaluation_grading_service as grading
from evaluation.report_grading import ReportGradeRequest
from evaluation.full_task import FullTaskRequest

router = APIRouter(prefix='/api/v2/evaluation-center')


@router.post('/grade-report')
async def grade_report(request: Request):
    try:
        body = ReportGradeRequest.model_validate(await request.json())
    except ValueError:
        return failure(request, 422, 'INVALID_REPORT_GRADE', '报告案例或输入无效')
    try:
        return result(request, await grading.create(body), 201)
    except (EvaluationError, catalog.CatalogError) as exc:
        return failure(request, exc.status, exc.code, str(exc))


@router.post('/calibrate')
async def calibrate(request: Request):
    try:
        return result(request, await grading.calibration())
    except (EvaluationError, catalog.CatalogError) as exc:
        return failure(request, exc.status, exc.code, str(exc))


@router.get('/reviews')
async def review_list(request: Request):
    try:
        return result(request,dict(items=reviews.listing()))
    except EvaluationError as exc:
        return failure(request,exc.status,exc.code,str(exc))


@router.post('/reviews')
async def review_create(request: Request):
    try:
        body=reviews.EvaluationReviewRequest.model_validate(await request.json())
    except ValueError:
        return failure(request,422,'INVALID_EVALUATION_REVIEW','人工核验目标、版本或备注无效')
    try:
        return result(request,reviews.create(body,request.state.principal.role),201)
    except (EvaluationError,catalog.CatalogError) as exc:
        return failure(request,exc.status,exc.code,str(exc))


@router.get('/capabilities')
async def capabilities(request: Request):
    return result(request,runs.capabilities())


@router.post('/runs')
async def start_run(request: Request):
    try:
        raw=await request.json()
        profile = raw.get('profile') if isinstance(raw, dict) else None
        model = (FullTaskRequest if profile in ('full_task_local', 'full_task_live') else
                 SearchRequest if profile in ('search_replay', 'search_live') else MechanismRequest)
        body=model.model_validate(raw)
    except ValueError:
        return failure(request,422,'INVALID_MECHANISM_REQUEST','案例重复、未接通或请求格式无效')
    try:
        return result(request,runs.start(body),202)
    except (EvaluationError,catalog.CatalogError) as exc:
        return failure(request,exc.status,exc.code,str(exc))


@router.get('/runs')
async def run_list(request: Request):
    try:
        return result(request,runs.list_runs())
    except EvaluationError as exc:
        return failure(request,exc.status,exc.code,str(exc))


@router.get('/runs/{run_id}')
async def run_detail(request: Request,run_id: str):
    try:
        return result(request,runs.get(run_id))
    except EvaluationError as exc:
        return failure(request,exc.status,exc.code,str(exc))


@router.get('/catalog')
async def listing(request: Request, version: str = catalog.VERSION):
    try:
        return result(request,catalog.summary(version))
    except catalog.CatalogError as exc:
        return failure(request,exc.status,exc.code,str(exc))


@router.get('/cases/{case_id}')
async def detail(request: Request, case_id: str):
    try:
        return result(request,catalog.detail(case_id))
    except catalog.CatalogError as exc:
        return failure(request,exc.status,exc.code,str(exc))
