"""段落补丁契约与原文保护。"""
import pytest

from services.rewrite_patches import apply_patch, paragraph_table


def test_patch_preserves_separators_and_untouched_text():
    body = '# 标题\r\n\r\n旧段落\n \n末段  '
    table = paragraph_table(body)
    payload = {'mode': 'patch', 'patches': [dict(table[1], replacement='新段落', finding_ids=['F1'])],
               'results': [{'finding_id': 'F1', 'status': 'fixed', 'reason': '修正'}]}
    assert apply_patch(body, payload, [{'finding_id': 'F1', 'location': {'paragraph': 2}}]) == '# 标题\r\n\r\n新段落\n \n末段  '


@pytest.mark.parametrize('fault', ['digest', 'paragraph', 'results', 'duplicate', 'target', 'status'])
def test_invalid_patch_is_rejected(fault):
    body = '原文'
    p = dict(paragraph_table(body)[0], replacement='新文', finding_ids=['F1'])
    payload = {'mode': 'patch', 'patches': [p], 'results': [{'finding_id': 'F1', 'status': 'fixed'}]}
    if fault == 'digest':
        p['digest'] = 'sha256:wrong'
    elif fault == 'paragraph':
        p['paragraph'] = 2
    elif fault == 'results':
        payload['results'] = []
    elif fault == 'duplicate':
        payload['patches'].append(p)
    elif fault == 'target':
        p['finding_ids'] = ['F2']
    else:
        payload['results'][0]['status'] = 'unknown'
    with pytest.raises(ValueError):
        apply_patch(body, payload, [{'finding_id': 'F1', 'location': {'paragraph': 1}}])


@pytest.mark.parametrize('fault', ['id', 'reason', 'mode', 'replacement', 'uncovered'])
def test_contract_rejects_remaining_invalid_shapes(fault):
    p = dict(paragraph_table('原文')[0], replacement='新文', finding_ids=['F1'])
    payload = {'mode': 'patch', 'patches': [p], 'results': [{'finding_id': 'F1', 'status': 'fixed'}]}
    if fault == 'id':
        payload['results'][0]['finding_id'] = 'other'
    elif fault == 'reason':
        payload['results'][0]['status'] = 'cannot_fix'
    elif fault == 'mode':
        payload['mode'] = 'full'
    elif fault == 'replacement':
        p['replacement'] = '第一段\n\n第二段'
    else:
        payload['patches'] = []
    with pytest.raises(ValueError):
        apply_patch('原文', payload, [{'finding_id': 'F1', 'location': {'paragraph': 1}}])
