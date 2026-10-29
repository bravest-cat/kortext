"""
Post-tool interceptor pipeline: Incremental AST synchronization, bounded telemetry spooling,
Task DAG lease heartbeats (FM3),
compaction-proof .kortext/ledger.md updates, HUD state snapshot refresh,
and 3-stage 25%-compaction-ceiling context awareness.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Sequence

from kortext._home import get_kortext_home
from kortext.hooks.context import HookContext
from kortext.hooks.types import InterceptorResult

logger = logging.getLogger("kortext.hooks.pipelines.post_tool")

FILE_MODIFYING_TOOLS = frozenset({
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
})

MAX_SPOOL_FILES = 100
_TEST_CMD_MARKERS = ("pytest", "npm test", "pnpm test", "cargo test", "go test", "unittest")


def _safe_session_suffix(session_id: str) -> str:
    clean = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in (session_id or "").strip())
    return clean[:48]


def record_test_execution_proof(command: str, passed: bool, session_id: str = "") -> None:
    """Records ground-truth test execution outcome in ~/.kortext/run/last_test_run.json (and session-scoped file)."""
    try:
        run_dir = get_kortext_home() / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "command": command,
            "passed": bool(passed),
            "session_id": session_id,
            "timestamp": time.time(),
            "iso_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        targets = [run_dir / "last_test_run.json"]
        sfx = _safe_session_suffix(session_id)
        if sfx:
            targets.append(run_dir / f"last_test_run_{sfx}.json")
        for proof_file in targets:
            tmp_file = proof_file.with_suffix(f".tmp.{os.getpid()}")
            tmp_file.write_text(json.dumps(payload), encoding="utf-8")
            tmp_file.replace(proof_file)
    except Exception as exc:
        logger.debug("Test execution proof write skipped: %s", exc)


def invalidate_test_execution_proof(modified_file: str = "", session_id: str = "") -> None:
    """Invalidates ~/.kortext/run/last_test_run.json (and session-scoped proof) when source files are edited after a test run (FM4 fix)."""
    run_dir = get_kortext_home() / "run"
    targets = [run_dir / "last_test_run.json"]
    sfx = _safe_session_suffix(session_id)
    if sfx:
        targets.append(run_dir / f"last_test_run_{sfx}.json")
    for proof_file in targets:
        if not proof_file.is_file():
            continue
        try:
            data = json.loads(proof_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["passed"] = False
                data["invalidated_by_edit"] = modified_file
                data["invalidated_at"] = time.time()
                proof_file.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            try:
                proof_file.unlink(missing_ok=True)
            except OSError:
                pass


def _norm_cmd(cmd: str) -> str:
    return " ".join(str(cmd or "").split())


def has_recent_passing_test_run(max_age_sec: float = 1800.0, session_id: str = "", command: Optional[str] = None) -> bool:
    """
    Returns True if PostToolUse observed a passing test run within max_age_sec with no subsequent file edits.
    With `command`, the observed run must be that exact command (whitespace-normalised).
    """
    run_dir = get_kortext_home() / "run"
    sfx = _safe_session_suffix(session_id)
    proof_file = (run_dir / f"last_test_run_{sfx}.json") if (sfx and (run_dir / f"last_test_run_{sfx}.json").is_file()) else (run_dir / "last_test_run.json")
    if not proof_file.is_file():
        return False
    try:
        data = json.loads(proof_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("passed"):
            return False
        if command is not None and _norm_cmd(data.get("command", "")) != _norm_cmd(command):
            return False
        ts = float(data.get("timestamp") or 0.0)
        if (time.time() - ts) > max_age_sec:
            return False
        dirty_file = run_dir / "dirty_files.json"
        if dirty_file.is_file():
            try:
                d_data = json.loads(dirty_file.read_text(encoding="utf-8"))
                if isinstance(d_data, dict) and d_data.get("files"):
                    dirty_ts = float(d_data.get("updated_at") or 0.0)
                    if dirty_ts >= ts:
                        return False
            except Exception as exc:
                logger.debug("Suppressed in post_tool:%d: %s", 110, exc)
        return True
    except Exception:
        return False


def consume_test_execution_proof(task_id: str) -> None:
    """Marks the current passing proof as used, so one test run completes one task."""
    for proof_file in (get_kortext_home() / "run").glob("last_test_run*.json"):
        try:
            data = json.loads(proof_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("passed"):
                data.update(passed=False, consumed_by=task_id, consumed_at=time.time())
                proof_file.write_text(json.dumps(data), encoding="utf-8")
        except (OSError, ValueError) as exc:
            logger.warning("Could not consume test proof %s: %s", proof_file, exc)


def queue_pending_advisory(message: str, session_id: str = "", conversation_id: str = "") -> None:
    """Queues PostToolUse advisories into ~/.kortext/run/pending_advisory.json for PostInvocation/PreInvocation flush."""
    if not message or not message.strip():
        return
    try:
        run_dir = get_kortext_home() / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        adv_file = run_dir / "pending_advisory.json"
        existing_msgs: List[str] = []
        if adv_file.is_file():
            try:
                prev = json.loads(adv_file.read_text(encoding="utf-8"))
                if isinstance(prev, dict) and isinstance(prev.get("messages"), list):
                    if time.time() - float(prev.get("updated_at") or 0.0) <= 300.0:
                        existing_msgs = [str(m) for m in prev["messages"] if str(m).strip()]
            except Exception:
                existing_msgs = []
        msg_clean = message.strip()
        if msg_clean not in existing_msgs:
            existing_msgs.append(msg_clean)
        payload = {
            "session_id": session_id,
            "conversation_id": conversation_id,
            "messages": existing_msgs[-5:],
            "updated_at": time.time(),
        }
        tmp_file = adv_file.with_suffix(f".tmp.{os.getpid()}")
        tmp_file.write_text(json.dumps(payload), encoding="utf-8")
        tmp_file.replace(adv_file)
    except Exception as exc:
        logger.debug("Queueing pending advisory skipped: %s", exc)


def consume_pending_advisories(
    session_id: str = "",
    conversation_id: str = "",
    max_age_sec: float = 300.0,
) -> List[str]:
    """Atomically reads and clears queued PostToolUse advisories from ~/.kortext/run/pending_advisory.json."""
    adv_file = get_kortext_home() / "run" / "pending_advisory.json"
    if not adv_file.is_file():
        return []
    try:
        data = json.loads(adv_file.read_text(encoding="utf-8"))
        adv_file.unlink(missing_ok=True)
        if not isinstance(data, dict):
            return []
        ts = float(data.get("updated_at") or 0.0)
        if ts > 0 and (time.time() - ts) > max_age_sec:
            return []
        rec_sess = str(data.get("session_id") or "")
        rec_conv = str(data.get("conversation_id") or "")
        caller_ids = {x for x in (session_id, conversation_id) if x and x != "default_session"}
        stored_ids = {x for x in (rec_sess, rec_conv) if x and x != "default_session"}
        if caller_ids and stored_ids and not (caller_ids & stored_ids):
            return []
        msgs = data.get("messages") or []
        return [str(m).strip() for m in msgs if isinstance(m, str) and m.strip()]
    except Exception:
        return []


class IncrementalASTSync:
    """
    Performs fast incremental AST index synchronization and spools bounded invalidation events
    for modified Python and codebase files.
    """

    def __init__(self, spool_dir: Optional[Path] = None, sync_ast: bool = True):
        if spool_dir is not None:
            self.spool_dir = spool_dir
        else:
            self.spool_dir = get_kortext_home() / "spool" / "invalidation"
        self.sync_ast = sync_ast

    def _prune_spool_dir(self) -> None:
        """Bounds spool directory size so orphan files never accumulate infinitely."""
        try:
            files = sorted(
                self.spool_dir.glob("event_*.json"),
                key=lambda f: f.stat().st_mtime if f.exists() else 0.0,
            )
            if len(files) > MAX_SPOOL_FILES:
                for old_f in files[: len(files) - MAX_SPOOL_FILES]:
                    try:
                        old_f.unlink(missing_ok=True)
                    except OSError:
                        logger.debug("Suppressed non-fatal exception")
        except Exception as e:
            logger.debug("Spool pruning skipped: %s", e)

    def process(self, context: HookContext) -> InterceptorResult:
        if context.tool_name not in FILE_MODIFYING_TOOLS:
            return InterceptorResult.allow()

        target_file = context.target_file
        if target_file:
            invalidate_test_execution_proof(modified_file=target_file)
        if not target_file or not os.path.isfile(target_file):
            return InterceptorResult.allow()

        ast_synced_sha: Optional[str] = None

        # 1. Fast AST direct sync if requested and target is a supported code file
        if self.sync_ast:
            try:
                from kortext.cache import IngestCache
                from kortext.chunking.ast_splitter import ASTCodeSplitter, LANG_MAP, PY_LANG
                from kortext.hooks.pipelines.pre_invoke import _is_falkor_substrate_available
                from kortext.symbols.graph import SymbolGraphManager

                ext = Path(target_file).suffix.lower()
                if ext in LANG_MAP and _is_falkor_substrate_available():
                    content = Path(target_file).read_text(encoding="utf-8")
                    splitter = ASTCodeSplitter(language=LANG_MAP.get(ext, PY_LANG))
                    ast_info = splitter.extract_symbols_and_references(content, file_path=target_file)
                    decls = ast_info.get("declarations", [])
                    imports = ast_info.get("imports", [])
                    refs = ast_info.get("references", [])

                    mgr = SymbolGraphManager()
                    mgr.save_file_ast(
                        file_path=target_file,
                        declarations=decls,
                        imports=imports,
                        references=refs,
                    )
                    ast_synced_sha = IngestCache.compute_sha256(Path(target_file))
            except Exception as e:
                logger.debug("Direct AST sync fallback safely caught: %s", e)

        # 2. Spool invalidation event for background vector indexer (recording ast_synced_sha256 to avoid duplicate AST sync)
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            event_file = self.spool_dir / f"event_{os.getpid()}_{int(time.time() * 1000)}.json"
            event_payload: Dict[str, Any] = {
                "action": "invalidate",
                "path": target_file,
                "project": context.workspace_project or "kortext",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if ast_synced_sha:
                event_payload["ast_synced_sha256"] = ast_synced_sha
            event_file.write_text(json.dumps(event_payload), encoding="utf-8")
            self._prune_spool_dir()
        except Exception as e:
            logger.debug("Failed spooling invalidation event: %s", e)

        return InterceptorResult.allow()


class TelemetrySpooler:
    """
    Spools non-blocking tool execution metrics, records passing test proofs,
    renews active Task DAG leases (FM3), and refreshes ~/.kortext/run/hud_state.json.
    """

    def __init__(self, telemetry_dir: Optional[Path] = None):
        if telemetry_dir is not None:
            self.telemetry_dir = telemetry_dir
        else:
            self.telemetry_dir = get_kortext_home() / "telemetry"

    def _renew_active_task_leases(self, context: HookContext) -> None:
        """FM3 Mitigation: Automatically renews leases for KORTEXT_TASK_ID or active workspace in_progress tasks."""
        if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("KORTEXT_FORCE_HEARTBEAT_IN_TEST"):
            return
        try:
            from kortext.hooks.pipelines.pre_invoke import _is_falkor_substrate_available
            from kortext.tasks.tracker import TaskDAGTracker

            active_task_id = os.getenv("KORTEXT_TASK_ID", "").strip()
            if active_task_id:
                TaskDAGTracker().heartbeat_task(active_task_id)
                return

            if _is_falkor_substrate_available():
                tracker = TaskDAGTracker()
                proj = context.workspace_project or "kortext"
                for t in tracker.get_in_progress_tasks(project_id=proj)[:3]:
                    tid = t.get("id")
                    if tid:
                        tracker.heartbeat_task(str(tid))
        except Exception as exc:
            logger.debug("Suppressed in post_tool:%d: %s", 375, exc)

    def process(self, context: HookContext) -> InterceptorResult:
        try:
            self.telemetry_dir.mkdir(parents=True, exist_ok=True)
            log_file = self.telemetry_dir / "tools.jsonl"
            tool_err = getattr(context, "tool_error", None)
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": context.session_id,
                "conversation_id": getattr(context, "conversation_id", "") or context.session_id,
                "workspace_project": context.workspace_project or "kortext",
                "model_name": getattr(context, "model_name", "") or None,
                "turn_index": context.turn_index,
                "tool_name": context.tool_name,
                "target_file": context.target_file if context.target_file else None,
                "command": context.command_line if context.command_line else None,
                "error": tool_err,
            }
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

            if context.tool_name == "run_command" and context.command_line:
                cmd_lower = context.command_line.lower()
                if any(m in cmd_lower for m in _TEST_CMD_MARKERS):
                    passed = not bool(tool_err)
                    record_test_execution_proof(
                        command=context.command_line,
                        passed=passed,
                        session_id=context.session_id or "",
                    )

            self._renew_active_task_leases(context)

            # Track dirty files on edit, invalidate stale test proof, and clear on passing test run (FM4 mitigation)
            dirty_file = get_kortext_home() / "run" / "dirty_files.json"
            if context.tool_name in FILE_MODIFYING_TOOLS and context.target_file:
                invalidate_test_execution_proof(modified_file=context.target_file)
                try:
                    dirty_data = json.loads(dirty_file.read_text(encoding="utf-8")) if dirty_file.is_file() else {"files": []}
                    files = list(dirty_data.get("files") or [])
                    if context.target_file not in files:
                        files.append(context.target_file)
                    dirty_file.write_text(json.dumps({"files": files[-50:], "updated_at": time.time()}), encoding="utf-8")
                except Exception as exc:
                    logger.debug("Suppressed in post_tool:%d: %s", 424, exc)
            elif context.tool_name == "run_command" and context.command_line and not tool_err:
                if any(m in context.command_line.lower() for m in _TEST_CMD_MARKERS):
                    try:
                        dirty_file.unlink(missing_ok=True)
                    except OSError:
                        pass

            # Touch hud_state.json updated_at if it exists so agy-hud in-process fast path stays fresh (zero I/O)
            hud_file = get_kortext_home() / "run" / "hud_state.json"
            if hud_file.is_file():
                try:
                    snap = json.loads(hud_file.read_text(encoding="utf-8"))
                    if isinstance(snap, dict):
                        snap["updated_at"] = time.time()
                        hud_file.write_text(json.dumps(snap, separators=(",", ":")), encoding="utf-8")
                except Exception as exc:
                    logger.debug("Suppressed in post_tool:%d: %s", 441, exc)
        except Exception as e:
            logger.debug("Telemetry logging failed: %s", e)

        return InterceptorResult.allow()


class ContextAndLoopAdvisor:
    """
    Delivers 3-stage 25%-compaction-ceiling context advisories:
      - Stage 1 (15% of 1M / ~150k tokens / 60% compaction budget): Soft budget advisory
      - Stage 2 (20% of 1M / ~200k tokens / 80% compaction budget): Pre-compaction state save
        to ~/.kortext/handoffs/session_<id>.json and .kortext/ledger.md + high-priority alert
      - Stage 3: Post-compaction recovery brief + .kortext/ledger.md header injection
    Also enforces 3-strike tool retry loop circuit-breakers via PostToolUse additionalContext.
    """

    def _should_fire_stage_bucket(self, session_id: str, stage_key: str) -> bool:
        if not session_id:
            return True
        bucket_file = get_kortext_home() / "spool" / "post_tool_stage_buckets.json"
        try:
            bucket_file.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(bucket_file.read_text(encoding="utf-8")) if bucket_file.is_file() else {}
            if not isinstance(data, dict):
                data = {}
            seen = list(data.get(session_id) or [])
            if stage_key in seen:
                return False
            seen.append(stage_key)
            data[session_id] = seen[-20:]
            bucket_file.write_text(json.dumps(data), encoding="utf-8")
            return True
        except Exception:
            return True

    def _persist_stage2_baton_and_ledger(
        self,
        context: HookContext,
        steps: Sequence[Any],
        token_state: Any,
    ) -> str:
        """Writes ~/.kortext/handoffs/session_<id>.json and updates .kortext/ledger.md before compaction."""
        conv_id = getattr(context, "conversation_id", None) or context.session_id or "active"
        safe_id = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in conv_id)
        handoffs_dir = get_kortext_home() / "handoffs"
        modified_files: List[str] = []
        recent_symbols: List[str] = []
        for st in steps:
            if not isinstance(st, (dict, MappingProxyType)):
                continue
            for tc in st.get("tool_calls", ()):
                if not isinstance(tc, (dict, MappingProxyType)):
                    continue
                t_name = str(tc.get("name") or tc.get("tool_name") or "")
                t_args = tc.get("args") or {}
                if not isinstance(t_args, (dict, MappingProxyType)):
                    continue
                if t_name in FILE_MODIFYING_TOOLS:
                    tf = t_args.get("TargetFile") or t_args.get("target_file") or t_args.get("path")
                    if tf and str(tf) not in modified_files:
                        modified_files.append(str(tf))
                elif "seek_symbol" in t_name or "trace_symbol_impact" in t_name:
                    sym = t_args.get("symbol_name") or t_args.get("symbol")
                    if sym and str(sym) not in recent_symbols:
                        recent_symbols.append(str(sym))

        last_test_info: Dict[str, Any] = {}
        proof_file = get_kortext_home() / "run" / "last_test_run.json"
        if proof_file.is_file():
            try:
                last_test_info = json.loads(proof_file.read_text(encoding="utf-8"))
            except Exception:
                last_test_info = {}

        proj = context.workspace_project or "kortext"
        out_path = handoffs_dir / f"session_{safe_id}.json"
        try:
            handoffs_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "conversation_id": conv_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "used_percentage": token_state.used_percentage,
                "estimated_tokens": token_state.estimated_tokens,
                "compaction_budget_pct": token_state.compaction_budget_pct,
                "step_count": len(steps),
                "modified_files": modified_files[-20:],
                "recent_symbols": recent_symbols[-20:],
                "last_test_run": last_test_info,
                "workspace_project": proj,
            }
            tmp_path = out_path.with_suffix(f".tmp.{os.getpid()}")
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp_path.replace(out_path)
        except Exception as exc:
            logger.debug("Stage 2 session baton write skipped: %s", exc)

        # Also update compaction-proof .kortext/ledger.md if cwd or workspaceRoot is available
        ws_candidate = os.getenv("KORTEXT_WORKSPACE_ROOT") or context.cwd or ""
        if ws_candidate and os.path.isdir(str(ws_candidate)):
            try:
                from kortext.tasks.tracker import TaskDAGTracker

                TaskDAGTracker().update_compaction_ledger(
                    workspace_dir=Path(str(ws_candidate)),
                    project_id=proj,
                )
            except Exception as exc:
                logger.debug("Suppressed in post_tool:%d: %s", 549, exc)
        return str(out_path)

    def _check_retry_loop(self, context: HookContext) -> Optional[str]:
        tool_err = getattr(context, "tool_error", None)
        run_dir = get_kortext_home() / "run"
        retry_file = run_dir / "tool_retry_state.json"
        if not tool_err:
            if context.tool_name == "run_command" and retry_file.is_file():
                try:
                    retry_file.unlink(missing_ok=True)
                except OSError:
                    pass
            return None
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            sig = f"{context.tool_name}:{context.command_line or context.target_file or ''}"
            state = json.loads(retry_file.read_text(encoding="utf-8")) if retry_file.is_file() else {}
            count = int(state.get("count", 0)) + 1 if state.get("sig") == sig else 1
            retry_file.write_text(json.dumps({"sig": sig, "count": count, "timestamp": time.time()}), encoding="utf-8")
            if count >= 3:
                return (
                    f"🛑 [Kortext Loop Detector] `{sig[:80]}` has failed {count} consecutive times. "
                    "Stop retrying the identical command: (1) Capture exact error output, "
                    "(2) Inspect declaration/callers via `seek_symbol` / `trace_symbol_impact`, "
                    "(3) Formulate a new root-cause hypothesis before executing another command."
                )
        except Exception as exc:
            logger.debug("Suppressed in post_tool:%d: %s", 577, exc)
        return None

    def process(self, context: HookContext) -> InterceptorResult:
        notes: list[str] = []
        loop_msg = self._check_retry_loop(context)
        if loop_msg:
            notes.append(loop_msg)

        # FM4 Mitigation: Warn when send_message payload exceeds 1,200 chars instead of passing a handoff:<task_id> pointer
        if context.tool_name in ("send_message", "mcp_send_message") or context.tool_name.endswith(":send_message"):
            msg_body = str(
                context.tool_args.get("Message")
                or context.tool_args.get("message")
                or context.tool_args.get("content")
                or ""
            )
            if len(msg_body) > 1200 and "handoff:" not in msg_body:
                notes.append(
                    f"⚠️ [FM4 Context Bloat Advisory] `send_message` payload is {len(msg_body):,} chars (>1,200 ceiling). "
                    "Write detailed findings to `~/.kortext/handoffs/<task_id>.json` and pass a compact 5-line summary "
                    "plus `handoff:<task_id>` pointer to prevent parent orchestrator context rot."
                )

        try:
            from kortext.hooks.transcript_tokens import (
                check_and_update_compaction_watermark,
                load_post_compaction_recovery_brief,
                resolve_session_token_state,
            )

            steps = context.read_transcript_steps()
            token_state = resolve_session_token_state(
                raw_payload=context.raw_payload,
                transcript_path=context.transcript_path,
                steps=steps,
                model_name=context.model_name,
            )
            conv_id = getattr(context, "conversation_id", None) or context.session_id
            wm_info = check_and_update_compaction_watermark(
                conversation_id=conv_id,
                current_tokens=token_state.estimated_tokens,
                step_count=len(steps),
                used_percentage=token_state.used_percentage,
            )
            if isinstance(wm_info, dict) and wm_info.get("compacted"):
                recovery = load_post_compaction_recovery_brief(
                    conversation_id=conv_id,
                    project=context.workspace_project or "kortext",
                    workspace_dir=context.cwd or None,
                )
                if recovery:
                    notes.append(recovery)
            elif token_state.stage2_precompact_reached:
                if self._should_fire_stage_bucket(conv_id, f"stage2_{int(token_state.compaction_budget_pct // 10) * 10}"):
                    baton_path = self._persist_stage2_baton_and_ledger(context, steps, token_state)
                    notes.append(
                        f"⚠️ [Kortext Pre-Compaction Alert: ~{token_state.estimated_tokens:,} tokens "
                        f"({token_state.used_percentage:.1f}% of window — auto-compaction occurs near ~25% / "
                        f"{token_state.compaction_ceiling_tokens:,})] Your session state has been checkpointed to "
                        f"`{baton_path}`. Finish your current atomic edit/test step and call `record_system_outcome` "
                        "or `manage_tasks(action='complete_task')` before starting a new phase."
                    )
            elif token_state.stage1_advisory_reached:
                if self._should_fire_stage_bucket(conv_id, "stage1_60pct"):
                    notes.append(
                        f"📊 [Kortext Context Telemetry: ~{token_state.estimated_tokens:,} / "
                        f"{token_state.compaction_ceiling_tokens:,} compaction ceiling "
                        f"({token_state.used_percentage:.1f}% of window)] Prefer `seek_symbol(detail_level='skeleton')` "
                        "and line-bounded `view_file(StartLine, EndLine)` over full-file reads to preserve your current context window."
                    )

            # Phase-Boundary Strategic Compaction Advisory (ECC strategic-compact pattern + CLI 1.4.7 50% thinking compression)
            is_phase_boundary = (
                ("manage_tasks" in context.tool_name and str(context.tool_args.get("action") or "").lower() in ("complete_task", "milestone_handoff"))
                or "record_system_outcome" in context.tool_name
            )
            if is_phase_boundary and (
                token_state.estimated_tokens >= 150_000
                or token_state.used_percentage >= 50.0
                or token_state.stage1_advisory_reached
            ):
                if self._should_fire_stage_bucket(conv_id, "phase_boundary_compact"):
                    baton_path = self._persist_stage2_baton_and_ledger(context, steps, token_state)
                    notes.append(
                        f"🧭 [Strategic Phase-Boundary Compaction] Logical phase completed at ~{token_state.estimated_tokens:,} tokens "
                        f"({token_state.used_percentage:.1f}% window). Handoff baton persisted to `{baton_path}`; "
                        "call `memory_session_handoff(action='create')` or rotate to a fresh successor before starting the next phase."
                    )
        except Exception as exc:
            logger.debug("ContextAndLoopAdvisor skipped: %s", exc)

        if notes:
            combined = "\n\n".join(notes)
            conv_id = getattr(context, "conversation_id", "") or ""
            sess_id = context.session_id or ""
            queue_pending_advisory(combined, session_id=sess_id, conversation_id=conv_id)
            return InterceptorResult.allow(
                reason=combined,
                data=MappingProxyType({"additionalContext": combined}),
            )
        return InterceptorResult.allow()


class PostToolPipeline:
    """Dispatches PostToolUse interceptors sequentially."""

    def __init__(self, interceptors: Optional[Sequence[Any]] = None):
        self.interceptors = tuple(interceptors) if interceptors is not None else (
            IncrementalASTSync(),
            TelemetrySpooler(),
            ContextAndLoopAdvisor(),
        )

    def execute(self, context: HookContext) -> InterceptorResult:
        merged_data: dict[str, Any] = {}
        last_reason: Optional[str] = None
        for interceptor in self.interceptors:
            try:
                res = interceptor.process(context)
                if res and res.reason:
                    last_reason = res.reason
                if res and res.data:
                    merged_data.update(dict(res.data))
            except Exception as e:
                logger.debug("PostTool interceptor %s failed: %s", type(interceptor).__name__, e)
                continue

        if merged_data or last_reason:
            return InterceptorResult.allow(reason=last_reason, data=MappingProxyType(merged_data))
        return InterceptorResult.allow()

TelemetrySpooler.inspect = TelemetrySpooler.process
ContextAndLoopAdvisor.inspect = ContextAndLoopAdvisor.process
