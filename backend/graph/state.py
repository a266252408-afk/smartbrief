"""LangGraph 多智能体工作流的共享状态定义。
"""
from typing import TypedDict, List, Dict, Optional
from services.task_outcome import ExecutionStatus, QualityStatus


class AgentState(TypedDict):
    """多智能体共享状态（"白板"）。

    所有 Agent 节点都读写这张"白板"：
    - 节点函数接收完整 state，返回**增量 dict**（只包含要修改的字段）
    - LangGraph 框架负责把增量合并回共享状态

    字段分六组：
    1. 任务信息：task_id / topic
    2. Plan 进度：plan / current_step_index / completed_steps
    3. ReAct 日志：react_logs
    4. 产出数据：research_data / draft_content / final_report / error
    5. A2A 审核循环：review_comments / review_history / iteration / max_iterations / passed / forced_pass
    6. 长期记忆命中：memory_hits
    """
    # ---------- 1. 任务信息 ----------
    task_id: str              # 任务唯一 id（由 API 层生成，形如 task_ab12cd34）
    execution_status: ExecutionStatus
    quality_status: QualityStatus
    forced_output: bool
    error_code: Optional[str]
    topic: str                # 用户提交的研究主题（如 "智能手表 2025 市场"）

    # ---------- 2. Plan 进度 ----------
    plan: List[Dict]                # 步骤列表：[{"id":1, "agent":"researcher", "task":"...", "depends_on":[]}]
                                    # agent 取值：researcher（联网研究）/ writer（撰写章节）
    current_step_index: int         # 当前进度下标（= 已完成步骤数，供前端 Plan 看板定位）
    completed_steps: List[int]      # 已完成步骤 id 列表（如 [1, 2]）

    # ---------- 3. ReAct 日志 ----------
    react_logs: List[Dict]          # 日志流：[{"type":"Thought|Action|Observation", "content":"...", "step_id":1}]
                                    # 前端任务详情页按 step_id 分组展示，就是从这里来的

    # ---------- 4. 产出数据 ----------
    research_data: Dict             # 研究数据：{"step_1": {"observations": [...]}, "step_2": {...}}
    draft_content: str              # 报告草稿（writer 步骤生成的章节按序拼接；rewriter 修改后覆盖）
    final_report: Optional[str]     # 工作流结束时保存的最终稿，包含审核或强制输出结果
    error: Optional[str]            # 任务异常信息（失败时填充，供前端展示）

    # ---------- 5. A2A 审核循环----------
    review_comments: List[str]      # 最近一轮审核意见（通过时多为空数组；打回/强制轮为意见原文，历史全量见 review_history）
    review_history: List[Dict]      # A2A 各轮审核记录（[{round, passed, comments}]，按轮保留，供详情页分轮展示）
    iteration: int                  # 当前循环次数（0 起；每次打回 +1）
    max_iterations: int             # 循环上限（默认 3，超限强制通过）
    passed: Optional[bool]          # 最近一次审核是否通过（None 表示尚未审核）
    forced_pass: bool               # 是否因达循环上限被强制通过（供前端显示橙色警告）

    # ---------- 6. 长期记忆命中----------
    memory_hits: List[Dict]         # Planner 检索历史报告命中：[{"topic","snippet","score","task_id","created_at"}]
                                    # 前端详情页"记忆命中"卡片数据源；任务成功后随状态落库
