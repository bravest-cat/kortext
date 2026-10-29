"""
Post-invocation pipeline: Runaway execution loop detection, token/turn telemetry tracking,
and automatic pre-compaction session handoff baton snapshot when context window crosses
the 25% compaction threshold (~200k-250k tokens on 1M models) or 75% on 200k models.
Conforms strictly to Antigravity PostInvocation schema:
  {"injectSteps": [...], "terminationBehavior": "" | "force_continue" | "terminate"}
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Sequence

from kortext._home import get_kortext_home
from kortext.hooks.context import HookContext
from kortext.hooks.types import InterceptorResult

logger = logging.getLogger("kortext.hooks.pipelines.post_invoke")


class RunawayExecutionGuard:
    """
    Detects repeated identical tool failures or error loops and trips a circuit breaker.
    """

    def __init__(self, max_identical_errors: int = 3):
        self.max_identical_errors = max_identical_errors

    def process(self, context: HookContext) -> InterceptorResult:
        steps = context.read_transcript_steps()
        if len(steps) < self.max_identical_errors:
            return InterceptorResult.allow()

        recent_errors: List[str] = []
        for step in reversed(steps):
            err_text = ""
            tool_res = step.get("tool_result")
            if isinstance(tool_res, (dict, MappingProxyType)):
                err_text = str(tool_res.get("error") or tool_res.get("stderr") or "").strip()
            elif isinstance(step.get("error"), str):
                err_text = str(step["error"]).strip()

            if err_text:
                recent_errors.append(err_text)
            else:
                break

            if len(recent_errors) >= self.max_identical_errors:
                break

        if len(recent_errors) >= self.max_identical_errors:
            first_err = recent_errors[0]
            if all(e == first_err for e in recent_errors):
                reason = (
                    f"🛑 [Runaway Execution Guard] Detected identical repetitive error {len(recent_errors)} times "
                    f"in a row: '{first_err[:120]}...'. Circuit breaker tripped. "
                    "Analyze root cause systematically or reconsider approach rather than repeating the failing action."
                )
                print(reason, file=sys.stderr)
                return InterceptorResult.continue_execution(
                    reason=reason,
                    data=MappingProxyType({
                        "repeated_error": first_err,
                        "count": len(recent_errors),
                        "injectSteps": ({"ephemeralMessage": reason},),
                        "terminationBehavior": "force_continue",
                    }),
                )

        return InterceptorResult.allow()


class TokenUsageTracker:
    """
    Tracks session turn telemetry, reads context window utilization from hud_state.json
    (populated by statusline), hook payload, or transcript.jsonl usage records, and persists
    a pre-compaction session handoff baton (~/.kortext/handoffs/session_<conversation_id>.json)
    before auto-compaction (~20% / 200k tokens on 1M window, or 75% on 200k window).
    """

    def __init__(self, telemetry_dir: Optional[Path] = None, compaction_threshold_pct: float = 75.0):
        self.telemetry_dir = telemetry_dir or (get_kortext_home() / "telemetry")
        self.compaction_threshold_pct = compaction_threshold_pct

    def _persist_session_checkpoint(
        self,
        context: HookContext,
        steps: Sequence[Any],
        used_pct: float,
        estimated_tokens: int = 0,
        compaction_budget_pct: float = 0.0,
    ) -> Optional[str]:
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
                if t_name in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
                    tf = t_args.get("TargetFile") or t_args.get("target_file") or t_args.get("path")
                    if tf and str(tf) not in modified_files:
                        modified_files.append(str(tf))
                elif "seek_symbol" in t_name or "trace_symbol_impact" in t_name:
                    sym = t_args.get("symbol_name") or t_args.get("symbol")
                    if sym and str(sym) not in recent_symbols:
                        recent_symbols.append(str(sym))

        resolved_proj = context.workspace_project or getattr(context, "project_name", None) or "kortext"
        try:
            handoffs_dir.mkdir(parents=True, exist_ok=True)
            out_path = handoffs_dir / f"session_{safe_id}.json"
            tmp_path = out_path.with_suffix(f".tmp.{os.getpid()}")
            payload = {
                "conversation_id": conv_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "used_percentage": used_pct,
                "estimated_tokens": estimated_tokens,
                "compaction_budget_pct": compaction_budget_pct,
                "step_count": len(steps),
                "modified_files": modified_files[-20:],
                "recent_symbols": recent_symbols[-20:],
                "workspace_project": resolved_proj,
            }
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp_path.replace(out_path)
            return str(out_path)
        except Exception as exc:
            logger.debug("Session checkpoint write skipped: %s", exc)
            return None

    def process(self, context: HookContext) -> InterceptorResult:
        from kortext.hooks.transcript_tokens import resolve_session_token_state

        data_out: Dict[str, Any] = {}
        try:
            self.telemetry_dir.mkdir(parents=True, exist_ok=True)
            log_file = self.telemetry_dir / "invocations.jsonl"
            steps = context.read_transcript_steps()
            token_state = resolve_session_token_state(
                steps=steps,
                raw_payload=context.raw_payload,
                transcript_path=context.transcript_path,
                model_name=context.model_name,
            )
            used_pct = token_state.used_percentage
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": context.session_id,
                "workspace_project": context.workspace_project or "kortext",
                "turn_index": context.turn_index,
                "step_count": len(steps),
                "estimated_tokens": token_state.estimated_tokens,
                "context_window_size": token_state.context_window_size,
                "used_percentage": used_pct,
                "compaction_budget_pct": token_state.compaction_budget_pct,
            }
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

            if used_pct >= self.compaction_threshold_pct or token_state.stage2_precompact_reached:
                ckpt_path = self._persist_session_checkpoint(
                    context,
                    steps,
                    used_pct,
                    estimated_tokens=token_state.estimated_tokens,
                    compaction_budget_pct=token_state.compaction_budget_pct,
                )
                if ckpt_path:
                    data_out["session_checkpoint"] = ckpt_path
        except Exception as e:
            logger.debug("Invocation telemetry logging failed: %s", e)

        return InterceptorResult.allow(data=MappingProxyType(data_out) if data_out else None)


class PendingAdvisoryFlusher:
    """
    Flushes queued PostToolUse advisories (from ContextAndLoopAdvisor: 3-strike loop detector,
    60%/80% compaction warnings, and post-compaction recovery briefs) into PostInvocation injectSteps,
    since Antigravity's PostToolUse hook contract only accepts `{}` on stdout.
    """

    def process(self, context: HookContext) -> InterceptorResult:
        from kortext.hooks.pipelines.post_tool import consume_pending_advisories

        conv_id = getattr(context, "conversation_id", "") or ""
        sess_id = context.session_id or ""
        msgs = consume_pending_advisories(session_id=sess_id, conversation_id=conv_id)
        if not msgs:
            return InterceptorResult.allow()
        combined = "\n\n".join(msgs)
        return InterceptorResult.allow(
            reason=combined,
            data=MappingProxyType({
                "flushed_advisories": tuple(msgs),
                "injectSteps": ({"ephemeralMessage": combined},),
            }),
        )


class PostInvokePipeline:
    """Dispatches PostInvocation interceptors sequentially and consolidates injectSteps."""

    def __init__(self, interceptors: Optional[Sequence[Any]] = None):
        self.interceptors = tuple(interceptors) if interceptors is not None else (
            RunawayExecutionGuard(),
            TokenUsageTracker(),
            PendingAdvisoryFlusher(),
        )

    def execute(self, context: HookContext) -> InterceptorResult:
        merged_data: Dict[str, Any] = {
            "injectSteps": (),
            "terminationBehavior": "",
        }
        ephemeral_messages: List[str] = []
        other_steps: List[Dict[str, Any]] = []
        continue_reason: Optional[str] = None
        should_continue = False

        for interceptor in self.interceptors:
            try:
                res = interceptor.process(context)
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
                        elif k == "terminationBehavior":
                            if v:
                                merged_data[k] = v
                        else:
                            merged_data[k] = v
                if res.decision == "continue":
                    should_continue = True
                    continue_reason = res.reason or continue_reason
            except Exception as e:
                logger.debug("PostInvoke interceptor %s failed: %s", type(interceptor).__name__, e)
                continue

        final_steps: List[Dict[str, Any]] = []
        if ephemeral_messages:
            final_steps.append({"ephemeralMessage": "\n\n".join(ephemeral_messages)})
        final_steps.extend(other_steps)
        if final_steps:
            merged_data["injectSteps"] = tuple(final_steps)

        if should_continue:
            return InterceptorResult.continue_execution(
                reason=continue_reason or "",
                data=MappingProxyType(merged_data),
            )
        return InterceptorResult.allow(data=MappingProxyType(merged_data))
