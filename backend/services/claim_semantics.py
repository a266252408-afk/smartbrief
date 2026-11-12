"""有证据的期间与百分比口径检查；来源歧义时不猜测、不自动改写事实。"""
import re

from services.numbers import token_matches_text


def source_text(source):
    return '\n'.join(str(source.get(k) or '') for k in ('title', 'excerpt'))


def geography_context(text):
    return sorted({label for label, pattern in (
        ('中国', r'中国|我国|国内'), ('全球', r'全球|global|worldwide'),
        ('北美', r'北美|North America'), ('欧洲', r'欧洲|Europe')) if re.search(pattern, text or '', re.I)})


def object_context(text):
    return sorted({label for label, pattern in (
        ('smartwatch', r'智能手表|smartwatch'), ('wrist_wearable', r'腕戴|腕带'),
        ('band', r'手环|fitness band'), ('wearable', r'可穿戴设备|wearables?')) if re.search(pattern, text or '', re.I)})


def period_context(text):
    periods = set()
    for m in re.finditer(r'(?i)Q\s*([1-4])|第?\s*([一二三四1-4])\s*季度', text or ''):
        n = m.group(1) or m.group(2)
        periods.add('Q' + str('一二三四'.index(n) + 1 if n in '一二三四' else n))
    for pattern, value in [(r'上半年|(?i:H1)', 'H1'), (r'下半年|(?i:H2)', 'H2'),
                           (r'全年|年度|(?i:annual|yearly|full[-\s]?year)', 'annual')]:
        if re.search(pattern, text or ''):
            periods.add(value)
    return {'years': sorted({int(y) for y in re.findall(r'(?<!\d)((?:19|20)\d{2})(?!\d)', text or '')}),
            'periods': sorted(periods)}


def semantic_issues(body, sources):
    """只有明确数值、被引来源、唯一冲突口径同时具备才产生硬问题。"""
    from services.citations import REFERENCE, quantitative_tokens, split_sentences
    known = {s['source_id']: s for s in sources if s.get('source_id') and not s.get('source_deleted')}
    issues = []
    for n, paragraph in enumerate(re.split(r'\n\s*\n', body or ''), 1):
        paragraph_refs = set(REFERENCE.findall(paragraph))
        for j, sentence in enumerate(split_sentences(paragraph), 1):
            tokens = [t for t in quantitative_tokens(sentence) if not re.fullmatch(r'(?:19|20)\d{2}', t)]
            if not tokens:
                continue
            refs = set(REFERENCE.findall(sentence)) or (paragraph_refs if len(paragraph_refs) == 1 else set())
            supported = []
            for sid in sorted(refs & known.keys()):
                corpus = source_text(known[sid])
                if all(token_matches_text(t, corpus) for t in tokens):
                    supported.append((sid, corpus))
            claim_period = period_context(sentence)
            claim_period.update(geography=geography_context(sentence),
                                basis=sorted(set(re.findall(r'同比|环比', sentence))))
            conflicts, compatible = [], False
            for sid, corpus in supported:
                passages = [p for p in split_sentences(corpus) if all(token_matches_text(t, p) for t in tokens)]
                context = period_context(passages[0]) if len(passages) == 1 else {'years': [], 'periods': []}
                global_context = period_context(corpus)
                context.update(geography=geography_context(passages[0]) if len(passages) == 1 else [],
                               basis=sorted(set(re.findall(r'同比|环比', passages[0]))) if len(passages) == 1 else [])
                global_context.update(geography=geography_context(corpus),
                                      basis=sorted(set(re.findall(r'同比|环比', corpus))))
                for key in ('years', 'periods', 'geography', 'basis'):
                    if not context[key] and len(global_context[key]) == 1:
                        context[key] = global_context[key]
                mismatches = [key for key in ('years', 'periods', 'geography', 'basis') if len(claim_period[key]) == 1
                              and len(context[key]) == 1 and claim_period[key] != context[key]]
                if mismatches:
                    conflicts.append((sid, context, passages[0] if passages else corpus, mismatches))
                else:
                    compatible = True
            if conflicts and not compatible:
                sid, context, quote, mismatches = conflicts[0]
                kinds = set()
                if set(mismatches) & {'years', 'periods'}:
                    kinds.add('period_mismatch')
                if 'basis' in mismatches:
                    kinds.add('comparison_basis_mismatch')
                if 'geography' in mismatches:
                    kinds.add('geography_mismatch')
                for kind in sorted(kinds):
                    issues.append({'kind': kind, 'severity': 'hard', 'paragraph': n,
                        'sentence_index': j, 'sentence': sentence.strip(), 'numbers': tokens,
                        'source_ids': [sid], 'quote': quote,
                        'reason': f'声明口径{claim_period}与来源明确口径{context}不一致',
                        'expected': '按来源原文核对年份、季度、地区及同比/环比；不同口径不得替代，无法支持时删除声明或注明缺口'})
            for match in re.finditer(r'(\d+(?:\.\d+)?)\s*个?百分点', sentence):
                value = match.group(1)
                source_refs = [(sid, source_text(known[sid])) for sid in sorted(refs & known.keys())]
                if any(re.search(re.escape(value) + r'\s*个?百分点', corpus) for _, corpus in source_refs):
                    continue
                evidence = [(sid, p) for sid, corpus in source_refs for p in split_sentences(corpus)
                            if re.search(r'(?:同比|环比|增长|下降|下滑|下跌).{0,12}?' + re.escape(value) + r'\s*[%％]', p)]
                if evidence:
                    sid, quote = evidence[0]
                    issues.append({'kind': 'percentage_point_mismatch', 'severity': 'hard', 'paragraph': n,
                        'sentence_index': j, 'sentence': sentence.strip(), 'numbers': [value + '个百分点'],
                        'source_ids': [sid], 'quote': quote,
                        'reason': '来源给出百分比增速，正文改成了百分点变化',
                        'expected': '保留来源的同比/环比百分比表述；只有来源明确给出或完整基数支持时才能写百分点'})
    return issues
