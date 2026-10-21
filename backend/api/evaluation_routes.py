"""v2评测HTTP适配；服务层负责运行和归档规则。"""
from fastapi import APIRouter, Request
from starlette.responses import JSONResponse
from api.memory_routes import failure
from services import evaluation_service as service

router = APIRouter(prefix='/api/v2/evaluations')


def result(request,data,status=200):
    return JSONResponse(dict(data=data,meta=dict(request_id=request.state.request_id)),status_code=status)


@router.post('')
async def start(request: Request):
    try:
        body = service.EvaluationRequest.model_validate(await request.json())
    except ValueError:
        return failure(request,422,'INVALID_EVALUATION_REQUEST','评测版本、模式或开关无效')
    try:
        return result(request,service.start(body),202)
    except service.EvaluationError as exc:
        return failure(request,exc.status,exc.code,str(exc))


@router.get('')
async def listing(request: Request):
    try:
        page = int(request.query_params.get('page','1'))
        page_size = int(request.query_params.get('page_size','20'))
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError
    except ValueError:
        return failure(request,422,'INVALID_EVALUATION_QUERY','分页参数无效')
    try:
        return result(request,service.list_reports(page,page_size))
    except service.EvaluationError as exc:
        return failure(request,exc.status,exc.code,str(exc))


@router.get('/{eval_id}/comparison')
async def comparison(request: Request,eval_id: str,baseline: str | None=None):
    if not baseline:
        return failure(request,422,'INVALID_EVALUATION_QUERY','需要提供对照基线')
    try:
        return result(request,service.compare(eval_id,baseline))
    except service.EvaluationError as exc:
        return failure(request,exc.status,exc.code,str(exc))


@router.get('/{eval_id}')
async def detail(request: Request,eval_id: str):
    try:
        record = service.get(eval_id)
        if record is None:
            return failure(request,404,'EVALUATION_NOT_FOUND','评测不存在')
        return result(request,record)
    except service.EvaluationError as exc:
        return failure(request,exc.status,exc.code,str(exc))
