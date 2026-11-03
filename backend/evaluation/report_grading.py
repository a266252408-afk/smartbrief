"""独立报告评分契约；程序与语义判分分离。"""
from typing import Literal
import asyncio
import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field
from evaluation import catalog
from services.citations import PLACEHOLDER_REFERENCE, REFERENCE, normalize_reference_spacing

SUPPORTED = [f'G{i:03}' for i in range(2, 17, 2)]
Label = Literal['pass', 'fail', 'unknown', 'not_applicable']
GRADER_VERSION = 'independent-evidence-1'


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class ReportGradeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    profile: Literal['report_grading'] = 'report_grading'
    case_id: Literal['G002', 'G004', 'G006', 'G008', 'G010', 'G012', 'G014', 'G016']
    report: str = Field(max_length=100000)


class Judgement(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    rubric_id: str
    label: Label
    reason: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[str] = []


class ReportGradeResult(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    case_id: str
    check_status: Literal['pass', 'fail', 'unknown']
    report_quality: Literal['passed', 'rejected', 'unknown']
    report: str
    report_sha256: str
    judgements: list[Judgement]
    grader_version: str
    error: str | None


class CalibrationCounts(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    total: int
    unknown: int
    false_accepts: int
    false_rejects: int
    accuracy: float | None


class CalibrationRow(Judgement):
    case_id: str
    check_status: Literal['pass', 'fail', 'unknown']
    split: Literal['development', 'evaluation_only']
    expected: Literal['pass', 'fail', 'unknown']


class CalibrationResult(CalibrationCounts):
    run_id: str | None = None
    groups: dict[str, CalibrationCounts]
    results: list[CalibrationRow]
    grader_version: str
    scope: str


def citation_structure(report: str, allowed: set[str]) -> tuple[bool, str]:
    """只判引用结构：非空、无占位符、引用存在且属于当前资料、引用绑定声明。

    “数据待补充”是合法的缺口披露，不是引用失败；数字声明的映射由生产
    check_citations 与 Q_SUPPORT 分别负责，避免把日期/季度讨论误判为引用错误。
    """
    if not (report or '').strip():
        return False, '空稿，无正文'
    if PLACEHOLDER_REFERENCE.search(report):
        return False, '存在[S编号]/引用位占位符'
    text = normalize_reference_spacing(report)
    cited = REFERENCE.findall(text)
    if not cited:
        return False, '正文缺少[S编号]引用'
    foreign = sorted(set(cited) - allowed)
    if foreign:
        return False, '引用不属于当前资料：' + '、'.join(foreign)
    for number, paragraph in enumerate(re.split(r'\n\s*\n', text), 1):
        if REFERENCE.findall(paragraph) and not re.search(r'\w', REFERENCE.sub('', paragraph).strip('#>* - ')):
            return False, f'段落{number}引用无绑定声明'
    return True, '引用仅验证当前资料映射，不证明语义'


async def grade_report(body, grader=None, *, material=None):
    detail = material if material is not None else catalog.detail(body.case_id)
    allowed = set(detail.get('allowed_citations', [source['id'] for source in detail['sources']]))
    valid, cite_reason = citation_structure(body.report, allowed)
    judgements = [] if detail.get('skip_citation_check') else [Judgement(rubric_id='Q_CITE',
        label='pass' if valid else 'fail', reason=cite_reason)]
    for rubric in detail['rubrics']:
        if rubric['id'] == 'Q_CITE':
            continue
        payload = dict(query=detail['case']['input']['user_request'], response=body.report,
                       criterion=rubric['criterion'], sources=detail['sources'])
        judged = await semantic(payload, grader)
        judgements.append(Judgement(rubric_id=rubric['id'], **judged))
    labels = [j.label for j in judgements]
    status = 'fail' if 'fail' in labels else 'pass' if all(x == 'pass' for x in labels) else 'unknown'
    return ReportGradeResult(case_id=body.case_id, check_status=status,
        report_quality={'pass': 'passed', 'fail': 'rejected', 'unknown': 'unknown'}[status],
        report=body.report, report_sha256=digest(body.report), judgements=judgements,
        grader_version=GRADER_VERSION, error=None if grader else '语义评分器未启用，需独立授权和校准').model_dump()


async def semantic(payload, grader):
    if grader is not None:
        try:
            raw = await asyncio.wait_for(grader(payload), timeout=getattr(grader,'timeout_seconds',10))
            judged = Judgement.model_validate(dict(rubric_id='semantic', **raw))
            allowed = {s['id'] for s in payload['sources']}
            if not set(judged.evidence_refs) <= allowed:
                raise ValueError('评分引用越界')
            return judged.model_dump(exclude={'rubric_id'})
        except Exception:
            return dict(label='unknown', reason='独立评分失败或响应无效，请复核', evidence_refs=[])
    return dict(label='unknown', reason='未启用独立语义评分器', evidence_refs=[])


async def calibrate(grader=None, on_result=None):
    # _load先验证冻结资料指纹；参考标签在模型返回之后才使用。
    _, _, sources, _, _ = catalog._load()
    cases = catalog._read(catalog.PACK_ROOT / 'calibration.json')['cases']
    rows = []
    for case in cases:
        judged = await semantic(dict(query='仅依据给定资料判断声明是否得到支持', response=case['claim'],
            criterion='范围/统计期/数字/单位/预测与实绩须全部一致；不足为unknown',
            sources=[sources[s] for s in case['source_ids']]), grader)
        expected = 'pass' if case['label'] == 'supported' else 'unknown' if case['label'] == 'insufficient' else 'fail'
        rows.append(dict(case_id=case['id'], check_status=judged['label'] if judged['label'] in ('pass', 'fail') else 'unknown',
                         rubric_id='Q_SUPPORT', split=case['split'], expected=expected, **judged))
        if on_result is not None:
            await on_result(rows[-1])
    def counts(items):
        unknown = sum(r['label'] in ('unknown', 'not_applicable') for r in items)
        return dict(total=len(items), unknown=unknown,
            false_accepts=sum(r['label'] == 'pass' and r['expected'] != 'pass' for r in items),
            false_rejects=sum(r['label'] == 'fail' and r['expected'] == 'pass' for r in items),
            accuracy=sum(r['label'] == r['expected'] for r in items) / len(items) if items and not unknown else None)
    return CalibrationResult(**counts(rows), groups={split: counts([r for r in rows if r['split'] == split])
        for split in ('development', 'evaluation_only')}, results=rows, grader_version=GRADER_VERSION,
        scope='评分器校准，非被测系统成绩；默认未知，不证明线上准确率').model_dump()


class OpenJudgeAdapter:
    def __init__(self, grader):
        self.grader = grader

    async def __call__(self, payload):
        # 兼容OpenJudge pointwise aevaluate；禁止bool/字符串/中间分数被隐式放行。
        result = await self.grader.aevaluate(**payload)
        score, reason = result.score, result.reason
        if type(score) not in (int, float) or score not in (0, 1) or not isinstance(reason, str) or not reason.strip():
            raise ValueError('独立评分只接受明确二值结论')
        return dict(label='pass' if score == 1 else 'fail', reason=reason, evidence_refs=[])
