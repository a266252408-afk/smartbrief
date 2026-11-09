"""确定性质量规则：不调用模型，不把未知结果升级为通过。"""
import re

# 零证据时不再调用 writer/rewriter 模型，统一输出这段确定性说明。
# 标记同时用于 rewriter 识别“无需再改写的缺口稿”。
DATA_GAP_MARKER = "本部分未获得可追溯来源，未生成结论"


def data_gap_section(task: str, topic: str | None = None) -> str:
    """生成不含事实断言的确定性数据缺口章节。"""
    subtask = task.split("子任务：", 1)[-1].strip() if "子任务：" in task else (task or "").strip()
    # 主题/子任务放在标题行：引用检查会忽略标题中的数字，避免把计划原文里的
    # 年份或“Top 5”等标签误判为报告中的数据声明。
    lines = ["## 数据缺口说明", ""]
    if topic:
        lines += [f"### 研究主题：{topic}", ""]
    if subtask:
        lines += [f"### 子任务：{subtask}", ""]
    lines += [f"{DATA_GAP_MARKER}。", "", "> 数据待补充：暂无可追溯来源。"]
    return "\n".join(lines)


def is_data_gap_draft(draft) -> bool:
    return isinstance(draft, str) and DATA_GAP_MARKER in draft


def needs_research(state: dict) -> bool:
    """仅缺证据的拒绝进入一次补研究；措辞/引用格式问题仍直接改写。"""
    if not state.get("topic") or state.get("quality_status") != "rejected":
        return False
    if state.get("research_revision_count", 0) >= 1:
        return False
    if not state.get("evidence"):
        return True
    if state.get("error_code") == "CITATION_INVALID":
        return False
    comments = "；".join(state.get("review_comments", []))
    return bool(re.search(
        r"(?:缺|不足|补充|补齐|待补充).{0,16}(?:数据|资料|证据|来源)|"
        r"(?:数据|资料|证据|来源).{0,16}(?:不足|缺失|缺乏|不相关|非相关|无关)|"
        r"(?:不相关|非相关|无关).{0,16}(?:资料|证据|来源|参考文献)", comments))


REVIEW_KEYS = {"passed", "comments", "findings", "possible_rule_issue", "full_rewrite_required"}


