"""严格审核节点：质量拒绝与运行结束分别表达。"""
import asyncio
import hashlib
import json
import re
from llm.client import chat
from services.review_contracts import ReviewResult
from services.rewrite_patches import paragraph_table
from services.usage_service import usage_scope, UsagePersistenceError
from services.retrieval_service import refresh_document_sources
from services.report_quality import parse_review, usable_draft, structure_gaps, duplicate_metrics, needs_research, repeated_passages
from services.citations import check_citations, reference_ids
from services.evidence_ranking import review_evidence_block
from services.findings import build_findings, finding_block, merge_findings, reference_findings
from services.event_logs import EventLogs
from services.review_policy import report_metrics, policy_violations, freeze_policy, policy_text, decide_quality
from services.review_rules import assess_issue, is_confirmed_blocker, material_pending
from services.issue_identity import index_document, attach_identities, citation_issue
from services.review_validation import finding_errors, response_audit

REVIEW_STEP_ID = 0


class ReviewResponseError(ValueError):
    """保留模型调用成功后发生的解析/结构错误，供审核历史落库。"""

    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        super().__init__(diagnostic['message'])


def _parse_response(resp):
    choices = getattr(resp, 'choices', None) or []
    choice = choices[0] if choices else None
    raw = getattr(getattr(choice, 'message', None), 'content', None) or ''
    if not isinstance(raw, str):
        raw = repr(raw)
    diagnostic = {
        'stage': 'response_validation', 'response_id': getattr(resp, 'id', None),
        'model': getattr(resp, 'model', None), 'finish_reason': getattr(choice, 'finish_reason', None),
        'raw_response': raw[:32000], 'raw_length': len(raw), 'raw_truncated': len(raw) > 32000,
        'raw_sha256': hashlib.sha256(raw.encode()).hexdigest(),
    }
    try:
        if diagnostic['finish_reason'] == 'length':
            raise ValueError('审核响应已截断')
        return parse_review(raw)
    except (ValueError, TypeError) as exc:
        diagnostic['message'] = str(exc)
        diagnostic['exception_type'] = type(exc).__name__
        diagnostic['code'] = ('REVIEW_EMPTY_RESPONSE' if not raw.strip() else
                              'REVIEW_JSON_INVALID' if isinstance(exc, json.JSONDecodeError) else
                              'REVIEW_SCHEMA_INVALID')
        if diagnostic['finish_reason'] == 'length':
            diagnostic['code'] = 'REVIEW_RESPONSE_TRUNCATED'
        # 仅隔离格式不完整的 finding；顶层契约仍严格校验，不猜测布尔值、位置或证据。
        text = raw.strip()
        fence = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
        try:
            data = json.loads(fence.group(1) if fence else text)
            if not isinstance(data, dict) or not isinstance(data.get('findings'), list):
                raise ValueError('没有可隔离的 findings 数组')
            base = dict(data, findings=[])
            parsed = parse_review(json.dumps(base, ensure_ascii=False))
            invalid = []
            valid = []
            for index, item in enumerate(data['findings']):
                try:
                    checked = parse_review(json.dumps(dict(base, findings=[item]), ensure_ascii=False))
                    valid.extend(checked['findings'])
                except (ValueError, TypeError) as finding_error:
                    invalid.append({'index': index, 'message': str(finding_error)})
            if not invalid:
                raise ValueError('错误不在 finding 条目')
        except (ValueError, TypeError):
            raise ReviewResponseError(diagnostic) from exc
        diagnostic['code'] = 'REVIEW_FINDINGS_PARTIAL'
        diagnostic['invalid_findings'] = invalid
        parsed.update(passed=False, findings=valid, review_diagnostics=diagnostic)
        parsed['comments'] = ['审核返回的部分 finding 不完整，未采纳且本轮不能判为通过'] + parsed['comments']
        return parsed

