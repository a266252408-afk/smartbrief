"""数字归一：把中文/英文计数与百分比转成可比较的值。

只用于结构校验与证据选择，不判断事实是否成立。
"""
import math
import re

_PERCENT_UNITS = {"%", "％", "percent", "per cent"}
_MULTIPLIERS = {
    "百": 1e2,
    "千": 1e3,
    "万": 1e4,
    "萬": 1e4,
    "十万": 1e5,
    "百万": 1e6,
    "千万": 1e7,
    "亿": 1e8,
    "億": 1e8,
    "十亿": 1e9,
    "百亿": 1e10,
    "千亿": 1e11,
    "万亿": 1e12,
    "k": 1e3,
    "w": 1e4,
    "l": 1.0,
    "million": 1e6,
    "mn": 1e6,
    "billion": 1e9,
    "bn": 1e9,
    "trillion": 1e12,
    "tn": 1e12,
}
# 复合单位必须排在单字单位前，否则“1.2万亿”会先匹配成“1.2万”。
_CN_MAGNITUDE = r"万亿|千亿|百亿|十亿|千万|百万|十万|亿|億|万|萬|千|百"
# K/W/L 只在后面不是字母时才算单位，避免把“5kW”“3kWh”当成“5千”。
_NUMBER = re.compile(
    r"(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    rf"(?:\s*({_CN_MAGNITUDE}|%|％|million|billion|trillion|bn|mn|tn|percent|per\s+cent"
    r"|台|元|美元|人|个|件|家|座|吨|公里|km|倍|次|名"
    r"|K(?![A-Za-z])|W(?![A-Za-z])|L(?![A-Za-z])))?",
    re.IGNORECASE,
)
# 只把“数字完全相同 + 英文量级词”当作可确定性换算的单位错误。
_ENGLISH_MAGNITUDE = re.compile(
    r"(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(billion|bn|million|mn|trillion|tn)\b",
    re.IGNORECASE,
)
_TOKEN_PARTS = re.compile(
    r"^(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    rf"(?P<cn>{_CN_MAGNITUDE})?\s*(?P<suffix>.*)$"
)
_CURRENCY_SUFFIX = re.compile(r"^(?:元|美元|欧元|日元|港元|人民币)")


def parse_number_values(text: str) -> list[tuple[float, str, str | None]]:
    """返回 (归一后的值, 类型, 原始单位)；类型为 percent 或 count。"""
    values: list[tuple[float, str, str | None]] = []
    for match in _NUMBER.finditer(text or ""):
        raw = match.group(1).replace(",", "")
        unit = match.group(2)
        value = float(raw)
        normalized_unit = (unit or "").lower()
        if normalized_unit in _PERCENT_UNITS:
            values.append((value, "percent", unit))
            continue
        value *= _MULTIPLIERS.get(normalized_unit, 1.0)
        values.append((value, "count", unit))
    return values


def numbers_match(left: tuple[float, str, str | None], right: tuple[float, str, str | None]) -> bool:
    if left[1] != right[1]:
        return False
    if left[1] == "percent":
        return abs(left[0] - right[0]) <= 0.05
    # 计数允许 1% 的舍入差，覆盖“1.43 亿”与“1.431 亿”这类同源改写。
    return math.isclose(left[0], right[0], rel_tol=0.01, abs_tol=0.5)


def token_matches_text(token: str, text: str) -> bool:
    token_values = parse_number_values(token)
    text_values = parse_number_values(text)
    return any(numbers_match(a, b) for a in token_values for b in text_values)


def split_number_token(token: str) -> tuple[str, str | None, str] | None:
    """把定量 token 拆成 (数值字符串, 中文量级, 后缀)，不改写数值。"""
    match = _TOKEN_PARTS.match((token or "").strip())
    if not match:
        return None
    return (match.group("number").replace(",", ""), match.group("cn"),
            (match.group("suffix") or "").strip())


def _trim_number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def format_count_value(value: float) -> str:
    """把归一后的计数写成中文量级，用于确定性单位换算后的替换文本。"""
    for unit, multiplier in (("万亿", 1e12), ("亿", 1e8), ("万", 1e4)):
        if abs(value) >= multiplier:
            return f"{_trim_number(value / multiplier)}{unit}"
    return _trim_number(value)


def scale_conversion(token: str, source_text: str) -> dict | None:
    """数字完全相同时，给出按来源英文量级换算后的中文写法。

    只在以下条件全部满足时返回建议：草稿数字与来源英文数字完全一致；来源中该数字
    只对应一个英文量级词；换算方向为放大；换算结果与原 token 不同。其余情况返回
    None，交给审核与返工处理，避免误改真实数值。
    """
    match = _TOKEN_PARTS.match((token or "").strip())
    if not match:
        return None
    raw = match.group("number").replace(",", "")
    cn_unit = match.group("cn")
    suffix = (match.group("suffix") or "").strip()
    # 没有中文量级时必须带币种后缀；“1W+”这类标签不参与换算。
    if not cn_unit and not _CURRENCY_SUFFIX.match(suffix):
        return None
    number = float(raw)

    magnitudes: dict[float, str] = {}
    for hit in _ENGLISH_MAGNITUDE.finditer(source_text or ""):
        source_number = float(hit.group(1).replace(",", ""))
        if not math.isclose(source_number, number, rel_tol=1e-12, abs_tol=0.0):
            continue
        word = hit.group(2).lower()
        magnitudes.setdefault(_MULTIPLIERS[word], word)
    if len(magnitudes) != 1:
        return None
    english_multiplier, english_word = next(iter(magnitudes.items()))
    cn_multiplier = _MULTIPLIERS.get((cn_unit or "").lower(), 1.0)
    factor = english_multiplier / cn_multiplier
    if factor <= 1 or math.isclose(factor, 1.0):
        return None

    expected_value = number * english_multiplier
    if any(
        numbers_match((value, "count", None), (expected_value, "count", None))
        for value, kind, _ in parse_number_values(token) if kind == "count"
    ):
        return None
    expected = format_count_value(expected_value) + suffix
    return {
        "expected": expected,
        "factor": int(factor) if float(factor).is_integer() else factor,
        "english": english_word,
    }
