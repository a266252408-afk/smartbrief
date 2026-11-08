"""带限定词的百分比校验：同口径逐字匹配放行近似表述，不放宽相反关系。"""
from services.numeric_claims import claim_token_matches

CORPUS = ("有数据显示，2025年国内庭院花园整体市场规模突破2000亿元，"
          "近三年年均增速稳定在10％以上，预计2026年将接近2600亿元。")
CLAIM = ("相关测算显示，2025年国内庭院花园整体市场规模突破2000亿元，"
         "近三年年均增速稳定在10％以上，预计2026年将接近2600亿元。")


def test_approximate_percent_matches_same_relation_text():
    assert claim_token_matches("10％", CLAIM, CORPUS)


def test_approximate_percent_rejects_opposite_relation():
    corpus = CORPUS.replace("10％以上", "10％以下")
    assert not claim_token_matches("10％", CLAIM, corpus)


def test_approximate_percent_not_borrowed_from_unrelated_value():
    corpus = CORPUS.replace("10％以上", "8％以上")
    assert not claim_token_matches("10％", CLAIM, corpus)


def test_exact_percent_without_qualifier_still_matches():
    assert claim_token_matches("10％", "增长率为10％", "增长率为10％")
