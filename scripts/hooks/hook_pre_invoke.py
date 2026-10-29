#!/usr/bin/env python3
"""
Antigravity PreInvocation Hook (thin wrapper delegating to kortext.hooks.runner).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
for p in (str(SCRIPT_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from common import (
    check_http_health,
    check_tcp_port,
    check_unix_socket,
    get_hook_payload,
)
from kortext._home import get_kortext_home
from kortext.hooks.context import HookContext
import kortext.hooks.pipelines.pre_invoke as _pre_invoke_mod
from kortext.hooks.pipelines.pre_invoke import _check_qdrant_ready
from kortext.hooks.runner import emit_response, run_hook

_ORIG_HTTP_HEALTH = check_http_health


def check_falkordb_status(timeout_sec: float = 0.2) -> bool:
    """Checks FalkorDB health via Unix Domain Socket first, then TCP fallback."""
    env_sock = os.environ.get("FALKORDB_SOCKET_PATH")
    if env_sock:
        candidate_socks = [Path(env_sock)]
    else:
        home_sock = Path.home() / ".kortext" / "run" / "falkordb.sock"
        kh_sock = get_kortext_home() / "run" / "falkordb.sock"
        candidate_socks = [home_sock] if home_sock == kh_sock else [home_sock, kh_sock]
    for sock_path in candidate_socks:
        if check_unix_socket(sock_path, timeout_sec=timeout_sec):
            return True
    port = int(os.environ.get("FALKORDB_PORT", "6379"))
    host = os.environ.get("FALKORDB_HOST", "127.0.0.1")
    return check_tcp_port(host, port, timeout_sec=timeout_sec)


def check_qdrant_status(timeout_sec: float = 0.2) -> bool:
    """Checks Qdrant readiness across embedded mode (~/.kortext/qdrant) and HTTP endpoint."""
    if check_http_health is not _ORIG_HTTP_HEALTH:
        return bool(check_http_health("127.0.0.1", 6333, "/healthz", timeout_sec=timeout_sec))
    return _check_qdrant_ready(get_kortext_home(), timeout_sec=timeout_sec)


_ORIG_STATUS_CHECKS = (check_falkordb_status, check_qdrant_status)


def main() -> None:
    payload = get_hook_payload()
    ctx = HookContext.from_stdin(raw_text=json.dumps(payload), event="pre_invoke")
    if (check_falkordb_status, check_qdrant_status) != _ORIG_STATUS_CHECKS:
        f_ok = bool(check_falkordb_status(timeout_sec=0.05))
        q_ok = bool(check_qdrant_status(timeout_sec=0.05))
        orig_u = _pre_invoke_mod._check_unix_socket
        orig_t = _pre_invoke_mod._check_tcp
        orig_q = _pre_invoke_mod._check_qdrant_ready
        try:
            _pre_invoke_mod._check_unix_socket = lambda *_a, **_kw: f_ok
            _pre_invoke_mod._check_tcp = lambda *_a, **_kw: f_ok
            _pre_invoke_mod._check_qdrant_ready = lambda *_a, **_kw: q_ok
            res = run_hook("pre_invoke", ctx)
        finally:
            _pre_invoke_mod._check_unix_socket = orig_u
            _pre_invoke_mod._check_tcp = orig_t
            _pre_invoke_mod._check_qdrant_ready = orig_q
    else:
        res = run_hook("pre_invoke", ctx)
    emit_response(event="pre_invoke", decision=res.decision, reason=res.reason, data=res.data)


if __name__ == "__main__":
    main()
