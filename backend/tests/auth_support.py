"""仅供隔离测试的固定凭证，不是运行环境密钥。"""
ADMIN_TOKEN = "fixture-admin-" + "a" * 32
READ_TOKEN = "fixture-reader-" + "b" * 32
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
READ_HEADERS = {"Authorization": f"Bearer {READ_TOKEN}"}
