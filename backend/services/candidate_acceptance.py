"""候选稿验收：问题内容与出现次数独立于段落坐标。"""
from collections import Counter
import json
import re


def hard_problem_counts(check):
    from services.issue_identity import problem_counts
    return problem_counts(i for i in check.get('binding_issues', []) if i.get('severity') == 'hard')


def has_rewrite_protocol(body):
    """识别独立的返工对象，允许普通 JSON 示例及正常代码段。"""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^\s*\{", body):
        try:
            value, _ = decoder.raw_decode(body[match.end() - 1:])
        except ValueError:
            continue
        if (isinstance(value, dict) and value.get("mode") in {"full", "patch"}
                and ("content" in value or "patches" in value)
                and ("results" in value or "reason" in value)):
            return True
    return False


def validate_candidate(before, after, before_check, after_check, *, full=False):
    """拒绝协议污染及新增问题，段落移动不会改变问题身份。"""
    if has_rewrite_protocol(after) and (full or not has_rewrite_protocol(before)):
        raise ValueError("正文含返工协议")
    old_errors = Counter(re.sub(r"段落\d+", "段落", error) for error in before_check["errors"])
    new_errors = Counter(re.sub(r"段落\d+", "段落", error) for error in after_check["errors"])
    if new_errors - old_errors:
        raise ValueError("候选新增引用错误")
    if hard_problem_counts(after_check) - hard_problem_counts(before_check):
        raise ValueError("候选新增硬错误")
