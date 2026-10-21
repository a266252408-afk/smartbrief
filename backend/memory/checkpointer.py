"""短期记忆：LangGraph Checkpointer 封装（backend/checkpoints.db）。

职责边界（与业务库分工，见 CLAUDE.md）：
- `backend/data.db`        由 task_repository 显式写入**任务业务数据**；
- `backend/checkpoints.db` 由 LangGraph 框架在每次图节点执行后**自动写入图运行
  时状态快照**，task_id 同时作为 checkpoint 的 thread_id，二者一一对应。

注意（langgraph-checkpoint-sqlite >= 3 的 API）：`from_conn_string()` 返回的不是
Saver 实例而是连接生命周期管理器 —— 因此：
- 执行工作流（要跨节点持久写快照）必须用 `async with open_checkpointer() as (cp, kind):`
  把 compile + invoke 包在连接生命周期内（AsyncSqliteSaver）；
- 轻量只读查询（has_checkpoint / list_snapshots）走同步 `SqliteSaver` 短暂连接。

降级（保证缺依赖环境也能跑）：
1. AsyncSqliteSaver（langgraph-checkpoint-sqlite，跨进程持久化 —— 后端重启后仍可续跑）
2. MemorySaver（langgraph 自带，仅进程内；以单例复用，同一进程内仍可 resume）
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from data_paths import runtime_path

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[1]
CHECKPOINT_DB = runtime_path('checkpoints.db')

# 降级路径的进程内 MemorySaver 单例（供 open_checkpointer 与只读查询共用，
# 保证同一进程内"写快照 → 检测/恢复"使用同一实例）
_memory_saver: Any = None


def _get_memory_saver() -> Any:
    global _memory_saver
    if _memory_saver is None:
        from langgraph.checkpoint.memory import MemorySaver

        _memory_saver = MemorySaver()
    return _memory_saver


@asynccontextmanager
async def open_checkpointer(
    db_path: Optional[str] = None,
) -> AsyncIterator[Tuple[Any, str]]:
    """在连接生命周期内打开持久化 Checkpointer。

    新版 langgraph-checkpoint-sqlite 要求 `async with from_conn_string(...)`
    进入连接生命周期：期间图每次节点执行后自动向 SQLite 落快照，退出即关闭连接。
    sqlite 依赖/文件不可用则降级为进程内 MemorySaver（kind="memory"）。

    :param db_path: 覆盖默认 backend/checkpoints.db（测试隔离用）
    :yield: (checkpointer, kind)，kind ∈ {"sqlite", "memory"}
    """
    path = Path(db_path or CHECKPOINT_DB)
    cp: Any = None
    kind = "memory"
    opener: Any = None
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        # 显式打开连接（真正连库发生在 opener.__aenter__，返回 saver 实例）；
        # 失败即降级。注意：降级分支绝不能包住 yield —— body 抛出的异常若被
        # except 吞掉并再次 yield，asynccontextmanager 会报
        # "generator didn't stop after athrow()"。
        opener = AsyncSqliteSaver.from_conn_string(str(path))
        cp = await opener.__aenter__()
        await cp.setup()  # 真正验证库可用，不能等图执行后才发现损坏。
        kind = "sqlite"
    except Exception:
        logger.warning("SQLite Checkpointer 不可用，使用临时状态从头重试")
        if opener is not None:
            try:
                await opener.__aexit__(None,None,None)
            except Exception:
                logger.warning("不可用检查点连接清理失败")
            opener = None
        cp = _get_memory_saver()
        kind = "memory"
    try:
        yield cp, kind
    finally:
        # saver 实例无 __aexit__，连接生命周期归属 opener（async generator）
        if opener is not None:
            await opener.__aexit__(None, None, None)


def has_checkpoint(task_id: str, db_path: Optional[str] = None) -> bool:
    """（同步只读）是否存在快照；是否仍有待执行节点由resumable_mode判断。"""
    path = Path(db_path or CHECKPOINT_DB)
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        with SqliteSaver.from_conn_string(str(path)) as cp:
            return cp.get_tuple({"configurable": {"thread_id": task_id}}) is not None
    except Exception as exc:  # pragma: no cover - sqlite 打开失败降级 memory
        logger.warning("SQLite 查询任务 %s 快照失败（%s），退回进程内 MemorySaver", task_id, exc)
        return _get_memory_saver().get_tuple(
            {"configurable": {"thread_id": task_id}}
        ) is not None


def list_snapshots(task_id: str, db_path: Optional[str] = None) -> List[Dict]:
    """（同步只读）列出某任务的 checkpoint 快照（供前端展示断点状态）。"""
    path = Path(db_path or CHECKPOINT_DB)
    snapshots: List[Dict] = []
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        with SqliteSaver.from_conn_string(str(path)) as cp:
            configs = cp.list({"configurable": {"thread_id": task_id}})
            # CheckpointTuple 形如 (config, checkpoint, metadata, parent_config, pending_writes)，
            # 部分实现字段数不同，故用 *rest 兼容展开
            for config, _checkpoint, meta, *_rest in configs:
                cfg = config.get("configurable", {})
                snapshots.append({
                    "checkpoint_id": cfg.get("checkpoint_id"),
                    "created_at": (meta or {}).get("created_at"),
                    "step": (meta or {}).get("step", 0),
                })
    except Exception as exc:  # pragma: no cover - sqlite 打开失败降级 memory
        logger.warning("SQLite 读取任务 %s 快照失败（%s），退回进程内 MemorySaver", task_id, exc)
        try:
            cp = _get_memory_saver()
            for config, _checkpoint, meta, *_rest in cp.list(
                {"configurable": {"thread_id": task_id}}
            ):
                cfg = config.get("configurable", {})
                snapshots.append({
                    "checkpoint_id": cfg.get("checkpoint_id"),
                    "created_at": (meta or {}).get("created_at"),
                    "step": (meta or {}).get("step", 0),
                })
        except Exception:  # pragma: no cover
            pass
    return snapshots


def reset_checkpointer() -> None:
    """清空进程内 MemorySaver 单例（测试隔离用：换临时库文件前调用）。"""
    global _memory_saver
    _memory_saver = None


def resumable_mode(task_id: str) -> str:
    """只有持久化图仍有后续节点才称断点续跑；不把终止快照当作可恢复。"""
    if not CHECKPOINT_DB.is_file():
        return "restart"
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        from graph.workflow import build_graph
        with SqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as cp:
            state = build_graph(checkpointer=cp).get_state({"configurable":{"thread_id":task_id}})
            return "checkpoint" if state.next else "restart"
    except Exception:
        logger.warning("任务 %s 持久化检查点不可续跑，将从头重试",task_id)
        return "restart"
