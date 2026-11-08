"""按实际端点与模型选择输出能力，不探测、不隐式增加调用。"""
import json
import os
from urllib.parse import urlsplit
from services.review_contracts import response_schema

def prepare_output(messages, contract, model, base_url):
    if contract is None:
        return messages, {}, {'mode':'text'}
    host = urlsplit(base_url or '').hostname or ''
    overrides = json.loads(os.getenv('STRUCTURED_OUTPUT_CAPABILITIES', '{}'))
    mode = overrides.get(host + '/' + model)
    if mode is None:
        mode = 'json_schema' if (host.endswith('.maas.aliyuncs.com') and model == 'qwen3.7-flash') else 'prompt'
    if mode not in {'json_schema','json_object','prompt'}:
        raise ValueError('结构化输出能力配置无效')
    schema = response_schema(contract)
    messages = [*messages, {'role':'user','content':'严格按以下 JSON Schema 返回 JSON：' + json.dumps(schema,ensure_ascii=False)}]
    kwargs = {}
    if mode == 'json_schema':
        kwargs['response_format'] = {'type':mode,'json_schema':{'name':contract.__name__,'strict':True,'schema':schema}}
    elif mode == 'json_object':
        kwargs['response_format'] = {'type':mode}
    return messages, kwargs, {'mode':mode,'model':model,'reason':None if mode == 'json_schema' else '按实际模型能力兼容输出，仍严格本地校验'}
