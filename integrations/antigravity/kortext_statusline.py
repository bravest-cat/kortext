#!/usr/bin/env python3
"""
Kortext Zero-Network Statusline HUD for Antigravity CLI (<15ms).

Supports:
  1. Fast atomic snapshot reading (~/.kortext/run/hud_state.json and ~/.kortext/watcher_status.json)
     to avoid spawning live FalkorDB GRAPH.QUERY or headless CLI subprocesses on every TUI redraw.
  2. Embedded Qdrant detection (~/.kortext/qdrant/meta.json) when QDRANT_URL is unset,
     plus HTTP/TCP :6333 when QDRANT_URL is set.
  3. FalkorDB Unix Domain Socket (~/.kortext/run/falkordb.sock) and TCP :6379 checks.
  4. Native parsing of Antigravity statusline stdin JSON payload (workspace.project_dir, cwd,
     workspacePaths, model, context_window, nested quota buckets, agent_state, task_count, execution_mode)
     + single read-only Cypher query for project-scoped FalkorDB ready/in-progress task counts.
  5. Bridges statusline context_window metrics (total_input_tokens, context_window_size, used_percentage,
     exceeds_200k_tokens) into ~/.kortext/run/hud_state.json for 25%-compaction-aware lifecycle hooks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any, Dict, Optional, Tuple


def check_unix_socket(sock_path: Path, timeout: float = 0.05) -> bool:
    if not sock_path.is_socket():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock_path))
            return True
    except Exception:
        return False


def check_tcp_socket(host: str, port: int, timeout: float = 0.05) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def check_falkor(kortext_home: Path) -> bool:
    sock_path = kortext_home / "run" / "falkordb.sock"
    if check_unix_socket(sock_path):
        return True
    return check_tcp_socket("127.0.0.1", 6379)


def check_qdrant(kortext_home: Path) -> bool:
    qdrant_url = os.getenv("QDRANT_URL", "").strip()
    if not qdrant_url or qdrant_url.lower() in ("none", "embedded", "local", "disabled"):
        qdir = kortext_home / "qdrant"
        if (qdir / "meta.json").is_file() or qdir.is_dir():
            return True
    return check_tcp_socket("127.0.0.1", 6333)


def check_omlx() -> bool:
    return check_tcp_socket("127.0.0.1", 8000)


_ORIG_PROBES = (check_falkor, check_qdrant, check_omlx)


def read_watcher_status(kortext_home: Path, max_age_sec: float = 60.0) -> Optional[Dict[str, Any]]:
    """Reads ~/.kortext/watcher_status.json or ~/.kortext/run/watcher_status.json if fresh."""
    for candidate in (kortext_home / "watcher_status.json", kortext_home / "run" / "watcher_status.json"):
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    ts = float(data.get("updated_at") or data.get("timestamp") or 0.0)
                    if ts == 0.0 or (time.time() - ts <= max_age_sec):
                        return data
            except Exception:
                continue
    return None


def read_hud_snapshot(
    kortext_home: Path,
    project_id: Optional[str] = None,
    max_age_sec: float = 60.0,
) -> Optional[Dict[str, Any]]:
    """Reads ~/.kortext/run/hud_state.json if fresh within max_age_sec (default 60s)."""
    hud_file = kortext_home / "run" / "hud_state.json"
    if not hud_file.is_file():
        return None
    try:
        data = json.loads(hud_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        updated_at = float(data.get("updated_at") or 0.0)
        if time.time() - updated_at > max_age_sec:
            return None
        if project_id:
            projects = data.get("projects")
            if isinstance(projects, dict) and project_id in projects:
                p_data = projects[project_id]
                if isinstance(p_data, dict):
                    merged = dict(data)
                    merged["ready_tasks"] = int(p_data.get("ready_tasks", data.get("ready_tasks", 0)) or 0)
                    merged["in_progress_tasks"] = int(
                        p_data.get("in_progress_tasks", data.get("in_progress_tasks", 0)) or 0
                    )
                    return merged
        return data
    except Exception:
        return None


def write_hud_snapshot(
    kortext_home: Path,
    falkor_ok: bool,
    qdrant_ok: bool,
    omlx_ok: bool,
    ready_cnt: int,
    in_prog_cnt: int,
    project_id: Optional[str] = None,
    used_percentage: Optional[float] = None,
    total_input_tokens: Optional[int] = None,
    context_window_size: Optional[int] = None,
    exceeds_200k_tokens: Optional[bool] = None,
    session_id: str = "",
) -> None:
    """Atomically updates ~/.kortext/run/hud_state.json; token figures are also kept per session."""
    try:
        run_dir = kortext_home / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        hud_file = run_dir / "hud_state.json"
        existing: Dict[str, Any] = {}
        if hud_file.is_file():
            try:
                existing = json.loads(hud_file.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:
                existing = {}

        existing["falkor_ok"] = bool(falkor_ok)
        existing["qdrant_ok"] = bool(qdrant_ok)
        existing["omlx_ok"] = bool(omlx_ok)
        existing["ready_tasks"] = int(ready_cnt)
        existing["in_progress_tasks"] = int(in_prog_cnt)
        if used_percentage is not None:
            existing["used_percentage"] = float(used_percentage)
        if total_input_tokens is not None:
            existing["total_input_tokens"] = int(total_input_tokens)
        if context_window_size is not None:
            existing["context_window_size"] = int(context_window_size)
        if exceeds_200k_tokens is not None:
            existing["exceeds_200k_tokens"] = bool(exceeds_200k_tokens)
        existing["updated_at"] = time.time()
        if session_id and (used_percentage is not None or total_input_tokens is not None):
            now = time.time()
            sessions = {
                k: v for k, v in (existing.get("sessions") or {}).items()
                if isinstance(v, dict) and now - float(v.get("updated_at") or 0) < 86400
            }
            sessions[session_id] = {
                "used_percentage": used_percentage,
                "total_input_tokens": total_input_tokens,
                "context_window_size": context_window_size,
                "exceeds_200k_tokens": bool(exceeds_200k_tokens),
                "updated_at": now,
            }
            existing["sessions"] = sessions

        if project_id:
            projects = existing.get("projects")
            if not isinstance(projects, dict):
                projects = {}
            projects[project_id] = {
                "ready_tasks": int(ready_cnt),
                "in_progress_tasks": int(in_prog_cnt),
            }
            existing["projects"] = projects

        tmp_file = hud_file.with_suffix(f".tmp.{os.getpid()}")
        tmp_file.write_text(json.dumps(existing), encoding="utf-8")
        tmp_file.replace(hud_file)
    except Exception:
        pass


def query_dag_task_counts(falkor_ok: bool, project_id: Optional[str] = None) -> Tuple[int, int]:
    """Queries (ready_count, in_progress_count) via TaskDAGTracker or read-only FalkorDB query."""
    if not falkor_ok:
        return 0, 0
    try:
        repo_root = Path(__file__).resolve().parent.parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from kortext.tasks import tracker as tracker_mod

        t_inst = tracker_mod.TaskDAGTracker()
        return len(t_inst.get_ready_tasks(project_id=project_id)), len(
            t_inst.get_in_progress_tasks(project_id=project_id)
        )
    except Exception:
        return 0, 0


_ORIG_QUERY_COUNTS = query_dag_task_counts


def parse_stdin_state(raw_stdin: str) -> Dict[str, Any]:
    if not raw_stdin or not raw_stdin.strip():
        return {}
    try:
        data = json.loads(raw_stdin)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _session_id_from_state(state: Dict[str, Any]) -> str:
    return str(state.get("conversationId") or state.get("conversation_id") or state.get("sessionId") or state.get("session_id") or "")


def _resolve_project_from_state(state: Dict[str, Any]) -> Optional[str]:
    ws_obj = state.get("workspace")
    if isinstance(ws_obj, dict):
        proj_dir = str(ws_obj.get("project_dir") or ws_obj.get("current_dir") or "").strip()
        if proj_dir:
            name = Path(proj_dir).name.strip()
            if name and name not in ("/", ".", ""):
                return name
    cwd = str(state.get("cwd") or "").strip()
    if cwd:
        name = Path(cwd).name.strip()
        if name and name not in ("/", ".", ""):
            return name
    ws = state.get("workspacePaths") or state.get("workspace_paths")
    if isinstance(ws, list) and ws:
        first_ws = str(ws[0] or "").strip()
        if first_ws:
            name = Path(first_ws).name.strip()
            if name and name not in ("/", ".", ""):
                return name
    return None


def _extract_quota_percentage(quota: Any) -> Optional[float]:
    """Extracts remaining quota percentage from either flat keys or Antigravity nested bucket dicts."""
    if not isinstance(quota, dict):
        return None
    direct = quota.get("remaining_percentage") if "remaining_percentage" in quota else quota.get("remainingPercentage")
    if direct is not None:
        try:
            return float(direct)
        except (ValueError, TypeError):
            pass
    frac = quota.get("remaining_fraction") if "remaining_fraction" in quota else quota.get("remainingFraction")
    if frac is not None:
        try:
            return float(frac) * 100.0
        except (ValueError, TypeError):
            pass

    bucket_pcts = []
    for val in quota.values():
        if isinstance(val, dict):
            b_frac = val.get("remaining_fraction") if "remaining_fraction" in val else val.get("remainingFraction")
            if b_frac is not None:
                try:
                    bucket_pcts.append(float(b_frac) * 100.0)
                    continue
                except (ValueError, TypeError):
                    pass
            b_pct = val.get("remaining_percentage") if "remaining_percentage" in val else val.get("remainingPercentage")
            if b_pct is not None:
                try:
                    bucket_pcts.append(float(b_pct))
                except (ValueError, TypeError):
                    pass
    if bucket_pcts:
        return min(bucket_pcts)
    return None


def format_kortext_badge(compact: bool = False, state: Optional[Dict[str, Any]] = None) -> str:
    kortext_home = Path(os.getenv("KORTEXT_HOME", str(Path.home() / ".kortext"))).expanduser()
    if not kortext_home.is_dir() and "KORTEXT_HOME" not in os.environ:
        host_fallback = Path(__file__).resolve().parents[3] / ".kortext"
        if host_fallback.is_dir():
            kortext_home = host_fallback
    state = state or {}
    proj = _resolve_project_from_state(state)
    session_id = _session_id_from_state(state)

    use_snapshot = (
        (check_falkor, check_qdrant, check_omlx) == _ORIG_PROBES
        and query_dag_task_counts is _ORIG_QUERY_COUNTS
    )
    watcher_info = read_watcher_status(kortext_home) if use_snapshot else None
    snap = read_hud_snapshot(kortext_home, project_id=proj) if use_snapshot else None

    cw = state.get("context_window")
    used_pct_val: Optional[float] = None
    total_input_tokens: Optional[int] = None
    context_window_size: Optional[int] = None
    if isinstance(cw, dict):
        if cw.get("total_input_tokens") is not None:
            try:
                total_input_tokens = int(cw["total_input_tokens"])
            except (ValueError, TypeError):
                total_input_tokens = None
        if cw.get("context_window_size") is not None:
            try:
                context_window_size = int(cw["context_window_size"])
            except (ValueError, TypeError):
                context_window_size = None
        raw_pct = cw.get("used_percentage")
        if raw_pct is None and total_input_tokens and context_window_size:
            try:
                raw_pct = (float(total_input_tokens) / float(context_window_size)) * 100.0
            except (ValueError, TypeError, ZeroDivisionError):
                raw_pct = None
        if raw_pct is not None:
            try:
                used_pct_val = float(raw_pct)
            except (ValueError, TypeError):
                used_pct_val = None

    exceeds_200k = bool(state.get("exceeds_200k_tokens", False))

    if snap is not None and "falkor_ok" in snap and "qdrant_ok" in snap and "omlx_ok" in snap:
        falkor_ok = bool(snap.get("falkor_ok"))
        qdrant_ok = bool(snap.get("qdrant_ok") or (watcher_info and watcher_info.get("status") == "running"))
        omlx_ok = bool(snap.get("omlx_ok"))
        ready_cnt = int(snap.get("ready_tasks") or 0)
        in_prog_cnt = int(snap.get("in_progress_tasks") or 0)
        if use_snapshot and (used_pct_val is not None or total_input_tokens is not None or exceeds_200k):
            write_hud_snapshot(
                kortext_home,
                falkor_ok,
                qdrant_ok,
                omlx_ok,
                ready_cnt,
                in_prog_cnt,
                project_id=proj,
                used_percentage=used_pct_val,
                total_input_tokens=total_input_tokens,
                context_window_size=context_window_size,
                session_id=session_id,
                exceeds_200k_tokens=exceeds_200k,
            )
    else:
        falkor_ok = check_falkor(kortext_home)
        qdrant_ok = check_qdrant(kortext_home) or bool(watcher_info and watcher_info.get("status") == "running")
        omlx_ok = check_omlx()
        ready_cnt, in_prog_cnt = query_dag_task_counts(falkor_ok, project_id=proj)
        if use_snapshot:
            write_hud_snapshot(
                kortext_home,
                falkor_ok,
                qdrant_ok,
                omlx_ok,
                ready_cnt,
                in_prog_cnt,
                project_id=proj,
                used_percentage=used_pct_val,
                total_input_tokens=total_input_tokens,
                context_window_size=context_window_size,
                session_id=session_id,
                exceeds_200k_tokens=exceeds_200k,
            )

    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    GRAY = "\033[90m"
    CYAN = "\033[36m"
    RESET = "\033[0m"

    dot_f = f"{GREEN}●{RESET}" if falkor_ok else f"{RED}●{RESET}"
    dot_q = f"{GREEN}●{RESET}" if qdrant_ok else f"{RED}●{RESET}"
    dot_m = f"{GREEN}●{RESET}" if omlx_ok else f"{GRAY}○{RESET}"

    cli_tasks = int(state.get("task_count") or 0) if str(state.get("task_count") or "").isdigit() else 0

    dag_segment = ""
    if ready_cnt > 0 or in_prog_cnt > 0:
        dag_segment = f" {CYAN}DAG:{ready_cnt}r/{in_prog_cnt}p{RESET}"
    elif cli_tasks > 0:
        dag_segment = f" {CYAN}T:{cli_tasks}{RESET}"

    ctx_segment = ""
    compact_ctx_warn = ""
    is_large_win = bool(context_window_size and context_window_size >= 500_000)
    warn_pct = 20.0 if is_large_win else 65.0
    red_pct = 23.0 if is_large_win else 80.0
    yellow_pct = 15.0 if is_large_win else 60.0

    if used_pct_val is not None or exceeds_200k:
        pct_num = used_pct_val if used_pct_val is not None else 100.0
        color = RED if (pct_num >= red_pct or exceeds_200k) else (YELLOW if pct_num >= yellow_pct else GREEN)
        ctx_segment = f" {GRAY}Ctx:{RESET}{color}{pct_num:.0f}%{RESET}"
        pre_warn_pct = 18.0 if is_large_win else 55.0
        if pct_num >= warn_pct or exceeds_200k or (total_input_tokens and total_input_tokens >= 200_000):
            compact_ctx_warn = f" {YELLOW}⚠️ Compact{RESET}"
        elif pct_num >= pre_warn_pct or (total_input_tokens and total_input_tokens >= 180_000):
            compact_ctx_warn = f" {YELLOW}⚡ Pre-Compact{RESET}"

    quota_segment = ""
    q_pct = _extract_quota_percentage(state.get("quota"))
    if q_pct is not None:
        q_color = RED if q_pct <= 15.0 else (YELLOW if q_pct <= 40.0 else GREEN)
        quota_segment = f" {GRAY}Q:{RESET}{q_color}{q_pct:.0f}%{RESET}"

    if compact:
        return f"{GRAY}│{RESET} {CYAN}Kortext{RESET} F:{dot_f} Q:{dot_q} M:{dot_m}{dag_segment}{compact_ctx_warn}"

    mode_str = str(state.get("execution_mode") or "").strip()
    mode_segment = f" | {GRAY}Mode:{RESET} {mode_str}" if mode_str else ""
    return (
        f"{CYAN}🧠 Kortext{RESET} "
        f"[{GRAY}Falkor:{RESET} {dot_f} | "
        f"{GRAY}Qdrant:{RESET} {dot_q} | "
        f"{GRAY}oMLX:{RESET} {dot_m}"
        f"{dag_segment}{ctx_segment}{quota_segment}{mode_segment}]"
    )


def main():
    compact = "--compact" in sys.argv
    refresh_only = "--refresh-only" in sys.argv
    sync_tokens_only = "--sync-tokens-only" in sys.argv
    raw_stdin = ""
    if not sys.stdin.isatty():
        try:
            raw_stdin = sys.stdin.read()
        except Exception:
            raw_stdin = ""

    state = parse_stdin_state(raw_stdin)

    cw = state.get("context_window")
    used_pct_val: Optional[float] = None
    total_input_tokens: Optional[int] = None
    context_window_size: Optional[int] = None
    if isinstance(cw, dict):
        if cw.get("used_percentage") is not None:
            try:
                used_pct_val = float(cw["used_percentage"])
            except (ValueError, TypeError):
                pass
        if cw.get("total_input_tokens") is not None:
            try:
                total_input_tokens = int(cw["total_input_tokens"])
            except (ValueError, TypeError):
                pass
        if cw.get("context_window_size") is not None:
            try:
                context_window_size = int(cw["context_window_size"])
            except (ValueError, TypeError):
                pass
    exceeds_200k = bool(state.get("exceeds_200k_tokens", False))
    session_id = _session_id_from_state(state)

    if sync_tokens_only:
        if used_pct_val is not None or total_input_tokens is not None or exceeds_200k:
            kortext_home = Path(os.getenv("KORTEXT_HOME", str(Path.home() / ".kortext"))).expanduser()
            proj = _resolve_project_from_state(state)
            snap = read_hud_snapshot(kortext_home, project_id=proj)
            if snap is not None and "falkor_ok" in snap:
                falkor_ok = bool(snap.get("falkor_ok", True))
                qdrant_ok = bool(snap.get("qdrant_ok", True))
                omlx_ok = bool(snap.get("omlx_ok", False))
                ready_cnt = int(snap.get("ready_tasks") or 0)
                in_prog_cnt = int(snap.get("in_progress_tasks") or 0)
            else:
                falkor_ok = check_falkor(kortext_home)
                qdrant_ok = check_qdrant(kortext_home)
                omlx_ok = check_omlx()
                ready_cnt, in_prog_cnt = query_dag_task_counts(falkor_ok, project_id=proj)
            write_hud_snapshot(
                kortext_home,
                falkor_ok,
                qdrant_ok,
                omlx_ok,
                ready_cnt,
                in_prog_cnt,
                project_id=proj,
                used_percentage=used_pct_val,
                total_input_tokens=total_input_tokens,
                context_window_size=context_window_size,
                session_id=session_id,
                exceeds_200k_tokens=exceeds_200k,
            )
        return

    if refresh_only:
        kortext_home = Path(os.getenv("KORTEXT_HOME", str(Path.home() / ".kortext"))).expanduser()
        proj = _resolve_project_from_state(state)
        falkor_ok = check_falkor(kortext_home)
        qdrant_ok = check_qdrant(kortext_home)
        omlx_ok = check_omlx()
        ready_cnt, in_prog_cnt = query_dag_task_counts(falkor_ok, project_id=proj)
        write_hud_snapshot(
            kortext_home,
            falkor_ok,
            qdrant_ok,
            omlx_ok,
            ready_cnt,
            in_prog_cnt,
            project_id=proj,
            used_percentage=used_pct_val,
            total_input_tokens=total_input_tokens,
            context_window_size=context_window_size,
            exceeds_200k_tokens=exceeds_200k,
        )
        return

    if compact:
        sys.stdout.write(format_kortext_badge(compact=True, state=state))
        sys.stdout.flush()
        return

    print(format_kortext_badge(compact=False, state=state))


if __name__ == "__main__":
    main()
