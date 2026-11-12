"""程序预检的责任边界：规则命中不等于已确认事实错误。"""
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class RuleAssessment(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    origin: Literal['program', 'model']
    decision: Literal['confirmed', 'suspected', 'dismissed', 'unresolved']
    material: bool
    decision_reason: str = Field(min_length=1)


def is_confirmed_blocker(issue):
    return issue.get('decision') == 'confirmed' and bool(issue.get('material') or issue.get('severity') == 'hard')


def material_pending(issue):
    material = issue.get('material', issue.get('severity') == 'hard')
    return bool(material and issue.get('decision') not in {'confirmed', 'dismissed'})


def _numeric_frame(text):
    from services.citations import QUANTITY, REFERENCE
    text = REFERENCE.sub('', text or '')
    text = QUANTITY.sub('#', text)
    return re.sub(r'[\s。；，,.!?！？、：:;“”"\[\]（）()]+|^#{1,6}\s+', '', text).strip()


def _measurement_quote(quote):
    # 发布年份不自动变成统计年份；保留原始quote用于诊断。
    value = re.sub(r'(?:19|20)\d{2}年?(?:\d{1,2}月)?(?:\d{1,2}日)?\s*(?:发布|出版|更新|检索)', '', quote)
    return re.sub(r'(?:发布|出版|更新|检索)(?:日期|时间)?[：:\s]*(?:19|20)\d{2}年?(?:[-./月]\d{1,2})?(?:[-./日]\d{1,2})?日?', '', value)


def assess_issue(issue, sources):
    from services.citations import split_sentences, quantitative_tokens
    from services.claim_semantics import period_context, geography_context
    from services.numeric_claims import claim_token_matches
    kind = issue.get('kind') or issue.get('rule')
    decision, material, reason = 'suspected', True, '规则覆盖或语义归属尚未确认，须核验完整证据'
    known = {s['source_id']: s for s in sources if s.get('source_id') and not s.get('source_deleted')}
    refs = [known[sid] for sid in issue.get('source_ids', []) if sid in known]
    quote = issue.get('quote') or ''
    quote_valid = bool(quote and any(quote in (s.get('title', '') + '\n' + s.get('excerpt', '')) for s in refs))
    if kind == 'unbound_source' or issue.get('severity') == 'info':
        decision, material, reason = 'dismissed', False, '成组来源的说明信息，不是事实错误'
    elif kind in {'invalid_reference', 'missing_citation', 'rewrite_protocol'}:
        decision, reason = 'confirmed', '已核对引用完整性或明确结构约束'
    elif kind == 'avoidable_gap':
        decision, material, reason = 'suspected', False, '数据缺口建议（soft），作为提示保留，不阻塞通过'
    elif kind == 'required_structure':
        structure_kind = issue.get('structure_kind')
        if structure_kind in {'missing_summary', 'missing_conclusion'} and issue.get('has_section_headings'):
            reason = '存在其他章节标题，是否承担摘要或结论角色须由结构核验确认'
        elif structure_kind in {'missing_title', 'missing_summary', 'missing_conclusion', 'empty_summary', 'empty_conclusion', 'duplicate_chapter', 'glued_heading', 'rewrite_protocol'}:
            decision, reason = 'confirmed', '明确的标题完整性、空章节、标题正文粘连或协议装配约束未满足'
    elif kind == 'percentage_point_mismatch' and quote_valid:
        decision, reason = 'confirmed', '来源百分比与正文百分点的单位明确不同'
    elif kind in {'period_mismatch', 'geography_mismatch', 'comparison_basis_mismatch'} and quote_valid:
        actual_quote = quote
        for source in refs:
            prefix = source.get('title', '') + '\n'
            if actual_quote.startswith(prefix):
                actual_quote = actual_quote[len(prefix):]
                break
        actual_quote = _measurement_quote(actual_quote)
        a, b = period_context(issue.get('sentence', '')), period_context(actual_quote)
        if kind == 'period_mismatch':
            conflict = any(len(a[k]) == len(b[k]) == 1 and a[k] != b[k] for k in ('years', 'periods'))
        elif kind == 'geography_mismatch':
            x, y = geography_context(issue.get('sentence', '')), geography_context(actual_quote)
            conflict = len(x) == len(y) == 1 and x != y
        else:
            x = set(re.findall(r'同比|环比', issue.get('sentence', '')))
            y = set(re.findall(r'同比|环比', actual_quote))
            conflict = len(x) == len(y) == 1 and x != y
        if conflict:
            decision, reason = 'confirmed', '实际原句给出唯一且明确的冲突口径'
    elif kind == 'unbound_number':
        sentence = issue.get('sentence', '')
        for source in refs:
            if source.get('excerpt_status') == 'incomplete_tail':
                continue
            for passage in split_sentences(source.get('excerpt', '')):
                if _numeric_frame(sentence) != _numeric_frame(passage):
                    continue
                tokens = quantitative_tokens(sentence)
                if tokens:
                    matched = all(claim_token_matches(t, sentence, passage) for t in tokens)
                    decision = 'dismissed' if matched else 'confirmed'
                    material = not matched
                    reason = '同一明确句式的数值已匹配' if matched else '同一明确句式内数值或单位冲突'
                    break
            if decision != 'suspected':
                break
        if decision == 'suspected':
            # 被引来源不含该数值、候选来源含同指标数值时，可直接确认“引用挂错”。
            tokens = quantitative_tokens(sentence)
            candidates = [known[sid] for sid in issue.get('candidate_sources', []) if sid in known]
            if tokens and candidates:
                for source in candidates:
                    if any(all(claim_token_matches(t, sentence, passage) for t in tokens)
                           for passage in split_sentences(source.get('excerpt', ''))):
                        decision, reason = 'confirmed', '被引来源不含该数值，候选来源包含同指标数值，引用挂错'
                        break
    assessment = RuleAssessment(origin='program', decision=decision, material=material, decision_reason=reason)
    return dict(issue, **assessment.model_dump())


_COMPARISON = re.compile(r'最高|最低|第一|仅次于|领先|首位|最快|最强|最大|最小|年内最高|居首')


def _numbers_in(text):
    text = re.sub(r'\[S\d+\]', '', text or '')
    return {re.sub(r'[%,]', '', token) for token in re.findall(
        r'\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|\d+(?:\.\d+)?%?', text)}


def assess_model_finding(finding, report_sentence=""):
    evidence = finding.get('evidence') or {}
    quote = evidence.get('quote') or ''
    text = str(finding.get('problem') or '') + str(finding.get('expected') or '')
    if evidence.get('kind') == 'report' and _COMPARISON.search(text) and not _COMPARISON.search(quote):
        return dict(finding, **RuleAssessment(origin='model', decision='dismissed', material=False,
            decision_reason='比较级意见的报告原句不含比较级表述，疑似误述').model_dump())
    problem_numbers = _numbers_in(text)
    if evidence.get('kind') == 'report' and problem_numbers and not (problem_numbers & _numbers_in(quote)):
        return dict(finding, **RuleAssessment(origin='model', decision='dismissed', material=False,
            decision_reason='意见提到的数字不在报告原句中，疑似定位错误').model_dump())
    if evidence.get('kind') == 'source' and problem_numbers and report_sentence and not (
            problem_numbers & _numbers_in(report_sentence)):
        return dict(finding, **RuleAssessment(origin='model', decision='dismissed', material=False,
            decision_reason='意见提到的数字不在定位句所在报告原句中，疑似定位错误').model_dump())
    material = finding.get('severity') == 'hard' or finding.get('evidence', {}).get('kind', 'source') == 'source'
    decision = 'unresolved' if material and finding.get('severity') != 'hard' else 'confirmed'
    return dict(finding, **RuleAssessment(origin='model', decision=decision, material=material,
        decision_reason='已验证出处的语义审核意见' if decision == 'confirmed' else '来源事实意见不能仅按soft视作文风，待证据核验').model_dump())
