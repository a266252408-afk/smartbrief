"""把确定性引用问题转成可执行 finding：问题、证据、位置、期望结果。

finding 只描述问题与可执行修改，不直接改判定；审核 agent 可以补充问题/期望，
但不能推翻硬规则，也不能伪造来源证据。
"""
import re

from services.citations import split_sentences
from services.numbers import scale_conversion, split_number_token, token_matches_text


def _currency_suffix(source_text: str) -> str:
    text = (source_text or "").lower()
    for suffix, pattern in (
        ("美元", r"美元|usd|dollar|\$"),
        ("欧元", r"欧元|eur"),
        ("日元", r"日元|jpy"),
        ("港元", r"港元|hkd"),
        ("元", r"人民币|cny|rmb|元"),
    ):
        if re.search(pattern, text):
            return suffix
    return ""

_FINDING_PREFIX = {
    "unbound_number": "BIND",
    "merged_fact_range": "RANGE",
    "unverified_comparison": "CMP",
    "unsupported_wording": "WORD",
    "avoidable_gap": "GAP",
    "period_mismatch": "PERIOD",
    "percentage_point_mismatch": "PCT",
    "comparison_basis_mismatch": "BASIS",
    "geography_mismatch": "GEO",
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _quote(source: dict, numbers=(), literal: bool = False, limit: int = 200) -> str:
    excerpt = str(source.get("excerpt") or "")
    sentences = split_sentences(excerpt) or [excerpt]
    for sentence in sentences:
        for number in numbers or ():
            if literal:
                match = re.match(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", str(number))
                if match and _normalize(match.group(0)) in _normalize(sentence):
                    return sentence[:limit]
            elif token_matches_text(number, sentence):
                return sentence[:limit]
    return (sentences[0] if sentences else excerpt)[:limit]


def _known_sources(sources) -> dict:
    return {
        source.get("source_id"): source
        for source in (sources or [])
        if isinstance(source, dict) and source.get("source_id")
        and not source.get("source_deleted")
    }


def _evidence_for_issue(issue: dict, known: dict) -> tuple[dict | None, list[dict]]:
    numbers = issue.get("numbers") or []
    kind = issue.get("kind")
    if kind == "unbound_number":
        candidates = issue.get("candidate_sources") or []
        if candidates and candidates[0] in known:
            return ({"source_id": candidates[0],
                     "quote": _quote(known[candidates[0]], numbers)}, [])
        for matches in (issue.get("scale_matches") or {}).values():
            if matches and matches[0].get("source") in known:
                source_id = matches[0]["source"]
                return ({"source_id": source_id,
                         "quote": _quote(known[source_id], numbers, literal=True)}, [])
        cited = [source_id for source_id in (issue.get("source_ids") or []) if source_id in known]
        if cited:
            return ({"source_id": cited[0], "quote": _quote(known[cited[0]])}, [])
        return None, []
    source_ids = [
        source_id for source_id in (issue.get("source_ids") or [])
        if source_id in known
    ]
    if kind == "avoidable_gap":
        source_ids = [source_id for source_id in (issue.get("candidate_sources") or [])
                      if source_id in known]
    if not source_ids:
        return None, []
    if kind == 'unsupported_wording':
        anchors = re.findall(r'客单价|均价|售价|报价|金额|营收|收入', issue.get('sentence', ''))
        source_ids.sort(key=lambda sid: not any(a in known[sid].get('excerpt', '') for a in anchors))
    excerpt = known[source_ids[0]].get('excerpt', '')
    quote = issue.get('quote')
    if not quote or quote not in (known[source_ids[0]].get('title', '') + '\n' + excerpt):
        quote = _quote(known[source_ids[0]], numbers, literal=kind == 'merged_fact_range')
    if kind == 'unsupported_wording':
        anchors = re.findall(r'客单价|均价|售价|报价|金额|营收|收入', issue.get('sentence', ''))
        for passage in split_sentences(excerpt):
            common = next((a for a in anchors if a in passage), None)
            if common:
                start = max(0, passage.index(common)-60)
                quote = passage[start:start+240]
                break
    evidence = {'source_id': source_ids[0], 'quote': quote}
    evidence_sources = [
        {"source_id": source_id,
         "quote": _quote(known[source_id], numbers, literal=kind == "merged_fact_range")}
        for source_id in source_ids[:3]
    ]
    return evidence, evidence_sources


def _problem(issue: dict) -> str:
    kind = issue.get("kind")
    if kind == "unbound_number":
        return {
            "miscitation": "引用挂错：数字不在所引来源",
            "unit_scale": "单位量级错误",
            "unsupported": "数字无来源支持",
        }.get(issue.get("category"), "数字未在所引来源中找到")
    return {
        "merged_fact_range": "两个独立事实被合并成区间",
        "unverified_comparison": "比较级缺少来源排序支撑",
        "unsupported_wording": "表述未在所引来源出现",
        "avoidable_gap": "标注数据待补充但来源已有同指标",
        "period_mismatch": "声明与来源的统计期间不一致",
        "percentage_point_mismatch": "百分比增速被改写成百分点",
        "comparison_basis_mismatch": "同比与环比口径混淆",
        "geography_mismatch": "声明与来源的统计地区不一致",
    }.get(kind, "确定性问题")


def _expected(issue: dict, known: dict) -> str:
    kind = issue.get("kind")
    if issue.get('expected'):
        return issue['expected']
    if kind == "unbound_number":
        category = issue.get("category")
        numbers = issue.get("numbers") or []
        if category == "miscitation":
            candidates = issue.get("candidate_sources") or []
            cited = "、".join(issue.get("source_ids") or [])
            if len(candidates) == 1:
                return f"将 [{cited}] 改引 {candidates[0]}"
            return f"改引候选来源 {'、'.join(candidates)} 之一，并核对原文"
        if category == "unit_scale":
            fixes = []
            for token in numbers:
                for matches in (issue.get("scale_matches") or {}).get(token, []):
                    source = known.get(matches.get("source"))
                    if not source:
                        continue
                    hint = scale_conversion(token, source.get("excerpt", ""))
                    if hint:
                        parts = split_number_token(token)
                        expected = hint["expected"]
                        if parts and not parts[2]:
                            expected += _currency_suffix(source.get("excerpt", ""))
                        fixes.append(f"{token} 改为 {expected}")
                        break
            if fixes:
                return "按来源原文换算：" + "；".join(fixes)
            return "按来源原文换算单位后书写"
        return "删除该数字，或改用包含该数字的来源；无来源时标注数据待补充"
    if kind == "merged_fact_range":
        sources = "、".join(issue.get("source_ids") or [])
        return f"分别引用 {sources} 并说明统计口径；不得合并成区间"
    if kind == "unverified_comparison":
        return "删除比较级，或改用来源中出现的排序表述"
    if kind == "unsupported_wording":
        return f"删除表述 {'、'.join(issue.get('terms') or [])}，或改引包含该表述的来源"
    if kind == "avoidable_gap":
        candidates = "、".join(issue.get("candidate_sources") or [])
        return f"核对统计口径与期间后使用 {candidates} 中的同指标数据"
    return "按审核意见修正"


def build_findings(body: str, sources, issues) -> list[dict]:
    """把确定性 issue 转成 finding；info 级 unbound_source 不生成 finding。"""
    known = _known_sources(sources)
    counters = {}
    findings = []
    for issue in issues or []:
        from services.review_rules import assess_issue
        issue = issue if issue.get("origin") == "program" else assess_issue(issue, sources)
        kind = issue.get("kind")
        if kind == "unbound_source":
            continue
        evidence, evidence_sources = _evidence_for_issue(issue, known)
        if evidence is None:
            continue
        prefix = _FINDING_PREFIX.get(kind, "FIND")
        counters[prefix] = counters.get(prefix, 0) + 1
        finding = {
            **{k: issue[k] for k in ("origin", "decision", "material", "decision_reason")},
            "finding_id": f"{prefix}-{counters[prefix]:04d}",
            "rule": kind,
            "kind": kind,
            "severity": issue.get("severity", "hard"),
            "problem": _problem(issue),
            "evidence": evidence,
            "location": {
                "paragraph": issue.get("paragraph", 1),
                "sentence": issue.get("sentence_index", 1),
            },
            "expected": _expected(issue, known),
            "numbers": issue.get("numbers") or [],
            "cited": sorted(set(issue.get("source_ids") or [])),
            "candidate_sources": issue.get("candidate_sources") or [],
        }
        if evidence_sources:
            finding["evidence_sources"] = evidence_sources
        findings.append(finding)
    return findings


def _quote_in_source(quote: str, source: dict) -> bool:
    # 只允许省略尾部：保留的长片段仍须连续逐字命中，不接受内部拼接或短泛词。
    quote = re.sub(r'(?:\.{3}|…+)\s*$', '', quote or '') if len(_normalize(quote)) >= 24 else quote
    return bool(_normalize(quote)) and any(_normalize(quote) in _normalize(source.get(k, ''))
                                         for k in ('title', 'excerpt'))


def merge_findings(deterministic: list[dict], model_findings: list[dict],
                   sources, body="") -> tuple[list[dict], list[dict]]:
    """合并审核 agent 的 findings；代码重新校验证据，不信任模型引用的来源。"""
    known = _known_sources(sources)
    merged = {finding["finding_id"]: dict(finding) for finding in deterministic}
    rejected = []
    for item in model_findings or []:
        evidence = item.get("evidence") or {}
        if evidence.get('kind') == 'report':
            from services.rewrite_patches import paragraph_table
            paragraphs = paragraph_table(body)
            n = item.get('location', {}).get('paragraph', 0)
            if not 1 <= n <= len(paragraphs) or evidence.get('quote', '') not in paragraphs[n-1]['text']:
                rejected.append(item)
                continue
            merged[item['finding_id']] = dict(item, rule='reviewer', kind='reviewer')
            continue
        source = known.get(evidence.get("source_id"))
        if not source or not _quote_in_source(evidence.get("quote", ""), source):
            rejected.append(item)
            continue
        # 同位置、同修改动作、同来源证据的重复描述保留代码编号。
        if any(base.get('location') == item.get('location')
               and base.get('expected') == item.get('expected')
               and base.get('evidence', {}).get('source_id') == evidence.get('source_id')
               and _normalize(base.get('evidence', {}).get('quote', '')) == _normalize(evidence.get('quote', ''))
               for base in deterministic):
            continue
        finding_id = item.get("finding_id")
        if finding_id in merged:
            base = merged[finding_id]
            base["problem"] = item.get("problem") or base["problem"]
            base["expected"] = item.get("expected") or base["expected"]
            continue
        merged[finding_id] = {
            "finding_id": finding_id,
            "rule": item.get("rule") or "reviewer",
            "kind": item.get("kind") or "reviewer",
            "severity": item.get("severity", "soft"),
            "problem": item["problem"],
            "evidence": evidence,
            "location": item["location"],
            "expected": item["expected"],
            "numbers": item.get("numbers") or [],
            "cited": item.get("cited") or [evidence.get("source_id")],
            "candidate_sources": item.get("candidate_sources") or [],
            "scope": item.get('scope', 'paragraph'),
        }
    from services.review_rules import assess_issue, assess_model_finding
    program_ids = {f['finding_id'] for f in deterministic}
    result = []
    for key, finding in merged.items():
        if key in program_ids:
            result.append(finding if finding.get('origin') == 'program' else assess_issue(finding, sources))
        else:
            result.append(assess_model_finding(finding))
    return result, rejected


def finding_block(findings: list[dict], limit: int | None = None) -> str:
    lines = []
    for finding in findings[:limit]:
        location = finding.get("location") or {}
        evidence = finding.get("evidence") or {}
        lines.append(
            f"- {finding.get('finding_id')} | {finding.get('severity')} | {finding.get('problem')} "
            f"| 位置 第{location.get('paragraph', '?')}段第{location.get('sentence', '?')}句 "
            f"| 证据 {evidence.get('source_id', '?')}: \"{evidence.get('quote', '')}\" "
            f"| 期望：{finding.get('expected', '')}"
        )
    return "\n".join(lines)


def reference_findings(body: str, check: dict) -> list[dict]:
    """把无效来源定位到原稿；不猜测哪个有效来源能支持声明。"""
    from services.citations import reference_ids
    from services.rewrite_patches import paragraph_table
    invalid = {c['source_id']:c.get('reason') or '引用无效'
               for c in check.get('citations', []) if not c.get('valid')}
    findings = []
    for paragraph in paragraph_table(body):
        for sid in sorted(reference_ids(paragraph['text']) & invalid.keys()):
            findings.append({'finding_id':f'REF-{len(findings)+1:04d}', 'rule':'invalid_reference',
                'severity':'hard', 'problem':invalid[sid],
                'expected':f'核对给定证据后替换无效引用[{sid}]；没有支持来源则删除声明或明确资料不足，不得编造来源',
                'location':{'paragraph':paragraph['paragraph'],'sentence':1},
                'evidence':{'kind':'report','quote':paragraph['text']}, 'cited':[sid]})
    return findings
