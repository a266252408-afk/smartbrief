"""审核标准的可计算边界；模型仍负责事实与分析质量的复核。"""
import re
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field



def report_metrics(body):
    text = re.sub(r'\[S\d+\]', '', body or '')
    return {'characters': len(body or ''), 'chinese_characters': len(re.findall(r'[\u4e00-\u9fff]', text)),
            'word_units': len(re.findall(r'[\u4e00-\u9fff]|[A-Za-z0-9]+', text)), 'minimum_target': 1000}


def policy_violations(review, body, *, policy=None, deterministic_findings=(), check_errors=()):
    """仅拦截可证伪的字数要求和明确新增的格式要求，不判断全文好坏。"""
    metrics = report_metrics(body)
    texts = list(review.get('comments') or [])
    texts += [f.get('problem', '') + '：' + f.get('expected', '') for f in review.get('findings', [])]
    errors = []
    for text in texts:
        if metrics['word_units'] >= 1000 and re.search(r'(?:篇幅|字数|正文).{0,24}(?:不足|低于|不够)|(?:扩充|增加).{0,8}字数', text):
            errors.append({'code':'LENGTH_CONTRADICTS_COUNT', 'text':text, 'metrics':metrics})
        if re.search(r'(?:每[个一]?章|各章|章节|第二章至第四章).{0,30}(?:小结|总结段落|结语段落)|独立标题页', text):
            if re.search(r'必须|缺失|缺少|不完整|添加|增加|填补|要求', text):
                errors.append({'code':'UNLISTED_FORMAT_REQUIREMENT', 'text':text})
    if policy is not None:
        reported_hard = any(f.get('severity') == 'hard' for f in review.get('findings', []))
        from services.review_rules import is_confirmed_blocker, material_pending, assess_model_finding
        hard = reported_hard or any(is_confirmed_blocker(f) for f in deterministic_findings)
        pending = any(material_pending(f) for f in deterministic_findings) or any(
            material_pending(assess_model_finding(f)) for f in review.get('findings', []))
        if (review.get('passed') and reported_hard) or (not review.get('passed') and not hard and not pending and not check_errors):
            errors.append({'code': 'BOOLEAN_FINDING_CONFLICT', 'text': '布尔判定须与具体阻塞问题一致；不能仅以soft建议拒绝、携带hard却自报通过，或删除原意见后默认通过'})
    return errors


class PolicyContract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class DataRequirements(PolicyContract):
    minimum_points: Literal[2] = 2
    evidence_required: Literal[True] = True
    core_questions_required: Literal[True] = True


class LengthGuidance(PolicyContract):
    target: Literal[1000] = 1000
    evidence_gap_may_be_shorter: Literal[True] = True


class ReviewPolicy(PolicyContract):
    version: Literal['review-policy-1'] = 'review-policy-1'
    task_id: str = Field(min_length=1)
    required_sections: tuple[Literal['summary'], Literal['body'], Literal['conclusion']] = ('summary', 'body', 'conclusion')
    required_questions: tuple[str, ...]
    data_requirements: DataRequirements = Field(default_factory=DataRequirements)
    length_guidance: LengthGuidance = Field(default_factory=LengthGuidance)
    blocking_rules: tuple[Literal['reference_integrity'], Literal['confirmed_fact_conflict'], Literal['core_evidence_gap'], Literal['required_structure']] = ('reference_integrity', 'confirmed_fact_conflict', 'core_evidence_gap', 'required_structure')


def freeze_policy(state):
    """同任务沿用已冻结标准；不从审核意见增加要求或降低事实门槛。"""
    existing = state.get('review_policy')
    if existing is not None:
        policy = ReviewPolicy.model_validate_json(json.dumps(existing, ensure_ascii=False))
        if policy.task_id != state.get('task_id'):
            raise ValueError('审核标准不属于当前任务')
    else:
        topic = state.get('topic') or ''
        policy = ReviewPolicy(task_id=state.get('task_id', ''), required_questions=(topic,) if topic else ())
    return policy.model_dump(mode='json')


def policy_text(policy):
    """只给模型共享业务要求，不要求其填写内部身份或执行回执。"""
    return ('本任务冻结审核标准：' + json.dumps({k: v for k, v in policy.items() if k != 'task_id'}, ensure_ascii=False)
            + '。事实、来源与数字必须有证据；文风建议为非阻塞soft，不能为凑字数编造；不得临时增加要求。')


def decide_quality(*, usable, review_complete, confirmed_blockers, unresolved_material=False):
    """完整性与实质错误优先；审核故障不能抹掉已确认拒绝依据。"""
    if any(type(v) is not bool for v in (usable, review_complete, unresolved_material)):
        raise ValueError('质量决策必须使用程序确认的布尔状态')
    if not usable:
        quality = 'not_run'
    elif confirmed_blockers:
        quality = 'rejected'
    elif not review_complete or unresolved_material:
        quality = 'unavailable'
    else:
        quality = 'passed'
    return dict(quality_status=quality, passed=quality == 'passed', review_complete=review_complete)
