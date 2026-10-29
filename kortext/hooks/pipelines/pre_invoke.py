"""
Pre-invocation pipeline: Substrate health probing, Task DAG notifications,
Turn-0 /boost AST grounding, and 25%-compaction-aware context budgeting. Silent by default
to protect KV-cache prefix reuse: only injects a consolidated ephemeralMessage when a
substrate is offline, active ready/in_progress tasks exist for the current workspace
project, an explicit /boost session starts, or a context budget / post-compaction transition occurs.
Conforms strictly to Antigravity PreInvocation schema: {"injectSteps": [{"ephemeralMessage": "..."}]}.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
import urllib.request
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Sequence

from kortext._home import get_kortext_home
from kortext.hooks.context import HookContext
from kortext.hooks.types import InterceptorResult

logger = logging.getLogger("kortext.hooks.pipelines.pre_invoke")


def _is_raw_falkor_reachable() -> bool:
    """Fast unmocked OS socket check (<20ms) for subprocesses outside pytest conftest."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.02)
            s.connect(("127.0.0.1", 6379))
            return True
    except OSError:
        return False


def _check_unix_socket(sock_path: Path, timeout_sec: float = 0.05) -> bool:
    """Non-blocking connect probe for Unix domain sockets."""
    if not sock_path.is_socket():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_sec)
            sock.connect(str(sock_path))
            return True
    except Exception:
        return False


def _check_tcp(host: str, port: int, timeout_sec: float = 0.05) -> bool:
    """Non-blocking TCP socket connect probe."""
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True
    except Exception:
        return False


