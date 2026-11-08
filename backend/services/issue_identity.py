"""统一目标、冲突字段及出现次数；不以措辞相似度确认修复。"""
from collections import Counter, defaultdict
from decimal import Decimal, localcontext
import hashlib
import json
import re
from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field


class ContentBlock(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    target_id: str = Field(pattern=r'^B[a-f0-9]{24}$')
    paragraph: int = Field(ge=1)
    text: str
    digest: str
    certainty: Literal['new', 'exact', 'mapped', 'unverified']


class ContentIndex(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    version: Literal[1] = 1
    task_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    body_sha256: str
    blocks: tuple[ContentBlock, ...]


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def index_document(task_id, body, previous=None, *, target_mapping=None, base_revision=None):
    from services.rewrite_patches import paragraph_table
    old = ContentIndex.model_validate_json(json.dumps(previous)) if previous is not None else None
    if old and old.task_id != task_id:
        raise ValueError('内容索引不属于当前任务')
    if base_revision is not None and (not old or type(base_revision) is not int or old.revision != base_revision):
        raise ValueError('内容索引版本不匹配')
    rows = paragraph_table(body)
    prior = {b.target_id: b for b in old.blocks} if old else {}
    mapping = target_mapping or {}
    if mapping and base_revision is None:
        raise ValueError("目标映射缺少base_revision")
    if any(key not in prior or type(n) is not int or not 1 <= n <= len(rows) for key, n in mapping.items()) or len(set(mapping.values())) != len(mapping):
        raise ValueError('目标映射无效或存在重复归属')
    digest = hashlib.sha256(body.encode()).hexdigest()
    if old and old.body_sha256 == digest:
        if any(prior[key].paragraph != n for key, n in mapping.items()):
            raise ValueError('正文未变不能改写目标归属')
        return old.model_dump(mode='json')
    old_text = defaultdict(list)
    for block in prior.values():
        old_text[block.text.replace('\r\n', '\n')].append(block)
    new_counts = Counter(row['text'].replace('\r\n', '\n') for row in rows)
    reverse, used, blocks = {n: key for key, n in mapping.items()}, set(), []
    for row in rows:
        text = row['text'].replace('\r\n', '\n')
        matches = old_text[text]
        if row['paragraph'] in reverse:
            key, certainty = reverse[row['paragraph']], 'mapped'
        elif len(matches) == new_counts[text] == 1 and matches[0].target_id not in used and matches[0].target_id not in mapping:
            key, certainty = matches[0].target_id, 'exact'
        else:
            key, certainty = 'B' + uuid4().hex[:24], 'unverified' if old else 'new'
        used.add(key)
        blocks.append(ContentBlock(target_id=key, paragraph=row['paragraph'], text=row['text'], digest=row['digest'], certainty=certainty))
    return ContentIndex(task_id=task_id, revision=old.revision+1 if old else 1,
                        body_sha256=digest, blocks=tuple(blocks)).model_dump(mode='json')


def _canonical_number(token):
    from services.numbers import split_number_token, _MULTIPLIERS
    parts = split_number_token(str(token))
    if not parts:
        return ['raw', re.sub(r'\s+', '', str(token))]
    number, scale, suffix = parts
    magnitude = scale or (suffix.lower() if suffix.lower() in _MULTIPLIERS else None)
    multiplier = Decimal(str(_MULTIPLIERS.get(magnitude, 1)))
    with localcontext() as context:
        context.prec = max(28, len(Decimal(number).as_tuple().digits) + len(multiplier.as_tuple().digits) + 1)
        value = format((Decimal(number) * multiplier).normalize(), 'f')
    unit = '' if magnitude == suffix.lower() else suffix.replace('％', '%')
    return [value, unit]


def conflict_fields(issue):
    if (issue.get('rule') or issue.get('kind')) in {'required_structure','rewrite_protocol'}:
        return dict(subject='',metric='',numbers=[],period={},geography=[],basis=[],sources=[],terms=[],
                    currency=[],structure_kind=issue.get('structure_kind'),negation=[])
    if isinstance(issue.get('conflict_fields'), dict):
        return issue['conflict_fields']
    from services.claim_semantics import period_context, geography_context
    from services.citations import REFERENCE, quantitative_tokens
    raw = issue.get('sentence') or issue.get('report_quote') or ''
    text = REFERENCE.sub('', raw)
    text = re.sub(r'段落\d+', '', text)
    text = re.sub(r'(?m)^\s*#{1,6}[^\n]*(?:\n|$)', '', text)
    metric = re.search(r'市场规模|客单价|出货量|零售额|零售量|渗透率|增长率|份额|销量|收入|规模|增长|排名', text)
    subject = text[:metric.start()] if metric else ''
    subject = re.sub(r'^(?:该|本|这个|这一)', '', subject)
    subject = re.sub(r'(?:19|20)\d{2}年?|同比|环比|约|大约|当前|[\s，,。；;：:]', '', subject)
    evidence = issue.get('evidence') or {}
    sources = issue.get('source_ids') or issue.get('cited') or ([evidence['source_id']] if evidence.get('source_id') else REFERENCE.findall(raw))
    numbers = issue.get('numbers') or quantitative_tokens(text)
    return dict(subject=subject, metric=metric[0] if metric else '',
                numbers=sorted({_hash(_canonical_number(n)): _canonical_number(n) for n in numbers}.values()),
                period=period_context(text), geography=geography_context(text),
                basis=sorted(set(re.findall(r'同比|环比', text))), sources=sorted(set(sources)),
                terms=sorted(set(issue.get('terms') or issue.get('keywords') or [])),
                currency=sorted(set(re.findall(r'人民币|美元|欧元|日元|港元|(?<!美)元|USD|CNY', text))),
                structure_kind=issue.get('structure_kind'), negation=sorted(set(re.findall(r'并非|不是|不等于|未|没有|否认',text))))


def identity_key(issue, *, target=False, include_target=True):
    fields = dict(conflict_fields(issue))
    if target:
        fields.pop('numbers', None)
    return _hash(dict(rule=issue.get('rule') or issue.get('kind'),
                      target_id=issue.get('target_id') if include_target else None, fields=fields,
                      evidence_identity=issue.get('evidence_identity')))


def occurrence_id(key, ordinal):
    return 'I' + _hash([key, ordinal])[:24]


def attach_identities(body, findings, index=None, evidence=()):
    from services.citations import split_sentences
    from services.rewrite_patches import paragraph_table
    rows = {p['paragraph']: p for p in paragraph_table(body)}
    blocks = {b['paragraph']: b for b in index['blocks']} if index else {}
    sources = {s['source_id']: s for s in evidence}
    counts, result = Counter(), []
    for finding in findings:
        value = dict(finding)
        location = value.get('location') or {}
        paragraph = location.get('paragraph', value.get('paragraph'))
        sentence = location.get('sentence', value.get('sentence_index', 1))
        row, block = rows.get(paragraph), blocks.get(paragraph)
        if row:
            parts = split_sentences(row['text'])
            if type(sentence) is int and 1 <= sentence <= len(parts):
                value['report_quote'] = value.get('sentence') or parts[sentence-1]
        if index and value.get('rule') in {'required_structure', 'rewrite_protocol'}:
            value['target_id'] = 'R' + _hash(index['task_id'])[:24]
            value['identity_certainty'] = 'global'
        elif block:
            value['target_id'] = block['target_id']
            value['identity_certainty'] = block['certainty']
        else:
            value['identity_certainty'] = value.get('identity_certainty') or ('legacy' if row or value.get('sentence') else 'unverified')
        value['conflict_fields'] = conflict_fields(value)
        if (value.get('rule') == 'reviewer' and not any(value['conflict_fields'].get(k) for k in ('numbers','metric','terms'))):
            value['identity_certainty'] = 'unverified'
        refs = value['conflict_fields']['sources']
        if sources:
            value['evidence_identity'] = _hash({sid: {k: sources[sid].get(k) for k in (
                'task_id', 'source_id', 'title', 'excerpt', 'url', 'document_id', 'chunk_id', 'page', 'source_deleted')}
                for sid in refs if sid in sources})
        key = identity_key(value)
        value['issue_key'] = key
        value['occurrence'] = counts[key]
        value['issue_id'] = occurrence_id(key, counts[key])
        counts[key] += 1
        result.append(value)
    return result


def problem_counts(findings):
    return Counter(identity_key(f) for f in findings if isinstance(f, dict))


def same_repair_target(before, after):
    if (before.get('rule') or before.get('kind')) != (after.get('rule') or after.get('kind')):
        return False
    if before.get('target_id') and after.get('target_id'):
        return identity_key(before, target=True) == identity_key(after, target=True)
    a, b = conflict_fields(before), conflict_fields(after)
    for key in ('subject', 'metric', 'sources', 'basis', 'geography'):
        if a.get(key) and b.get(key) and a[key] != b[key]:
            return False
    # 旧接口只用已保存字段作保守未解决判断；不借未知字段宣布解决。
    return bool({tuple(v) for v in a['numbers']} & {tuple(v) for v in b['numbers']}) or (
        before.get('location', {}).get('paragraph') == after.get('location', {}).get('paragraph')
        and before.get('location', {}).get('paragraph') is not None)


def citation_issue(error, task_id):
    categories = [('来源记录无效', 'source_registry'), ('暂无可追溯来源', 'no_sources'),
                  ('正文缺少', 'no_citations'), ('含数字段落', 'missing_citation'),
                  ('返工丢失', 'lost_citation'), ('来源已删除', 'deleted_source'),
                  ('来源不存在', 'unknown_source'), ('不属于当前任务', 'wrong_task')]
    category = next((key for text,key in categories if text in error), 'unknown_integrity')
    fields = conflict_fields({'source_ids': sorted(set(re.findall(r'\[(S[0-9]+)\]', error)))})
    fields['error_category'] = category
    return dict(rule='citation', kind='citation', severity='hard', origin='program', decision='confirmed',
                material=True, message=error, conflict_fields=fields,
                target_id='R'+_hash(task_id)[:24],identity_certainty='global' if category!='unknown_integrity' else 'unverified')


def index_candidate(task_id, before, after, previous, *, patch=False):
    """按已执行补丁上下文和唯一原文生成映射；段落收缩时不猜旧坐标。"""
    from services.rewrite_patches import paragraph_table
    current = index_document(task_id, before, previous)
    if not patch:
        return index_document(task_id, after, current)
    rows = paragraph_table(after)
    old_text, new_text = defaultdict(list), defaultdict(list)
    for block in current['blocks']:
        old_text[block['text'].replace('\r\n','\n')].append(block)
    for row in rows:
        new_text[row['text'].replace('\r\n','\n')].append(row)
    mapping = {blocks[0]['target_id']:new_text[text][0]['paragraph'] for text,blocks in old_text.items()
               if len(blocks)==len(new_text[text])==1}
    if len(rows)==len(current['blocks']):
        used=set(mapping.values())
        for block in current['blocks']:
            if block['target_id'] not in mapping and block['paragraph'] not in used:
                mapping[block['target_id']]=block['paragraph']
                used.add(block['paragraph'])
    return index_document(task_id,after,current,target_mapping=mapping,base_revision=current['revision'])