SYSTEM_PROMPT = """你是市场调研报告质量审核员。请对给定的报告草稿进行评审，严格按以下标准判断：

1. 是否包含至少 2 个具体数据点（数字、百分比、金额等）？
2. 是否使用系统引用标注 [S#] 将数字声明绑定到给定来源？[S#] 是合法的内部来源编号，不得要求改写成机构名、报告全称或参考文献列表。
3. 正文是否完整覆盖主题？字数目标为不少于 1000 字；若给定资料不足以支撑该长度，允许在明确标注“数据待补充”的情况下低于目标，但不得为凑字数编造内容。
4. 数字声明是否被所引来源摘要支持？若某来源被引用但摘要中找不到对应数字或归因，指出过度引用或无支持。
5. 是否使用了可获得的最新实绩？若草稿使用旧预测而忽略更新实绩，指出。
6. 同一指标前后是否矛盾？比较基数（环比/同比/较某期）是否与来源一致？
7. 报告结构是否完整：是否有标题、执行摘要、结论与建议，章节编号是否一致？
8. “最高/最低/第一/仅次于”等比较级是否与来源排序一致？来源没有给出排序时不得使用比较级。

仅返回 JSON：
{"passed": true/false, "comments": ["意见1", "意见2"], "findings": [], "possible_rule_issue": null, "full_rewrite_required": false}
- passed：满足全部标准为 true，否则为 false
- comments：审核意见列表；通过时可为空数组；不通过时必须给出具体修改意见
- finding_id 必须为 MODEL-0001 形式的非空字符串；只返回新增问题，确定性 findings 由系统保留。
- 结构问题使用 evidence={"kind":"report","quote":"报告原文"}；来源问题使用 kind=source、source_id、quote。
- findings：结构化问题列表。每条必须包含 finding_id、problem、evidence{source_id, quote}、location{paragraph, sentence}、expected、severity(hard/soft)
- location.paragraph 和 location.sentence 必须是从 1 开始的整数，不能返回字符串、null 或省略 sentence；没有额外问题时 findings 返回 []
- evidence.kind=source 时 quote 必须逐字来自给定来源摘要；kind=report 时必须逐字来自指定报告段落。不得编造。
- expected 必须是可执行修改；scope=paragraph 表示局部，scope=full 表示整篇重组（location仅作问题锚点）。全局问题不得限制在单个段落。
- comments 用于解释 findings，不得额外增加无证据的阻塞要求；不要求独立标题页等未列标准。
- 不得推翻确定性 hard 结论，也不得删除给定 finding；若认为规则误判，写入 possible_rule_issue，但保留 finding 与 passed=false
- full_rewrite_required：仅当结构整体不成立、占位稿或修改点超过一半时为 true
不要输出任何其他文字，不要使用 Markdown 代码块。
"""


def _scale_hints(issue: dict) -> str:
    hints = [
        f"{token} 在 {match['source']} 中为同数值的 {match['factor']} 倍（疑似单位换算）"
        for token, matches in (issue.get("scale_matches") or {}).items()
        for match in matches[:1]
    ]
    return "；" + "、".join(hints) if hints else ""


def _binding_error_text(issue: dict) -> str:
    """按问题类别给出可执行文案：挂错 / 单位量级 / 无来源支持 / 区间合并。"""
    target = "、".join(issue.get("source_ids") or [])
    numbers = "、".join(issue.get("numbers") or [])
    if issue.get("kind") == "merged_fact_range":
        return f"事实区间合并：[{target}] {numbers} 来自不同来源，必须分别引用并说明统计口径"
    category = issue.get("category")
    if category == "miscitation":
        candidates = "、".join(issue.get("candidate_sources") or [])
        return f"引用挂错：[{target}] 数字 {numbers} 不在所引来源，实际出处：{candidates}"
    if category == "unit_scale":
        return f"单位量级错误：[{target}] 数字 {numbers} 与来源数值相同但量级不一致" + _scale_hints(issue)
    return f"无来源支持：[{target}] 数字 {numbers} 未在任何来源中找到"


