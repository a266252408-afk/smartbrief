"""冒烟评测：逐个执行 TEST_CASES，采集指标、持久化进度并生成报告。

默认只有一个用例，用于验证链路，不代表跨行业质量评估。
扩展候选主题见 docs/evaluation.md；同一时刻仅允许一次评测。
"""
import asyncio
import logging
import time
from uuid import uuid4

from db import evaluation_repository
from db import task_repository
from evaluation import metrics
from graph import workflow

logger = logging.getLogger(__name__)

# 默认冒烟评测仅执行一个用例，验证工作流与指标采集链路。
TEST_CASES = [
    {"topic": "便携式储能电源 2026 北美市场", "keywords": ["市场规模", "增长率", "头部玩家", "用户痛点", "便携储能"]},
]


# 运行中的评测并发保护（同一时刻仅允许一次）
_running = False


def case_count() -> int:
    """标准用例总数（前端「评测中心」展示用）。"""
    return len(TEST_CASES)


async def _run_single_case(case: dict, case_index: int) -> dict:
    """执行单个用例：创建任务记录 → 跑完整工作流 → 收集原始数据。

    实现说明：与 `POST /api/v1/generate` 的提交逻辑完全一致
    （先落库 running 初始记录，再异步跑图），仅多了计时与采样。
    用例结果逐项收集：耗时 / 成功与否 / ReAct 循环次数（Action 日志数）/
    关键词覆盖率（报告全文子串匹配）/ 记忆命中数（memory_hits 长度）。
    """
    task_id = f"task_{uuid4().hex[:8]}"
    task_repository.create_task(task_id, case["topic"])
    started = time.monotonic()
    try:
        await workflow.run_task(task_id, case["topic"])
        duration = time.monotonic() - started
        task = task_repository.get_task(task_id)
        logs = task_repository.get_logs(task_id)
        report_text = (task or {}).get("final_report") or ""
        execution_success = bool(task and task.get("execution_status") == "completed")
        quality_passed = execution_success and task.get("quality_status") == "passed" and not task.get("forced_output")
        return {
            "case_index": case_index,
            "topic": case["topic"],
            "keywords": case["keywords"],
            "task_id": task_id,
            "success": quality_passed,
            "execution_success": execution_success,
            "quality_passed": quality_passed,
            "quality_status": (task or {}).get("quality_status", "unknown"),
            "forced_output": bool((task or {}).get("forced_output")),
            "failure_category": None if quality_passed else (task or {}).get("error_code") or "QUALITY_NOT_PASSED",
            "duration_seconds": round(duration, 2),
            "react_loops": metrics.count_react_actions(logs),
            "keyword_coverage": metrics.compute_keyword_coverage(report_text, case["keywords"]),
            "memory_hits": len((task or {}).get("memory_hits") or []),
            "forced_pass": bool((task or {}).get("forced_pass")),
            "error": None,
        }
    except Exception as exc:  # 单用例失败不中断整轮评测（失败也计入成功率分母）
        logger.exception("评测用例 %d 执行失败", case_index)
        return {
            "case_index": case_index,
            "topic": case["topic"],
            "keywords": case["keywords"],
            "task_id": task_id,
            "success": False,
            "execution_success": False,
            "quality_passed": False,
            "quality_status": "not_run",
            "forced_output": False,
            "failure_category": "TASK_EXECUTION_FAILED",
            "duration_seconds": None,
            "react_loops": 0,
            "keyword_coverage": 0.0,
            "memory_hits": 0,
            "forced_pass": False,
            "error": str(exc),
        }


def start_evaluation() -> str:
    """触发一次自动化评测（同步返回 eval_id，后台批量执行）。

    :raises RuntimeError: 已有评测在运行中（重复触发时返回 400）
    :return: eval_id（形如 eval_ab12cd34），前端立即用其轮询进度
    """
    global _running
    if _running:
        raise RuntimeError("评测已在运行中，请等待完成后再触发")
    _running = True
    eval_id = f"eval_{uuid4().hex[:8]}"
    evaluation_repository.create(eval_id, total_cases=len(TEST_CASES))
    asyncio.create_task(_execute(eval_id))  # 后台执行，接口立即返回
    logger.info("评测已启动：%s（共 %d 个用例）", eval_id, len(TEST_CASES))
    return eval_id


async def _execute(eval_id: str) -> None:
    """后台批量执行全部用例 → 计算指标 → 生成 Markdown → 落库（终态）。

    任何环节异常都落到 evaluation_reports.status=failed，
    绝不静默丢失（前端可提示重试）。
    """
    global _running
    try:
        results = []
        for index, case in enumerate(TEST_CASES, start=1):
            result = await _run_single_case(case, index)
            results.append(result)
            evaluation_repository.set_progress(eval_id, len(results))  # 进度逐条更新
        metrics_data = metrics.compute_metrics(results)
        report_md = metrics.build_markdown(eval_id, results, metrics_data)
        evaluation_repository.finish(eval_id, results, metrics_data, report_md)
        logger.info("评测完成：%s（成功率 %.1f%%）", eval_id, metrics_data["summary"]["success_rate"] * 100)
    except Exception as exc:
        logger.exception("评测 %s 中断", eval_id)
        evaluation_repository.fail(eval_id, str(exc))
    finally:
        _running = False  # 释放并发锁，允许下一次评测