def _parse_findings(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("审核 findings 必须为数组")
    findings = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("审核 finding 必须为对象")
        finding_id = item.get("finding_id")
        problem = item.get("problem")
        expected = item.get("expected")
        evidence = item.get("evidence")
        location = item.get("location")
        severity = item.get("severity")
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ValueError("finding_id 无效")
        if not isinstance(problem, str) or not problem.strip():
            raise ValueError("finding problem 无效")
        if not isinstance(expected, str) or not expected.strip():
            raise ValueError("finding expected 无效")
        if not isinstance(evidence, dict) or not isinstance(evidence.get("source_id"), str) \
                or not evidence["source_id"].strip() \
                or not isinstance(evidence.get("quote"), str) or not evidence["quote"].strip():
            raise ValueError("finding evidence 无效")
        if not isinstance(location, dict) or type(location.get("paragraph")) is not int \
                or location["paragraph"] < 1 or type(location.get("sentence")) is not int \
                or location["sentence"] < 1:
            raise ValueError("finding location 无效")
        if severity not in {"hard", "soft"}:
            raise ValueError("finding severity 无效")
        findings.append({
            "finding_id": finding_id.strip(),
            "problem": problem.strip(),
            "evidence": {"source_id": evidence["source_id"].strip(),
                         "quote": evidence["quote"].strip()},
            "location": {"paragraph": location["paragraph"], "sentence": location["sentence"]},
            "expected": expected.strip(),
            "severity": severity,
            **{key: item[key] for key in ("rule", "kind", "numbers", "cited", "candidate_sources")
               if key in item},
        })
    return findings


def parse_review(content: str) -> dict:
    from services.review_contracts import ReviewResult, parse_json
    data = parse_json(content)
    if isinstance(data, dict) and isinstance(data.get('comments'), list):
        data['comments'] = [c.strip() for c in data['comments'] if isinstance(c, str) and c.strip()] if all(isinstance(c,str) for c in data['comments']) else data['comments']
    return ReviewResult.model_validate(data).model_dump(exclude_unset=True)


def usable_draft(draft) -> bool:
    if not isinstance(draft, str) or not re.sub(r"[#>*`\s-]", "", draft):
        return False
    # 占位标记与 scheduler 的降级文案一一对应（新增占位需两边同步维护）
    return not any(marker in draft for marker in (
        "（报告草稿为空）", "（无 writer 步骤生成内容）", "（章节撰写失败：", "撰写超时，内容待补充",
    ))


def ensure_title(draft, topic: str | None) -> str:
    """缺一级标题时用研究主题补一个；只补标题，不改正文。"""
    if not usable_draft(draft):
        return draft
    first = next((line.strip() for line in str(draft).splitlines() if line.strip()), "")
    if re.match(r"#\s+\S", first) and not re.match(r"#\s*第\s*[一二三四五六七八九十0-9]+\s*章", first):
        return draft
    title = (topic or "").strip()
    if not title or title.startswith("（未知"):
        return draft
    if not title.endswith("报告"):
        title += "报告"
    return f"# {title}\n\n{draft}"


_SUMMARY_HEADING = re.compile(r"(?:执行)?摘要")
_CONCLUSION_HEADING = re.compile(r"结论|总结|建议")


def _trim_blank(lines: list[str]) -> list[str]:
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def repair_structure(draft, topic: str | None = None) -> str:
    """确定性修复报告骨架：唯一 H1、去重复标题、摘要在前、结论与建议在后。

    只调整标题层级与章节顺序，不改写正文；无法安全判断的版式保持原样。
    """
    if not usable_draft(draft):
        return draft
    text = ensure_title(draft, topic)
    lines = text.splitlines()

    h1 = [index for index, line in enumerate(lines) if re.match(r"^#\s+\S", line)]
    if h1:
        first_title = re.sub(r"^#\s+", "", lines[h1[0]]).strip()
        drop = set()
        for index in h1[1:]:
            title = re.sub(r"^#\s+", "", lines[index]).strip()
            if title == first_title:
                drop.add(index)
            else:
                lines[index] = f"## {title}"
        lines = [line for index, line in enumerate(lines) if index not in drop]

    h2 = [index for index, line in enumerate(lines) if re.match(r"^##\s+\S", line)]
    if not h2:
        return "\n".join(lines).strip()

    preamble = _trim_blank(lines[:h2[0]])
    blocks = []
    for number, start in enumerate(h2):
        end = h2[number + 1] if number + 1 < len(h2) else len(lines)
        blocks.append((re.sub(r"^##\s+", "", lines[start]).strip(),
                       _trim_blank(lines[start:end])))

    summary = next((block for title, block in blocks if _SUMMARY_HEADING.search(title)), None)
    conclusion = next((block for title, block in reversed(blocks)
                       if _CONCLUSION_HEADING.search(title)), None)
    ordered = []
    if summary is not None:
        ordered.append(summary)
    ordered.extend(block for title, block in blocks
                   if block is not summary and block is not conclusion)
    if conclusion is not None and conclusion is not summary:
        ordered.append(conclusion)

    chunks = ["\n".join(preamble).strip()] if preamble else []
    chunks.extend("\n".join(block).strip() for block in ordered if block)
    return "\n\n".join(chunk for chunk in chunks if chunk)


def _required_heading_role(title):
    title = re.sub(r'^[一二三四五六七八九十0-9]+[、.．]\s*', '', title.strip())
    name = re.split(r'[：:]', title, maxsplit=1)[0].strip()
    return {'摘要': 'summary', '执行摘要': 'summary', '核心摘要': 'summary',
            '结论': 'conclusion', '总结': 'conclusion', '主要结论': 'conclusion',
            '核心结论': 'conclusion', '研究结论': 'conclusion', '结论与建议': 'conclusion'}.get(name)


def _section_has_content(lines, index, level):
    content = []
    for line in lines[index+1:]:
        heading = re.match(r'^(#{1,6})\s+', line)
        if heading and len(heading[1]) <= level:
            break
        if not heading:
            content.append(line)
    return bool(re.sub(r'[\s#>*`~|\-:]+', '', '\n'.join(content)))


def structure_gaps(draft) -> list[dict]:
    """报告骨架检查：标题、摘要、结论与章节编号。只提示，不改写内容。"""
    if not isinstance(draft, str) or not draft.strip():
        return []
    lines = [line.strip() for line in draft.splitlines() if line.strip()]
    first = lines[0] if lines else ""
    has_title = bool(re.match(r"#\s+\S", first)) and not re.match(
        r"#\s*第\s*[一二三四五六七八九十0-9]+\s*章", first)
    issues = []
    if not has_title:
        issues.append({"kind": "missing_title",
                       "message": "缺少报告标题：首行应是一级标题，不能直接以“第一章”开头"})
    role_content = {'summary': [], 'conclusion': []}
    for index, line in enumerate(lines):
        match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if match and (role := _required_heading_role(match[2])):
            role_content[role].append(_section_has_content(lines, index, len(match[1])))
    for role, label in [('summary', '执行摘要'), ('conclusion', '结论与建议')]:
        if not role_content[role]:
            issues.append({'kind': 'missing_' + role, 'message': '缺少' + label})
        elif not any(role_content[role]):
            issues.append({'kind': 'empty_' + role, 'message': label + '没有实际正文'})
    chapters = [re.sub(r"^##\s*", "", line).strip() for line in lines if re.match(r"^##\s+\S", line)]
    numbers = [match.group(1) for chapter in chapters
               if (match := re.match(r"第\s*([一二三四五六七八九十0-9]+)\s*章", chapter))]
    if len(numbers) != len(set(numbers)):
        issues.append({"kind": "duplicate_chapter", "message": "章节编号重复"})
    from services.candidate_acceptance import has_rewrite_protocol
    if has_rewrite_protocol(draft):
        issues.append({"kind": "rewrite_protocol", "message": "报告正文包含返工协议或报告副本"})
    return issues


def repeated_passages(draft):
    """重复提及一个指标允许；跨章节复制至少两个完整长句时给出位置。"""
    from services.citations import split_sentences
    sections = []
    current = {"paragraph": 1, "sentences": set()}
    for n, paragraph in enumerate(re.split(r"\n\s*\n", draft or ""), 1):
        if re.match(r"^##\s+", paragraph):
            sections.append(current)
            current = {"paragraph": n, "sentences": set()}
        text = re.sub(r"(?m)^#{1,6}[^\n]*", "", paragraph)
        for sentence in split_sentences(text):
            normalized = re.sub(r"\[S\d+\]|\s+", "", sentence)
            if len(normalized) >= 12:
                current["sentences"].add(normalized)
    sections.append(current)
    issues = []
    for index, section in enumerate(sections):
        for previous in sections[:index]:
            shared = section["sentences"] & previous["sentences"]
            if len(shared) >= 2:
                issues.append({"kind": "duplicate_passage", "paragraph": section["paragraph"],
                               "previous_paragraph": previous["paragraph"],
                               "message": f"段落{previous['paragraph']}与段落{section['paragraph']}所在章节复制了{len(shared)}个完整句子；摘要应提炼结论"})
    return issues


def _metric_key(token: str):
    from services.numbers import parse_number_values

    values = parse_number_values(token)
    if not values:
        return None
    value, kind, unit = values[0]
    if kind == "percent":
        return ("percent", round(value, 4))
    normalized = (unit or "").lower()
    if "美元" in normalized:
        currency = "usd"
    elif "元" in normalized:
        currency = "cny"
    elif normalized in {"台", "辆", "个", "件", "座", "人", "名"}:
        currency = "unit"
    else:
        currency = "count"
    return (currency, round(value, 4))


def _context_terms(text: str, start: int, end: int, width: int = 16) -> set[str]:
    window = text[max(0, start - width):min(len(text), end + width)]
    window = re.sub(r"[#>*`\s\d%％,，。；;：:、（）()\-—+.+/]", " ", window)
    return {window[index:index + 2] for index in range(len(window) - 1) if len(window[index:index + 2].strip()) == 2}


def _jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def duplicate_metrics(draft, limit: int = 5) -> list[dict]:
    """同一指标带着相近上下文在多个正文章节重复展开时提示合并；摘要与结论不计入。"""
    if not isinstance(draft, str) or not draft.strip():
        return []
    from services.citations import quantitative_tokens

    parts = re.split(r"(?m)^##\s+", draft)
    records: dict[tuple, list[dict]] = {}
    for part in parts[1:]:
        title = part.splitlines()[0].strip() if part.splitlines() else ""
        if re.search(r"摘要|结论|总结", title):
            continue
        cursor = 0
        for token in quantitative_tokens(part):
            position = part.find(token, cursor)
            if position < 0:
                position = part.find(token)
            if position < 0:
                continue
            cursor = position + len(token)
            key = _metric_key(token)
            if not key:
                continue
            terms = _context_terms(part, position, position + len(token))
            groups = records.setdefault(key, [])
            for group in groups:
                if _jaccard(group["terms"], terms) >= 0.35:
                    group["terms"] |= terms
                    if title not in group["sections"]:
                        group["sections"].append(title)
                    break
            else:
                groups.append({"sample": token, "terms": terms, "sections": [title]})
    duplicates = []
    for groups in records.values():
        for group in groups:
            if len(group["sections"]) >= 2:
                duplicates.append({"value": group["sample"], "sections": group["sections"],
                                   "section_count": len(group["sections"])})
    duplicates.sort(key=lambda item: item["section_count"], reverse=True)
    return duplicates[:limit]
