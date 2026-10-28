"""评测指标计算。

评估指标体系（指标阈值为目标值，非实测成绩）：

| 维度   | 指标               | 计算方式                | 目标值    |
| ---- | ---------------- | -------------------- | ------ |
| 任务完成 | success_rate      | 质量通过任务数 / 总任务数      | v1/v2 兼容目标 ≥ 90% |
| 内容质量 | normal_quality_pass_rate | 非故障用例质量通过数 / 非故障用例数 | ≥ 90%  |
| 容错   | fault_handling_rate | 故障用例正确处理数 / 故障用例数  | = 100% |
| 协同效率 | avg_duration_seconds | 执行完成用例耗时均值          | ≤ 120s |
| 协同效率 | avg_react_loops   | 总 Action 次数 / 任务数     | ≤ 8 次  |
| 内容质量 | keyword_coverage_rate | 关键词命中数 / 总关键词数（平均） | ≥ 70%  |
| 记忆效用 | memory_hit_rate   | 有记忆命中的任务数 / 总任务数    | ≥ 30%  |

说明：v3 的 20 例协议集固定包含 4 个故障用例，整体质量通过率上限为 80%，
因此 90% 目标只适用于非故障用例；整体通过率保留为参考，不参与达标判定。

本模块负责纯计算（无 IO、无依赖），便于单测；
runner 负责批量执行并收集每个用例的原始数据，最终调用本模块汇总。
"""
import math
from datetime import datetime
from typing import Iterable

# 目标值（达标判定依据；前端评测中心会展示“实测值 vs 目标值”）
TARGETS = {
    "success_rate": 0.90,
    "avg_duration_seconds": 120.0,
    "avg_react_loops": 8.0,
    "keyword_coverage_rate": 0.70,
    "memory_hit_rate": 0.30,
}

# v3 分组目标：整体质量通过率含故障用例，不再套用 90%；分组指标单独判定。
GROUP_TARGETS = {
    "normal_quality_pass_rate": 0.90,
    "fault_handling_rate": 1.0,
}

# 指标目标值的展示文案
TARGET_LABELS = {
    "success_rate": "质量通过率",
    "normal_quality_pass_rate": "正常用例质量通过率（非故障）",
    "fault_handling_rate": "故障处理正确率",
    "avg_duration_seconds": "平均执行耗时（秒）",
    "avg_react_loops": "平均 ReAct 循环次数",
    "keyword_coverage_rate": "关键词覆盖率",
    "memory_hit_rate": "记忆命中率（长期记忆被引用）",
}

# 指标所属维度（Markdown 报告展示）
TARGET_DIMENSIONS = {
    "success_rate": "任务完成",
    "normal_quality_pass_rate": "内容质量",
    "fault_handling_rate": "容错",
    "avg_duration_seconds": "协同效率",
    "avg_react_loops": "协同效率",
    "keyword_coverage_rate": "内容质量",
    "memory_hit_rate": "记忆效用",
}


def compute_keyword_coverage(text: str, keywords: Iterable[str]) -> float:
    """单个用例的关键词覆盖率：命中关键词数 / 总关键词数（0~1）。

    实现说明：这是最朴素的“关键词命中”——按子串匹配报告全文
    （大小写不敏感），用于衡量报告是否覆盖了用户关心的要点。
    """
    keywords = [k for k in keywords if k]
    if not keywords:
        return 0.0
    lowered = (text or "").lower()
    hit = sum(1 for k in keywords if k.lower() in lowered)
    return round(hit / len(keywords), 4)


def count_react_actions(logs: Iterable[dict]) -> int:
    """统计 ReAct 循环次数：= 该任务的全部 Action 类日志条数。

    实现说明：每次 Action 代表一次“思考 → 行动 → 观察”循环，
    task_logs 表里 log_type='Action' 的条数即循环次数（评测用）。
    """
    return sum(1 for log in logs or [] if log.get("log_type") == "Action")


def compute_p95(values: Iterable[float]) -> float:
    """P95 耗时：排序后取第 95 百分位（不足 20 个样本时放宽取最大）。

    实现说明：P95 比平均值更能反映“尾部慢任务”体验；
    小样本下退化为最大值，避免空洞的插值结果。
    """
    values = sorted(float(v) for v in values)
    if not values:
        return 0.0
    idx = max(0, math.ceil(len(values) * 0.95) - 1)
    return round(values[min(idx, len(values) - 1)], 2)


