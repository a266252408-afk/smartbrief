# =====================================================================
# SmartBrief 后端镜像（FastAPI + 多智能体编排引擎 + MCP Server）
# 构建上下文 = 仓库根目录（smartbrief/），由 docker-compose.yml 指定
#   说明：后端在容器内会以子进程拉起 mcp_servers/web_search_server.py，
#   其脚本路径按 /app/mcp_servers 计算（见 mcp_client.py 的 parents[2]），
#   因此目录布局必须保持 WORKDIR=/app + backend/ + mcp_servers/。
# =====================================================================
FROM python:3.13-slim-bookworm

# 运行期环境：不缓冲日志（docker compose logs 实时可见）、不写 pyc、pip 不缓存
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 1) 先只拷贝依赖清单并安装（层缓存：requirements.txt 不变则不会重装）
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 2) 拷贝后端源码与 MCP Server（MCP 由后端以子进程拉起，两处都需要）
COPY backend/ /app/backend/
COPY mcp_servers/ /app/mcp_servers/

# 3) RAG 预置 seed（模板 + 术语）单独备份到 /app/seed：
#    data/ 目录会被 compose 卷挂载（backend_data:/app/backend/data），
#    空卷会“盖住”镜像里同路径的内容，故由 entrypoint 首次启动时复制进卷。
COPY backend/data/templates /app/seed/backend-data/templates
COPY backend/data/terminologies.txt /app/seed/backend-data/terminologies.txt

# 4) 容器入口脚本（Windows 环境编写的脚本可能带 CRLF，先清理再执行）
COPY deploy/backend/entrypoint.sh /app/entrypoint.sh
RUN tr -d '\r' < /app/entrypoint.sh > /tmp/entrypoint.sh \
    && mv /tmp/entrypoint.sh /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh

# 运行期端口（FastAPI）
EXPOSE 8002

WORKDIR /app/backend
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]