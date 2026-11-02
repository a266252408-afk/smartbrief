"""真实评测配置只读视图，不返回密钥或发起请求。"""
import os
from urllib.parse import urlsplit, urlunsplit
from evaluation import catalog


def model_config(prefix='OPENAI'):
    name=os.getenv(prefix+'_MODEL','')
    raw=os.getenv(prefix+'_BASE_URL','')
    try:
        url=urlsplit(raw)
        valid=bool(url.scheme=='https' and url.hostname and not (url.username or url.password or url.query or url.fragment))
        public=urlunsplit((url.scheme,url.netloc,url.path,'','')) if valid else '端点配置无效'
    except ValueError:
        valid,public=False,'端点配置无效'
    return dict(name=name,base_url=public,ready=bool(name and valid and os.getenv(prefix+'_API_KEY','').strip()))


def read_config():
    return dict(dataset_hash=catalog.summary()['dataset_hash'],model=model_config(),
        search=dict(ready=bool(os.getenv('EXA_API_KEY','').strip()),base_url='https://api.exa.ai/search'),
        scope='仅公开题干与给定资料；完整任务使用冻结资料，不代表自主联网研究')
