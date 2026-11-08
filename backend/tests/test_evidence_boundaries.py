"""证据预算按完整句段取舍，不能割裂数值和单位。"""
from services.evidence_excerpt import select_excerpt
from services.exa_search import _snippet
from services.evidence import normalize_evidence


def test_percentage_crosses_target():
    text = "说明。" * 160 + "苹果同比增长40.9%。" + "后续。" * 100
    assert "40.9%" in select_excerpt(text, 490)
    assert _snippet({"text": text}).endswith("。")


def test_decimal_english_and_paragraph():
    text = "Market share was 17.3%, with revenue of 1,271 million.\n\nNext paragraph."
    assert select_excerpt(text, 20).endswith("million.")
    assert select_excerpt("第一段。\n\n第二段增长40.9%。", 4) == "第一段。"


def test_unsplittable_fragment_over_limit():
    assert select_excerpt("字" * 4001, 500) == ""
    assert select_excerpt("字" * 800, 500) == "字" * 800


def test_normalization_keeps_only_complete_sentences_at_storage_limit():
    text = "完整句。" * 998 + "长尾" * 100
    result = normalize_evidence("t", {"step": {"observations": [{"result": {"results": [
        {"title": "文章", "url": "https://example.invalid/a", "snippet": text}]}}]}})
    assert result[0]["excerpt"].endswith("。")
    assert len(result[0]["excerpt"]) <= 4000


def test_supplement_can_complete_same_url_without_renumbering_or_mutating_snapshot():
    def research(text):
        return {"step": {"observations": [{"result": {"results": [
            {"title": "文章", "url": "https://example.invalid/a", "snippet": text}]}}]}}
    original = normalize_evidence("t", research("出货660万台。同比增长40.9"))
    updated = normalize_evidence("t", research("出货660万台。同比增长40.9%。"), original)
    assert len(updated) == 1 and updated[0]["source_id"] == "S1"
    assert updated[0]["excerpt"].endswith("40.9%。")
    assert original[0]["excerpt"].endswith("40.9")


def test_malformed_provider_excerpt_is_skipped():
    assert normalize_evidence("t", {"step": {"observations": [{"result": {"results": [
        {"title": "文章", "url": "https://example.invalid/a", "snippet": None}]}}]}}) == []