def _check_http(host: str, port: int, path: str = "/healthz", timeout_sec: float = 0.05) -> bool:
    """Non-blocking HTTP GET probe."""
    try:
        req = urllib.request.Request(f"http://{host}:{port}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return resp.status < 500
    except Exception:
        return False


def _check_qdrant_ready(home: Path, timeout_sec: float = 0.05) -> bool:
    """Checks Qdrant readiness across both embedded mode and HTTP mode."""
    qdrant_url = os.getenv("QDRANT_URL", "").strip()
    if not qdrant_url:
        qdir = home / "qdrant"
        if (qdir / "meta.json").is_file() or qdir.is_dir():
            return True
    if _check_http("127.0.0.1", 6333, path="/healthz", timeout_sec=timeout_sec):
        return True
    return _check_tcp("127.0.0.1", 6333, timeout_sec=timeout_sec)


class SubstrateHealthProbe:
    """
    Non-blocking socket/storage probe (<2ms) verifying FalkorDB (UDS/TCP) and Qdrant (embedded/HTTP).
    Silent when healthy; only injects an ephemeralMessage if a substrate is offline on Turn 0.
    """

    def __init__(self, timeout_sec: float = 0.05):
        self.timeout_sec = timeout_sec

    def check(self, context: HookContext) -> InterceptorResult:
        if not context.is_first_turn:
            return InterceptorResult.allow()

        home = get_kortext_home()
        falkor_sock = home / "run" / "falkordb.sock"

        falkor_ok = False
        if _check_unix_socket(falkor_sock, self.timeout_sec):
            falkor_ok = True
        elif _check_tcp("127.0.0.1", 6379, self.timeout_sec):
            falkor_ok = True

        qdrant_ok = _check_qdrant_ready(home, self.timeout_sec)
        omlx_ok = _check_tcp("127.0.0.1", 8000, self.timeout_sec)

        try:
            from kortext.tasks.tracker import write_hud_state_snapshot

            write_hud_state_snapshot(falkor_ok=falkor_ok, qdrant_ok=qdrant_ok, omlx_ok=omlx_ok)
        except Exception as exc:
            logger.debug("Suppressed in pre_invoke:%d: %s", 114, exc)

        down_services: List[str] = []
        if not falkor_ok:
            down_services.append("FalkorDB")
        if not qdrant_ok:
            down_services.append("Qdrant")

        data: Dict[str, Any] = {
            "falkordb": falkor_ok,
            "qdrant": qdrant_ok,
            "down_services": down_services,
        }

        if down_services:
            msg = (
                f"⚠️ [Kortext Health Alert] Offline services detected: {', '.join(down_services)}. "
                "Run `kortext start` to bring memory and graph substrates online."
            )
            print(msg, file=sys.stderr)
            data["health_alert"] = msg
            if context.is_first_turn:
                data["injectSteps"] = ({"ephemeralMessage": msg},)

        return InterceptorResult.allow(data=MappingProxyType(data))


def _is_falkor_substrate_available(tracker_cls: Any = None, client_cls: Any = None) -> bool:
    """Returns True if FalkorDB is reachable via UDS/TCP or if running under test harness."""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    home = get_kortext_home()
    if _check_unix_socket(home / "run" / "falkordb.sock", timeout_sec=0.02):
        return True
    return _is_raw_falkor_reachable()


class FrontierDAGNotifier:
    """
    Checks manage_tasks / TaskDAGTracker for unblocked ready or active in_progress tasks
    scoped to the active workspace project on Turn 0. Silent when no tasks are active.
    """

    def process(self, context: HookContext) -> InterceptorResult:
        if not context.is_first_turn:
            return InterceptorResult.allow()

        try:
            from kortext.falkordb_client import FalkorDBClient
            from kortext.tasks.tracker import TaskDAGTracker, write_hud_state_snapshot

            if not _is_falkor_substrate_available(TaskDAGTracker, FalkorDBClient):
                return InterceptorResult.allow()

            tracker = TaskDAGTracker()
            project_id = context.workspace_project or "kortext"
            ready_tasks = tracker.get_ready_tasks(project_id=project_id)
            in_prog_fn = getattr(tracker, "get_in_progress_tasks", None)
            in_progress_tasks = in_prog_fn(project_id=project_id) if callable(in_prog_fn) else []

            write_hud_state_snapshot(
                ready_tasks=len(ready_tasks),
                in_progress_tasks=len(in_progress_tasks),
                project_id=project_id,
            )

            active_tasks = list(in_progress_tasks) + list(ready_tasks)
            if active_tasks:
                task_summary = [f"{t.get('id')}: {t.get('title')}" for t in active_tasks[:3]]
                msg = f"📋 [Frontier DAG Notice] Active tasks awaiting execution: {', '.join(task_summary)}"
                return InterceptorResult.allow(
                    data=MappingProxyType({
                        "ready_tasks": tuple(ready_tasks),
                        "in_progress_tasks": tuple(in_progress_tasks),
                        "dag_notice": msg,
                        "injectSteps": ({"ephemeralMessage": msg},),
                    })
                )
        except Exception as e:
            logger.debug("FrontierDAGNotifier check bypassed: %s", e)

        return InterceptorResult.allow()


class BoostPromptGroundingProbe:
    """
    Automatically grounds Antigravity /boost or /kortext-boost invocations on Turn 0
    by extracting candidate AST symbols from the user prompt and injecting a <400-token
    Grounded Execution Brief (exact file paths, line numbers, signatures, and 4-pattern route).
    Silent on normal turns unless /boost, /kortext-boost, or execution_mode='boost' is active.
    """

    def _extract_user_prompt(self, context: HookContext) -> str:
        if getattr(context, "last_user_input", ""):
            return str(context.last_user_input).strip()
        raw = context.raw_payload
        if isinstance(raw, (dict, MappingProxyType)):
            for key in ("lastUserInput", "last_user_input", "prompt", "userPrompt", "query", "message"):
                val = raw.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        steps = context.read_transcript_steps()
        for step in reversed(steps):
            if isinstance(step, (dict, MappingProxyType)):
                for key in ("user_message", "prompt", "content", "text"):
                    val = step.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        return ""

    def process(self, context: HookContext) -> InterceptorResult:
        raw = context.raw_payload if isinstance(context.raw_payload, (dict, MappingProxyType)) else {}
        exec_mode = str(raw.get("execution_mode") or raw.get("executionMode") or "").strip().lower()
        prompt_text = self._extract_user_prompt(context)
        explicit_slash = prompt_text.lstrip().startswith(("/boost", "/kortext-boost", "/teamwork-preview"))

        if not context.is_first_turn and not explicit_slash:
            return InterceptorResult.allow()

        is_boost_session = (
            exec_mode in ("boost", "deepcoder", "deepinvestigator", "teamwork")
            or explicit_slash
        )
        if not is_boost_session or not prompt_text:
            return InterceptorResult.allow()

        try:
            from kortext.symbols.seek import SymbolSeekEngine, extract_symbol_candidates
            from kortext.tasks.tracker import TaskDAGTracker

            candidates = extract_symbol_candidates(prompt_text)
            if not candidates:
                return InterceptorResult.allow()

            seeker = SymbolSeekEngine()
            matched_lines: List[str] = []
            target_files: List[str] = []
            for sym in candidates[:4]:
                res_raw = seeker.seek_symbol(sym, detail_level="skeleton")
                parsed = json.loads(res_raw) if isinstance(res_raw, str) else res_raw
                if isinstance(parsed, dict) and parsed.get("matches"):
                    m = parsed["matches"][0]
                    fp = str(m.get("file") or m.get("file_path") or "")
                    ln = m.get("line", 1)
                    sig = str(m.get("signature") or sym)
                    if fp and fp not in target_files:
                        target_files.append(fp)
                    matched_lines.append(f"• `{sig}` → `{fp}:{ln}`")

            if not matched_lines:
                return InterceptorResult.allow()

            tracker = TaskDAGTracker()
            proj_id = context.workspace_project or "kortext"
            route = tracker.recommend_execution_pattern(
                symbols=candidates[:4],
                target_files=target_files,
                complexity=5,
                project_id=proj_id,
            )
            overlap_info = tracker.detect_file_overlaps(target_files, project_id=proj_id)
            ownership_note = (
                f"Exclusive Ownership: `{overlap_info.get('recommended_workspace_mode', 'shared')}` "
                f"({len(overlap_info.get('overlapping_tasks', []))} active overlaps)"
            )
            inv_lines: List[str] = []
            try:
                from kortext.invariants import grounded_read_path

                g_file = grounded_read_path()

                if g_file.is_file():
                    g_items = json.loads(g_file.read_text(encoding="utf-8"))
                    if isinstance(g_items, list):
                        t_fnames = {Path(f).name for f in target_files}
                        for g_inv in g_items:
                            if not isinstance(g_inv, dict) or g_inv.get("quarantined"):
                                continue
                            if g_inv.get("symbol_name") in candidates or Path(str(g_inv.get("file_path", ""))).name in t_fnames:
                                inv_lines.append(f"[{g_inv.get('domain', 'INV')}] {g_inv.get('rule', '')}")
            except Exception as exc:
                logger.debug("Suppressed in pre_invoke:%d: %s", 295, exc)

            brief_parts = [
                "⚡ [Kortext /boost Grounding Brief]",
                *matched_lines[:4],
                f"Target Files: `{', '.join(target_files[:4])}`" if target_files else "",
                f"Recommended Pattern: `{route['recommended_pattern']}` ({route['token_multiplier_estimate']} tokens, {ownership_note})",
            ]
            if inv_lines:
                brief_parts.append("Active Invariants: " + "; ".join(inv_lines[:2]))
            brief_parts.append("Verification Gate: `uv run pytest -q`")
            brief = "\n".join(p for p in brief_parts if p)

            if context.cwd:
                try:
                    brief_file = Path(context.cwd) / ".kortext" / "briefs" / "boost_grounding.md"
                    brief_file.parent.mkdir(parents=True, exist_ok=True)
                    brief_file.write_text(brief + "\n", encoding="utf-8")
                except Exception as exc:
                    logger.debug("Suppressed in pre_invoke:%d: %s", 314, exc)

            return InterceptorResult.allow(
                data=MappingProxyType({
                    "boost_brief": brief,
                    "target_files": tuple(target_files),
                    "injectSteps": ({"ephemeralMessage": brief},),
                })
            )
        except Exception as exc:
            logger.debug("BoostPromptGroundingProbe skipped: %s", exc)
            return InterceptorResult.allow()


class ContextBudgetAdvisor:
    """
    True token-aware and step-bucket-gated context advisor with 25%-compaction-ceiling awareness,
    automatic pre-compaction baton persistence, and post-compaction recovery injection.
    Fires only on state transitions (step multiples, token bucket crossings, or compaction recovery)
    to preserve prompt-cache stability.
    """

    def __init__(
        self,
        warning_threshold_steps: int = 20,
        token_warning_threshold: int = 120_000,
        pct_warning_threshold: float = 60.0,
        handoffs_dir: Optional[Path] = None,
    ):
        self.warning_threshold_steps = warning_threshold_steps
        self.token_warning_threshold = token_warning_threshold
        self.pct_warning_threshold = pct_warning_threshold
        self.handoffs_dir = handoffs_dir or (get_kortext_home() / "handoffs")

    def _should_fire_bucket(self, session_id: str, bucket_key: str) -> bool:
        if not session_id:
            return True
        state_file = get_kortext_home() / "spool" / "budget_buckets.json"
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
            if not isinstance(data, dict):
                data = {}
            seen = data.get(session_id, [])
            if bucket_key in seen:
                return False
            seen.append(bucket_key)
            data[session_id] = seen[-20:]
            state_file.write_text(json.dumps(data), encoding="utf-8")
            return True
        except Exception as e:
            logger.debug("Bucket state check bypassed: %s", e)
            return True

    def process(self, context: HookContext) -> InterceptorResult:
        from kortext.hooks.transcript_tokens import (
            check_and_update_compaction_watermark,
            load_post_compaction_recovery_brief,
            resolve_session_token_state,
        )

        steps = context.read_transcript_steps()
        step_count = len(steps)
        token_state = resolve_session_token_state(
            steps=steps,
            raw_payload=context.raw_payload,
            transcript_path=context.transcript_path,
            model_name=context.model_name,
        )
        est_tokens = token_state.estimated_tokens
        used_pct = token_state.used_percentage
        exceeds_200k = token_state.exceeds_200k

        conv_key = getattr(context, "conversation_id", "") or context.session_id
        wm = check_and_update_compaction_watermark(
            session_id=conv_key,
            step_count=step_count,
            estimated_tokens=est_tokens,
            used_percentage=used_pct,
        )
        resuming_marker = "# Resuming from a compaction" in str(getattr(context, "last_user_input", "") or "")
        if wm.get("compacted") or resuming_marker:
            recovery_brief = load_post_compaction_recovery_brief(
                session_id=conv_key or "compacted_session",
                project_id=context.workspace_project or "kortext",
                workspace_dir=context.cwd or None,
            )
            if recovery_brief:
                return InterceptorResult.allow(
                    data=MappingProxyType({
                        "compaction_recovered": True,
                        "recovery_brief": recovery_brief,
                        "step_count": step_count,
                        "estimated_tokens": est_tokens,
                        "used_percentage": used_pct,
                        "injectSteps": ({"ephemeralMessage": recovery_brief},),
                    })
                )

        token_pressure = (
            token_state.stage1_advisory_reached
            or est_tokens >= self.token_warning_threshold
            or used_pct >= self.pct_warning_threshold
            or exceeds_200k
        )
        step_boundary = (
            step_count >= self.warning_threshold_steps
            and (step_count % self.warning_threshold_steps == 0 or not context.session_id)
        )

        if not (token_pressure or step_boundary):
            return InterceptorResult.allow()

        bucket_step = 5 if token_state.is_large_window else 20
        bucket_label = (
            f"tok_{int(used_pct // bucket_step) * bucket_step}"
            if token_pressure
            else f"step_{(step_count // self.warning_threshold_steps) * self.warning_threshold_steps}"
        )
        if not self._should_fire_bucket(context.session_id, bucket_label):
            return InterceptorResult.allow()

        baton_path_str: Optional[str] = None
        if token_pressure or token_state.stage2_precompact_reached:
            try:
                from kortext.handoff import HandoffManager

                hm = HandoffManager(self.handoffs_dir)
                hm.create_baton(
                    target_agent="antigravity",
                    current_scope=(
                        f"Auto-checkpoint for session {context.session_id or 'active'} at "
                        f"~{est_tokens:,} tokens ({used_pct:.1f}% window, "
                        f"{token_state.compaction_budget_pct:.0f}% of ~250k compaction ceiling)"
                    ),
                    open_hypotheses=[f"Session reached {step_count} steps / ~{est_tokens} tokens before compaction."],
                )
                baton_path_str = str(self.handoffs_dir)
            except Exception as e:
                logger.debug("Auto pre-compaction baton skipped: %s", e)

        ceiling_note = (
            f"{used_pct:.1f}% of 1M window / {token_state.compaction_budget_pct:.0f}% of ~250k compaction ceiling"
            if token_state.is_large_window
            else f"{used_pct:.1f}% window"
        )
        msg = (
            f"💡 [Context Budget Advisor] Session at {step_count} steps (~{est_tokens:,} tokens, {ceiling_note}). "
            "Prefer `seek_symbol(detail_level='skeleton')` over full-file reads, and if tackling a complex "
            "multi-subsystem task (C >= 7), delegate independent work units via Task DAG to preserve context budget."
        )
        if baton_path_str:
            msg += f" Pre-compaction handoff baton persisted to `{baton_path_str}`."

        return InterceptorResult.allow(
            data=MappingProxyType({
                "budget_warning": msg,
                "step_count": step_count,
                "estimated_tokens": est_tokens,
                "used_percentage": used_pct,
                "compaction_budget_pct": token_state.compaction_budget_pct,
                "baton_persisted": bool(baton_path_str),
                "injectSteps": ({"ephemeralMessage": msg},),
            })
        )


class InvariantInjector:
    """
    Optional Turn 0 invariant injector (no periodic Turn 10n re-injection to preserve KV-cache prefix).
    Not enabled by default in PreInvokePipeline since rules already reside in rules/*.md.
    """

    def __init__(self, journal_dir: Optional[Path] = None):
        self.journal_dir = journal_dir or (Path(__file__).resolve().parent.parent.parent.parent / "journal")

    def process(self, context: HookContext) -> InterceptorResult:
        if not context.is_first_turn:
            return InterceptorResult.allow()

        inv_file = self.journal_dir / "invariants.md"
        invariants: List[str] = []
        if inv_file.is_file():
            try:
                text = inv_file.read_text(encoding="utf-8")
                pattern = re.compile(
                    r"###\s+Invariant\s+\[(.*?)\]\s*\n\*\*Rule:\*\*\s*(.*?)(?=\n\*\*Rationale:|\n###|\Z)",
                    re.DOTALL,
                )
                for match in pattern.finditer(text):
                    topic = match.group(1).strip()
                    rule = match.group(2).strip().replace("\n", " ")
                    invariants.append(f"• **[{topic}]**: {rule}")
                    if len(invariants) >= 3:
                        break
            except Exception as e:
                logger.debug("Failed reading invariants file: %s", e)

        inv_str = "\n".join(invariants) if invariants else "• Ambient invariants synchronized."

        ambient_message = (
            "🧠 [Kortext Cognitive OS Initialized]\n"
            "Active Protocols:\n"
            "1. ANTI-RIPGREP MANDATE: Prefer `seek_symbol` over `rg`/`grep_search` when looking up known symbols.\n"
            "2. DELEGATION PROTOCOL: For high-complexity tasks (Complexity >= 7 or parallel streams), use `manage_tasks`.\n"
            "3. PERSISTENCE MANDATE: Record significant architectural outcomes via `record_system_outcome`.\n\n"
            f"Active Invariants:\n{inv_str}"
        )

        inject_steps = ({"ephemeralMessage": ambient_message},)
        return InterceptorResult.allow(
            data=MappingProxyType({"injectSteps": inject_steps})
        )


class PreInvokePipeline:
    """
    Dispatches PreInvocation interceptors and consolidates any injected ephemeralMessage steps.
    Silent by default on healthy Turn 0 to protect KV-cache prefix reuse.
    """

    def __init__(self, interceptors: Optional[Sequence[Any]] = None):
        if interceptors is not None:
            self.interceptors = tuple(interceptors)
        else:
            default_list: List[Any] = [
                SubstrateHealthProbe(),
                FrontierDAGNotifier(),
                BoostPromptGroundingProbe(),
                ContextBudgetAdvisor(),
            ]
            if os.getenv("KORTEXT_INJECT_INVARIANTS", "0") == "1":
                default_list.append(InvariantInjector())
            self.interceptors = tuple(default_list)

    def execute(self, context: HookContext) -> InterceptorResult:
        merged_data: Dict[str, Any] = {}
        ephemeral_messages: List[str] = []
        other_steps: List[Dict[str, Any]] = []

        for interceptor in self.interceptors:
            try:
                if hasattr(interceptor, "process"):
                    res = interceptor.process(context)
                elif hasattr(interceptor, "check"):
                    res = interceptor.check(context)
                else:
                    continue

                if res.data:
                    for k, v in res.data.items():
                        if k == "injectSteps" and isinstance(v, (list, tuple)):
                            for step in v:
                                if isinstance(step, (dict, MappingProxyType)) and "ephemeralMessage" in step:
                                    msg_txt = str(step["ephemeralMessage"]).strip()
                                    if msg_txt and msg_txt not in ephemeral_messages:
                                        ephemeral_messages.append(msg_txt)
                                elif isinstance(step, (dict, MappingProxyType)):
                                    other_steps.append(dict(step))
                        else:
                            merged_data[k] = v
            except Exception as e:
                logger.debug("PreInvoke interceptor %s failed: %s", type(interceptor).__name__, e)
                continue

        final_steps: List[Dict[str, Any]] = []
        try:
            from kortext.hooks.pipelines.post_tool import consume_pending_advisories

            conv_id = getattr(context, "conversation_id", "") or ""
            sess_id = context.session_id or ""
            for pending_msg in consume_pending_advisories(session_id=sess_id, conversation_id=conv_id):
                if pending_msg and pending_msg not in ephemeral_messages:
                    ephemeral_messages.append(pending_msg)
        except Exception as exc:
            logger.debug("Suppressed in pre_invoke:%d: %s", 607, exc)

        if ephemeral_messages:
            final_steps.append({"ephemeralMessage": "\n\n".join(ephemeral_messages)})
        final_steps.extend(other_steps)

        if final_steps:
            merged_data["injectSteps"] = tuple(final_steps)

        return InterceptorResult.allow(data=MappingProxyType(merged_data))
