"""评测模块测试：指标计算（metrics）+ 批量执行（runner）+ 评测仓储。
"""
import asyncio
from unittest.mock import patch

import pytest

from db import evaluation_repository, task_repository
from db.database import init_db
from evaluation import metrics, runner


# ---------------------------------------------------------------- metrics 纯计算


def test_compute_keyword_coverage():
    text = "2026 年便携储能市场规模持续增长，头部玩家集中度提升。"
    assert metrics.compute_keyword_coverage(text, ["市场规模", "增长", "头部玩家"]) == 1.0
    # 未命中的关键词不计入
    assert metrics.compute_keyword_coverage(text, ["市场规模", "不存在词"]) == 0.5
    # 大小写不敏感；空关键词列表返回 0
    assert metrics.compute_keyword_coverage("Hello World", ["hello"]) == 1.0
    assert metrics.compute_keyword_coverage("text", []) == 0.0
    # 空文本返回 0
    assert metrics.compute_keyword_coverage("", ["市场规模"]) == 0.0


def test_count_react_actions():
    logs = [
        {"log_type": "Thought", "content": "t"},
        {"log_type": "Action", "content": "a"},
        {"log_type": "Observation", "content": "o"},
        {"log_type": "Action", "content": "a2"},
    ]
    assert metrics.count_react_actions(logs) == 2
    assert metrics.count_react_actions([]) == 0


def test_compute_p95():
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 200]
    # 20 个样本的 95% 分位 = 排序后第 ceil(20*0.95)=19 位 → 下标 18 → 190
    assert metrics.compute_p95(values) == 190.0
    assert metrics.compute_p95([5, 7]) == 7.0    # 小样本退化为最大值
    assert metrics.compute_p95([7, 5]) == 7.0    # 排序后取最大
    assert metrics.compute_p95([]) == 0.0
    assert metrics.compute_p95([42]) == 42.0


def test_compute_metrics_and_targets():
    results = [
        {"success": True, "execution_success": True, "quality_passed": True, "duration_seconds": 50.0, "react_loops": 5, "keyword_coverage": 0.8, "memory_hits": 1},
        {"success": True, "execution_success": True, "quality_passed": True, "duration_seconds": 70.0, "react_loops": 4, "keyword_coverage": 0.6, "memory_hits": 0},
        {"success": False, "execution_success": False, "quality_passed": False, "duration_seconds": None, "react_loops": 0, "keyword_coverage": 0.0, "memory_hits": 0},
    ]
    data = metrics.compute_metrics(results)
    summary = data["summary"]
    assert summary["total_cases"] == 3
    assert summary["success_cases"] == 2
    assert summary["failed_cases"] == 1
    assert summary["success_rate"] == pytest.approx(2 / 3, abs=1e-3)  # 4 位小数保留
    # 平均耗时只统计成功任务
    assert summary["avg_duration_seconds"] == 60.0
    assert summary["avg_react_loops"] == pytest.approx(3.0)
    # 平均覆盖率按 2 位小数保留（round(1.4/3, 2) = 0.47）
    assert summary["keyword_coverage_rate"] == pytest.approx(0.47, abs=1e-3)
    assert summary["memory_hit_rate"] == pytest.approx(1 / 3, abs=1e-3)  # 4 位小数保留
    # 达标判定逐项给出
    assert data["targets"]["success_rate"] == 0.90
    assert data["targets"] is not metrics.TARGETS  # v3 追加分组目标时不污染全局口径
    assert data["met"]["avg_duration_seconds"] is True
    assert data["met"]["success_rate"] is False


def test_build_markdown_contains_tables():
    results = [
        {"case_index": 1, "topic": "便携储能", "success": True, "duration_seconds": 55.0,
         "react_loops": 4, "keyword_coverage": 0.8, "memory_hits": 1},
        {"case_index": 2, "topic": "智能手表", "success": False, "duration_seconds": None,
         "react_loops": 0, "keyword_coverage": 0.0, "memory_hits": 0},
    ]
    data = metrics.compute_metrics(results)
    md = metrics.build_markdown("eval_test_1", results, data)
    assert md.startswith("# SmartBrief 自动化评测报告")
    assert "| 维度 | 指标 | 实测值 | 目标值 | 达标 |" in md
    assert "| # | 主题 | 状态 | 耗时(s) | ReAct次数 | 关键词覆盖率 | 记忆命中 |" in md
    assert "✅" in md and "❌" in md
    assert "本轮执行 2 个用例" in md


