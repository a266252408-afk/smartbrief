"""将模型自报与确定性复查分开；模型语义意见始终待独立复审。"""
from services.citations import check_citations
from services.findings import build_findings, reference_findings
from services.rewrite_patches import paragraph_table
from services.issue_identity import same_repair_target, attach_identities, conflict_fields


def verify_findings(body, evidence, findings, model_results, required=(), task_id='', *, content_index=None, strict_identity=False):
    check = check_citations(task_id, body, evidence, required)
    current = build_findings(body, evidence, check['binding_issues']) + reference_findings(body, check)
    for issue in check['issues']:
        current.append({'rule':'missing_citation','location':{'paragraph':issue.get('paragraph',1),'sentence':1}})
    current = attach_identities(body, current, content_index, evidence)
    reported = {r.get('finding_id'):r for r in model_results if isinstance(r, dict)}
    rows, targets = [], []
    rules = {'unbound_number','merged_fact_range','unsupported_wording','unverified_comparison',
             'period_mismatch','percentage_point_mismatch','comparison_basis_mismatch','geography_mismatch',
             'missing_citation','invalid_reference'}
    for finding in findings:
        rule = finding.get('rule') or finding.get('kind')
        # 旧finding未标rule时不猜测；不可将模型自报直接当作已解决。
        matches = [f for f in current if same_repair_target(finding, f)]
        known_targets = {b['target_id']:b for b in content_index['blocks']} if content_index else {}
        target = known_targets.get(finding.get('target_id'))
        available = {s['source_id'] for s in evidence if s.get('source_id') and not s.get('source_deleted')}
        missing_sources = set(conflict_fields(finding)['sources']) - available
        uncertain = strict_identity and (not target or target['certainty'] == 'unverified' or bool(missing_sources)
            or rule in {'unverified_comparison','unsupported_wording','avoidable_gap'}
            or finding.get('decision') in {'suspected','unresolved'})
        status = 'unverified' if rule not in rules or uncertain else 'unresolved' if matches else 'resolved'
        report = reported.get(finding['finding_id'], {})
        row = {'finding_id':finding['finding_id'], 'reported_status':report.get('status'),
               'status':status, 'reason':('确定性问题仍存在' if status == 'unresolved' else
                 '确定性问题已消失，整体质量仍待复审' if status == 'resolved' else '语义意见待独立审核，未采信模型自报')}
        rows.append(row)
        if status == 'unresolved' and report.get('status') == 'fixed':
            # 使用候选稿上的最新坐标和证据，避免全文重组后的旧段落编号失效。
            for match in matches:
                target = dict(finding, **{k:v for k,v in match.items() if k != 'finding_id'})
                target['finding_id'] = finding['finding_id']
                target['scope'] = 'paragraph'
                if target['location']['paragraph'] <= len(paragraph_table(body)):
                    targets.append(target)
    # 一个ID仅定位一处，剩余相同问题继续由独立审核确认。
    return rows, list({f['finding_id']:f for f in targets}.values())
