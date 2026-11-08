"""确定性补齐报告流水线依赖，兼容缺少depends_on的旧计划。"""
import re


def is_assembly_step(step, plan):
    task = str(step.get('task', ''))
    if re.search(r'仅撰写主体|不输出报告标题', task):
        return False
    prior = []
    for item in plan:
        if item['id'] == step['id']:
            break
        prior.append(item)
    return any(s.get('agent') == 'writer' for s in prior) and bool(
        re.search(r'统稿|整合|组装|完整报告|全文', task) or ('摘要' in task and '结论' in task))


def normalize_report_plan(plan):
    """补齐隐含研究/统稿前置，保留原显式依赖；已存在逆向依赖时不造环。"""
    steps = [dict(s, depends_on=list(s.get('depends_on') or [])) for s in plan]
    by_id = {s['id']: s for s in steps}

    def depends_on(start, target, seen=None):
        seen = set() if seen is None else seen
        if start in seen:
            return False
        seen.add(start)
        deps = by_id.get(start, {}).get('depends_on', [])
        return target in deps or any(depends_on(d, target, seen) for d in deps)

    prior = []
    for step in steps:
        if step.get('agent') == 'writer':
            candidates = [s['id'] for s in prior if s.get('agent') == 'researcher'
                          or (s.get('agent') == 'writer' and is_assembly_step(step, steps))]
            for dep in candidates:
                if dep not in step['depends_on'] and not depends_on(dep, step['id']):
                    step['depends_on'].append(dep)
            step['depends_on'] = sorted(set(step['depends_on']))
        prior.append(step)
    return steps
