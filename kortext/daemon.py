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


def pid_file() -> Path:
    return _run_dir() / "daemon.pid"


def _write_pid_file() -> None:
    pfile = pid_file()
    pfile.write_text(f"{os.getpid()}\n", encoding="utf-8")


def _remove_pid_file() -> None:
    try:
        pfile = pid_file()
        if pfile.is_file() and pfile.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pfile.unlink(missing_ok=True)
    except Exception:
        pass


def stop() -> bool:
    """Terminates the daemon process if running and cleans up daemon.pid.

    Returns True if a live daemon process was found and terminated, False otherwise.
    """
    pfile = pid_file()
    if not pfile.is_file():
        print("Kortext daemon is not running.")
        return False

    try:
        raw = pfile.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (ValueError, OSError):
        pfile.unlink(missing_ok=True)
        print("Removed invalid daemon PID file.")
        return False

    if pid == os.getpid():
        return False

    try:
        import psutil
    except ImportError:
        psutil = None

    proc = None
    is_live = False
    is_kortext = False

    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                is_live = True
                try:
                    cmdline = proc.cmdline()
                    cmd_str = " ".join(cmdline)
                    if "kortext" in cmd_str:
                        is_kortext = True
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    else:
        try:
            os.kill(pid, 0)
            is_live = True
        except OSError:
            is_live = False

        if is_live and os.name == "posix":
            proc_cmd = Path(f"/proc/{pid}/cmdline")
            if proc_cmd.is_file():
                try:
                    raw_cmd = proc_cmd.read_bytes().replace(b"\x00", b" ").decode(errors="ignore")
                    if "kortext" in raw_cmd:
                        is_kortext = True
                except OSError:
                    pass

    if not is_live:
        pfile.unlink(missing_ok=True)
        print(f"Kortext daemon is not running (cleaned up stale PID {pid}).")
        return False

    if not is_kortext:
        pfile.unlink(missing_ok=True)
        print(f"Stale daemon PID {pid} does not belong to kortext; not terminating.")
        return False

    import signal
    try:
        if proc is not None:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, ProcessLookupError):
                pass
            else:
                try:
                    proc.wait(timeout=5)
                except (psutil.TimeoutExpired, psutil.NoSuchProcess):
                    try:
                        proc.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                    time.sleep(0.1)
                except OSError:
                    break
            else:
                if hasattr(signal, "SIGKILL"):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
        pfile.unlink(missing_ok=True)
        print(f"Stopped kortext daemon (PID {pid}).")
        return True
    except Exception as exc:
        pfile.unlink(missing_ok=True)
        print(f"Failed to terminate kortext daemon (PID {pid}): {exc}")
        return False


def serve(mcp: Any) -> None:
    """Runs `mcp` as the daemon (blocking)."""
    import atexit
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse

    _write_pid_file()
    atexit.register(_remove_pid_file)

    @mcp.custom_route("/health", methods=["GET"])
    async def _health(_request: Any) -> JSONResponse:
        return JSONResponse({"service": "kortext", "pid": os.getpid()})

    try:
        mcp.run(
            transport="http",
            host=HOST,
            port=port(),
            show_banner=False,
            middleware=[Middleware(BearerGate, secret=token())],
        )
    finally:
        _remove_pid_file()
        try:
            atexit.unregister(_remove_pid_file)
        except Exception:
            pass


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
