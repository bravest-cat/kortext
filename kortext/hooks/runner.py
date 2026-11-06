"""
CLI runner and dispatcher for Antigravity lifecycle hooks.
Conforms strictly to the Antigravity hooks.md stdin/stdout JSON contract.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from types import MappingProxyType
from typing import Any, Mapping, Optional

from kortext.hooks.context import HookContext
from kortext.hooks.pipelines.post_invoke import PostInvokePipeline
from kortext.hooks.pipelines.post_tool import PostToolPipeline
from kortext.hooks.pipelines.pre_invoke import PreInvokePipeline
from kortext.hooks.pipelines.pre_tool import PreToolPipeline
from kortext.hooks.pipelines.stop import StopPipeline
from kortext.hooks.types import InterceptorResult

logger = logging.getLogger("kortext.hooks.runner")

VALID_HOOK_PROFILES = frozenset({"minimal", "standard", "strict", "disabled", "off"})


def get_hook_profile() -> str:
    """Returns the active hook execution profile ('minimal', 'standard', 'strict', or 'disabled')."""
    raw = os.getenv("KORTEXT_HOOK_PROFILE", os.getenv("ECC_HOOK_PROFILE", "standard")).strip().lower()
    if raw in VALID_HOOK_PROFILES:
        return "disabled" if raw == "off" else raw
    return "standard"


def emit_response(
    event: str = "",
    decision: str = "allow",
    reason: Optional[str] = None,
    data: Optional[Mapping[str, Any]] = None,
) -> None:
    """
    Serializes hook response dictionary to stdout and flushes in strict compliance with hooks.md:
      - PreToolUse:     {"decision": "allow"|"deny"|"ask"|..., "reason"?: str, "permissionOverrides"?: [...]}
      - PostToolUse:    {}
      - SessionStart / PreInvocation: {"injectSteps": [{"ephemeralMessage": "..."}]} or {}
      - PostInvocation: {"injectSteps": [...], "terminationBehavior": ""|"force_continue"|"terminate"}
      - Stop:           {"decision": "stop"|"continue", "reason"?: str}
    """
    output: dict[str, Any] = {}
    ev = event.lower().strip()

    if ev == "post_tool":
        output = {}
    elif ev in ("pre_invoke", "session_start", "sessionstart"):
        term_behavior = str(data.get("terminationBehavior", "")) if data else ""
        if decision in ("terminate", "deny") and not term_behavior:
            term_behavior = "terminate"

        raw_steps = list(data.get("injectSteps", ())) if data else []

        if term_behavior == "terminate":
            # Only error messages are kept on terminate; other steps must not reach the model
            err_steps = [
                step
                for step in raw_steps
                if isinstance(step, (dict, MappingProxyType)) and "errorMessage" in step
            ]
            if not err_steps:
                msg = reason or "Execution terminated by hook"
                err_steps = [{"errorMessage": {"userMessage": msg}}]
            inject_steps = err_steps
        else:
            inject_steps = raw_steps

        if inject_steps:
            output["injectSteps"] = inject_steps
        if term_behavior:
            output["terminationBehavior"] = term_behavior
    elif ev == "post_invoke":
        inject_steps = list(data.get("injectSteps", ())) if data else []
        term_behavior = str(data.get("terminationBehavior", "")) if data else ""
        if decision == "continue" and not term_behavior:
            term_behavior = "force_continue"
            if reason and not inject_steps:
                inject_steps = [{"ephemeralMessage": reason}]
        output["injectSteps"] = inject_steps
        output["terminationBehavior"] = term_behavior
    elif ev == "stop":
        output["decision"] = decision
        if reason is not None:
            output["reason"] = reason
    elif ev in ("task_completed", "taskcompleted", "teammate_idle", "teammateidle"):
        if decision in ("deny", "continue", "block"):
            if reason:
                print(reason, file=sys.stderr)
            sys.exit(2)
        output = {}
    else:
        output["decision"] = decision
        if reason is not None:
            output["reason"] = reason
        if data and "permissionOverrides" in data:
            output["permissionOverrides"] = list(data["permissionOverrides"])

    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()


def run_hook(event: str, context: Optional[HookContext] = None) -> InterceptorResult:
    """
    Dispatches hook context to the corresponding pipeline, respecting KORTEXT_HOOK_PROFILE.
    """
    ctx = context if context is not None else HookContext.from_stdin(event=event)
    ev = event.lower().strip()
    profile = get_hook_profile()

    if profile == "disabled":
        return InterceptorResult.stop() if ev == "stop" else InterceptorResult.allow()

    if profile == "minimal" and ev in ("post_tool", "post_invoke"):
        return InterceptorResult.allow()

    if ev == "pre_tool":
        pipeline = PreToolPipeline()
        return pipeline.execute(ctx)
    elif ev == "post_tool":
        pipeline = PostToolPipeline()
        return pipeline.execute(ctx)
    elif ev in ("pre_invoke", "session_start", "sessionstart"):
        pipeline = PreInvokePipeline()
        return pipeline.execute(ctx)
    elif ev == "post_invoke":
        pipeline = PostInvokePipeline()
        return pipeline.execute(ctx)
    elif ev in ("stop", "task_completed", "taskcompleted", "teammate_idle", "teammateidle"):
        pipeline = StopPipeline()
        return pipeline.execute(ctx)
    else:
        logger.debug("Unknown hook event '%s', defaulting to allow", event)
        return InterceptorResult.allow()


def main() -> None:
    """
    Main CLI entry point for lifecycle hooks: python3 -m kortext.hooks.runner <event>
    """
    target_event = "pre_tool"
    try:
        event = sys.argv[1] if len(sys.argv) > 1 else ""
        context = HookContext.from_stdin(event=event)
        target_event = event or context.event or "pre_tool"

        result = run_hook(target_event, context)
        emit_response(
            event=target_event,
            decision=result.decision,
            reason=result.reason,
            data=result.data,
        )
    except Exception as exc:
        print(f"[KortextHooks] Runner encountered unexpected error: {exc}", file=sys.stderr)
        fallback_decision = "stop" if target_event == "stop" else "allow"
        emit_response(event=target_event, decision=fallback_decision)


if __name__ == "__main__":
    main()
