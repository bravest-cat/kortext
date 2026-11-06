"""
Tests for user-facing CLI features:
1. kortext init [PATH]
2. kortext doctor [--json]
3. kortext savings [--days N] [--project P] [--json] and GET /api/savings
4. kortext affected [--base B] [--root R] [--json] and find_affected_tests MCP tool
5. kortext dream --purge-stale / --no-purge-stale
6. Bug hunt: verification that deleted modules are not imported
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kortext import cli
from kortext.affected import find_affected_tests, is_code_file, is_test_file
from kortext.doctor import format_doctor_report, run_doctor
from kortext.savings import (
    extract_files_and_sizes_from_reply,
    get_savings_summary,
    record_savings_event,
)


# ============================================================================
# 1. kortext init
# ============================================================================


def test_init_parser_flags():
    parser = cli.build_parser()
    args = parser.parse_args(["init", "/tmp/myworkspace", "--no-plugin", "--skip-binaries"])
    assert args.command == "init"
    assert args.path == "/tmp/myworkspace"
    assert args.no_plugin is True
    assert args.skip_binaries is True


def test_init_idempotent_runs_twice_non_interactive(tmp_path, monkeypatch, capsys):
    # Ensure HOME points to tmp_path so real ~/.gemini is never touched
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "sample_workspace"
    workspace.mkdir(parents=True)
    sample_file = workspace / "main.py"
    sample_file.write_text("def hello(): return 'world'\n", encoding="utf-8")

    args = argparse.Namespace(path=str(workspace), no_plugin=True, skip_binaries=True)

    with patch("kortext.setup.start_substrates", return_value=(True, "running")), patch(
        "kortext.ingest.run_cli_ingest"
    ) as mock_ingest:
        # First run
        cli.cmd_init(args)
        out1 = capsys.readouterr().out
        assert "Step 1/4: Setup complete" in out1
        assert "Step 2/4: Plugin install skipped (--no-plugin)" in out1
        assert "Step 3/4: Substrates running" in out1
        assert "Step 4/4: Indexed workspace" in out1
        assert "Next steps:" in out1
        mock_ingest.assert_called_once_with(str(workspace.resolve()), force=False)

        # Second run (safe to run twice)
        mock_ingest.reset_mock()
        cli.cmd_init(args)
        out2 = capsys.readouterr().out
        assert "Step 1/4: Setup complete" in out2
        assert "Step 4/4: Indexed workspace" in out2
        mock_ingest.assert_called_once_with(str(workspace.resolve()), force=False)


def test_init_stops_at_first_failure_with_fix(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    args = argparse.Namespace(path=str(tmp_path), no_plugin=True, skip_binaries=True)

    # Simulate unsupported python
    fake_diag = SimpleNamespace(python_compatible=False, python_version="3.9.1")
    with patch("kortext.setup.run_diagnostics", return_value=fake_diag):
        with pytest.raises(SystemExit) as exc:
            cli.cmd_init(args)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Python >= 3.10 is required" in out


# ============================================================================
# 2. kortext doctor
# ============================================================================


def test_doctor_all_ten_checks():
    checks = run_doctor()
    assert len(checks) == 10
    check_names = {c["check"] for c in checks}
    expected_names = {
        "python_version",
        "substrate_binaries",
        "falkordb",
        "qdrant",
        "daemon",
        "plugin_installed",
        "embedder_cache",
        "cwd_indexed",
        "spool_failed",
        "agy_version",
    }
    assert check_names == expected_names
    for c in checks:
        assert c["status"] in ("ok", "warn", "fail")
        assert "what" in c and len(c["what"]) > 0
        assert "fix" in c


def test_doctor_formatting_one_line_each():
    checks = [
        {"status": "ok", "check": "python_version", "what": "Python 3.11+", "fix": ""},
        {"status": "warn", "check": "daemon", "what": "Daemon sleeping", "fix": "kortext server"},
        {"status": "fail", "check": "falkordb", "what": "FalkorDB unreachable", "fix": "kortext start"},
    ]
    report = format_doctor_report(checks)
    lines = report.splitlines()
    assert len(lines) == 3
    assert lines[0] == "[ok] Python 3.11+"
    assert lines[1] == "[warn] Daemon sleeping - Fix: kortext server"
    assert lines[2] == "[fail] FalkorDB unreachable - Fix: kortext start"


def test_doctor_substrates_down_reports_not_crash_under_5s(monkeypatch, capsys):
    # Set substrates to unreachable ports
    monkeypatch.setenv("FALKORDB_PORT", "65431")
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:65432")

    t0 = time.perf_counter()
    checks = run_doctor()
    elapsed = time.perf_counter() - t0

    assert elapsed < 5.0, f"Doctor took {elapsed:.2f}s when substrates down (limit 5.0s)"

    falkor_check = next(c for c in checks if c["check"] == "falkordb")
    qdrant_check = next(c for c in checks if c["check"] == "qdrant")

    assert falkor_check["status"] == "fail"
    assert "kortext start" in falkor_check["fix"]
    assert qdrant_check["status"] == "fail"
    assert "kortext start" in qdrant_check["fix"]

    # cmd_doctor exits 1 on failure
    args = argparse.Namespace(json=False)
    with patch("kortext.doctor.run_doctor", return_value=checks):
        with pytest.raises(SystemExit) as exc:
            cli.cmd_doctor(args)
        assert exc.value.code == 1


def test_doctor_json_output(capsys):
    args = argparse.Namespace(json=True)
    try:
        cli.cmd_doctor(args)
    except SystemExit:
        pass
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 10


# ============================================================================
# 3. Savings report
# ============================================================================


def test_record_savings_event_cheap_under_2ms_and_bounded_rotation(tmp_path):
    # Benchmark call speed
    t0 = time.perf_counter()
    for i in range(50):
        record_savings_event(
            event="test_event",
            reply_bytes=100 + i,
            file_bytes=1000 + i,
            project="test_proj",
            home=tmp_path,
        )
    elapsed = (time.perf_counter() - t0) / 50
    assert elapsed < 0.002, f"Savings append took {elapsed*1000:.3f} ms (expected < 2 ms)"

    savings_file = tmp_path / "run" / "savings.jsonl"
    assert savings_file.is_file()

    # Test rotation at 5 MB
    chunk = ("a" * 1000 + "\n").encode("utf-8")
    with open(savings_file, "wb") as f:
        for _ in range(5300):
            f.write(chunk)
    assert savings_file.stat().st_size >= 5 * 1024 * 1024

    # Next append triggers rotation to savings.jsonl.1
    record_savings_event(event="rotated_event", reply_bytes=50, home=tmp_path)
    rotated_file = tmp_path / "run" / "savings.jsonl.1"
    assert rotated_file.is_file()
    assert rotated_file.stat().st_size >= 5 * 1024 * 1024
    assert savings_file.stat().st_size < 1024


def test_record_savings_event_never_raises():
    # Pass an invalid home path that cannot be written
    record_savings_event(
        event="unraisable",
        reply_bytes=10,
        home=Path("/proc/non_existent_and_not_writable_dir"),
    )


def test_extract_files_and_sizes_from_reply(tmp_path):
    f1 = tmp_path / "a.py"
    f1.write_text("print('hello')", encoding="utf-8")
    size1 = f1.stat().st_size

    reply = {
        "results": [
            {"file_path": str(f1), "name": "foo"},
            {"file_path": str(f1), "name": "bar"},  # Duplicate
        ]
    }
    total_bytes, distinct_files = extract_files_and_sizes_from_reply(reply)
    assert len(distinct_files) == 1
    assert total_bytes == size1


def test_get_savings_summary_and_cli(tmp_path, capsys):
    # Populate a savings log
    record_savings_event(event="grep_denied", reply_bytes=0, file_bytes=2000, project="p1", home=tmp_path)
    record_savings_event(event="seek_symbol", reply_bytes=400, file_bytes=4400, project="p1", home=tmp_path)

    summary = get_savings_summary(days=30, project="p1", home=tmp_path)
    assert summary["counts_by_kind"] == {"grep_denied": 1, "seek_symbol": 1}
    assert summary["reply_bytes_served"] == 400
    assert summary["distinct_file_bytes"] == 6400
    # ESTIMATE = (6400 - 400) // 4 = 1500 tokens
    assert summary["estimate_tokens_saved"] == 1500
    assert "upper bound" in summary["note"]

    # Test CLI output
    args = argparse.Namespace(days=30, project="p1", json=False)
    with patch("kortext.savings.get_kortext_home", return_value=tmp_path):
        cli.cmd_savings(args)
    out = capsys.readouterr().out
    assert "Kortext Token Savings Report" in out
    assert "grep_denied: 1" in out
    assert "seek_symbol: 1" in out
    assert "Reply bytes served: 400 bytes" in out
    assert "ESTIMATE tokens saved: 1,500 tokens" in out
    assert "upper bound" in out
    assert "%" not in out  # No headline percentage


@pytest.mark.asyncio
async def test_api_savings_route(tmp_path):
    from kortext.dashboard.routes.status import get_savings

    record_savings_event(event="seek_symbol", reply_bytes=100, file_bytes=1300, project="proj_api", home=tmp_path)
    with patch("kortext.savings.get_kortext_home", return_value=tmp_path):
        res = await get_savings(days=30, project="proj_api")
        assert res["counts_by_kind"] == {"seek_symbol": 1}
        assert res["reply_bytes_served"] == 100
        assert res["distinct_file_bytes"] == 1300
        assert res["estimate_tokens_saved"] == (1300 - 100) // 4


# ============================================================================
# 4. Affected tests & find_affected_tests MCP tool
# ============================================================================


def test_is_test_file_and_is_code_file():
    # Test file patterns
    assert is_test_file("test_foo.py") is True
    assert is_test_file("foo_test.py") is True
    assert is_test_file("foo_test.go") is True
    assert is_test_file("foo.test.ts") is True
    assert is_test_file("foo.spec.ts") is True
    assert is_test_file("FooTest.java") is True
    assert is_test_file("tests/TestFoo.kt") is True
    assert is_test_file("src/normal_file.py") is False
    assert is_test_file("main.go") is False

    # Code file patterns
    assert is_code_file("kortext/cli.py") is True
    assert is_code_file("src/main.rs") is True
    assert is_code_file("pyproject.toml") is False
    assert is_code_file("README.md") is False
    assert is_code_file("config.json") is False


def test_affected_tests_complete_false_on_config():
    # When a diff contains non-code or unindexed file
    mock_diff = "diff --git a/pyproject.toml b/pyproject.toml\n@@ -1 +1 @@\n"
    completed = subprocess.CompletedProcess(args=["git", "diff"], returncode=0, stdout=mock_diff, stderr="")
    with patch("subprocess.run", return_value=completed):
        res = find_affected_tests(workspace_root=".", base="HEAD")
        assert res["complete"] is False
        assert "pyproject.toml" in res["reason"]
        assert "uv run pytest -q" in res["command"]


def test_affected_tests_complete_false_on_file_with_no_symbols():
    # When a code file is changed but has no symbols in the graph
    mock_diff = "diff --git a/kortext/empty_module.py b/kortext/empty_module.py\n@@ -1,5 +1,5 @@\n"
    completed = subprocess.CompletedProcess(args=["git", "diff"], returncode=0, stdout=mock_diff, stderr="")
    mock_client = MagicMock()
    mock_client.query.return_value = ["header", []]
    with patch("subprocess.run", return_value=completed), \
         patch("kortext.affected.get_shared_falkordb_client", return_value=mock_client), \
         patch("pathlib.Path.is_file", return_value=True):
        res = find_affected_tests(workspace_root=".", base="HEAD")
        assert res["complete"] is False
        assert "no symbols in graph" in res["reason"]


@pytest.mark.asyncio
async def test_affected_mcp_tool_metadata():
    from kortext.server import mcp

    tool = await mcp.get_tool("find_affected_tests")
    assert tool is not None, "find_affected_tests tool was not registered on FastMCP"
    assert tool.meta.get("antigravity/alwaysLoad") is False
    assert "static call graph only" in tool.description


# ============================================================================
# 5. kortext dream --purge-stale bug fix
# ============================================================================


def test_dream_purge_stale_flag_parsing():
    parser = cli.build_parser()

    # Default is True
    args_default = parser.parse_args(["dream"])
    assert args_default.purge_stale is True

    # Explicit --purge-stale is True
    args_explicit = parser.parse_args(["dream", "--purge-stale"])
    assert args_explicit.purge_stale is True

    # Explicit --no-purge-stale is False
    args_negated = parser.parse_args(["dream", "--no-purge-stale"])
    assert args_negated.purge_stale is False


def test_dream_no_purge_stale_reaches_engine():
    args = argparse.Namespace(project="test_proj", purge_stale=False, dry_run=True)

    async def _mock_dream_cycle(**kwargs):
        return {"markdown_report": "report ok"}

    with patch("kortext.dream.DreamEngine.run_dream_cycle", side_effect=_mock_dream_cycle) as mock_run:
        cli.cmd_dream(args)
        mock_run.assert_called_once_with(
            project="test_proj",
            purge_stale=False,
            dry_run=True,
        )


# ============================================================================
# 6. Bug hunt: deleted modules verification
# ============================================================================


def test_deleted_modules_not_imported():
    deleted_names = [
        "kortext.retrieval.hipporag",
        "kortext.retrieval.knee_trimmer",
        "kortext.retrieval.reranker",
        "kortext.retrieval.retention",
        "kortext.trust.instincts",
        "kortext.learning",
    ]
    for mod_name in deleted_names:
        with pytest.raises(ModuleNotFoundError):
            __import__(mod_name)


def test_post_invoke_pipeline_imports_cleanly():
    from kortext.hooks.pipelines.post_invoke import PostInvokePipeline

    pipeline = PostInvokePipeline()
    assert len(pipeline.interceptors) == 3
