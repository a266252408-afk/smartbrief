"""将模型自报与确定性复查分开；模型语义意见始终待独立复审。"""
from services.citations import check_citations
from services.findings import build_findings, reference_findings
from services.rewrite_patches import paragraph_table
from services.numbers import token_matches_text


def verify_findings(body, evidence, findings, model_results, required=(), task_id=''):
    check = check_citations(task_id, body, evidence, required)
    current = build_findings(body, evidence, check['binding_issues']) + reference_findings(body, check)
    for issue in check['issues']:
        current.append({'rule':'missing_citation','location':{'paragraph':issue.get('paragraph',1),'sentence':1}})
    reported = {r.get('finding_id'):r for r in model_results if isinstance(r, dict)}
    rows, targets = [], []
    rules = {'unbound_number','merged_fact_range','unsupported_wording','unverified_comparison',
             'period_mismatch','percentage_point_mismatch','comparison_basis_mismatch','geography_mismatch',
             'missing_citation','invalid_reference'}
    for finding in findings:
        rule = finding.get('rule') or finding.get('kind')
        # 旧finding未标rule时不猜测；不可将模型自报直接当作已解决。
        numbers = set(finding.get('numbers') or [])
        matches = [f for f in current if f.get('rule') == rule and
                   (not numbers or any(token_matches_text(n, other) for n in numbers for other in f.get('numbers', []))
                    or f.get('location', {}).get('paragraph') == finding.get('location', {}).get('paragraph'))]
        status = 'unverified' if rule not in rules else 'unresolved' if matches else 'resolved'
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
