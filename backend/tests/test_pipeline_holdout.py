"""未参与历史手表/冰箱规则修复的合成材料，核对新主题的允许与拒绝边界。"""
import pytest
from services.fact_binding import build_registry, render_bound_text
from tests.test_evidence import source


@pytest.mark.parametrize("original,wrong", [
    ("2024年上半年欧洲咖啡机销售额12.8亿元。", "2025年上半年欧洲咖啡机销售额12.8亿元"),
    ("2025年第三季度全球游戏主机出货量1280万台。", "2025年第三季度中国游戏主机出货量1280万台"),
])
def test_independent_material_preserves_scope(original, wrong):
    registry = build_registry("holdout", [source("holdout", excerpt=original)])
    key = next(iter(registry))
    valid = original[:-1] + f"[{key}]。"
    body, claims = render_bound_text(valid, "holdout", registry)
    assert "[S1]" in body and claims[0]["fact_ids"] == [key]
    with pytest.raises(ValueError, match="不一致"):
        render_bound_text(wrong + f"[{key}]。", "holdout", registry)


def test_same_source_comparison_exposes_unknown_scope_before_draft():
    from services.citations import check_citations
    evidence = [source("holdout", excerpt="咖啡机销售额12.8亿元。")]
    legacy = check_citations("holdout", "2026年中国咖啡机销售额12.8亿元[S1]。", evidence)
    assert legacy["valid"] and not any(i.get("severity") == "hard" for i in legacy["binding_issues"])
    registry = build_registry("holdout", evidence)
    key = next(iter(registry))
    with pytest.raises(ValueError):
        render_bound_text(f"2026年中国咖啡机销售额12.8亿元[{key}]。", "holdout", registry)
