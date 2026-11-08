"""版本1输入适配：有界字段别名、严格契约与结构化诊断。"""
import json
from typing import Any, Literal
from pydantic import Field, ValidationError
from services.review_contracts import Contract, CitationChange, FullResult, Patch, parse_json


class ProtocolDiagnostic(Contract):
    path: str
    type: str
    message: str


class NormalizationTrace(Contract):
    version: Literal[1] = 1
    path: str
    action: Literal['text_to_replacement', 'deduplicate_text']
    target: str


class PatchEnvelope(Contract):
    mode: Literal['patch']
    patches: list[Any]
    # 辅助回执独立诊断，不能抹去合法正文。
    results: Any = None
    citation_changes: list[CitationChange] = Field(default_factory=list)


class RewriteProtocolError(ValueError):
    def __init__(self, code, message, errors):
        self.code = code
        self.errors = errors
        super().__init__(message)


class VersionMismatch(ValueError):
    code = 'VERSION_MISMATCH'

    def __init__(self, errors=None):
        self.errors = errors or [dict(path='/digest', type='version_mismatch', message='段落 digest 不匹配')]
        super().__init__('段落 digest 不匹配')


def pointer(parts):
    return ''.join('/'+str(p).replace('~','~0').replace('/','~1') for p in parts) or '/'


def validate(model, value, prefix=''):
    try:
        return model.model_validate(value).model_dump()
    except ValidationError as exc:
        errors=[dict(path=prefix+pointer(e['loc']) if e['loc'] else prefix or '/',type=e['type'],message=e['msg'])
                for e in exc.errors(include_input=False,include_url=False)]
        missing_body=any(e['type']=='missing' and e['path'].endswith(('/replacement','/content')) for e in errors)
        raise RewriteProtocolError('MISSING_CONTENT' if missing_body else 'CONTRACT_REJECTED',
                                   '返工规范契约无效',errors) from exc


def parse_rewrite(raw):
    try:
        return parse_json(raw)
    except json.JSONDecodeError as exc:
        raise RewriteProtocolError('CONTRACT_REJECTED','返工JSON无效',
                                   [dict(path='/',type='json_invalid',message=exc.msg)]) from exc


def full_payload(payload):
    if not isinstance(payload,dict):
        return validate(FullResult,payload)
    return validate(FullResult,dict(payload,results=[]))


def normalize_patch(item, index, trace):
    prefix=f'/patches/{index}'
    if not isinstance(item,dict):
        return validate(Patch,item,prefix)
    value=dict(item)
    if 'text' in value:
        text=value['text']
        if not isinstance(text,str):
            raise RewriteProtocolError('CONTRACT_REJECTED','text必须是字符串',
                [dict(path=prefix+'/text',type='string_type',message='text必须是字符串')])
        if 'replacement' in value and value['replacement'] != text:
            raise RewriteProtocolError('FIELD_CONFLICT','text与replacement冲突',
                [dict(path=prefix+'/text',type='alias_conflict',message='text与replacement必须完全相同')])
        action='deduplicate_text' if 'replacement' in value else 'text_to_replacement'
        value['replacement']=value.pop('text')
        trace.append(NormalizationTrace(path=prefix+'/text',action=action,target=prefix+'/replacement').model_dump())
    return validate(Patch,value,prefix)
