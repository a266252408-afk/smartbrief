"""带限定词的百分比关系验证；不放宽普通百分比的相等容差。"""
import re

from services.numbers import token_matches_text

_PERCENT = re.compile(r'(\d+(?:\.\d+)?)\s*[%％]')
_METRICS = {'health': r'健康|心率|血氧', 'sport': r'运动', 'entertainment': r'娱乐',
            'work': r'工作', 'sales': r'销量|出货', 'revenue': r'收入|销售额', 'share': r'份额|市占'}


def _metrics(text):
    return {key for key, pattern in _METRICS.items() if re.search(pattern, text)}


def _relation(token, text):
    escaped = re.escape(token)
    for pattern, name in [(escaped + r'\s*以上|(?:至少|不低于)\s*' + escaped, 'ge'),
                          (escaped + r'\s*以下|(?:至多|不超过)\s*' + escaped, 'le'),
                          (r'(?:超过|高于)\s*' + escaped, 'gt'),
                          (r'(?:低于|少于)\s*' + escaped, 'lt')]:
        if re.search(pattern, text):
            return name
    return None


def claim_token_matches(token, claim, corpus):
    """只在明确的同指标、兼容口径中确认数值上下界；模糊情况仍拒绝。"""
    match = _PERCENT.fullmatch(token.strip())
    if not match:
        return token_matches_text(token, corpus)
    relation = _relation(token, claim)
    if relation is None:
        return token_matches_text(token, corpus)
    from services.claim_semantics import geography_context, object_context, period_context
    # 有明确年份/地区/对象时不能借另一个口径的数值证明上下界。
    a, b = period_context(claim), period_context(corpus)
    for key in ('years', 'periods'):
        if a[key] and not set(a[key]).issubset(b[key]):
            return False
    if geography_context(claim) and not set(geography_context(claim)).issubset(geography_context(corpus)):
        return False
    if object_context(claim) and not set(object_context(claim)).issubset(object_context(corpus)):
        return False
    # 原文与声明使用同一种近似表述（如“近三年…10％以上”）时属于逐字匹配，
    # 不依赖指标分类，也不能因为“近/约”把来源自身排除。
    for clause in re.split(r'[。；;\n]', corpus):
        if _relation(token, clause) == relation and token_matches_text(token, clause):
            return True
    metric = _metrics(claim)
    if not metric:
        return False
    value = float(match[1])
    for passage, clause in [(p,c) for p in re.split(r'[。；;\n]', corpus) for c in re.split(r'[，,]', p)]:
        # 一个分句含多个百分比时无法确定指标归属，保守拒绝。
        numbers = list(_PERCENT.finditer(clause))
        if len(numbers) != 1 or not (metric & _metrics(clause)):
            continue
        context = period_context(passage)
        if any(a[key] and context[key] and not set(a[key]).issubset(context[key]) for key in ('years', 'periods')):
            continue
        geography = geography_context(passage)
        if geography and geography_context(claim) and not set(geography_context(claim)).issubset(geography):
            continue
        population = object_context(passage)
        if population and object_context(claim) and not set(object_context(claim)).issubset(population):
            continue
        number = numbers[0]
        source_relation = _relation(number[0], clause)
        # 原文与声明使用同一种近似表述（如“近三年…10％以上”）时属于逐字匹配，
        # 不能因为“近/约”把来源自身也排除。
        if source_relation == relation and token_matches_text(token, clause):
            return True
        surrounding = clause[max(0, number.start()-4):number.start()] + clause[number.end():number.end()+2]
        if re.search(r'近|约|不足|不到', surrounding):
            # 近似值不能当作精确实绩推导严格边界。
            continue
        other = float(numbers[0][1])
        if source_relation:
            if source_relation[0] != relation[0]:
                continue
            if other == value and source_relation.endswith('e') and relation.endswith('t'):
                continue
        if {'ge':other >= value, 'le':other <= value, 'gt':other > value, 'lt':other < value}[relation]:
            return True
        if other == value and source_relation == relation:
            return True
    return False
