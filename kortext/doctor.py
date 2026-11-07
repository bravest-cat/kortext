"""
Kortext Installation Diagnostics (Doctor).

Runs 10 diagnostic checks across:
1. Python version (3.11+)
2. Substrate binaries present and hashes known
3. FalkorDB answering
4. Qdrant reachable or lock held by daemon
5. Daemon health and token file permissions (POSIX)
6. Plugin installed and hooks enabled
7. Embedder model cached
8. Whether the cwd is indexed and how stale that is
9. Failed re-index events in spool/failed/
10. The agy version found
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from kortext._home import get_kortext_home
from kortext.binaries import (
    DEFAULT_FALKORDB_VERSION,
    DEFAULT_QDRANT_VERSION,
    PINNED_BINARY_SHA256,
    _DEFAULT_VERIFIED_SHA256,
    detect_platform,
)
from kortext.cache import CACHE_DB_PATH, IngestCache
from kortext.health import reindex_spool_status

logger = logging.getLogger("kortext.doctor")


def _is_port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _is_unix_socket_open(socket_path: Path, timeout: float = 0.4) -> bool:
    if not hasattr(socket, "AF_UNIX") or not socket_path.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(str(socket_path))
        s.close()
        return True
    except (OSError, socket.timeout):
        return False


def run_doctor(
    cwd: Optional[Path] = None,
    home: Optional[Path] = None,
) -> List[Dict[str, str]]:
    """
    Runs 10 diagnostic checks. Returns list of {status, check, what, fix}.
    Safe against offline substrates and finishes in under 5 s.
    """
    kortext_home = home or get_kortext_home()
    work_dir = (cwd or Path.cwd()).resolve()
    checks: List[Dict[str, str]] = []

    # 1. Python version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 11):
        checks.append({
            "status": "ok",
            "check": "python_version",
            "what": f"Python version {py_ver} (meets 3.11+ requirement)",
            "fix": "",
        })
    else:
        checks.append({
            "status": "fail",
            "check": "python_version",
            "what": f"Python version {py_ver} is unsupported (requires 3.11+)",
            "fix": "uv python install 3.11",
        })

    # 2. Substrate binaries present and hashes known
    plat = detect_platform()
    bin_dir = kortext_home / "bin"
    qdrant_name = "qdrant.exe" if plat.is_windows else "qdrant"
    qdrant_bin = bin_dir / qdrant_name
    falkor_bin = bin_dir / "falkordb.so"

    missing_bins = []
    if not qdrant_bin.is_file():
        missing_bins.append(qdrant_name)
    if not plat.is_windows and not falkor_bin.is_file():
        missing_bins.append("falkordb.so")

    if missing_bins:
        checks.append({
            "status": "fail",
            "check": "substrate_binaries",
            "what": f"Substrate binary missing: {', '.join(missing_bins)} in {bin_dir}",
            "fix": "kortext setup",
        })
    else:
        q_target = ("qdrant", DEFAULT_QDRANT_VERSION, plat.os_name, plat.arch)
        expected_q = PINNED_BINARY_SHA256.get(q_target, _DEFAULT_VERIFIED_SHA256.get(q_target))
        actual_q = IngestCache.compute_sha256(qdrant_bin)
        q_match = bool(expected_q and actual_q.lower() == expected_q.lower())

        f_match = True
        if not plat.is_windows:
            f_target = ("falkordb", DEFAULT_FALKORDB_VERSION, plat.os_name, plat.arch)
            expected_f = PINNED_BINARY_SHA256.get(f_target, _DEFAULT_VERIFIED_SHA256.get(f_target))
            actual_f = IngestCache.compute_sha256(falkor_bin)
            f_match = bool(expected_f and actual_f.lower() == expected_f.lower())

        if q_match and f_match:
            checks.append({
                "status": "ok",
                "check": "substrate_binaries",
                "what": "Substrate binaries present and SHA-256 hashes verified",
                "fix": "",
            })
        else:
            checks.append({
                "status": "warn",
                "check": "substrate_binaries",
                "what": "Substrate binaries present with custom or unpinned hash",
                "fix": "kortext setup",
            })

    # 3. FalkorDB answering
    sock_path = kortext_home / "run" / "falkordb.sock"
    falkor_host = os.getenv("FALKORDB_HOST", "127.0.0.1")
    falkor_port = int(os.getenv("FALKORDB_PORT", "6379"))

    falkor_up = False
    falkor_detail = "Offline"
    if _is_unix_socket_open(sock_path, timeout=0.3):
        try:
            import redis
            r = redis.Redis(unix_socket_path=str(sock_path), socket_timeout=0.4)
            if r.ping():
                falkor_up = True
                falkor_detail = "Unix Socket"
        except Exception:
            pass
    if not falkor_up and _is_port_open(falkor_host, falkor_port, timeout=0.3):
        try:
            import redis
            r = redis.Redis(host=falkor_host, port=falkor_port, socket_timeout=0.4)
            if r.ping():
                falkor_up = True
                falkor_detail = f"TCP :{falkor_port}"
        except Exception:
            pass

    if falkor_up:
        checks.append({
            "status": "ok",
            "check": "falkordb",
            "what": f"FalkorDB answering ({falkor_detail})",
            "fix": "",
        })
    else:
        checks.append({
            "status": "fail",
            "check": "falkordb",
            "what": "FalkorDB is offline",
            "fix": "kortext start",
        })

    # 4. Qdrant reachable or lock held by the daemon
    qdrant_url = os.getenv("QDRANT_URL", "").strip()
    qdrant_up = False
    q_mode = "Embedded"

    if qdrant_url:
        import urllib.parse
        parsed_q = urllib.parse.urlparse(qdrant_url)
        q_host = parsed_q.hostname or "127.0.0.1"
        q_port = parsed_q.port or 6333
        qdrant_up = _is_port_open(q_host, q_port, timeout=0.3)
        q_mode = f"HTTP :{q_port}"
    else:
        from kortext._qdrant import is_embedded_qdrant_locked, is_qdrant_online
        from kortext import daemon
        if is_qdrant_online():
            qdrant_up = True
            q_mode = "HTTP :6333"
        elif is_embedded_qdrant_locked() or daemon.is_up(timeout=0.4):
            qdrant_up = True
            q_mode = "Embedded (lock held by daemon)"

    if qdrant_up:
        checks.append({
            "status": "ok",
            "check": "qdrant",
            "what": f"Qdrant reachable or lock held by daemon ({q_mode})",
            "fix": "",
        })
    else:
        checks.append({
            "status": "fail",
            "check": "qdrant",
            "what": "Qdrant unreachable and lock not held by daemon",
            "fix": "kortext start",
        })

    # 5. Daemon health and token file permissions (POSIX)
    from kortext import daemon
    daemon_up = daemon.is_up(timeout=0.4)
    token_path = kortext_home / "run" / "daemon.token"
    bad_posix_perms = False
    perm_octal = ""

    if token_path.is_file() and os.name != "nt":
        st_mode = token_path.stat().st_mode & 0o777
        perm_octal = oct(st_mode)
        if st_mode & 0o077 != 0:
            bad_posix_perms = True

    if bad_posix_perms:
        checks.append({
            "status": "fail",
            "check": "daemon_token",
            "what": f"Daemon token file permissions too open ({perm_octal} instead of 0600)",
            "fix": f"chmod 600 {token_path}",
        })
    elif daemon_up:
        checks.append({
            "status": "ok",
            "check": "daemon",
            "what": "Daemon healthy and token file permissions secure",
            "fix": "",
        })
    else:
        checks.append({
            "status": "warn",
            "check": "daemon",
            "what": "Daemon is not running (starts on demand on first MCP tool invocation)",
            "fix": "kortext server",
        })

    # 6. Plugin installed and hooks enabled
    plugin_dir = Path.home() / ".gemini" / "config" / "plugins" / "kortext"
    hooks_file = plugin_dir / "hooks.json"
    if not hooks_file.is_file():
        alt_hooks = Path(__file__).resolve().parent.parent / "integrations" / "antigravity" / "hooks.json"
        if alt_hooks.is_file() and (plugin_dir.exists() or (Path.home() / ".gemini" / "config" / "mcp_config.json").is_file()):
            hooks_file = alt_hooks

    if not (plugin_dir.exists() or (Path.home() / ".gemini" / "config" / "mcp_config.json").is_file()):
        checks.append({
            "status": "fail",
            "check": "plugin_installed",
            "what": "Kortext plugin is not installed in ~/.gemini/config/plugins/kortext",
            "fix": "kortext install",
        })
    elif not hooks_file.is_file():
        checks.append({
            "status": "fail",
            "check": "hooks_enabled",
            "what": "Plugin hooks manifest (hooks.json) not found",
            "fix": "kortext install",
        })
    else:
        try:
            h_data = json.loads(hooks_file.read_text(encoding="utf-8"))
            disabled = [k for k, v in h_data.items() if isinstance(v, dict) and not v.get("enabled", False)]
            if disabled:
                checks.append({
                    "status": "fail",
                    "check": "hooks_enabled",
                    "what": f"Plugin hooks disabled: {', '.join(disabled)}",
                    "fix": "kortext install",
                })
            else:
                checks.append({
                    "status": "ok",
                    "check": "hooks_enabled",
                    "what": "Plugin installed and all lifecycle hooks enabled",
                    "fix": "",
                })
        except Exception as exc:
            checks.append({
                "status": "fail",
                "check": "hooks_enabled",
                "what": f"Failed reading hooks manifest: {exc}",
                "fix": "kortext install",
            })

    # 7. Embedder model cached
    model_cached = False
    try:
        from fastembed import TextEmbedding
        TextEmbedding(model_name="BAAI/bge-small-en-v1.5", local_files_only=True)
        model_cached = True
    except Exception:
        # Check cache folders directly
        cache_dirs = [
            os.getenv("FASTEMBED_CACHE_PATH"),
            Path.home() / ".cache" / "fastembed",
            Path("/home/ravi/.cache/fastembed"),
        ]
        for cd in cache_dirs:
            if cd and Path(cd).is_dir():
                matches = list(Path(cd).glob("*bge-small-en-v1.5*"))
                if matches:
                    model_cached = True
                    break

    if model_cached:
        checks.append({
            "status": "ok",
            "check": "embedder_cache",
            "what": "Embedder model cached (BAAI/bge-small-en-v1.5)",
            "fix": "",
        })
    else:
        checks.append({
            "status": "fail",
            "check": "embedder_cache",
            "what": "Embedder model not cached locally",
            "fix": "python -c 'from fastembed import TextEmbedding; TextEmbedding(\"BAAI/bge-small-en-v1.5\")'",
        })

    # 8. Whether the cwd is indexed and how stale that is
    ingest_cache = IngestCache(CACHE_DB_PATH)
    cwd_str = str(work_dir).replace("\\", "/")
    cwd_files: Dict[str, Dict[str, float]] = {}
    try:
        with ingest_cache._get_connection() as conn:
            rows = conn.execute("SELECT file_path, mtime, updated_at FROM file_cache").fetchall()
            for r in rows:
                fp = str(r["file_path"]).replace("\\", "/")
                if fp.startswith(cwd_str):
                    cwd_files[fp] = {"mtime": float(r["mtime"]), "updated_at": float(r["updated_at"])}
    except Exception:
        pass

    if not cwd_files:
        checks.append({
            "status": "warn",
            "check": "cwd_indexed",
            "what": f"Current directory is not indexed: {work_dir.name}",
            "fix": "kortext index",
        })
    else:
        # Check staleness by scanning mtimes of sample source files
        stale_count = 0
        sample_code_files = list(work_dir.glob("*.py"))[:10] + list((work_dir / "kortext").glob("*.py"))[:10] if (work_dir / "kortext").is_dir() else list(work_dir.glob("*.py"))[:15]
        for f in sample_code_files:
            f_norm = str(f.resolve()).replace("\\", "/")
            if f_norm in cwd_files:
                cached_mtime = cwd_files[f_norm].get("mtime", 0.0)
                try:
                    if f.stat().st_mtime > cached_mtime + 0.01:
                        stale_count += 1
                except OSError:
                    pass

        if stale_count > 0:
            checks.append({
                "status": "warn",
                "check": "cwd_indexed",
                "what": f"Current directory indexed ({len(cwd_files)} files), but stale ({stale_count} modified files)",
                "fix": "kortext index",
            })
        else:
            checks.append({
                "status": "ok",
                "check": "cwd_indexed",
                "what": f"Current directory indexed and up-to-date ({len(cwd_files)} files in cache)",
                "fix": "",
            })

    # 9. Failed re-index events in spool/failed/
    reindex = reindex_spool_status(limit=5)
    failed_count = int(reindex.get("failed", 0))
    if failed_count == 0:
        checks.append({
            "status": "ok",
            "check": "spool_failed",
            "what": "No failed re-index events in spool/failed/",
            "fix": "",
        })
    else:
        checks.append({
            "status": "warn",
            "check": "spool_failed",
            "what": f"{failed_count} failed re-index event(s) in spool/failed/",
            "fix": "mv ~/.kortext/spool/failed/* ~/.kortext/spool/invalidation/",
        })

    # 10. The agy version found
    agy_bin = shutil.which("agy")
    if agy_bin:
        ver = "found"
        try:
            res = subprocess.run([agy_bin, "--version"], capture_output=True, text=True, timeout=1.0)
            ver = (res.stdout or res.stderr).strip().splitlines()[0] if (res.stdout or res.stderr).strip() else "found"
        except Exception:
            pass
        checks.append({
            "status": "ok",
            "check": "agy_version",
            "what": f"agy version {ver} found at {agy_bin}",
            "fix": "",
        })
    else:
        checks.append({
            "status": "warn",
            "check": "agy_version",
            "what": "agy binary not found on PATH",
            "fix": "Install Antigravity CLI (agy) and add to PATH",
        })

    return checks


def format_doctor_report(checks: List[Dict[str, str]]) -> str:
    """Formats one line each: status (ok, warn, fail), what, and the exact command that fixes it."""
    lines = []
    for c in checks:
        st = c["status"].lower()
        what = c["what"]
        fix = c.get("fix", "").strip()
        if fix and st != "ok":
            lines.append(f"[{st}] {what} - Fix: {fix}")
        else:
            lines.append(f"[{st}] {what}")
    return "\n".join(lines)
