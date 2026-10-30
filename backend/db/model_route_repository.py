"""任务主备绑定独立持久化；图从头重试不清除已发生的接管。"""
from db.database import get_connection


def get_route(task_id):
    from services.model_routing import ModelRoute
    with get_connection() as conn:
        row = conn.execute('SELECT data FROM task_model_routes WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        return None
    route = ModelRoute.model_validate_json(row[0])
    if route.task_id != task_id:
        raise ValueError('模型绑定归属损坏')
    return route.model_dump()


def bind_backup(route):
    """首次接管不可覆盖；同任务并发以数据库中第一个绑定为准。"""
    from services.execution_context import assert_active
    assert_active(route.task_id)
    with get_connection() as conn:
        conn.execute('INSERT OR IGNORE INTO task_model_routes(task_id,data) VALUES (?,?)',
                     (route.task_id, route.model_dump_json()))
    return get_route(route.task_id)
