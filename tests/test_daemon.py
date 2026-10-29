"""The shared MCP daemon: token gate, one daemon for many windows, CLI delegation."""

import asyncio
import os
import socket
import sys

import httpx
import pytest

from kortext import daemon


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def isolated_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KORTEXT_HOME", str(tmp_path / ".kortext"))
    monkeypatch.setenv("KORTEXT_QDRANT_MODE", "embedded")
    monkeypatch.setenv("KORTEXT_DAEMON_PORT", str(_free_port()))
    yield
    try:
        pid = httpx.get(
            f"http://127.0.0.1:{daemon.port()}/health",
            headers={"Authorization": f"Bearer {daemon.token()}"},
            timeout=1,
        ).json()["pid"]
        os.kill(pid, 15)
    except Exception:
        pass


def test_token_file_is_private_and_stable(isolated_daemon, tmp_path):
    first = daemon.token()
    assert daemon.token() == first
    if os.name != "nt":
        assert (tmp_path / ".kortext" / "run" / "daemon.token").stat().st_mode & 0o077 == 0


def test_two_windows_share_one_daemon_and_embedded_qdrant(isolated_daemon):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    server = [sys.executable, "-c", "from kortext.server import main; main()"]

    async def window():
        async with Client(StdioTransport(command=server[0], args=server[1:], env=dict(os.environ))) as c:
            res = await c.call_tool("list_vector_collections", {})
            return res.content[0].text

    async def both():
        return await asyncio.wait_for(asyncio.gather(window(), window()), 120)

    a, b = asyncio.run(both())
    assert '"status":"embedded"' in a and '"status":"embedded"' in b
    assert "locked" not in a + b

    health = f"http://127.0.0.1:{daemon.port()}/health"
    assert httpx.get(health, timeout=2).status_code == 401
