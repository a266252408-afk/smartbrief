"""在真实生产 MCP 协议服务中只替换搜索外部边界。"""
from pathlib import Path
import sys


def main():
    backend = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(backend), str(backend.parent)]
    from tests.offline_guard import install
    install()
    from tests.fakes import fixture_search
    from mcp_servers import web_search_server
    web_search_server._web_search = fixture_search
    web_search_server.mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
