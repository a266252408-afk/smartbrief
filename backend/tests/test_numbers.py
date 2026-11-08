"""数字归一与量级换算：复合中文单位、英文量级与确定性换算建议。"""
import pytest

from services.numbers import (
    format_count_value,
    parse_number_values,
    scale_conversion,
    token_matches_text,
)


@pytest.mark.parametrize("text,expected", [
    ("1.2万亿", 1.2e12),
    ("3.5千亿", 3.5e11),
    ("6百亿", 6e10),
    ("2十亿", 2e9),
    ("1.2千万", 1.2e7),
    ("3.5百万", 3.5e6),
    ("4十万", 4e5),
    ("1,451億", 1451e8),
    ("7.5億", 7.5e8),
])
def test_composite_chinese_units(text, expected):
    values = [value for value, kind, _ in parse_number_values(text) if kind == "count"]
    assert values == pytest.approx([expected])


@pytest.mark.parametrize("text,expected", [
    ("46.39 billion", 46.39e9),
    ("59.49 Bn", 59.49e9),
    ("74.98 billion", 74.98e9),
    ("161.46 Bn", 161.46e9),
    ("3 million", 3e6),
    ("2.5 Mn", 2.5e6),
    ("1.2 trillion", 1.2e12),
])
def test_english_magnitude_words(text, expected):
    values = [value for value, kind, _ in parse_number_values(text) if kind == "count"]
    assert values == pytest.approx([expected])


@pytest.mark.parametrize("value,text", [
    (46.39e9, "463.9亿"),
    (59.49e9, "594.9亿"),
    (161.46e9, "1614.6亿"),
    (46.39e6, "4639万"),
    (5.949e12, "5.949万亿"),
])
def test_format_count_value_uses_chinese_units(value, text):
    assert format_count_value(value) == text


def test_scale_conversion_billion_to_yi():
    hint = scale_conversion("46.39亿美元", "The market grows from USD 46.39 billion in 2026.")
    assert hint == {"expected": "463.9亿美元", "factor": 10, "english": "billion"}


def test_scale_conversion_bn_to_yi():
    hint = scale_conversion("59.49亿美元", "2026 市場規模 $ 59.49 Bn")
    assert hint == {"expected": "594.9亿美元", "factor": 10, "english": "bn"}


def test_scale_conversion_keeps_suffix_and_composite_unit():
    hint = scale_conversion("161.46亿美元", "2036 forecast USD 161.46 Bn")
    assert hint["expected"] == "1614.6亿美元"
    assert hint["factor"] == 10


@pytest.mark.parametrize("token,source_text", [
    # 数字并不完全相同，不能因为量级词相同就改。
    ("46.39亿美元", "USD 46.40 billion"),
    # 同一数字在来源里出现两个量级，无法判断该用哪个。
    ("46.39亿美元", "USD 46.39 billion and 46.39 million"),
    # 来源本身是中文单位，没有英文量级词，不做换算。
    ("544.4亿美元", "2025年市場規模為544.4億美元"),
    # 来源写法与草稿同值，factor=1，不改。
    ("463.9亿美元", "USD 46.39 billion"),
    # 带 W 的标签不是金额，避免生成“10亿W+”这类错误。
    ("1W+", "1 billion"),
])
def test_scale_conversion_refuses_unsafe_cases(token, source_text):
    assert scale_conversion(token, source_text) is None


def test_traditional_numerals_still_match_after_composite_units():
    assert token_matches_text("1451亿", "預計到2035年將超過1,451億美元")
    assert token_matches_text("544.4亿", "2025年電動自行車市場規模為544.4億美元")


def test_format_count_value_keeps_small_counts():
    assert format_count_value(123) == "123"


@pytest.mark.parametrize("token,source_text", [
    ("abc", "USD 46.39 billion"),
    # 来源量级比草稿小，属于缩小方向，本轮不自动改。
    ("46.39亿美元", "USD 46.39 million"),
    # 0 的换算结果与原文同值，不产生替换。
    ("0亿美元", "USD 0 billion"),
])
def test_scale_conversion_edge_cases(token, source_text):
    assert scale_conversion(token, source_text) is None
