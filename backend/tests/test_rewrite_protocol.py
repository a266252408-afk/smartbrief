"""R02-03 归一化和真实正文校验，模型只替换外部边界。"""
import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest
from agents import rewriter
from services.rewrite_patches import apply_partial_patches, paragraph_table, rejection_code
from services.fact_binding import build_registry
from tests.test_evidence import source
from tests.test_rewriter import FakeResp


def partial(edit):
    body='旧正文'
    proposal=dict(paragraph=1,digest=paragraph_table(body)[0]['digest'],finding_ids=['F1'],**edit)
    raw=dict(mode='patch',patches=[proposal],results=[])
    saved=copy.deepcopy(raw)
    out=apply_partial_patches(body,raw,[dict(finding_id='F1',location={'paragraph':1})],pipeline_version=2)
    assert raw==saved
    return out


@pytest.mark.parametrize('edit',[{'text':'新正文'},{'text':'新正文','replacement':'新正文'}])
def test_alias_is_consumed_and_audited(edit):
    out=partial(edit)
    assert out['body']=='新正文' and 'text' not in out['accepted'][0]
    assert out['normalization'][0]['version']==1
    assert out['normalization'][0]['path']=='/patches/0/text'


@pytest.mark.parametrize(('edit','code','path'),[
    ({'text':'甲','replacement':'乙'},'FIELD_CONFLICT','/patches/0/text'),
    ({},'MISSING_CONTENT','/patches/0/replacement'),
    ({'text':None},'CONTRACT_REJECTED','/patches/0/text'),
    ({'replacement':'新正文','unknown':True},'CONTRACT_REJECTED','/patches/0/unknown'),
])
def test_bad_fields_keep_body_and_precise_path(edit,code,path):
    out=partial(edit)
    assert out['body']=='旧正文' and not out['accepted']
    rejected=out['rejected'][0]
    assert rejected['code']==code and rejected['errors'][0]['path']==path
    assert rejected['proposal'].get('text')==edit.get('text')


def test_explicit_delete_is_distinct_from_missing_body():
    assert partial({'replacement':''})['body']==''
    assert partial({})['body']=='旧正文'


def test_digest_word_is_not_version_evidence():
    assert rejection_code(ValueError('unexpected digest field'))=='CONTRACT_REJECTED'


def test_real_digest_mismatch_has_typed_code_and_path():
    out=apply_partial_patches('正文',dict(mode='patch',patches=[dict(paragraph=1,digest='stale',replacement='新正文',finding_ids=['F1'])]),
                              [dict(finding_id='F1',location={'paragraph':1})],pipeline_version=2)
    assert out['body']=='正文'
    assert out['rejected'][0]['code']=='VERSION_MISMATCH'
    assert out['rejected'][0]['errors'][0]['path']=='/patches/0/digest'


def test_stale_digest_is_reported_as_version_at_attempt_level():
    task,payload=bound_task()
    payload['patches'][0]['digest']='stale'
    out,_=execute(task,[payload,payload,payload])
    first=out['review_history'][-1]['rewrite']['attempts'][0]
    assert first['code']=='VERSION_MISMATCH'
    assert first['errors'][0]['path']=='/patches/0/digest'


def bound_task():
    evidence=[source(excerpt='市场规模10亿元。')]
    body='# 报告\n\n市场规模99亿元[S1]。'
    task=dict(task_id='t',topic='市场',report_pipeline_version=2,draft_content=body,evidence=evidence,
              findings=[dict(finding_id='F1',rule='unbound_number',severity='hard',location={'paragraph':2,'sentence':1},numbers=['99亿元'])])
    key=next(iter(build_registry('t',evidence)))
    payload=dict(mode='patch',patches=[dict(paragraph=2,digest=paragraph_table(body)[1]['digest'],text=f'市场规模10亿元[{key}]。',finding_ids=['F1'])],results=[])
    return task,payload


def execute(task,payloads):
    model=AsyncMock(side_effect=[FakeResp(json.dumps(p,ensure_ascii=False)) for p in payloads])
    with patch.object(rewriter,'chat',new=model):
        out=asyncio.run(rewriter.rewriter_node(task))
    return out,model


def test_alias_reaches_real_binding_and_candidate_validation():
    task,payload=bound_task()
    out,model=execute(task,[payload])
    assert model.await_count==1 and '10亿元[S1]' in out['draft_content']
    assert out['review_history'][-1]['rewrite']['attempts'][0]['normalization']
    assert out['quality_status']=='rejected'


def test_normalized_wrong_fact_still_rejected():
    task,payload=bound_task()
    payload['patches'][0]['text']=payload['patches'][0]['text'].replace('10亿元','999亿元')
    out,_=execute(task,[payload,payload,payload])
    assert out['draft_content']==task['draft_content']
    assert any(r['code']=='FACT_BINDING_REJECTED' for a in out['review_history'][-1]['rewrite']['attempts'] for r in a.get('rejected',[]))


def test_two_invalid_protocol_responses_stop_without_full_upgrade():
    task,_=bound_task()
    out,model=execute(task,[{}, {}, {}])
    assert model.await_count==2 and out['draft_content']==task['draft_content']
    attempts=out['review_history'][-1]['rewrite']['attempts']
    assert [a['mode'] for a in attempts]==['patch','patch']
    assert all(a['code']=='CONTRACT_REJECTED' and a['errors'] for a in attempts)