def test_build_markdown_v3_shows_group_metrics_and_failure_category():
    results = [
        {"case_index": 1, "topic": "常规", "category": "regular", "scenario": "normal",
         "execution_success": True, "quality_passed": True, "forced_output": False,
         "failure_category": None, "duration_seconds": 1.0, "react_loops": 3,
         "keyword_coverage": 1.0, "memory_hits": 1},
        {"case_index": 2, "topic": "故障", "category": "fault", "scenario": "budget",
         "execution_success": False, "quality_passed": False, "forced_output": False,
         "failure_category": "BUDGET_EXCEEDED", "duration_seconds": 0.5, "react_loops": 0,
         "keyword_coverage": 0.0, "memory_hits": 0},
    ]
    data = metrics.compute_metrics(results)
    data["summary"].update(
        metric_version=3, normal_total_cases=1, normal_passed_cases=1,
        normal_quality_pass_rate=1.0, fault_total_cases=1, fault_handled_cases=1,
        fault_false_pass_cases=0, fault_handling_rate=1.0,
    )
    data["targets"].update(metrics.GROUP_TARGETS)
    data["met"].update(normal_quality_pass_rate=True, fault_handling_rate=True)
    md = metrics.build_markdown("eval_v3", results, data)
    assert "正常用例质量通过率" in md and "故障处理正确率" in md
    assert "整体质量通过率（含故障用例，仅供参考）" in md
    assert "失败分类" in md and "BUDGET_EXCEEDED" in md


# ---------------------------------------------------------------- evaluation_repository


@pytest.fixture
def db(tmp_path, monkeypatch):
    """评测仓储测试：SQLite 库文件隔离到 tmp_path。"""
    monkeypatch.setattr("db.database.DB_PATH", tmp_path / "eval_test.db")
    init_db(tmp_path / "eval_test.db")
    yield


def test_evaluation_repository_lifecycle(db):
    evaluation_repository.create("eval_ab12", 2)
    record = evaluation_repository.get("eval_ab12")
    assert record["status"] == "running"
    assert record["total_cases"] == 2
    assert record["completed_cases"] == 0
    assert record["results"] == []

    evaluation_repository.set_progress("eval_ab12", 1)
    assert evaluation_repository.get("eval_ab12")["completed_cases"] == 1

    results = [{"topic": "a", "success": True}, {"topic": "b", "success": False}]
    data = metrics.compute_metrics(results)
    evaluation_repository.finish("eval_ab12", results, data, "# 评测完成")
    record = evaluation_repository.get("eval_ab12")
    assert record["status"] == "completed"
    assert len(record["results"]) == 2
    assert record["metrics"]["summary"]["total_cases"] == 2
    assert record["report_md"] == "# 评测完成"


def test_fail_running_interrupted(db):
    """后端启动清理：遗留 running 评测置为 failed（前端评测中心不卡进度条）。"""
    evaluation_repository.create("eval_1", 10)
    evaluation_repository.create("eval_2", 10)
    evaluation_repository.finish(
        "eval_2", [{"topic": "储能", "success": True}],
        {"summary": {"total_cases": 1}, "targets": {}, "met": {}}, "# 完成",
    )

    assert evaluation_repository.fail_running_interrupted() == 1  # 只有 eval_1 是 running
    record = evaluation_repository.get("eval_1")
    assert record["status"] == "failed"
    assert "服务中断" in record["error"]
    # 已完成的评测不受影响
    assert evaluation_repository.get("eval_2")["status"] == "completed"
    # 无残留 running 时返回 0（幂等）
    assert evaluation_repository.fail_running_interrupted() == 0


def test_evaluation_repository_delete(db):
    """评测仓储删除：整行移除（无子表），不存在返回 False。"""
    evaluation_repository.create("eval_del", 10)
    evaluation_repository.finish(
        "eval_del", [{"topic": "储能", "success": True}],
        {"summary": {"total_cases": 1}, "targets": {}, "met": {}}, "# 完成",
    )
    assert evaluation_repository.delete("eval_del") is True
    assert evaluation_repository.get("eval_del") is None
    assert evaluation_repository.delete("eval_del") is False  # 已删/不存在返回 False


def test_evaluation_repository_fail_and_list(db):
    evaluation_repository.create("eval_1", 10)
    evaluation_repository.create("eval_2", 10)
    evaluation_repository.fail("eval_2", "LLM 不可用")
    assert evaluation_repository.get("eval_2")["status"] == "failed"
    assert evaluation_repository.get("eval_2")["error"] == "LLM 不可用"

    items, total = evaluation_repository.list_reports(limit=10, offset=0)
    assert total == 2
    assert len(items) == 2
    # 列表项不携带大文本（results / report_md），详情单查
    assert "results" not in items[0]
    assert evaluation_repository.get("eval_none") is None


# ---------------------------------------------------------------- runner 批量执行


@pytest.fixture
def runner_db(tmp_path, monkeypatch):
    """runner 测试：SQLite 隔离 + workflow.run_task 打桩（不发真实 LLM）。"""
    monkeypatch.setattr("db.database.DB_PATH", tmp_path / "runner_test.db")
    init_db(tmp_path / "runner_test.db")
    yield


