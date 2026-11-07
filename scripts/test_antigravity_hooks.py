#!/usr/bin/env python3
"""
Automated Test Suite for Antigravity Lifecycle Hooks Suite (kortext.hooks.runner & scripts/hooks/*).

Tests:
  1. PreInvocation hook (silent by default on healthy Turn 0/1/10 to protect KV-cache prefix, or optional Turn 0 injection).
  2. PreToolUse hook (allow safe commands & piped grep, gate destructive commands, redirect standalone rg/grep with auto-seek).
  3. PostToolUse hook (hot-sync trigger and {} return).
  4. Stop hook (background outcome auto-extraction on small edits vs hard enforcement when KORTEXT_OUTCOME_GUARD_ENFORCE=1).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_runner(event: str, payload: dict, extra_env: dict | None = None) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    if extra_env:
        env.update(extra_env)
    p = subprocess.Popen(
        [sys.executable, "-m", "kortext.hooks.runner", event],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    out, err = p.communicate(json.dumps(payload))
    assert p.returncode == 0, f"Hook runner {event} failed: {err}"
    return json.loads(out)


def test_pre_invoke():
    # Turn 0 with KORTEXT_INJECT_INVARIANTS=1
    res0 = _run_runner("pre_invoke", {"invocationNum": 0}, {"KORTEXT_INJECT_INVARIANTS": "1"})
    assert "injectSteps" in res0, "Turn 0 with KORTEXT_INJECT_INVARIANTS=1 must inject steps"
    assert "Kortext" in res0["injectSteps"][0]["ephemeralMessage"], "Turn 0 message must mention Kortext"
    print("✅ PreInvocation Turn 0 (opt-in): Injected consolidated step successfully.")

    # Turn 1 must be silent
    res1 = _run_runner("pre_invoke", {"invocationNum": 1})
    assert res1 == {}, f"Turn 1 must be empty JSON, got {res1}"
    print("✅ PreInvocation Turn 1: Zero latency empty response verified.")

    # Turn 10 must also be silent (no every-10-turn KV-cache invalidation)
    res10 = _run_runner("pre_invoke", {"invocationNum": 10})
    assert res10 == {}, f"Turn 10 must be silent to preserve KV-cache prefix, got {res10}"
    print("✅ PreInvocation Turn 10: Silent KV-cache preservation verified.")


def test_pre_tool():
    # Safe tool
    res_safe = _run_runner("pre_tool", {"toolCall": {"name": "view_file", "args": {"AbsolutePath": "/test.py"}}})
    assert res_safe.get("decision") == "allow", "Safe tool should be allowed"
    print("✅ PreToolUse Safe: Standard view_file allowed immediately.")

    # Piped filter command (e.g. ps aux | grep python) -> allowed
    res_pipe = _run_runner("pre_tool", {"toolCall": {"name": "run_command", "args": {"CommandLine": "ps aux | grep python"}}})
    assert res_pipe.get("decision") == "allow", "Piped filter grep should be allowed"
    print("✅ PreToolUse Piped Grep: 'ps aux | grep python' allowed.")

    # Destructive command
    res_danger = _run_runner("pre_tool", {"toolCall": {"name": "run_command", "args": {"CommandLine": "rm -rf /something"}}})
    assert res_danger.get("decision") in ("ask", "deny"), "Destructive command must be gated or denied"
    print("✅ PreToolUse Safety Gate: 'rm -rf /' caught and gated.")

    # Symbol-like grep_search intercepted with auto-seek
    res_grep = _run_runner("pre_tool", {"toolCall": {"name": "grep_search", "args": {"Query": "def seek_symbol"}}})
    assert res_grep.get("decision") == "deny", "Symbol-like grep_search must be redirected to seek_symbol"
    assert "seek_symbol" in res_grep.get("reason", "")
    print("✅ PreToolUse Anti-Ripgrep Gate: 'grep_search' for symbol definition redirected to seek_symbol.")


def test_post_tool():
    res = _run_runner("post_tool", {"toolCall": {"name": "view_file", "args": {}}})
    assert res == {}, "PostToolUse must return {}"
    print("✅ PostToolUse: Returned {} conforming to protocol.")


def test_stop_guard():
    # Case A: Read-only session (no write_to_file) -> stop allowed
    res_readonly = _run_runner(
        "stop",
        {"executionNum": 0, "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "transcriptPath": ""},
    )
    assert res_readonly.get("decision") == "stop", "Read-only session must be allowed to stop"
    print("✅ Stop Hook Read-Only: Allowed termination with zero friction.")

    # Case B: File modified without outcome when KORTEXT_OUTCOME_GUARD_ENFORCE=1 -> continue required on executionNum == 1
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"tool_calls": [{"name": "write_to_file", "args": {"TargetFile": "/dummy.py"}}]}) + "\n")
        path_b = f.name

    res_mod = _run_runner(
        "stop",
        {"executionNum": 0, "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "transcriptPath": path_b},
        {"KORTEXT_OUTCOME_GUARD_ENFORCE": "1"},
    )
    assert res_mod.get("decision") == "continue", "Unpersisted modification with enforce=1 must return continue"
    print("✅ Stop Hook Guard (enforced): Intercepted unpersisted file changes.")

    # Case B2: executionNum > 1 -> must allow stop to prevent infinite continuation loop
    res_loop_guard = _run_runner(
        "stop",
        {"executionNum": 2, "terminationReason": "model_stop", "fullyIdle": True, "transcriptPath": path_b},
        {"KORTEXT_OUTCOME_GUARD_ENFORCE": "1"},
    )
    assert res_loop_guard.get("decision") == "stop", "executionNum > 1 must allow stop to prevent infinite loop"
    print("✅ Stop Hook Loop Guard: executionNum=2 allowed termination cleanly.")

    # Case C: File modified WITH record_system_outcome -> stop allowed
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"tool_calls": [{"name": "write_to_file", "args": {"TargetFile": "/dummy.py"}}]}) + "\n")
        f.write(json.dumps({"tool_calls": [{"name": "record_system_outcome", "args": {}}]}) + "\n")
        path_c = f.name

    res_done = _run_runner(
        "stop",
        {"executionNum": 0, "terminationReason": "NO_TOOL_CALL", "fullyIdle": True, "transcriptPath": path_c},
        {"KORTEXT_OUTCOME_GUARD_ENFORCE": "1"},
    )
    assert res_done.get("decision") == "stop", "Persisted modification must be allowed to stop"
    print("✅ Stop Hook Persisted: Allowed termination once outcome was recorded.")


if __name__ == "__main__":
    print("=== Running Antigravity Lifecycle Hooks Test Suite ===")
    test_pre_invoke()
    test_pre_tool()
    test_post_tool()
    test_stop_guard()
    print("🎉 ALL LIFECYCLE HOOK TESTS PASSED SUCCESSFULLY!")