def _avg(values: Iterable[float]) -> float:
    values = list(values)
    return round(sum(values) / len(values), 2) if values else 0.0


def _rate(numerator: int, denominator: int) -> float:
    """比率（0~1，四舍五入 4 位）；分母为 0 时返回 0。"""
    return round(numerator / denominator, 4) if denominator else 0.0


def compute_metrics(results: list[dict]) -> dict:
    """汇总全部用例结果 → 5 项指标实测值 + 达标判定。

    :param results: runner 收集的用例结果列表，每个元素含：
        success(bool) / duration_seconds(float) / react_loops(int) /
        keyword_coverage(float) / memory_hits(int) / topic(str)
    :return: {summary, targets, met} —— summary 为实测值汇总，
        targets 为目标值，met 为逐项是否达标（前端雷达图与状态标签数据源）
    """
    total = len(results)
    success = [r for r in results if r.get("execution_success") is True and r.get("quality_passed") is True and not r.get("forced_output")]
    executed = [r for r in results if r.get("execution_success") is True]
    success_rate = _rate(len(success), total)
    # 平均耗时只统计执行完成用例（含质量未通过），避免失败路径的提前结束影响结果。
    durations = [r["duration_seconds"] for r in executed if r.get("duration_seconds") is not None]
    avg_duration = _avg(durations)
    p95_duration = compute_p95(durations)
    loops = _avg([r.get("react_loops", 0) for r in results])
    coverage = _avg([r.get("keyword_coverage", 0.0) for r in results])
    memory_hit_rate = _rate(sum(1 for r in results if r.get("memory_hits", 0) > 0), total)
    summary = {
        "metric_version": 2,
        "execution_success_cases": len(executed),
        "execution_success_rate": _rate(len(executed), total),
        "total_cases": total,
        "success_cases": len(success),
        "failed_cases": total - len(success),
        "success_rate": success_rate,
        "avg_duration_seconds": avg_duration,
        "p95_duration_seconds": p95_duration,
        "avg_react_loops": loops,
        "keyword_coverage_rate": coverage,
        "memory_hit_rate": memory_hit_rate,
    }
    met = {
        "success_rate": success_rate >= TARGETS["success_rate"],
        "avg_duration_seconds": avg_duration <= TARGETS["avg_duration_seconds"],
        "avg_react_loops": loops <= TARGETS["avg_react_loops"],
        "keyword_coverage_rate": coverage >= TARGETS["keyword_coverage_rate"],
        "memory_hit_rate": memory_hit_rate >= TARGETS["memory_hit_rate"],
    }
    # 返回目标值副本：v3 归档会追加分组目标，不能污染 v1/v2 的全局口径。
    return {"summary": summary, "targets": dict(TARGETS), "met": met}


