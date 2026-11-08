"""按主题领域过滤 RAG 术语，避免把相邻领域术语注入 Writer。

嵌入检索只看语义相似度，容易把“电瓶车”任务召回 BEV/PHEV、光伏储能等相邻领域
术语。这里用人工维护的领域表做确定性硬过滤；未识别的术语按通用术语保留。
"""
import re

DOMAIN_PATTERNS = {
    "four_wheel_ev": re.compile(
        r"BEV|PHEV|纯电动|插电|新能源汽车|乘用车|整车|汽车|NOA|领航辅助|车桩比|充电桩", re.I),
    "two_wheel_ev": re.compile(r"电瓶车|电动自行车|电动两轮车|两轮电动车|两轮车|自行车|e-?bike|ebike", re.I),
    "pv_storage": re.compile(r"光伏|储能|PCS|EPC|平价上网|光储|LCOE|度电成本", re.I),
    "smartwatch": re.compile(r"智能手表|可穿戴|手环", re.I),
    "battery": re.compile(r"电池|电芯|BMS|能量密度|循环寿命|快充|换电|续航", re.I),
}
# 术语名 → 领域；未列出的术语视为通用术语，始终保留。
TERM_DOMAINS = {
    "BEV/PHEV": {"four_wheel_ev"},
    "NOA": {"four_wheel_ev"},
    "车桩比": {"four_wheel_ev"},
    "BMS": {"battery"},
    "PCS": {"pv_storage"},
    "电芯": {"battery"},
    "能量密度": {"battery"},
    "循环寿命": {"battery"},
    "快充": {"battery"},
    "换电": {"battery"},
    "续航": {"general"},
    "分布式光伏": {"pv_storage"},
    "EPC": {"pv_storage"},
    "平价上网": {"pv_storage"},
    "度电成本 LCOE": {"pv_storage"},
    "可穿戴设备": {"smartwatch"},
    "智能手表出货量": {"smartwatch"},
    "光储一体": {"pv_storage"},
}


def _term_name(text: str) -> str:
    return (text or "").split("：", 1)[0].strip()


def task_domains(task: str) -> set[str]:
    domains = {name for name, pattern in DOMAIN_PATTERNS.items() if pattern.search(task or "")}
    if domains & {"two_wheel_ev", "four_wheel_ev"}:
        domains.add("battery")
    if "pv_storage" in domains:
        domains.add("battery")
    return domains


def filter_terms_for_task(task: str, terms: list[str]) -> tuple[list[str], list[dict]]:
    """返回 (保留术语, 被剔除术语及原因)。

    任务没有识别出领域时不做过滤，保持原有嵌入阈值行为。
    """
    domains = task_domains(task)
    if not domains:
        return list(terms), []
    allowed = domains | {"general"}
    selected, dropped = [], []
    for term in terms:
        name = _term_name(term)
        term_domains = TERM_DOMAINS.get(name, {"general"})
        if term_domains & allowed:
            selected.append(term)
        else:
            dropped.append({"term": name, "reason": f"领域不相关：{sorted(term_domains)}"})
    return selected, dropped
