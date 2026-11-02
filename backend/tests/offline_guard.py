"""标准测试禁止外部 DNS 与 TCP/UDP 连接；保留回环及本地 IPC。"""
import ipaddress
import os
import socket

_INSTALLED = False


def _local(host):
    if host is None or host in {"", "localhost", b"localhost"}:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii")
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["ANONYMIZED_TELEMETRY"] = "False"
    os.environ["OTEL_SDK_DISABLED"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ['MODEL_FAILOVER_ENABLED'] = 'false'
    for name in ("EVALUATION_JUDGE_API_KEY", "OPENAI_API_KEY", "BACKUP_OPENAI_API_KEY", "EMBEDDING_API_KEY", "EXA_API_KEY", "LANGCHAIN_API_KEY", "LANGSMITH_API_KEY"):
        os.environ[name] = ""
    original_dns = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto

    def dns(host, *args, **kwargs):
        if not _local(host):
            raise PermissionError("离线测试禁止外部 DNS")
        return original_dns(host, *args, **kwargs)

    def check(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6} and not _local(address[0]):
            raise PermissionError("离线测试禁止外部网络")

    def connect(sock, address):
        check(sock, address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(sock, address)
        return original_connect_ex(sock, address)

    def sendto(sock, data, *args):
        check(sock, args[-1])
        return original_sendto(sock, data, *args)

    socket.getaddrinfo = dns
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