async def _review(draft: str, evidence: list, *, binding_issues=None, structure_issues=None,
                  duplicate_issues=None, required_ids=None, deterministic_findings=None,
                  check_errors=None, citation_retirements=None, policy=None) -> dict:
    ids = sorted({source.get("source_id") for source in evidence if source.get("source_id")})
    source_chars = sum(len(source.get("excerpt", "") or "") for source in evidence)
    guidance = f"可用来源编号：{'、'.join(ids) or '无'}；给定资料摘要约 {source_chars} 字。"
    if citation_retirements:
        guidance += ('\n以下声明与引用在返工中声明删除，程序只确认文本变化，未确认语义。逐项核对当前稿是否仍保留同义断言、'
                     '造成关键内容缺失或无支持陈述；存在则给出有证据的finding，不得因模型声称删除直接通过：'
                     + json.dumps(citation_retirements, ensure_ascii=False))
    if source_chars < 1500:
        guidance += "资料不足以支撑 1000 字时，允许明确标注“数据待补充”后低于目标，不得编造。"
    binding_lines = []
    # unbound_source 只是“成组引用中某一来源不含本句数字”，不是错误，不喂给审核员，
    # 否则会把正确的多来源引用误读成归因错误。
    actionable = {"unbound_number", "unverified_comparison", "unsupported_wording", "merged_fact_range"}
    for issue in (binding_issues or []):
        if issue.get("kind") not in actionable:
            continue
        target = issue.get("source_id") or "、".join(issue.get("source_ids") or [])
        candidates = issue.get("candidate_sources") or []
        candidate_hint = f"；候选来源：{'、'.join(candidates)}" if candidates else ""
        detail = issue.get("numbers") or issue.get("terms") or issue.get("keywords") or []
        binding_lines.append(
            f"- [{target}] {issue.get('kind')}：{issue.get('sentence', '')} "
            f"对象={detail}；{issue.get('reason', '')}{candidate_hint}"
        )
        if len(binding_lines) >= 8:
            break
    binding_block = "确定性引用绑定检查：未发现数字级不一致。"
    if binding_lines:
        binding_block = "确定性引用绑定检查（只验证数字能否在来源摘要中找到，不判断语义）：\n" + "\n".join(binding_lines)
    structure_lines = [f"- {item['message']}" for item in (structure_issues or [])]
    structure_block = ""
    if structure_lines:
        structure_block = "\n\n报告结构与重复检查：\n" + "\n".join(structure_lines)
    duplicate_lines = [
        f"- 指标 {item.get('value')}（{item.get('section_count')} 个章节）：{'、'.join(item.get('sections', []))}"
        for item in (duplicate_issues or [])[:5]
    ]
    if duplicate_lines:
        structure_block += "\n重复指标：\n" + "\n".join(duplicate_lines)
    original_draft = draft
    metrics = report_metrics(draft)
    draft = "程序字数统计：" + json.dumps(metrics, ensure_ascii=False) + "\n段落坐标：" + json.dumps(paragraph_table(draft), ensure_ascii=False) + "\n\n报告：\n" + draft
    finding_lines = finding_block(deterministic_findings or [])
    finding_text = ""
    if finding_lines:
        finding_text = ("\n\n确定性规则 findings（finding_id 固定，不得删除或推翻 hard 结论）：\n"
                        + finding_lines)
    error_lines = [f"- {error}" for error in (check_errors or [])]
    error_text = "\n\n确定性来源映射错误：\n" + "\n".join(error_lines) if error_lines else ""
    evidence_block = review_evidence_block(draft, evidence, required_ids=required_ids)
    system_prompt = SYSTEM_PROMPT
    if policy:
        system_prompt = system_prompt.replace(
            '不得推翻确定性 hard 结论，也不得删除给定 finding；若认为规则误判，写入 possible_rule_issue，但保留 finding 与 passed=false',
            '不得豁免来源归属等confirmed完整性事实。suspected/unresolved为待核验疑点，不等同已确认错误；对照完整来源核验，不得仅因规则未知就拒绝。程序保留原记录，若有实质错误给出准确hard意见，普通文风用soft')
        finding_text = finding_text.replace('确定性规则 findings（finding_id 固定，不得删除或推翻 hard 结论）', '程序预检 findings（confirmed才为已确认；suspected/unresolved必须核验）')
    if policy:
        system_prompt += ('\n全稿逐项核对冻结核心问题与分析前提；可返回core_coverage[{question,covered,quote,reason}]，'
            '一旦提供须逐项覆盖全部冻结问题，quote逐字取报告原句。semantic_duplicates须给first_quote/second_quote两处原句、'
            'information_loss布尔及reason；摘要正常复述不算正文重复，不可因指标多次出现删独有证据。')
    messages = [
        {"role": "system", "content": system_prompt + ("\n" + policy_text(policy) if policy else "")},
        {
            "role": "user",
            "content": (
                f"{guidance}\n\n{binding_block}{structure_block}{finding_text}{error_text}\n\n"
                f"{evidence_block}\n\n"
                f"待审核报告草稿：\n\n{draft}"
            ),
        },
    ]
    corrections = []
    response_attempts = []
    for attempt in range(2):
        resp = await chat(messages, role="review", output_contract=ReviewResult)
        audit=response_audit(resp)
        if policy:
            response_attempts.append(audit)
        try:
            result = _parse_response(resp)
            invalid=(finding_errors(result,original_draft,evidence,task_id=policy['task_id']) if policy else [])
            if policy:
                from services.content_integrity import validate_review_content
                try:
                    result['content_checks']=validate_review_content(result,original_draft,policy['required_questions'])
                except ValueError as exc:
                    invalid.append(dict(code='CONTENT_REVIEW_INVALID',path='/content_checks',message=str(exc)))
            diagnostic=result.get('review_diagnostics') if policy else None
            if invalid:
                diagnostic=dict(audit,code='REVIEW_FINDING_INVALID',message='审核意见原文、来源或位置无效',issues=invalid)
            if diagnostic:
                raise ReviewResponseError(diagnostic)
        except ReviewResponseError as exc:
            if not policy:
                raise
            audit.update(decision='rejected',code=exc.diagnostic['code'],message=exc.diagnostic['message'])
            if attempt:
                raise ReviewResponseError(dict(exc.diagnostic,review_response_attempts=response_attempts)) from exc
            messages=messages+[dict(role='user',content='审核响应无效，请仅纠正原文/出处/坐标或格式并返回完整审核JSON，不改报告、不补造原文：'+json.dumps(exc.diagnostic,ensure_ascii=False))]
            continue
        violations = policy_violations(result, original_draft, policy=policy, deterministic_findings=deterministic_findings or [], check_errors=check_errors or [])
        if not violations:
            audit['decision']='accepted'
            break
        audit.update(decision='rejected',code='REVIEW_POLICY_INVALID',message='审核违反冻结标准')
        corrections.append({'review': result, 'violations': violations})
        if attempt:
            raise ReviewResponseError({'stage':'review_policy', 'code':'REVIEW_POLICY_INVALID',
                'message':'审核重复违反冻结标准或布尔判定与阻塞问题不一致，未将其转交正文返工',
                'policy_corrections':corrections,'review_response_attempts':response_attempts})
        messages = messages + [{'role':'assistant','content':json.dumps(result, ensure_ascii=False)},
            {'role':'user','content':'请纠正以下审核标准错误并重新给出完整审核JSON；不得新增每章小结、独立标题页等硬要求。'
             '按程序字数判断长度；仅soft建议不得拒绝，有实质事实错误必须准确标明阻塞问题及证据；保留建议，不得为纠错直接放行：' + json.dumps(violations, ensure_ascii=False)}]
    if corrections:
        result['policy_corrections'] = corrections
    if policy:
        result['review_response_attempts']=response_attempts
    known_ids = {s.get('source_id') for s in evidence if not s.get('source_deleted')}
    result['evidence_coverage'] = {
        'required_ids': sorted(required_ids or []),
        'included_ids': sorted(set(required_ids or []) & known_ids),
        'missing_ids': sorted(set(required_ids or []) - known_ids),
        'required_excerpt_policy': 'full',
        'context_sha256': hashlib.sha256(evidence_block.encode()).hexdigest(),
    }
    if citation_retirements:
        result['citation_retirements'] = citation_retirements
    return result


