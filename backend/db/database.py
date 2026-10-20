"""SQLite 连接管理与建表（任务业务数据持久化）。

职责边界：本模块负责**业务数据**（任务 / ReAct 日志 / 评测报告）的持久化；
LangGraph 的**图运行时状态**（Checkpointer）由独立库文件
`checkpoints.db` 承载（见 memory/checkpointer.py）—— 两者职责分离、互不干扰：
- data.db        存"任务最终长什么样"（tasks / task_logs / evaluation_reports，业务层可查）
- checkpoints.db 存"图执行到哪一步了"（节点级快照，仅框架层可读）

`evaluation_reports` 表：自动化评测的执行进度与最终报告
（JSON 指标 + Markdown 可视化总结），供前端「评测中心」查询历史。
"""
import sqlite3
from pathlib import Path

from db.quality_migration import migrate_quality

# 业务 SQLite 库文件（backend/data.db）
DB_PATH = Path(__file__).resolve().parents[1] / "data.db"

# 建表 SQL（幂等：IF NOT EXISTS 保证重复执行不报错）
_SCHEMA = """
-- T06记忆权威登记；旧向量保持未验证，不回填为可信。
CREATE TABLE IF NOT EXISTS memory_entries (
    memory_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

-- 任务主表：一个 task_id 对应一条任务记录
CREATE TABLE IF NOT EXISTS tasks (
    task_id             TEXT PRIMARY KEY,   -- 任务唯一 id（API 层生成，形如 task_ab12cd34）
    topic               TEXT NOT NULL,      -- 用户提交的研究主题
    status              TEXT NOT NULL DEFAULT 'running',  -- running | paused（服务中断）| completed | failed
    execution_status    TEXT NOT NULL DEFAULT 'running',
    quality_status      TEXT NOT NULL DEFAULT 'pending',
    forced_output       INTEGER NOT NULL DEFAULT 0,
    error_code          TEXT,
    plan                TEXT,          -- JSON 数组（步骤列表 [{"id":1,"agent":"researcher",...}]）
    current_step_index  INTEGER DEFAULT 0,  -- 当前进度（= 已完成步骤数）
    completed_steps     TEXT,          -- JSON 数组（已完成步骤 id，如 "[1,2]"）
    research_data       TEXT,          -- JSON 对象（各步骤研究结果）
    draft_content       TEXT,          -- 报告草稿（Markdown）
    final_report        TEXT,          -- 最终报告（先复用草稿）
    error               TEXT,          -- 任务失败原因（status=failed 时填充）
    review_comments     TEXT,          -- JSON 数组（Reviewer 最后一次审核意见）
    review_history      TEXT,          -- JSON 数组（A2A 各轮审核记录 [{"round","passed","comments"}]，按轮保留）
    iteration           INTEGER DEFAULT 0,   -- A2A 循环次数（每次打回 +1）
    max_iterations      INTEGER DEFAULT 3,   -- A2A 循环上限（达上限强制通过；仅供前端展示，值由 workflow.run_task 落库）
    passed              INTEGER,       -- 最近一次审核是否通过（0/1/NULL 未审核；强制通过时亦为 1，原始判定见 review_history）
    forced_pass         INTEGER DEFAULT 0,   -- 是否因达循环上限被强制通过（0/1）
    citations           TEXT NOT NULL DEFAULT '[]',
    citation_errors     TEXT NOT NULL DEFAULT '[]',
    document_ids        TEXT NOT NULL DEFAULT '[]',
    retrievals          TEXT NOT NULL DEFAULT '[]',
    evidence            TEXT NOT NULL DEFAULT '[]',  -- 任务内来源，不反推历史证据
    memory_hits         TEXT,          -- JSON 数组（本次任务的长期记忆命中 [{"topic","score",...}]）
    created_at          TEXT DEFAULT (datetime('now','localtime')),  -- 创建时间
    updated_at          TEXT DEFAULT (datetime('now','localtime'))   -- 最后更新时间
);

-- ReAct 日志表：一条任务对应多条日志（Thought/Action/Observation）
CREATE TABLE IF NOT EXISTS task_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增主键（日志按此排序）
    task_id     TEXT NOT NULL REFERENCES tasks(task_id),  -- 归属任务（外键）
    step_id     INTEGER NOT NULL,   -- 归属 plan 步骤 id（前端按步骤分组展示）
    log_type    TEXT NOT NULL,      -- Thought | Action | Observation
    content     TEXT NOT NULL,      -- 日志正文
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

-- 查询加速：按 task_id 查日志走索引
CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id);

-- 评测报告表：自动化评测的执行记录与最终报告
CREATE TABLE IF NOT EXISTS evaluation_reports (
    eval_id         TEXT PRIMARY KEY,   -- 评测唯一 id（形如 eval_ab12cd34）
    status          TEXT NOT NULL DEFAULT 'running',  -- running（执行中）| completed | failed
    total_cases     INTEGER NOT NULL,   -- 标准用例总数（固定 10）
    completed_cases INTEGER DEFAULT 0,  -- 已完成用例数（前端进度条数据源，执行中轮询）
    results         TEXT,               -- JSON 数组（每个用例的执行结果：耗时/ReAct次数/关键词覆盖率…）
    metrics         TEXT,               -- JSON 对象（5 项指标实测值与目标值达标情况）
    report_md       TEXT,               -- Markdown 可视化总结（含指标表 + 用例明细表）
    error           TEXT,               -- 评测失败原因（status=failed 时填充）
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime'))
);
"""


