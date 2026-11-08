"""按可追溯问题身份判断进展，不把不同问题的相同数量当成未修复。"""
import hashlib
import json
import re
from collections import Counter


def problem_keys(findings):
    keys = set()
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        if f.get('rule') == 'citation':
            identity = {'rule': 'citation', 'message': re.sub(r'段落\d+', '段落', f.get('message', ''))}
        else:
            identity = {k: f.get(k) for k in ('rule', 'kind', 'problem', 'numbers', 'evidence')}
        keys.add(hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
    return keys


def review_progress(previous, current_findings, complete):
    old_counts = Counter(key for f in (previous.get('hard_finding_set') or []) for key in problem_keys([f])) if previous else Counter()
    new_counts = Counter(key for f in current_findings for key in problem_keys([f]))
    old, new = set(old_counts), set(new_counts)
    resolved, introduced, persisting = set(old_counts - new_counts), set(new_counts - old_counts), old & new
    stopped = bool(complete and previous and previous.get('review_complete')
                   and previous.get('rewrite', {}).get('changed') and old and not resolved)
    return {'resolved': sorted(resolved), 'introduced': sorted(introduced),
            'persisting': sorted(persisting), 'previous_problem_count': len(old),
            'current_problem_count': len(new), 'current_review_complete': complete,
            'stopped': stopped, 'basis': 'all_previous_hard_problems_persist' if stopped else 'continue_or_pass'}