@usage_scope("reviewer", state=True)
async def reviewer_node(state: dict) -> dict:
    draft = state.get("draft_content") or ""
    if state.get('report_pipeline_version') == 3 and state.get('report_document'):
        from services.report_document import validate_document,render_document,narrative_view
        validate_document(state['report_document'],state['task_id'])
        draft=render_document(state['report_document'])
        checked_draft=narrative_view(state['report_document'])
    else:
        checked_draft=draft
    logs = EventLogs(state.get("react_logs", []))
    history = list(state.get("review_history", []))
    iteration = int(state.get("iteration", 0))
    maximum = int(state.get("max_iterations", 3))
    round_no = iteration + 1
    if state.get("execution_status") == "failed" or not usable_draft(draft) or (state.get('report_pipeline_version') == 3 and not state.get('report_document')):
        logs.append({"type": "Observation", "content": "无可用草稿或写作失败，未执行审核", "step_id": 0})
        if state.get('report_pipeline_version') == 3:
            from services.document_writer import writer_provenance
            history.append(dict(round=round_no,passed=False,quality_status='not_run',review_complete=False,
                comments=['结构化文档未获验证，未执行审核'],**writer_provenance(state)))
        return {"execution_status": "failed", "quality_status": "not_run", "passed": False,
                "forced_pass": False, "forced_output": False,
                "error_code": state.get("error_code") or "DRAFT_UNAVAILABLE",
                "error": "报告写作失败", "react_logs": logs,
                **({'review_history':history} if state.get('report_pipeline_version') == 3 else {})}
    from evaluation.context import enabled
    if not enabled('review'):
        logs.append(dict(type='Observation',content='本轮评测关闭审核；质量未执行，不作为通过',step_id=0))
        return dict(quality_status='not_run',passed=False,forced_pass=False,forced_output=False,
                    error_code='REVIEW_DISABLED',react_logs=logs)
    policy = freeze_policy(state) if state.get("report_pipeline_version", 1) >= 2 else None
    content_index = index_document(state["task_id"], draft, state.get("content_index")) if policy else None
    evidence = await asyncio.to_thread(refresh_document_sources, state.get("evidence", []))
    check = check_citations(state.get("task_id", ""), checked_draft, evidence, state.get("required_citations", []))
    binding = check.get("binding_issues", [])
    hard_binding = [issue for issue in binding if issue.get("kind") == "unbound_number" and (not policy or is_confirmed_blocker(issue))]
    hard_ranges = [issue for issue in binding if issue.get("kind") == "merged_fact_range" and (not policy or is_confirmed_blocker(issue))]
    structure = structure_gaps(checked_draft)
    if state.get("report_pipeline_version", 1) >= 2:
        structure.extend(repeated_passages(checked_draft))
    duplicates = duplicate_metrics(checked_draft)
    binding_errors = [_binding_error_text(issue) for issue in hard_binding + hard_ranges]
    deterministic_findings = build_findings(draft, evidence, binding) + reference_findings(draft, check)
    if state.get("report_pipeline_version", 1) >= 2:
        first_paragraph = paragraph_table(draft)[0]["text"]
        for issue in structure:
            if issue["kind"] == "duplicate_passage":
                continue
            deterministic_findings.append({
                "finding_id": "STRUCT-REQUIRED-" + issue["kind"], "rule": "required_structure",
                "kind": "required_structure", "severity": "hard", "problem": issue["message"],
                "structure_kind": issue["kind"], "has_section_headings": bool(re.search(r"(?m)^#{2,6}\s+", draft)),
                "expected": "补齐唯一报告标题、执行摘要与结论，修正重复章节和返工协议；保持有效事实引用",
                "evidence": {"kind": "report", "quote": first_paragraph},
                "location": {"paragraph": 1, "sentence": 1}, "scope": "full",
            })
    # 重复章节与嵌入第二份报告可直接定位为全局装配问题。
    headings = {}
    for paragraph in paragraph_table(checked_draft):
        for line in paragraph['text'].splitlines():
            if not re.match(r'^##\s+', line):
                continue
            title = re.sub(r'^##\s*(?:[一二三四五六七八九十0-9]+[、.．]\s*)?', '', line).strip()
            if title in headings or re.search(r'市场调研报告|市场研究报告', title):
                deterministic_findings.append({
                    'finding_id': f'STRUCT-{paragraph["paragraph"]:04d}', 'rule': 'report_assembly',
                    'kind': 'report_assembly', 'severity': 'hard', 'scope': 'full',
                    'problem': '报告中出现重复章节或嵌入第二份报告标题',
                    'expected': '合并重复正文，整篇统一标题、摘要、章节与结论；仅保留一份报告',
                    'evidence': {'kind': 'report', 'quote': line},
                    'location': {'paragraph': paragraph['paragraph'], 'sentence': 1}})
            headings[title] = paragraph['paragraph']
    for issue in check.get('issues', []):
        n = issue.get('paragraph', 1)
        table = paragraph_table(draft)
        if 1 <= n <= len(table):
            deterministic_findings.append({'finding_id': f'CITE-{len(deterministic_findings)+1:04d}',
                'rule':'missing_citation', 'severity':'hard', 'problem':'数字声明缺少引用',
                'expected':'删除无支持声明或补充支持该声明的来源引用',
                'location':{'paragraph':n,'sentence':1},
                'evidence':{'kind':'report','quote':table[n-1]['text']}})
    if policy:
        from services.content_integrity import deduplicate_markdown
        _,duplicates_to_remove=deduplicate_markdown(checked_draft)
        if state.get('report_pipeline_version')==3:
            from services.content_integrity import deduplicate_document
            from services.report_document import render_parts
            _,removed=deduplicate_document(state['report_document'])
            rendered=dict(render_parts(state['report_document']))
            for entry in removed:
                quote=rendered[entry['block_id']]
                n=next(p['paragraph'] for p in paragraph_table(draft) if quote==p['text'])
                if not any(r['paragraph']==n for r in duplicates_to_remove):
                    duplicates_to_remove.append(dict(paragraph=n,quote=quote))
        for item in duplicates_to_remove:
            deterministic_findings.append(dict(finding_id=f"ASSEMBLY-{item['paragraph']}",rule='report_assembly',kind='exact_duplicate',
                origin='program',decision='confirmed',material=True,severity='hard',scope='full',
                location=dict(paragraph=item['paragraph'],sentence=1),problem='正文逐字重复装配',
                expected='只删除重复副本，保留摘要复述及独有事实和引用',evidence=dict(kind='report',quote=item['quote'])))
    if policy:
        deterministic_findings = [f if f.get("origin") == "program" else assess_issue(f, evidence) for f in deterministic_findings]
    deterministic_findings = attach_identities(draft, deterministic_findings, content_index, evidence)
    # 逐轮累计保留要求；Rewriter 只会执行本轮证据上下文里可见的部分，不可见的编号不会卡住返工。
    required = sorted(set(state.get("required_citations", [])) | {c["source_id"] for c in check["citations"] if c["valid"]})
    cited = reference_ids(checked_draft)
    common = {"evidence": evidence, "citations": check["citations"],
              "citation_errors": check["errors"] + binding_errors,
              "citation_issues": check.get("issues", []), "binding_issues": binding,
              "claim_issues": binding, "structure_issues": structure, "duplicate_issues": duplicates,
              "required_citations": required}
    if state.get('report_pipeline_version') == 3:
        common['draft_content']=draft
    if policy is not None:
        common["review_policy"] = policy
        common["content_index"] = content_index
    logs.extend([
        {"type": "Thought", "content": f"开始审核草稿（第 {round_no} 次审核）", "step_id": 0},
        {"type": "Action", "content": "llm_gen(query=按审核标准评审报告草稿)", "step_id": 0},
    ])
    try:
        # 硬规则失败时也调用审核 agent：agent 只补充问题与可执行建议，不能推翻确定性结论。
        review = await _review(
            draft, evidence,
            policy=policy,
            binding_issues=binding,
            structure_issues=structure,
            duplicate_issues=duplicates,
            required_ids=cited | set(required),
            deterministic_findings=deterministic_findings,
            check_errors=check["errors"],
            citation_retirements=[r for h in history for r in h.get('rewrite', {}).get('citation_lifecycle', {}).get('retired_claims', [])
                                 if r.get('review_required')],
        )
    except Exception as exc:
        from services.budget import BudgetExceeded
        exhausted=isinstance(exc,BudgetExceeded)
        if isinstance(exc,UsagePersistenceError) and (not exhausted or state.get('report_pipeline_version',1)<2):
            raise
        diagnostic = (exc.diagnostic if isinstance(exc, ReviewResponseError) else {
            'stage': 'review_call', 'code': 'BUDGET_EXCEEDED' if exhausted else 'REVIEW_CALL_FAILED',
            'exception_type': type(exc).__name__, 'model_error_code': getattr(exc, 'code', None),
            'message': '审核调用或准备失败，详见异常类型与模型错误码',
        })
        if 'EVIDENCE_CONTEXT_OVERFLOW' in str(exc):
            diagnostic.update(stage='evidence_context', code='EVIDENCE_CONTEXT_OVERFLOW', message=str(exc))
        finding_comments = [
            f"{finding['problem']}：{finding['expected']}"
            for finding in deterministic_findings if finding.get("severity") == "hard"
        ]
        comments = list(dict.fromkeys(
            check["errors"] + binding_errors + finding_comments
        ))[:5] or ["审核调用失败或返回结构无效，质量不可用"]
        prefix = '审核调用失败' if diagnostic['code'] == 'REVIEW_CALL_FAILED' else '审核返回无效'
        comments.insert(0, f"{prefix} [{diagnostic['code']}]：{diagnostic['message']}")
        fault_decision = decide_quality(usable=True, review_complete=False,
            confirmed_blockers=[f['finding_id'] for f in deterministic_findings if is_confirmed_blocker(f)] + check['errors']) if policy else dict(quality_status='unavailable')
        fault_quality = fault_decision['quality_status']
        history.append({"round": round_no, "passed": False if fault_quality == 'rejected' else None, "comments": comments,
                        "review_complete": False, "review_policy": policy,
                        "quality_status": fault_quality,
                        "review_diagnostics": diagnostic,
                        "findings": deterministic_findings, "possible_rule_issue": None,
                        "full_rewrite_required": False})
        history[-1]['review_response_attempts']=diagnostic.get('review_response_attempts',[])
        from services.document_writer import writer_provenance
        history[-1].update(writer_provenance(state))
        logs.append({"type": "Observation", "content": comments[0], "step_id": 0})
        fault_result = common | {"passed": False, "quality_status": fault_quality, "forced_pass": fault_quality == "rejected",
                "forced_output": exhausted or fault_quality == "rejected", "error_code": "BUDGET_EXCEEDED" if exhausted else "REVIEW_REJECTED" if fault_quality == "rejected" else "REVIEW_UNAVAILABLE",
                "review_comments": comments, "review_history": history,
                "findings": deterministic_findings, "possible_rule_issue": None,
                "full_rewrite_required": False, "react_logs": logs}
        if policy is not None:
            from services.repair_plan import plan_repair
            fault_result['repair_plan']=plan_repair(dict(state,**fault_result))
            history[-1]['repair_plan']=fault_result['repair_plan']
            history[-1]['stop_reason']='BUDGET_EXCEEDED' if exhausted else 'REVIEW_UNAVAILABLE'
        return fault_result
    dispute_review = None
    findings, rejected_findings = merge_findings(
        deterministic_findings, review.get("findings") or [], evidence, draft,
    )
    checks=review.get('content_checks') or {}
    for index,row in enumerate(checks.get('core_coverage',[])):
        if not row['covered']:
            paragraph=next((p['paragraph'] for p in paragraph_table(draft) if row['quote'] in p['text']),1)
            findings.append(dict(finding_id=f'CORE-{index}',rule='core_evidence_gap',origin='model',decision='confirmed',material=True,
                severity='hard',problem='核心问题未覆盖：'+row['question'],expected=row['reason'],
                location=dict(paragraph=paragraph,sentence=1),evidence=dict(kind='report',quote=row['quote'])))
    findings = attach_identities(draft, findings, content_index, evidence)
    if policy is not None:
        from services.review_adjudication import adjudicate
        from services.budget import BudgetExceeded
        try:
            findings, dispute_review = await adjudicate(
                findings, draft, evidence, policy, chat, history=history,
                task_id=state.get('task_id', ''), revision=(state.get('report_document') or {}).get('revision'))
        except BudgetExceeded:
            dispute_review=dict(status='unresolved',error='BudgetExceeded',initial_findings=findings,records=[])
            review['review_diagnostics']=dict(code='BUDGET_EXCEEDED',message='任务预算耗尽，未完成争议裁决')

    if rejected_findings:
        logs.append({"type": "Observation",
                     "content": f"审核 agent 的 {len(rejected_findings)} 条 finding 证据不在来源中，已丢弃",
                     "step_id": 0})
    hard_findings = [finding for finding in findings if (is_confirmed_blocker(finding) if policy else finding.get("severity") == "hard")]
    pending_material = bool(policy and (any(material_pending(f) for f in findings) or
                                       any(i["kind"] == "duplicate_passage" for i in structure)))
    passed = bool(review["passed"] and check["valid"] and not hard_findings and not rejected_findings)
    decision = None
    if policy is not None:
        decision = decide_quality(usable=True,
            review_complete=not bool(review.get('review_diagnostics') or rejected_findings or pending_material) and bool(review['passed'] or hard_findings or check['errors'] or
                (dispute_review and dispute_review['status'] == 'reviewed')),
            confirmed_blockers=[f['finding_id'] for f in hard_findings] + check['errors'], unresolved_material=pending_material)
        passed = decision['passed']
    comments = list(review.get("comments") or [])
    finding_comments = [f"{finding['problem']}：{finding['expected']}" for finding in hard_findings]
    deterministic_comments = list(dict.fromkeys(
        check["errors"] + binding_errors + finding_comments
    ))
    if not passed:
        if review["passed"]:
            comments.insert(0, "仍有实质事实疑点待核验，暂不能判为通过" if pending_material and not hard_findings and not check["errors"] else "确定性规则未通过，质量不能判为通过")
        for text in deterministic_comments:
            if text not in comments:
                comments.insert(0, text)
    possible_rule_issue = review.get("possible_rule_issue")
    full_rewrite_required = bool(review.get("full_rewrite_required") or
                                 any(f.get('scope') == 'full' and (not policy or is_confirmed_blocker(f)) for f in findings))
    if possible_rule_issue:
        logs.append({"type": "Observation",
                     "content": f"审核 agent 提出规则误判（仅记录，不改变判定）：{possible_rule_issue}",
                     "step_id": 0})
    if full_rewrite_required:
        logs.append({"type": "Observation",
                     "content": "审核 agent 要求整篇重写；Phase 4 决定是否采用",
                     "step_id": 0})
    # 引用结构错误也计入硬门禁；ID 为轮内编号，保存完整集合供追溯。
    hard_count = len(hard_findings) + len(check['errors'])
    from services.review_progress import review_progress
    review_complete = decision['review_complete'] if decision else not bool(review.get('review_diagnostics') or rejected_findings)
    integrity_set = [citation_issue(e, state.get('task_id', '')) for e in check['errors']]
    hard_set = hard_findings + integrity_set
    active_set = [f for f in findings if f.get('decision') != 'dismissed' and (f.get('material') or f.get('severity') == 'hard')] + integrity_set
    progress = review_progress(history[-1] if history else None, active_set if policy else hard_set, review_complete)
    convergence_stopped = not passed and progress['stopped']
    research_needed = needs_research(dict(state, evidence=evidence, quality_status='rejected', review_comments=comments))
    no_targets = not passed and not findings and not full_rewrite_required and not research_needed
    stop_reason = ('REVIEW_NO_ACTIONABLE_FINDINGS' if no_targets else
                   'NO_CONVERGENCE' if convergence_stopped else
                   'MAX_ITERATIONS' if not passed and iteration + 1 >= maximum else None)
    history.append({"contract_version": 2, "draft_sha256": hashlib.sha256(draft.encode()).hexdigest(),
                    "review_complete": review_complete,
                    "stop_reason": stop_reason,
                    "round": round_no, "passed": passed, "comments": comments,
                    "hard_error_count": hard_count,
                    "hard_finding_set": hard_set,
                    "issue_set": active_set if policy else hard_set,
                    "convergence": progress,
                    "convergence_stopped": convergence_stopped,
                    "findings": findings, "possible_rule_issue": possible_rule_issue,
                    "rejected_findings": [{
                        'finding': f,
                        'code': 'REPORT_QUOTE_OR_LOCATION_MISMATCH' if f.get('evidence', {}).get('kind') == 'report'
                                else 'SOURCE_QUOTE_MISMATCH',
                    } for f in rejected_findings],
                    "action_plan": {'scope': 'full' if full_rewrite_required else 'paragraph',
                                    'finding_ids': [f['finding_id'] for f in findings if f.get('decision') != 'dismissed'],
                                    'comments': comments},
                    "evidence_coverage": review.get('evidence_coverage'),
                    "content_checks": review.get("content_checks"),
                    "full_rewrite_required": full_rewrite_required})
    if review.get('policy_corrections'):
        history[-1]['policy_corrections'] = review['policy_corrections']
    if review.get('review_response_attempts'):
        history[-1]['review_response_attempts']=review['review_response_attempts']
    if state.get("report_pipeline_version", 1) >= 2:
        history[-1]["report_pipeline_version"] = state.get('report_pipeline_version',2)
        history[-1]["review_policy"] = policy
        history[-1]["content_index"] = content_index
        history[-1]["claim_bindings"] = state.get("claim_bindings", [])
        history[-1]["fact_registry"] = state.get("fact_registry", {})
        from services.document_writer import writer_provenance
        history[-1].update(writer_provenance(state))
        if dispute_review:
            history[-1]["dispute_review"] = dispute_review
    if review.get('citation_retirements'):
        history[-1]['citation_retirements'] = review['citation_retirements']
    if review.get('review_diagnostics'):
        history[-1]['review_diagnostics'] = review['review_diagnostics']
        logs.append({'type': 'Observation', 'step_id': 0,
                     'content': '审核 finding 格式不完整 [REVIEW_FINDINGS_PARTIAL]，已隔离坏条目并保留拒绝结果'})
    if not passed:
        iteration += 1
    forced = not passed and (iteration >= maximum or convergence_stopped or no_targets)
    quality = decision["quality_status"] if decision else "passed" if passed else "rejected"
    if decision:
        history[-1]["quality_status"] = quality
    text = f"第 {round_no} 次审核通过" if passed else f"第 {round_no} 次审核不通过：{'｜'.join(comments)}"
    if convergence_stopped:
        text += "；完整复审确认旧硬问题均未解决，提前停止并保留拒绝结果"
    elif forced:
        text += "；已达循环上限，保留拒绝结果并强制输出警告"
    logs.append({"type": "Observation", "content": text, "step_id": 0})
    if passed:
        error_code = None
    elif not check["valid"]:
        error_code = "CITATION_INVALID"
    elif any(finding.get("rule") == "unbound_number" for finding in hard_findings):
        error_code = "CITATION_UNBOUND"
    elif any(finding.get("rule") in {"merged_fact_range", "unverified_comparison", 'period_mismatch', 'percentage_point_mismatch',
                                   'comparison_basis_mismatch', 'geography_mismatch'}
             for finding in hard_findings):
        error_code = "CLAIM_UNSUPPORTED"
    else:
        error_code = "REVIEW_REJECTED"
    if quality == "unavailable":
        error_code = "REVIEW_UNAVAILABLE"
    result = common | {"passed": passed, "forced_pass": forced, "forced_output": forced,
              "quality_status": quality, "error_code": error_code,
              "review_comments": comments, "review_history": history,
              "findings": findings, "possible_rule_issue": possible_rule_issue,
              "full_rewrite_required": full_rewrite_required,
              "iteration": iteration, "react_logs": logs}
    if forced:
        result["draft_content"] = draft + "\n\n> ⚠️ 部分章节未通过校验（已强制输出）"
    if policy is not None:
        from services.repair_plan import plan_repair, stop_repair
        result['repair_plan']=plan_repair(dict(state,**result))
        history[-1]['repair_plan']=result['repair_plan']
        if not passed and result['repair_plan']['action']=='stop':
            reason=('BUDGET_EXCEEDED' if (review.get('review_diagnostics') or {}).get('code')=='BUDGET_EXCEEDED'
                    else result['repair_plan']['reason'])
            result.update(stop_repair(dict(state,**result),reason))
    return result