# 列的迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的旧表加列，
# 这里用 ALTER TABLE ADD COLUMN 逐列补齐（列已存在时抛 OperationalError → 跳过，幂等）。
# 注意：加列必须同步修改 _SCHEMA、task_repository 的 _JSON_FIELDS/_ALLOWED_UPDATE、
# workflow.run_task 落库与前端类型，五处缺一不可（见 CLAUDE.md 约定 12）。
_MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN document_ids TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE tasks ADD COLUMN retrievals TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE tasks ADD COLUMN review_comments TEXT",
    "ALTER TABLE tasks ADD COLUMN review_history TEXT",
    "ALTER TABLE tasks ADD COLUMN iteration INTEGER DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN max_iterations INTEGER DEFAULT 3",
    "ALTER TABLE tasks ADD COLUMN passed INTEGER",
    "ALTER TABLE tasks ADD COLUMN forced_pass INTEGER DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN memory_hits TEXT",
    "ALTER TABLE tasks ADD COLUMN evidence TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE tasks ADD COLUMN citations TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE tasks ADD COLUMN citation_errors TEXT NOT NULL DEFAULT '[]'",
]


def get_connection() -> sqlite3.Connection:
    """新建一条 SQLite 连接（行以 dict 风格访问，允许跨线程使用）。

    实现说明：每次操作**新建连接、用完即关**。
    FastAPI 是异步多线程环境，如果长连接跨线程共享，会报
    "SQLite objects created in a thread can only be used in that thread"。
    频繁开关连接的性能开销对本项目量级可忽略，换来的是线程安全。
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row          # 行记录支持 dict 风格访问（row["task_id"]）
    return conn


def init_db(db_path: Path | None = None) -> None:
    """初始化数据库：可选覆盖库文件路径（测试用临时库），并执行建表 SQL（幂等）。

    :param db_path: 指定库文件路径；不传则用默认 backend/data.db
                    （测试/交互式验证可通过它隔离出临时库，避免污染真实数据）
    """
    global DB_PATH
    if db_path is not None:
        DB_PATH = Path(db_path)             # 覆盖全局库路径（模块级单例）
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(_SCHEMA)         # 批量执行建表 SQL（IF NOT EXISTS → 幂等）
        conn.execute("BEGIN IMMEDIATE")  # 旧库补列与回填共同提交，失败全部回滚
        for sql in _MIGRATIONS:             # 旧库补列（已存在则跳过）
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
        migrate_quality(conn)
