"""离线协议、网络拒绝和外部替身的行为验收。"""
import asyncio
import pytest
from tests.fakes import ScriptedLLM


def test_llm_script_preserves_order_and_exhaustion():
    async def scenario():
        llm = ScriptedLLM(["固定回答", RuntimeError("供应商故障")])
        response = await llm.chat([{"role": "user", "content": "问题"}])
        assert response.choices[0].message.content == "固定回答"
        with pytest.raises(RuntimeError, match="供应商故障"):
            await llm.chat([])
        with pytest.raises(AssertionError, match="耗尽"):
            await llm.chat([])
    asyncio.run(scenario())


@pytest.mark.parametrize("host", ["provider.example", "localhost.evil", "203.0.113.5", "2001:db8::1"])
def test_external_dns_is_rejected(host):
    import socket
    with pytest.raises(PermissionError, match="禁止外部 DNS"):
        socket.getaddrinfo(host, 443)


@pytest.mark.parametrize("method", ["connect", "connect_ex", "sendto"])
def test_external_socket_is_rejected(method):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if method == "sendto" else socket.SOCK_STREAM) as sock:
        args = (b"test", ("203.0.113.5", 443)) if method == "sendto" else (("203.0.113.5", 443),)
        with pytest.raises(PermissionError, match="禁止外部网络"):
            getattr(sock, method)(*args)


def test_loopback_dns_remains_available():
    import socket
    assert socket.getaddrinfo("127.0.0.1", 8002)
    assert socket.getaddrinfo("::1", 8002)


def test_dotenv_cannot_restore_a_real_key(tmp_path, monkeypatch):
    import os
    from dotenv import load_dotenv
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=fixture-secret\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert load_dotenv(env) is False
    assert "OPENAI_API_KEY" not in os.environ


def test_embeddings_are_deterministic_and_input_sensitive(fake_embeddings):
    vectors = fake_embeddings.embed_documents(["甲", "乙", "甲"])
    assert vectors[0] == vectors[2] == fake_embeddings.embed_query("甲")
    assert vectors[0] != vectors[1]
    assert len(vectors[0]) == 8


def test_subprocess_can_install_the_same_network_guard():
    import subprocess
    import sys
    from pathlib import Path
    process = subprocess.run([
        sys.executable, "-c",
        "from tests.offline_guard import install; install(); import socket; socket.getaddrinfo('provider.example',443)"
    ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert process.returncode != 0
    assert "PermissionError" in process.stderr
