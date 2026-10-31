"""独立验证数据引用、来源/版本、公开任务隔离与完整性；不执行被测系统。"""
import ast
import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]


def read(name):
    return json.loads((ROOT / name).read_text('utf-8'))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate():
    manifest = read('manifest.json')
    actual = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if p.is_file()
              and p.name not in {'manifest.json','validation-result.txt'} and '__pycache__' not in p.parts}
    require(actual == set(manifest['files']), 'manifest文件集合不一致')
    for name, expected in manifest['files'].items():
        require(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==expected, f'文件指纹变化：{name}')
    sources = {p.stem:json.loads(p.read_text('utf-8')) for p in (ROOT/'sources').glob('*.json')}
    facts = {}
    for sid,s in sources.items():
        require(s['id']==sid, '来源ID不匹配')
        require(urlparse(s['url']).scheme=='https', f'非HTTPS来源：{sid}')
        if s['published']:
            require(date.fromisoformat(s['published'])<=date.fromisoformat(manifest['cutoff']),f'未来来源：{sid}')
        without_hash = {k:v for k,v in s.items() if k!='snapshot_sha256'}
        digest = hashlib.sha256(json.dumps(without_hash,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        require(digest==s['snapshot_sha256'],f'事实卡指纹不一致：{sid}')
        require(s['snapshot_kind']=='paraphrased_fact_card_not_original_page','不能冒充网页原始快照')
        for f in s['facts']:
            require(f['fact_id'] not in facts,'事实ID重复')
            require(f['statement'] and f['unit'] and f['data_period'] and f['scope'],'事实缺少口径')
            facts[f['fact_id']] = sid
    rubrics = read('rubrics.json')
    fixtures = read('fixtures.json')
    all_cases = []
    for collection in ['golden','regression','challenge']:
        private = read(f'private/{collection}.json')['cases']
        public = read(f'public/{collection}.json')['cases']
        require([c['case_id'] for c in private]==[c['case_id'] for c in public],'公开/隐藏案例不一致')
        for pub in public:
            require(set(pub)=={'case_id','title','area','user_request','cutoff','limits'},'公开案例泄漏隐藏字段')
        for c in private:
            cid = c['case_id']
            require(c['collection']==collection, f'归属错：{cid}')
            require(c['run_status']=='not_run' and c['application_runner_support']=='not_integrated',f'冒充运行：{cid}')
            require(c['provenance']['human_review']=='pending',f'冒充人工验收：{cid}')
            require(c['execution']['network_authorized'] is False and c['execution']['paid_calls_authorized'] is False,'越权调用')
            require(c['input']['user_request'] and c['oracle']['expected_handling'],'缺题干或预期')
            require(bool(c['oracle']['assertions']),f'无判定标准：{cid}')
            for assertion in c['oracle']['assertions']:
                require(assertion['rubric_id'] in rubrics and assertion['requirement'], f'判分引用错误：{cid}')
            for sid in c['input']['source_ids']:
                require(sid in sources,f'缺来源：{cid}')
                require(not(c['split']=='development' and sources[sid]['split']=='holdout'), f'留出资料进入开发题：{cid}')
            for fid in c['oracle']['required_fact_ids']:
                require(fid in facts and facts[fid] in c['input']['source_ids'],f'事实脱离来源：{cid}/{fid}')
            fixture = c['input']['fixture_id']
            if fixture:
                require(fixture in fixtures and fixtures[fixture]['synthetic'] is True,f'缺合成标记：{cid}')
            else:
                require(c['input']['source_ids'],f'无资料也无场景：{cid}')
            if c['area']=='robustness':
                require(c['execution']['profile']=='routing_fault_injection',f'鲁棒性绕过路由：{cid}')
            test = c['provenance']['regression_test']
            if test:
                path, function = test.split('::')
                tree = ast.parse((REPO/path).read_text('utf-8-sig'))
                require(any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==function for n in ast.walk(tree)),f'历史测试映射不存在：{cid}')
        all_cases.extend(private)
    ids = [c['case_id'] for c in all_cases]
    require(len(ids)==len(set(ids)),'案例ID重复')
    require(len(all_cases)==manifest['total_new_cases'],'案例数量不一致')
    require(len(fixtures)==sum(bool(c['input']['fixture_id']) for c in all_cases),'存在孤立fixture')
    coverage = read('coverage.json')
    require(dict(Counter(c['area'] for c in all_cases))==coverage['areas'],'领域覆盖统计不一致')
    require(dict(Counter(c['collection'] for c in all_cases))==coverage['collections'],'集合统计不一致')
    legacy = read('legacy/protocol-1.json')
    require(legacy==json.loads((REPO/'backend/data/evaluation_cases.json').read_text('utf-8')),'旧协议被改变')
    require(hashlib.sha256((REPO/'backend/data/evaluation_cases.json').read_bytes()).hexdigest()==manifest['legacy_original_sha256'],'旧协议原文件指纹变化')
    for row in read('calibration.json')['cases']:
        require(all(sid in sources for sid in row['source_ids']),'校准来源不存在')
        require(not(row['split']=='development' and any(sources[s]['split']=='holdout' for s in row['source_ids'])),'校准泄漏留出来源')
    require(set(coverage['areas'])=={'protocol','search','report','robustness','end_to_end'},'五板块覆盖不全')
    return dict(status='passed', new_cases=len(all_cases), legacy_cases=len(legacy['cases']),
                total_cases=len(all_cases)+len(legacy['cases']),source_cards=len(sources),
                verified_facts=len(facts),rubrics=len(rubrics),fixtures=len(fixtures),
                collections=coverage['collections'],areas=coverage['areas'],
                note='仅数据结构/引用/指纹校验；不代表业务运行通过或人工验收。')


if __name__ == '__main__':
    print(json.dumps(validate(),ensure_ascii=False,indent=2))
