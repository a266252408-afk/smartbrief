"""动作幂等凭据；与任务变更使用同一调用方事务。"""
import hashlib
import json


def digest(body):
    return hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def replay(conn, key, action, target, body):
    if key is None:
        return None
    row = conn.execute("SELECT * FROM task_actions WHERE key_digest=?",(key,)).fetchone()
    if row is None:
        return None
    if row["action"] != action or row["target"] != target or row["request_hash"] != digest(body):
        from services.task_service import TaskControlError
        raise TaskControlError("IDEMPOTENCY_CONFLICT","幂等键已用于其他动作或请求",409)
    data = json.loads(row["response"])
    if data["task_id"] != row["result_task_id"]:
        raise ValueError("动作凭据归属损坏")
    return data


def record(conn,key,action,target,body,data):
    if key is not None:
        conn.execute("INSERT INTO task_actions(key_digest,action,target,request_hash,result_task_id,response) VALUES (?,?,?,?,?,?)",
            (key,action,target,digest(body),data["task_id"],json.dumps(data,ensure_ascii=False)))
