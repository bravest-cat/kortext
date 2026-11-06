"""Tests for Antigravity CLI gaps (1.4.x - 1.5.x compatibility):
1. Reply size limit (10 KB max for search_knowledge_and_memory and seek_symbol).
2. Root conversation scoping in memory_session_handoff and fastmcp proxy forwarding.
3. Headless telemetry fallback to transcript when no statusline data exists.
4. PreInvocation prompt secret defense terminating before model invocation.
5. Statusline HUD cold path asynchronous refresh and plain-text ASCII fallback.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.server import create_proxy

from kortext.handoff import HandoffManager, HandoffRecord
from kortext.hooks.context import HookContext
from kortext.hooks.pipelines.pre_invoke import PreInvokePipeline, PromptSecretInterceptor
from kortext.hooks.runner import emit_response, run_hook
from kortext.hooks.transcript_tokens import (
    check_and_update_compaction_watermark,
    estimate_tokens_from_transcript_files,
    resolve_session_token_state,
)
from kortext.server_helpers import enforce_reply_size_limit, extract_root_conversation_id


# =============================================================================
# 1. Reply size limit (<10,000 bytes)
# =============================================================================


def test_enforce_reply_size_limit_normal_payload():
    small_payload = {
        "status": "found",
        "matches": [{"symbol": "test_fn", "file": "test.py", "line": 10}],
        "detail_level": "skeleton",
    }
    result = enforce_reply_size_limit(small_payload)
    parsed = json.loads(result)
    assert len(result.encode("utf-8")) <= 10000
    assert "truncated" not in parsed
    assert "dropped_count" not in parsed
    assert len(parsed["matches"]) == 1


def test_enforce_reply_size_limit_oversized_seek_symbol():
    large_matches = [
        {
            "symbol": f"symbol_{i}",
            "file": f"/path/to/module_{i}.py",
            "signature": f"def symbol_{i}(arg_a: str, arg_b: int, arg_c: list[dict[str, Any]]) -> dict[str, Any]:",
            "callers": [f"caller_{j}_{i}" for j in range(15)],
            "docstring": "Detailed documentation line for symbol " * 10,
        }
        for i in range(40)
    ]
    raw_payload = {
        "status": "found",
        "matches": large_matches,
        "detail_level": "interface",
    }
    raw_json = json.dumps(raw_payload)
    assert len(raw_json.encode("utf-8")) > 15000

    bounded_json = enforce_reply_size_limit(raw_payload, item_keys=["matches"], max_bytes=10000)
    assert len(bounded_json.encode("utf-8")) <= 10000

    parsed = json.loads(bounded_json)
    assert parsed["truncated"] is True
    assert parsed["dropped_count"] > 0
    assert "dropped" not in parsed
    assert len(parsed["matches"]) < 40
    # First (highest-ranked) match should still be preserved
    assert parsed["matches"][0]["symbol"] == "symbol_0"


def test_enforce_reply_size_limit_longest_serialized_list():
    # code_chunks has 2 small chunks (~100 bytes each)
    # episodic_history has 10 huge facts (~1500 bytes each, totaling ~15KB)
    code_chunks = ["def foo(): return 1", "def bar(): return 2"]
    episodic_facts = [f"Decision {i}: " + ("context " * 200) for i in range(10)]
    payload = {
        "status": "ok",
        "code_chunks": list(code_chunks),
        "episodic_history": list(episodic_facts),
    }
    res = enforce_reply_size_limit(payload, item_keys=["code_chunks", "episodic_history"], max_bytes=10000)
    parsed = json.loads(res)
    assert len(res.encode("utf-8")) <= 10000
    assert parsed["truncated"] is True
    assert parsed["dropped_count"] > 0
    assert "dropped" not in parsed
    # Under longest-serialized-list strategy, episodic_history is popped first
    # So code_chunks does NOT vanish! Both small chunks remain intact
    assert len(parsed["code_chunks"]) == 2
    assert len(parsed["episodic_history"]) < 10


def test_enforce_reply_size_limit_single_huge_item():
    # Case A: One single huge item in a list
    huge_list_payload = {
        "status": "found",
        "matches": ["A" * 30000],
    }
    res_a = enforce_reply_size_limit(huge_list_payload, item_keys=["matches"], max_bytes=10000)
    parsed_a = json.loads(res_a)
    assert len(res_a.encode("utf-8")) <= 10000
    assert parsed_a["status"] == "found"
    assert parsed_a["truncated"] is True
    assert parsed_a["dropped_count"] == 1
    assert parsed_a["matches"] == []

    # Case B: One single huge item outside any list (large non-list field)
    huge_field_payload = {
        "status": "found",
        "detail": "B" * 30000,
    }
    res_b = enforce_reply_size_limit(huge_field_payload, max_bytes=10000)
    parsed_b = json.loads(res_b)
    assert len(res_b.encode("utf-8")) <= 10000
    assert parsed_b["status"] == "found"
    assert parsed_b["truncated"] is True
    assert parsed_b["dropped_count"] >= 1

    # Case C: Single huge item with heavy overview and markdown fields
    huge_combo_payload = {
        "status": "ok",
        "markdown": "M" * 15000,
        "architectural_overview": "O" * 15000,
    }
    res_c = enforce_reply_size_limit(huge_combo_payload, max_bytes=10000)
    parsed_c = json.loads(res_c)
    assert len(res_c.encode("utf-8")) <= 10000
    assert parsed_c["status"] == "ok"
    assert parsed_c["truncated"] is True
    assert parsed_c["dropped_count"] >= 2


def test_enforce_reply_size_limit_oversized_search_knowledge():
    code_chunks = [
        f"# Chunk {i}\ndef process_stream_{i}():\n" + ("    data = buffer.read(1024)\n" * 25)
        for i in range(20)
    ]
    episodic_facts = [f"Episodic fact {i}: Architectural decision regarding pipeline {i}" for i in range(30)]

    raw_payload = {
        "query": "process stream buffer",
        "confidence_verdict": "strong",
        "code_chunks": code_chunks,
        "episodic_history": episodic_facts,
        "symbols": [{"name": f"sym_{i}"} for i in range(10)],
    }
    raw_json = json.dumps(raw_payload)
    assert len(raw_json.encode("utf-8")) > 15000

    bounded_json = enforce_reply_size_limit(
        raw_payload,
        item_keys=["code_chunks", "episodic_history", "symbols"],
        max_bytes=10000,
    )
    assert len(bounded_json.encode("utf-8")) <= 10000

    parsed = json.loads(bounded_json)
    assert parsed["truncated"] is True
    assert parsed["dropped_count"] > 0
    # Code chunks (lowest priority among tiers) dropped first
    assert len(parsed["code_chunks"]) < len(code_chunks)


# =============================================================================
# 2. Root conversation scope & FastMCP proxy forwarding
# =============================================================================


def test_memory_session_handoff_root_conversation_scope(tmp_path):
    manager = HandoffManager(tmp_path)

    # Agent creates baton for parent root conversation A
    b1_msg = manager.create_baton(
        target_agent="kortext-task-runner",
        current_scope="Parent A task",
        task_id="task_a_001",
        root_conversation_id="conv_root_A",
    )
    assert "task_a_001" in b1_msg

    # Agent creates baton for parent root conversation B
    b2_msg = manager.create_baton(
        target_agent="kortext-task-runner",
        current_scope="Parent B task",
        task_id="task_b_001",
        root_conversation_id="conv_root_B",
    )
    assert "task_b_001" in b2_msg

    # Subagent in root conversation A lists batons
    list_a = json.loads(manager.list_batons(root_conversation_id="conv_root_A"))
    baton_ids_a = [b["task_id"] for b in list_a["pending_batons"]]
    assert "task_a_001" in baton_ids_a
    assert "task_b_001" not in baton_ids_a

    # Subagent in root conversation B lists batons
    list_b = json.loads(manager.list_batons(root_conversation_id="conv_root_B"))
    baton_ids_b = [b["task_id"] for b in list_b["pending_batons"]]
    assert "task_b_001" in baton_ids_b
    assert "task_a_001" not in baton_ids_b

    # Read latest pending baton in conversation A
    read_a = json.loads(manager.read_baton(root_conversation_id="conv_root_A"))
    assert read_a["task_id"] == "task_a_001"
    assert read_a["root_conversation_id"] == "conv_root_A"


@pytest.mark.asyncio
async def test_fastmcp_proxy_forwards_meta_to_daemon():
    """Proves that _meta['antigravity.google/root_conversation_id'] crosses the fastmcp proxy to the tool."""
    daemon = FastMCP("kortext-daemon")
    captured_root_id = []

    @daemon.tool()
    def sample_daemon_tool(ctx: Context = None) -> str:
        root_id = extract_root_conversation_id(ctx)
        captured_root_id.append(root_id)
        return f"received:{root_id}"

    proxy = create_proxy(daemon, name="kortext-proxy")
    test_root_conv = "conv_root_agent_999"

    async with Client(proxy) as client:
        res = await client.call_tool(
            "sample_daemon_tool",
            {},
            meta={"antigravity.google/root_conversation_id": test_root_conv},
        )
        assert res.data == f"received:{test_root_conv}"

    assert captured_root_id == [test_root_conv]


# =============================================================================
# 3. Headless telemetry fallback to transcript
# =============================================================================


def test_headless_telemetry_falls_back_to_transcript(tmp_path):
    kortext_home = tmp_path / ".kortext"
    run_dir = kortext_home / "run"
    run_dir.mkdir(parents=True)

    # hud_state.json exists but only contains figures for a DIFFERENT session
    hud_state = {
        "updated_at": 100000.0,
        "total_input_tokens": 190000,
        "context_window_size": 200000,
        "used_percentage": 95.0,
        "sessions": {
            "session_other_window": {
                "total_input_tokens": 190000,
                "context_window_size": 200000,
                "used_percentage": 95.0,
            }
        },
    }
    (run_dir / "hud_state.json").write_text(json.dumps(hud_state), encoding="utf-8")

    # The current headless session has a transcript with usage metadata
    transcript_file = tmp_path / "transcript.jsonl"
    turn_steps = [
        {"type": "user", "content": "Write unit tests"},
        {
            "type": "assistant",
            "content": "Writing tests now",
            "usage": {
                "input_tokens": 42000,
                "cache_read_input_tokens": 10000,
                "output_tokens": 500,
            },
        },
    ]
    transcript_file.write_text("\n".join(json.dumps(s) for s in turn_steps) + "\n", encoding="utf-8")

    state = resolve_session_token_state(
        steps=turn_steps,
        raw_payload={"conversationId": "session_headless_current"},
        transcript_path=str(transcript_file),
        kortext_home=kortext_home,
        session_id="session_headless_current",
    )

    # Must NOT use the 190,000 tokens from session_other_window
    assert state.estimated_tokens == 52000  # 42000 + 10000
    assert state.source == "transcript_usage"
    assert state.used_percentage < 30.0


def test_headless_compaction_watermark_tracking(tmp_path):
    kortext_home = tmp_path / ".kortext"
    session_id = "session_headless_compact"

    # Step count drops from 20 to 2 (compaction boundary)
    check_and_update_compaction_watermark(
        session_id=session_id,
        step_count=20,
        estimated_tokens=180000,
        used_percentage=90.0,
        kortext_home=kortext_home,
    )

    res = check_and_update_compaction_watermark(
        session_id=session_id,
        step_count=2,
        estimated_tokens=25000,
        used_percentage=12.5,
        kortext_home=kortext_home,
    )

    assert res["compacted"] is True
    assert res["previous_steps"] == 20
    assert res["previous_tokens"] == 180000


# =============================================================================
# 4. PreInvocation prompt secrets defense
# =============================================================================


def test_pre_invoke_blocks_openai_key():
    interceptor = PromptSecretInterceptor()
    ctx = HookContext(
        last_user_input="Here is my test key: sk-proj-1234567890abcdef1234567890abcdef for the API.",
    )
    res = interceptor.process(ctx)
    assert res.decision == "terminate"
    assert res.data["terminationBehavior"] == "terminate"
    inject_steps = res.data["injectSteps"]
    assert len(inject_steps) == 1
    user_msg = inject_steps[0]["errorMessage"]["userMessage"]
    assert "Blocked" in user_msg
    # Secret must not be echoed in userMessage
    assert "sk-proj" not in user_msg


def test_pre_invoke_blocks_github_pat():
    interceptor = PromptSecretInterceptor()
    ctx = HookContext(
        last_user_input="curl -H 'Authorization: token ghp_123456789012345678901234567890123456' https://api.github.com",
    )
    res = interceptor.process(ctx)
    assert res.decision == "terminate"
    assert res.data["terminationBehavior"] == "terminate"
    assert "ghp_" not in res.data["injectSteps"][0]["errorMessage"]["userMessage"]


def test_pre_invoke_blocks_private_key():
    interceptor = PromptSecretInterceptor()
    ctx = HookContext(
        last_user_input="Deploy with this: -----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...",
    )
    res = interceptor.process(ctx)
    assert res.decision == "terminate"
    assert res.data["terminationBehavior"] == "terminate"


def test_pre_invoke_allows_normal_prompt():
    interceptor = PromptSecretInterceptor()
    ctx = HookContext(
        last_user_input="How do I implement Dijkstra's shortest path algorithm in Python?",
    )
    res = interceptor.process(ctx)
    assert res.decision == "allow"


def test_pre_invoke_bypassed_with_env_override(monkeypatch):
    monkeypatch.setenv("KORTEXT_BLOCK_PROMPT_SECRETS", "0")
    interceptor = PromptSecretInterceptor()
    ctx = HookContext(
        last_user_input="Here is sk-1234567890abcdef1234567890abcdef",
    )
    res = interceptor.process(ctx)
    assert res.decision == "allow"


def test_emit_response_serializes_termination_behavior():
    stdout_buf = io.StringIO()
    with patch("sys.stdout", stdout_buf):
        emit_response(
            event="pre_invoke",
            decision="terminate",
            data={
                "terminationBehavior": "terminate",
                "injectSteps": [
                    {"errorMessage": {"userMessage": "Blocked credential"}}
                ],
            },
        )
    raw = stdout_buf.getvalue()
    payload = json.loads(raw)
    assert payload["terminationBehavior"] == "terminate"
    assert payload["injectSteps"][0]["errorMessage"]["userMessage"] == "Blocked credential"


def test_pre_invoke_pipeline_real_prompt_secret_stops_early_and_emits_json():
    pipeline = PreInvokePipeline()
    ctx = HookContext(
        event="pre_invoke",
        last_user_input="Deploy the cluster using AWS key AKIAIOSFODNN7EXAMPLE immediately.",
    )
    result = pipeline.execute(ctx)
    assert result.decision == "terminate"
    assert result.data["terminationBehavior"] == "terminate"

    stdout_buf = io.StringIO()
    with patch("sys.stdout", stdout_buf):
        emit_response(
            event="pre_invoke",
            decision=result.decision,
            reason=result.reason,
            data=result.data,
        )

    out = json.loads(stdout_buf.getvalue())
    assert out["terminationBehavior"] == "terminate"
    assert "injectSteps" in out
    assert len(out["injectSteps"]) == 1
    err_step = out["injectSteps"][0]
    assert "errorMessage" in err_step
    assert "Blocked: Prompt contains potential credentials" in err_step["errorMessage"]["userMessage"]
    assert "AKIAIOSFODNN7EXAMPLE" not in err_step["errorMessage"]["userMessage"]
    assert "ephemeralMessage" not in err_step


def test_emit_response_terminate_discards_other_steps_and_always_includes_error():
    stdout_buf = io.StringIO()
    with patch("sys.stdout", stdout_buf):
        emit_response(
            event="pre_invoke",
            decision="terminate",
            reason="Violation detected",
            data={
                "injectSteps": [
                    {"ephemeralMessage": "Substrate offline notice"},
                    {"systemMessage": "Other system notice"},
                ],
            },
        )
    out = json.loads(stdout_buf.getvalue())
    assert out["terminationBehavior"] == "terminate"
    assert len(out["injectSteps"]) == 1
    assert out["injectSteps"][0] == {"errorMessage": {"userMessage": "Violation detected"}}


# =============================================================================
# 5. Statusline HUD cold path & ASCII fallback
# =============================================================================


def test_statusline_cold_path_does_not_call_query_dag_task_counts(tmp_path, monkeypatch):
    from integrations.antigravity.kortext_statusline import format_kortext_badge

    monkeypatch.setenv("KORTEXT_HOME", str(tmp_path))
    # No hud_state.json exists (completely cold)
    # Ensure query_dag_task_counts is NOT called during rendering
    with patch("integrations.antigravity.kortext_statusline.query_dag_task_counts") as mock_dag:
        badge = format_kortext_badge(compact=False)
        assert mock_dag.call_count == 0
        assert "Kortext" in badge


def test_statusline_ascii_mode_has_no_ansi_or_unicode(tmp_path, monkeypatch):
    from integrations.antigravity.kortext_statusline import format_kortext_badge

    monkeypatch.setenv("KORTEXT_HOME", str(tmp_path))
    monkeypatch.setenv("KORTEXT_STATUSLINE_ASCII", "1")

    badge_full = format_kortext_badge(compact=False)
    badge_compact = format_kortext_badge(compact=True)

    assert "\033" not in badge_full
    assert "\033" not in badge_compact
    assert all(ord(c) < 128 for c in badge_full), f"Non-ASCII found in full badge: {badge_full!r}"
    assert all(ord(c) < 128 for c in badge_compact), f"Non-ASCII found in compact badge: {badge_compact!r}"
    assert "Kortext" in badge_full
    assert "F:" in badge_compact