@pytest.fixture(autouse=True)
def reset_runner_lock():
    """每个用例跑完后释放 runner 的并发锁，避免跨用例污染。"""
    runner._running = False
    yield
    runner._running = False


def test_run_single_case_without_persisted_completion_is_not_success(runner_db):
    async def fake_run_task(task_id, topic, resume=False):
        pass  # 不写任何状态 → get_task 仍为 running，final_report 为空

    with patch.object(runner.workflow, "run_task", new=fake_run_task):
        result = asyncio.run(runner._run_single_case(runner.TEST_CASES[0], 1))
    assert result["success"] is False
    assert result["execution_success"] is False
    assert result["quality_passed"] is False
    assert result["case_index"] == 1
    assert result["topic"] == runner.TEST_CASES[0]["topic"]
    assert isinstance(result["duration_seconds"], float)
    assert result["react_loops"] == 0
    assert result["keyword_coverage"] == 0.0


def test_run_single_case_failure_records_error(runner_db):
    async def boom(task_id, topic, resume=False):
        raise RuntimeError("模拟 LLM 故障")

    with patch.object(runner.workflow, "run_task", new=boom):
        result = asyncio.run(runner._run_single_case(runner.TEST_CASES[0], 1))
    assert result["success"] is False
    assert result["error"] == "任务执行失败"
    assert result["failure_category"] == "TASK_EXECUTION_FAILED"
    assert "模拟 LLM 故障" not in result["error"]


def test_start_evaluation_and_execute(runner_db):
    """start_evaluation 立即返回 eval_id 并落库 running 记录；后台执行完成后落终态。

    start_evaluation 内部用 asyncio.create_task 调度后台执行，
    必须在同一事件循环里等待其落库 —— 故整段场景包在一个 asyncio.run 中。
    """

    async def fake_workflow(task_id, topic, resume=False):
        task_repository.update_task(
            task_id, status="completed", quality_status="passed", final_report="# 市场报告\n市场规模 2026 年增长 12%",
            memory_hits=[],
        )
        for i in range(3):
            task_repository.append_log(task_id, 1, "Action", f"web_search({i + 1})")

    async def scenario():
        with patch.object(runner.workflow, "run_task", new=fake_workflow):
            eval_id = runner.start_evaluation()
            assert eval_id.startswith("eval_")
            record = evaluation_repository.get(eval_id)
            assert record["status"] == "running"  # 触发后立即为 running（可被前端轮询）
            # 等待后台任务完成（最长 5 秒）
            await _wait_until(
                lambda: evaluation_repository.get(eval_id)["status"] == "completed",
                timeout=5,
            )
        return evaluation_repository.get(eval_id)

    record = asyncio.run(scenario())
    assert record["status"] == "completed"
    assert record["total_cases"] == len(runner.TEST_CASES)
    assert record["completed_cases"] == len(runner.TEST_CASES)
    assert len(record["results"]) == len(runner.TEST_CASES)
    assert record["results"][0]["success"] is True
    assert record["results"][0]["react_loops"] == 3
    assert record["results"][0]["keyword_coverage"] > 0.0  # “市场规模”命中
    assert "自动化评测报告" in record["report_md"]


def test_start_evaluation_rejects_concurrent_run(runner_db):
    runner._running = True
    with pytest.raises(RuntimeError, match="已在运行中"):
        runner.start_evaluation()


def test_exception_marks_report_failed(runner_db):
    """整体执行异常 → evaluation_reports.status=failed（不静默丢失）。

    注意：单用例异常会被 _run_single_case 捕获（计入失败用例，评测仍可完成），
    只有整体执行层抛错（如进度落库失败）才会走到 failed 终态 ——
    这里打桩让 set_progress 抛错来触发该路径。
    """

    async def fake_workflow(task_id, topic, resume=False):
        pass  # 用例快速“成功”，避免真实 LLM

    def boom_progress(eval_id, completed_cases):
        raise RuntimeError("进度落库失败：评测中断")

    async def scenario():
        with (
            patch.object(runner.workflow, "run_task", new=fake_workflow),
            patch.object(runner.evaluation_repository, "set_progress", new=boom_progress),
        ):
            eval_id = runner.start_evaluation()
            await _wait_until(
                lambda: evaluation_repository.get(eval_id)["status"] == "failed",
                timeout=5,
            )
        return evaluation_repository.get(eval_id)

    record = asyncio.run(scenario())
    assert record["status"] == "failed"
    assert "评测中断" in record["error"]


async def _wait_until(predicate, timeout: float = 5.0):
    """轮询等待后台任务落库（评测在 asyncio.create_task 中执行）。"""
    waited = 0.0
    while not predicate():
        if waited >= timeout:
            raise AssertionError("等待超时：后台评测未在限定时间内落库")
        await asyncio.sleep(0.05)
        waited += 0.05