def build_markdown(eval_id: str, results: list[dict], metrics_data: dict) -> str:
    """生成 Markdown 可视化总结（含指标总览表 + 用例明细表）。

    评测报告以 JSON 落库（结构化数据源），同时生成人类可读的
    Markdown 版本 —— 前端评测中心可直接渲染或下载，替代早期
    规划的“独立 JSON 文件目录”方案。

    v1/v2 报告保持原五指标表；v3 增加正常用例质量通过率、故障处理
    正确率，并把整体质量通过率标为参考值，避免 90% 目标与固定故障
    用例互相矛盾。
    """
    summary = metrics_data["summary"]
    version = summary.get("metric_version") or 0
    fmt = {
        "success_rate": lambda v: f"{v * 100:.1f}%",
        "avg_duration_seconds": lambda v: f"{v:.1f}s",
        "avg_react_loops": lambda v: f"{v:.1f} 次",
        "keyword_coverage_rate": lambda v: f"{v * 100:.1f}%",
        "memory_hit_rate": lambda v: f"{v * 100:.1f}%",
    }

    def rate_text(value):
        return "未跑完" if value is None else f"{value * 100:.1f}%"

    def mark_text(met):
        return "—" if met is None else ("✅" if met else "❌")

    lines = [
        "# SmartBrief 自动化评测报告",
        "",
        f"- 评测 ID：`{eval_id}`",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 用例数：{summary['total_cases']}（整体质量通过 {summary['success_cases']} / 未通过或未知 {summary['failed_cases']}）",
        "",
        f"- 执行完成率：{summary['execution_success_rate'] * 100:.1f}%（独立于质量通过率）",
    ]
    normal_detail = fault_detail = None
    if version >= 3:
        normal_total = summary.get("normal_total_cases")
        normal_passed = summary.get("normal_passed_cases")
        fault_total = summary.get("fault_total_cases")
        fault_handled = summary.get("fault_handled_cases")
        false_pass = summary.get("fault_false_pass_cases")
        normal_detail = f"{normal_passed}/{normal_total}" if normal_total is not None else "未跑完或旧版"
        fault_detail = f"{fault_handled}/{fault_total}" if fault_total is not None else "未跑完或旧版"
        normal_suffix = f"（{normal_detail} 例）" if normal_total is not None else "（未跑完或旧版记录）"
        fault_suffix = f"（{fault_detail} 例；误放行 {false_pass if false_pass is not None else '未知'} 例）" if fault_total is not None else "（未跑完或旧版记录）"
        lines += [
            f"- 正常用例质量通过率：{rate_text(summary.get('normal_quality_pass_rate'))}{normal_suffix}",
            f"- 故障处理正确率：{rate_text(summary.get('fault_handling_rate'))}{fault_suffix}",
        ]
    lines += [
        "",
        "## 一、指标总览",
        "",
        "| 维度 | 指标 | 实测值 | 目标值 | 达标 |",
        "| --- | --- | --- | --- | --- |",
    ]
    if version >= 3:
        rows = [
            ("任务完成", "执行完成率", f"{summary['execution_success_rate'] * 100:.1f}%", "仅展示", None),
            ("内容质量", f"正常用例质量通过率（{normal_detail}）", rate_text(summary.get("normal_quality_pass_rate")),
             f"{GROUP_TARGETS['normal_quality_pass_rate'] * 100:.1f}%", metrics_data["met"].get("normal_quality_pass_rate")),
            ("容错", f"故障处理正确率（{fault_detail}）", rate_text(summary.get("fault_handling_rate")),
             f"{GROUP_TARGETS['fault_handling_rate'] * 100:.1f}%", metrics_data["met"].get("fault_handling_rate")),
            ("内容质量", "整体质量通过率（含故障用例，仅供参考）", f"{summary['success_rate'] * 100:.1f}%", "—", None),
        ]
        for key in ("avg_duration_seconds", "avg_react_loops", "keyword_coverage_rate", "memory_hit_rate"):
            rows.append((
                TARGET_DIMENSIONS[key], TARGET_LABELS[key], fmt[key](summary[key]),
                fmt[key](TARGETS[key]), metrics_data["met"].get(key),
            ))
        for dimension, label, value, target, met in rows:
            lines.append(f"| {dimension} | {label} | {value} | {target} | {mark_text(met)} |")
    else:
        for key in TARGETS:
            target = TARGETS[key]
            met = metrics_data["met"][key]
            lines.append(
                f"| {TARGET_DIMENSIONS[key]} | {TARGET_LABELS[key]} | {fmt[key](summary[key])} | "
                f"{fmt[key](target)} | {'✅' if met else '❌'} |"
            )

    case_header = "| # | 主题 | 状态 | 耗时(s) | ReAct次数 | 关键词覆盖率 | 记忆命中 |"
    case_sep = "| --- | --- | --- | --- | --- | --- | --- |"
    if version >= 3:
        case_header += " 失败分类 |"
        case_sep += " --- |"
    lines += ["", "## 二、用例明细", "", case_header, case_sep]
    for r in results:
        line = (
            f"| {r.get('case_index', '-')} | {r.get('topic', '-')} | "
            f"{'✅ 质量通过' if r.get('execution_success') is True and r.get('quality_passed') is True and not r.get('forced_output') else '❌ 质量未通过或未知'} | "
            f"{r.get('duration_seconds', '-')} | {r.get('react_loops', '-')} | "
            f"{r.get('keyword_coverage', 0) * 100:.1f}% | {r.get('memory_hits', 0)} |"
        )
        if version >= 3:
            line += f" {r.get('failure_category') or '—'} |"
        lines.append(line)
    scope = '合成协议结果不代表在线效果；人工事实抽查pending。' if version >= 3 else '默认单用例仅用于冒烟验证。'
    lines += ["", f"> 本轮执行 {summary['total_cases']} 个用例，指标口径见 `evaluation/metrics.py` 与v3 `evaluation/quality.py`。{scope}", ""]
    return "\n".join(lines)
