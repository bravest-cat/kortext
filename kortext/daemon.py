"""
One kortext MCP server per machine.

Each agy window starts `kortext-server`, which is a thin stdio proxy to a shared HTTP daemon on
127.0.0.1. Only the daemon opens embedded Qdrant, so a second window or a CLI command no longer
hits the embedded-storage lock, and no Qdrant container is needed. The daemon is started on first
use and requires a per-user bearer token stored in ~/.kortext/run/daemon.token (mode 0600).
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

import httpx

from kortext._env import get_int_env
from kortext._home import get_kortext_home

logger = logging.getLogger("kortext.daemon")

HOST = "127.0.0.1"


def port() -> int:
    return get_int_env("KORTEXT_DAEMON_PORT", 8767)


def url() -> str:
    return f"http://{HOST}:{port()}/mcp"


def _run_dir() -> Path:
    d = get_kortext_home() / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def token() -> str:
    """The shared secret, created on first use and readable only by the user."""
    path = _run_dir() / "daemon.token"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except FileNotFoundError:
        pass
    value = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value)
    return value


def is_up(timeout: float = 0.5) -> bool:
    try:
        r = httpx.get(
            f"http://{HOST}:{port()}/health",
            headers={"Authorization": f"Bearer {token()}"},
            timeout=timeout,
        )
        return r.status_code == 200 and r.json().get("service") == "kortext"
    except (httpx.HTTPError, ValueError):
        return False


def _spawn() -> None:
    log = open(get_kortext_home() / "logs" / "daemon.log", "ab")  # noqa: SIM115 - handed to the child
    kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "env": os.environ.copy()}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "kortext.server", "--daemon"], **kwargs)


def ensure_running(timeout: float = 45.0) -> bool:
    """Starts the daemon unless one answers already. Returns False if it did not come up in time."""
    if is_up():
        return True
    (get_kortext_home() / "logs").mkdir(parents=True, exist_ok=True)
    import portalocker  # ships with qdrant-client

    # Two windows opening at once must not start two daemons.
    with portalocker.Lock(str(_run_dir() / "daemon.spawn.lock"), timeout=timeout):
        if is_up():
            return True
        _spawn()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if is_up():
                return True
            time.sleep(0.25)
    logger.error("kortext daemon did not answer on %s within %.0fs; see %s", url(), timeout, get_kortext_home() / "logs" / "daemon.log")
    return False


class BearerGate:
    """ASGI middleware: every request must carry the daemon token."""

    def __init__(self, app: Any, secret: str):
        self.app = app
        self.expected = f"Bearer {secret}".encode()

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and dict(scope.get("headers") or []).get(b"authorization") != self.expected:
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        await self.app(scope, receive, send)


def serve(mcp: Any) -> None:
    """Runs `mcp` as the daemon (blocking)."""
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def _health(_request: Any) -> JSONResponse:
        return JSONResponse({"service": "kortext", "pid": os.getpid()})

    mcp.run(
        transport="http",
        host=HOST,
        port=port(),
        show_banner=False,
        middleware=[Middleware(BearerGate, secret=token())],
    )


def run_proxy() -> bool:
    """Serves this agy window over stdio, forwarding to the daemon. False if the daemon is unavailable."""
    if not ensure_running():
        return False
    from fastmcp import Client
    from fastmcp.server import create_proxy

    create_proxy(Client(url(), auth=token()), name="kortext").run(transport="stdio", show_banner=False)
    return True


async def call_tool(name: str, arguments: Dict[str, Any]) -> Any:
    """Calls a tool on the daemon, starting it if needed (for CLI commands)."""
    if not ensure_running():
        raise RuntimeError(f"kortext daemon unavailable on {url()}")
    from fastmcp import Client

    async with Client(url(), auth=token()) as client:
        return await client.call_tool(name, arguments)
