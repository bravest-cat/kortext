import json
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from kortext.hooks.context import HookContext
from kortext.hooks.pipelines.post_tool import IncrementalASTSync, PostToolPipeline
from kortext.hooks.pipelines.stop import StopPipeline


def test_tool_name_normalization():
    """Verify tool names like mcp_kortext_seek_symbol are normalized to bare names."""
    for raw, expected in [
        ("mcp_kortext_seek_symbol", "seek_symbol"),
        ("mcp_kortext_trace_symbol_impact", "trace_symbol_impact"),
        ("mcp_kortext_search_knowledge_and_memory", "search_knowledge_and_memory"),
        ("client:run_command", "run_command"),
        ("mcp:kortext:seek_symbol", "seek_symbol"),
        ("edit_file", "edit_file"),
        ("write_file", "write_file"),
        ("read_file", "read_file"),
    ]:
        ctx = HookContext(tool_call={"name": raw})
        assert ctx.tool_name == expected, f"Expected {raw} -> {expected}, got {ctx.tool_name}"


def test_edit_file_post_tool_queues_reindex(tmp_path):
    """
    Assert that an edit_file PostToolUse payload queues re-index for the target path.
    """
    fixtures_dir = Path(__file__).parent / "fixtures" / "agy_1_5_2_hooks"
    payload = json.loads((fixtures_dir / "post_tool_edit_file.json").read_text(encoding="utf-8"))

    # Create target file on disk so isfile check passes
    target = tmp_path / "scratch_note.py"
    target.write_text("def hello():\n    return 2\n", encoding="utf-8")

    payload["toolCall"]["args"]["Path"] = str(target)
    payload["workspacePaths"] = [str(tmp_path)]

    spool_dir = tmp_path / "spool" / "invalidation"
    ctx = HookContext.from_stdin(json.dumps(payload))
    assert ctx.tool_name == "edit_file"
    assert ctx.target_file == str(target)

    sync = IncrementalASTSync(spool_dir=spool_dir, sync_ast=False)
    res = sync.process(ctx)
    assert res.decision == "allow"

    spooled_events = list(spool_dir.glob("event_*.json"))
    assert len(spooled_events) == 1, "Expected 1 spooled invalidation event"

    event_data = json.loads(spooled_events[0].read_text(encoding="utf-8"))
    assert event_data["action"] == "invalidate"
    assert event_data["path"] == str(target)


def test_stop_payload_reaches_record_session_log(tmp_path):
    """
    Assert that a real agy 1.5.2 Stop payload (executionNum=0, terminationReason=NO_TOOL_CALL)
    executes interceptors and reaches record_session_log.
    """
    fixtures_dir = Path(__file__).parent / "fixtures" / "agy_1_5_2_hooks"
    stop_payload = json.loads((fixtures_dir / "stop_.json").read_text(encoding="utf-8"))
    transcript_src = fixtures_dir / "transcript_full.jsonl"

    target_file = tmp_path / "module.py"
    target_file.write_text("def hello(): return 2\n", encoding="utf-8")

    # Write transcript pointing to target_file
    transcript_lines = transcript_src.read_text(encoding="utf-8").replace("/tmp/test-repo/module.py", str(target_file))
    transcript_file = tmp_path / "transcript_full.jsonl"
    transcript_file.write_text(transcript_lines, encoding="utf-8")

    stop_payload["transcriptPath"] = str(transcript_file)
    stop_payload["workspacePaths"] = [str(tmp_path)]
    stop_payload["artifactDirectoryPath"] = str(tmp_path)

    ctx = HookContext.from_stdin(json.dumps(stop_payload))
    assert ctx.execution_num == 0
    assert ctx.termination_reason == "NO_TOOL_CALL"

    pipeline = StopPipeline()
    with patch("kortext.vault.record_session_log") as mock_log, \
         patch("kortext.vault.generate_memory_index"):
        res = pipeline.execute(ctx)
        assert res.decision == "stop"
        mock_log.assert_called_once()
        called_ctx, modified_paths = mock_log.call_args[0]
        assert str(target_file) in modified_paths


def test_hooks_json_matchers():
    hooks_file = Path(__file__).resolve().parent.parent / "integrations" / "antigravity" / "hooks.json"
    data = json.loads(hooks_file.read_text(encoding="utf-8"))

    pre_matcher = data["kortext-safety-gate"]["PreToolUse"][0]["matcher"]
    for tool in ("write_file", "edit_file", "read_file", "run_command"):
        assert re.fullmatch(pre_matcher, tool), f"PreToolUse matcher did not fully match {tool}"

    post_matcher = data["kortext-cache-invalidation"]["PostToolUse"][0]["matcher"]
    for tool in ("write_file", "edit_file"):
        assert re.fullmatch(post_matcher, tool), f"PostToolUse matcher did not fully match {tool}"


def test_daemon_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("KORTEXT_HOME", str(tmp_path))
    import kortext.daemon

    # 1. Process with "kortext" in command line
    proc1 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)  # kortext"])
    pfile = kortext.daemon.pid_file()
    pfile.write_text(str(proc1.pid), encoding="utf-8")

    stopped1 = kortext.daemon.stop()
    assert stopped1 is True
    assert proc1.wait(timeout=5) is not None
    assert proc1.poll() is not None

    # 2. Process without kortext in command line
    py_bin = "/usr/bin/python3" if "kortext" in sys.executable and Path("/usr/bin/python3").is_file() else sys.executable
    proc2 = subprocess.Popen([py_bin, "-c", "import time; time.sleep(60)"])
    pfile.write_text(str(proc2.pid), encoding="utf-8")
    try:
        stopped2 = kortext.daemon.stop()
        assert stopped2 is False
        assert proc2.poll() is None
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc2.kill()


def test_pre_invoke_pipeline_advisory_and_memory_index(tmp_path, monkeypatch):
    monkeypatch.setenv("KORTEXT_HOME", str(tmp_path / "kortext_home"))
    monkeypatch.delenv("JOURNAL_DIR", raising=False)

    from kortext.hooks.pipelines.post_tool import queue_pending_advisory
    from kortext.hooks.pipelines.pre_invoke import PreInvokePipeline

    ws = tmp_path / "workspace"
    mem_dir = ws / ".kortext" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "INDEX.md").write_text("# Memory Index: test_proj\n- Decision 1\n", encoding="utf-8")

    queue_pending_advisory("Test pending advisory message", session_id="test_sess")

    ctx = HookContext(
        event="pre_invoke",
        session_id="test_sess",
        cwd=str(ws),
        workspace_paths=(str(ws),),
        turn_index=0,
        raw_payload={"executionNum": 1},
    )

    pipeline = PreInvokePipeline()
    res = pipeline.execute(ctx)
    data = res.data or {}
    inject_steps = data.get("injectSteps", ())

    assert len(inject_steps) >= 2

    advisory_step = next((s for s in inject_steps if "Test pending advisory message" in s.get("ephemeralMessage", "")), None)
    memory_step = next((s for s in inject_steps if "Decision 1" in s.get("ephemeralMessage", "")), None)

    assert advisory_step is not None
    assert memory_step is not None
    assert advisory_step != memory_step

    assert "[unverified memory" not in advisory_step.get("ephemeralMessage", "")
    assert "[unverified memory" in memory_step.get("ephemeralMessage", "")