def test_protocol_feedback_corrects_once_in_same_scope():
    task,good=bound_task()
    out,model=execute(task,[{},good])
    assert model.await_count==2 and '10亿元[S1]' in out['draft_content']
    request=json.loads(model.call_args.args[0][-1]['content'])
    assert request['protocol_feedback'] and request['mode']=='patch'


def test_unknown_top_field_is_not_discarded():
    task,payload=bound_task()
    payload['digest_typo']='not-a-version'
    out,model=execute(task,[payload,payload])
    assert model.await_count==2 and out['draft_content']==task['draft_content']
    row=out['review_history'][-1]['rewrite']['attempts'][0]
    assert row['code']=='CONTRACT_REJECTED' and row['errors'][0]['path']=='/digest_typo'


def test_ddb6_aliases_enter_content_validation():
    data=json.loads((Path(__file__).parent/'fixtures/review_reliability/task_ddb648.json').read_text(encoding='utf-8'))
    from services.review_contracts import parse_json
    from services.fact_binding import render_bound_text
    payload=parse_json(data['review_history'][0]['rewrite']['attempts'][0]['raw_response'])
    registry=build_registry(data['task_id'],data['evidence'])
    entered=[]
    def normalize(edit):
        entered.append(edit['replacement'])
        text,_=render_bound_text(edit['replacement'],data['task_id'],registry,previous=data['draft_content'])
        return dict(edit,replacement=text)
    out=apply_partial_patches(data['draft_content'],payload,data['review_history'][0]['findings'],normalize_proposal=normalize,pipeline_version=2)
    assert len(entered)==len(payload['patches'])==3
    assert len(out['normalization'])==3
    assert not any(r['code']=='VERSION_MISMATCH' for r in out['rejected'])


@pytest.mark.parametrize('value',[None,23,[],True])
def test_non_object_proposal_is_rejected_without_body_loss(value):
    out=apply_partial_patches('正文',dict(mode='patch',patches=[value]),[],pipeline_version=2)
    assert out['body']=='正文' and out['rejected'][0]['stage']=='protocol'
    assert out['rejected'][0]['errors'][0]['path']=='/patches/0'


def test_v1_does_not_consume_text_alias():
    payload=dict(mode='patch',patches=[dict(paragraph=1,digest=paragraph_table('正文')[0]['digest'],text='新正文',finding_ids=['F1'])])
    out=apply_partial_patches('正文',payload,[dict(finding_id='F1',location={'paragraph':1})])
    assert out['body']=='正文' and out['rejected']
    assert 'normalization' not in out


def test_missing_digest_is_contract_error_not_stale_version():
    out=apply_partial_patches('正文',dict(mode='patch',patches=[dict(paragraph=1,text='新正文',finding_ids=['F1'])]),[],pipeline_version=2)
    assert out['rejected'][0]['code']=='CONTRACT_REJECTED'
    assert out['rejected'][0]['errors'][0]['path']=='/patches/0/digest'
    assert out['normalization']


def test_invalid_json_only_gets_one_protocol_correction():
    task,_=bound_task()
    model=AsyncMock(return_value=FakeResp('{invalid digest json'))
    with patch.object(rewriter,'chat',new=model):
        out=asyncio.run(rewriter.rewriter_node(task))
    assert model.await_count==2 and out['draft_content']==task['draft_content']
    attempts=out['review_history'][-1]['rewrite']['attempts']
    assert all(a['errors'][0]['type']=='json_invalid' and a['code']=='CONTRACT_REJECTED' for a in attempts)


@pytest.mark.parametrize('bad',[None,{},dict(mode='full',reason='修复',results=[])])
def test_full_missing_content_keeps_draft_and_retries_full_once(bad):
    task,_=bound_task()
    task['full_rewrite_required']=True
    out,model=execute(task,[bad,bad])
    assert model.await_count==2 and out['draft_content']==task['draft_content']
    attempts=out['review_history'][-1]['rewrite']['attempts']
    assert [a['mode'] for a in attempts]==['full','full']
    assert all(a['code'] in {'MISSING_CONTENT','CONTRACT_REJECTED'} for a in attempts)


def test_full_protocol_correction_reaches_content_validation():
    task,_=bound_task()
    task['full_rewrite_required']=True
    key=next(iter(build_registry('t',task['evidence'])))
    good=dict(mode='full',content=f'# 报告\n\n市场规模10亿元[{key}]。',reason='修正数值',results=[])
    out,model=execute(task,[{},good])
    assert model.await_count==2 and '10亿元[S1]' in out['draft_content']
    assert [a['mode'] for a in out['review_history'][-1]['rewrite']['attempts']]==['full','full']


def test_normalization_cannot_hide_an_unknown_escaped_field():
    out=partial({'text':'新正文','a~/b':True})
    assert out['body']=='旧正文'
    assert out['normalization'][0]['action']=='text_to_replacement'
    assert out['rejected'][0]['errors'][0]['path']=='/patches/0/a~0~1b'


def test_protocol_trace_survives_real_repository_roundtrip():
    from db.database import init_db
    from db import task_repository as repo
    task,good=bound_task()
    out,_=execute(task,[{},good])
    init_db()
    repo.create_task('protocol-roundtrip','市场')
    repo.update_task('protocol-roundtrip',review_history=out['review_history'])
    saved=repo.get_task('protocol-roundtrip')
    attempts=saved['review_history'][-1]['rewrite']['attempts']
    assert attempts[0]['errors'] and attempts[1]['normalization']
    assert attempts[0]['raw_response']=='{}'
    assert 'text' in json.loads(attempts[1]['raw_response'])['patches'][0]
