"""候选稿验收：问题内容与出现次数独立于段落坐标。"""
import json
import re


def hard_problem_counts(check, *, confirmed_only=False):
    from services.issue_identity import problem_counts
    from services.review_rules import is_confirmed_blocker
    return problem_counts(i for i in check.get('binding_issues', [])
        if (is_confirmed_blocker(i) if confirmed_only else i.get('severity') == 'hard'))


def has_rewrite_protocol(body):
    """识别独立的返工对象，允许普通 JSON 示例及正常代码段。"""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^\s*\{", body):
        try:
            value, _ = decoder.raw_decode(body[match.end() - 1:])
        except ValueError:
            continue
        if (isinstance(value, dict) and value.get("mode") in {"full", "patch"}
                and ("content" in value or "patches" in value)
                and ("results" in value or "reason" in value)):
            return True
    return False


class CandidateRejected(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _problem_rows(body, check, *, pipeline_version=1, include_pending=False):
    from services.issue_identity import attach_identities, citation_issue
    from services.review_rules import is_confirmed_blocker, material_pending, assess_issue
    from services.findings import reference_findings
    from services.report_quality import structure_gaps
    rows = [dict(i, rule=i.get('rule') or i.get('kind')) for i in check.get('binding_issues', [])
            if (i.get('severity') == 'hard' if pipeline_version < 2 else
                is_confirmed_blocker(i) or (include_pending and material_pending(i)))]
    references = reference_findings(body, check)
    rows.extend(references)
    for issue in check.get('issues', []):
        rows.append(dict(kind='missing_citation', rule='missing_citation', severity='hard',
            origin='program',decision='confirmed',material=True,
            sentence=issue.get('excerpt',''),numbers=issue.get('tokens',[]),
            location=dict(paragraph=issue.get('paragraph'),sentence=1)))
    invalid_ids = {c.get('source_id') for c in check.get('citations', []) if not c.get('valid')}
    for error in check.get('errors', []):
        item = citation_issue(error, 'candidate')
        fields=item['conflict_fields']
        if fields['error_category']=='missing_citation' and check.get('issues'):
            continue
        if fields['sources'] and set(fields['sources']).issubset(invalid_ids):
            continue
        rows.append(item)
    if pipeline_version>=2:
        for issue in structure_gaps(body):
            item=assess_issue(dict(issue,rule='required_structure',kind='required_structure',structure_kind=issue['kind'],
                severity='hard',has_section_headings=bool(re.search(r'(?m)^#{2,6}\s+',body))),[])
            if is_confirmed_blocker(item) or (include_pending and material_pending(item)):
                rows.append(item)
    return attach_identities(body,rows)


def validate_candidate(before, after, before_check, after_check, *, full=False, pipeline_version=1, require_progress=False):
    """新增确认错误阻断；疑点待核验；确定性目标的纯润色不算修复。"""
    from services.issue_identity import problem_counts, identity_key
    if has_rewrite_protocol(after) and (full or not has_rewrite_protocol(before)):
        raise CandidateRejected('PROTOCOL_IN_BODY','正文含返工协议')
    old_rows=_problem_rows(before,before_check,pipeline_version=pipeline_version)
    new_rows=_problem_rows(after,after_check,pipeline_version=pipeline_version)
    introduced=problem_counts(new_rows)-problem_counts(old_rows)
    if introduced:
        kinds={f.get('rule') for f in new_rows if identity_key(f) in introduced}
        if kinds & {'invalid_reference','missing_citation','citation'}:
            raise CandidateRejected('CITATION_REJECTED','候选新增引用错误')
        if 'required_structure' in kinds:
            raise CandidateRejected('STRUCTURE_REJECTED','候选新增结构完整性错误')
        raise CandidateRejected('QUALITY_REJECTED','候选新增硬错误')
    old_active=problem_counts(_problem_rows(before,before_check,pipeline_version=pipeline_version,include_pending=True))
    new_active=problem_counts(_problem_rows(after,after_check,pipeline_version=pipeline_version,include_pending=True))
    resolved=old_active-new_active
    if require_progress and old_active and not resolved:
        raise CandidateRejected('NO_PROGRESS','候选未解决目标，纯润色或规则降级不算实际进展')
    return dict(resolved_count=sum(resolved.values()),introduced_count=0,
                current_problem_count=sum(new_active.values()),progress_verified=bool(resolved))
