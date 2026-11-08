"""返工补丁契约：段落编号从 1 开始，保留原始分隔符。"""
import hashlib
import re


def rejection_code(message):
    """稳定诊断码用于区分范围限制、格式错误和质量校验。"""
    for token, code in [('被点名段落', 'SCOPE_RESTRICTED'), ('digest', 'VERSION_MISMATCH'),
                        ('完整段落', 'PARAGRAPH_FORMAT'), ('新增引用', 'CITATION_REJECTED'),
                        ('新增硬错误', 'QUALITY_REJECTED'), ('没有实际修改', 'NO_CHANGE')]:
        if token in message:
            return code
    return 'CONTRACT_REJECTED'


def paragraph_table(body):
    return [{'paragraph': i // 2 + 1, 'digest': 'sha256:' + hashlib.sha256(p.encode()).hexdigest(),
             'text': p} for i, p in enumerate(re.split(r'(\n\s*\n)', body)) if i % 2 == 0]


def validate_results(payload, findings):
    results = payload.get('results')
    expected = {f['finding_id'] for f in findings}
    if not isinstance(results, list) or len(results) != len(expected):
        raise ValueError('回执必须与 finding 一一对应')
    if {r.get('finding_id') for r in results} != expected:
        raise ValueError('回执编号不匹配')
    for r in results:
        if r.get('status') not in {'fixed', 'skipped', 'cannot_fix'}:
            raise ValueError('回执状态无效')
        if r['status'] != 'fixed' and not str(r.get('reason') or '').strip():
            raise ValueError('未修复回执必须说明原因')
    return results


def apply_patch(body, payload, findings):
    validate_results(payload, findings)
    if payload.get('mode') != 'patch' or not isinstance(payload.get('patches'), list):
        raise ValueError('补丁模式无效')
    table = {p['paragraph']: p for p in paragraph_table(body)}
    known = {f['finding_id']: f for f in findings}
    parts = re.split(r'(\n\s*\n)', body)
    seen = set()
    covered = set()
    for p in payload['patches']:
        n = p.get('paragraph')
        ids = p.get('finding_ids')
        if type(n) is not int or n not in table or n in seen:
            raise ValueError('段落不存在或重复')
        if p.get('digest') != table[n]['digest']:
            raise ValueError('段落 digest 不匹配')
        if not ids or any(i not in known or known[i].get('location', {}).get('paragraph') != n for i in ids):
            raise ValueError('补丁必须指向被点名段落')
        replacement = p.get('replacement')
        if not isinstance(replacement, str) or re.search(r'\n\s*\n', replacement):
            raise ValueError('replacement 必须是一个完整段落')
        seen.add(n)
        covered.update(ids)
        parts[(n - 1) * 2] = replacement
    if any(r['status'] == 'fixed' and r['finding_id'] not in covered for r in payload['results']):
        raise ValueError('fixed 回执没有对应补丁')
    return ''.join(parts)


def receipt_diagnostics(payload, findings):
    """回执问题单独记录，不据此丢弃已经验证的正文。"""
    try:
        validate_results(payload, findings)
        return []
    except (ValueError, TypeError, AttributeError) as exc:
        return [str(exc)]


def apply_partial_patches(body, payload, findings, validate_candidate=None):
    """按原稿坐标逐条验证；坏补丁不影响已接受补丁。"""
    from services.review_contracts import Patch
    if not isinstance(payload, dict) or payload.get('mode') != 'patch' or not isinstance(payload.get('patches'), list):
        raise ValueError('补丁模式无效')
    candidate = body
    accepted, rejected, covered, seen = [], [], set(), set()
    for index, item in enumerate(payload['patches']):
        try:
            # 输入段落表的 text 是上下文，不属于输出补丁契约。
            data = Patch.model_validate(item).model_dump()
            n = data['paragraph']
            if n in seen:
                raise ValueError('同段落补丁重复')
            one = {'mode':'patch','patches':[data], 'results':[
                {'finding_id':f['finding_id'],'status':'fixed' if f['finding_id'] in data['finding_ids'] else 'skipped',
                 'reason':'由程序按补丁覆盖范围生成'} for f in findings]}
            proposed = apply_patch(candidate, one, findings)
            if proposed == candidate:
                raise ValueError('补丁没有实际修改')
            if validate_candidate is not None:
                validate_candidate(candidate, proposed, data)
            candidate = proposed
            seen.add(n)
            accepted.append(data)
            covered.update(data['finding_ids'])
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            rejected.append({'index':index, 'message':str(exc), 'code':rejection_code(str(exc)),
                             'proposal':item})
    from services.review_contracts import Receipt
    reported = {}
    for item in payload.get('results', []) if isinstance(payload.get('results'), list) else []:
        try:
            checked = Receipt.model_validate(item).model_dump()
            if checked['status'] != 'fixed':
                reported[checked['finding_id']] = checked
        except (ValueError, TypeError):
            continue
    receipt_errors = receipt_diagnostics(payload, findings)
    for item in payload.get('results', []) if isinstance(payload.get('results'), list) else []:
        if isinstance(item, dict) and item.get('status') == 'fixed' and item.get('finding_id') not in covered:
            receipt_errors.append('fixed 回执没有已接受的对应补丁：' + str(item.get('finding_id')))
    return {'body':candidate,'accepted':accepted,'rejected':rejected,
            'receipt_errors':receipt_errors,
            'results':[reported[f['finding_id']] if f['finding_id'] not in covered and f['finding_id'] in reported else {'finding_id':f['finding_id'], 'status':'fixed' if f['finding_id'] in covered else 'cannot_fix',
                        'reason':'有效补丁已应用，仍须复审' if f['finding_id'] in covered else '没有可接受的对应补丁'} for f in findings]}
