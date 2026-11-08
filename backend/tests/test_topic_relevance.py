"""RAG 术语主题领域过滤：电瓶车任务不再注入 BEV/PHEV 等相邻领域术语。"""
from services.topic_relevance import filter_terms_for_task, task_domains

TERMS = [
    "YoY：同比增长率",
    "BMS：电池管理系统",
    "换电：更换整块电池包",
    "BEV/PHEV：纯电动汽车/插电式混合动力汽车",
    "NOA：领航辅助驾驶",
    "PCS：储能变流器",
    "分布式光伏：用户侧就近发电",
    "可穿戴设备：智能手表、手环",
    "续航：单次满电可行驶里程",
    "智能手表出货量：智能手表品类出货总量",
]


def names(terms):
    return [term.split("：", 1)[0] for term in terms]


def test_ebike_task_drops_four_wheel_and_pv_terms():
    selected, dropped = filter_terms_for_task("2026电瓶车的市场调研", TERMS)
    kept = names(selected)
    assert {"YoY", "BMS", "换电", "续航"} <= set(kept)
    assert not {"BEV/PHEV", "NOA", "PCS", "分布式光伏", "可穿戴设备"} & set(kept)
    assert {"BEV/PHEV", "NOA", "PCS", "分布式光伏", "可穿戴设备"} <= {item["term"] for item in dropped}


def test_four_wheel_task_keeps_ev_and_battery_terms():
    selected, _ = filter_terms_for_task("2026新能源汽车市场", TERMS)
    kept = names(selected)
    assert {"BEV/PHEV", "NOA", "BMS"} <= set(kept)
    assert "PCS" not in kept and "可穿戴设备" not in kept


def test_smartwatch_task_keeps_wearable_and_drops_powertrain():
    selected, _ = filter_terms_for_task("2026智能手表市场", TERMS)
    kept = names(selected)
    assert {"可穿戴设备", "智能手表出货量", "续航"} <= set(kept)
    assert not {"BMS", "PCS", "BEV/PHEV"} & set(kept)


def test_unknown_task_does_not_filter():
    selected, dropped = filter_terms_for_task("2026智能冰箱市场", TERMS)
    assert selected == TERMS
    assert dropped == []


def test_empty_task_and_terms():
    assert filter_terms_for_task("", ["YoY：同比增长率"]) == (["YoY：同比增长率"], [])
    assert filter_terms_for_task("电瓶车", []) == ([], [])


def test_task_domains_ev_includes_battery():
    domains = task_domains("2026电瓶车市场")
    assert "two_wheel_ev" in domains
    assert "battery" in domains
    assert "four_wheel_ev" not in domains
