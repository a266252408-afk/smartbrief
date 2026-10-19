"""评测指标计算。

评估指标体系（指标阈值为目标值，非实测成绩）：

| 维度   | 指标               | 计算方式                | 目标值    |
| ---- | ---------------- | -------------------- | ------ |
| 任务完成 | success_rate      | 成功任务数 / 总任务数         | ≥ 90%  |
| 协同效率 | avg_duration_seconds | 总耗时 / 成功任务数         | ≤ 120s |
| 协同效率 | avg_react_loops   | 总 Action 次数 / 任务数     | ≤ 8 次  |
| 内容质量 | keyword_coverage_rate | 关键词命中数 / 总关键词数（平均） | ≥ 70%  |
| 记忆效用 | memory_hit_rate   | 有记忆命中的任务数 / 总任务数    | ≥ 30%  |

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

# 指标目标值的展示文案
TARGET_LABELS = {
    "success_rate": "任务成功率",
    "avg_duration_seconds": "平均执行耗时（秒）",
    "avg_react_loops": "平均 ReAct 循环次数",
    "keyword_coverage_rate": "关键词覆盖率",
    "memory_hit_rate": "记忆命中率（长期记忆被引用）",
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
    success = [r for r in results if r.get("success")]
    success_rate = _rate(len(success), total)
    # 平均耗时仅统计成功用例，避免失败路径的提前结束影响结果。
    durations = [r["duration_seconds"] for r in success if r.get("duration_seconds") is not None]
    avg_duration = _avg(durations)
    p95_duration = compute_p95(durations)
    loops = _avg([r.get("react_loops", 0) for r in results])
    coverage = _avg([r.get("keyword_coverage", 0.0) for r in results])
    memory_hit_rate = _rate(sum(1 for r in results if r.get("memory_hits", 0) > 0), total)
    summary = {
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
    return {"summary": summary, "targets": TARGETS, "met": met}


def build_markdown(eval_id: str, results: list[dict], metrics_data: dict) -> str:
    """生成 Markdown 可视化总结（含指标总览表 + 用例明细表）。

    评测报告以 JSON 落库（结构化数据源），同时生成人类可读的
    Markdown 版本 —— 前端评测中心可直接渲染或下载，替代早期
    规划的“独立 JSON 文件目录”方案。
    """
    summary = metrics_data["summary"]
    lines = [
        "# SmartBrief 自动化评测报告",
        "",
        f"- 评测 ID：`{eval_id}`",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 用例数：{summary['total_cases']}（成功 {summary['success_cases']} / 失败 {summary['failed_cases']}）",
        "",
        "## 一、指标总览",
        "",
        "| 维度 | 指标 | 实测值 | 目标值 | 达标 |",
        "| --- | --- | --- | --- | --- |",
    ]
    fmt = {
        "success_rate": lambda v: f"{v * 100:.1f}%",
        "avg_duration_seconds": lambda v: f"{v:.1f}s",
        "avg_react_loops": lambda v: f"{v:.1f} 次",
        "keyword_coverage_rate": lambda v: f"{v * 100:.1f}%",
        "memory_hit_rate": lambda v: f"{v * 100:.1f}%",
    }
    for key in TARGETS:
        target = TARGETS[key]
        met = metrics_data["met"][key]
        lines.append(
            f"| 任务完成 | {TARGET_LABELS[key]} | {fmt[key](summary[key])} | "
            f"{fmt[key](target)} | {'✅' if met else '❌'} |"
        )
    lines += ["", "## 二、用例明细", "", "| # | 主题 | 状态 | 耗时(s) | ReAct次数 | 关键词覆盖率 | 记忆命中 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in results:
        lines.append(
            f"| {r.get('case_index', '-')} | {r.get('topic', '-')} | "
            f"{'✅ 成功' if r.get('success') else '❌ 失败'} | "
            f"{r.get('duration_seconds', '-')} | {r.get('react_loops', '-')} | "
            f"{r.get('keyword_coverage', 0) * 100:.1f}% | {r.get('memory_hits', 0)} |"
        )
    lines += ["", f"> 本轮执行 {summary['total_cases']} 个用例，指标口径见 `evaluation/metrics.py`。默认单用例仅用于冒烟验证。", ""]
    return "\n".join(lines)
