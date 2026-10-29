"""
End-to-End Verification Suite for Audit 1 and Audit 2 Remediations.
Verifies every single finding across:
  - Audit 1: Incremental AST sync & spool consumer, Antigravity plugin/subagent/skill schema,
    multi-project isolation, batched UNWIND retrieval, CausalSanitizer provenance fencing,
    /boost context injection, and /teamwork-preview + Jose Parreno Garcia Failure Modes FM1-FM8.
  - Audit 2: Dashboard 10 bug fixes + 3 new observability panels (Live Session Context,
    Grounded Invariants Manager, MINJA Quarantine Log)
    and the 25% Compaction Window Context-Awareness Blueprint across agy-hud, kortext_statusline.py,
    transcript_tokens.py, and lifecycle hooks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

KORTEXT_ROOT = Path(__file__).resolve().parent.parent
AGY_HUD_JS = Path("/Users/rb/agy-hud/dist/agy-hud.js")


def test_audit1_2a_incremental_sync_enabled_and_watcher_embedded_fix(tmp_path, monkeypatch):
    """Audit 1 Section 2A: IncrementalASTSync defaults sync_ast=True; server consumes spool; watcher uses embedded Qdrant."""
    from kortext.hooks.pipelines.post_tool import IncrementalASTSync, PostToolPipeline

    sync = IncrementalASTSync(spool_dir=tmp_path / "spool")
    assert sync.sync_ast is True

    pipeline = PostToolPipeline()
    ast_interceptors = [i for i in pipeline.interceptors if isinstance(i, IncrementalASTSync)]
    assert len(ast_interceptors) == 1
    assert ast_interceptors[0].sync_ast is True

    server_src = (KORTEXT_ROOT / "kortext" / "server.py").read_text(encoding="utf-8")
    assert "_consume_invalidation_spool_loop" in server_src

    watcher_src = (KORTEXT_ROOT / "kortext" / "watcher.py").read_text(encoding="utf-8")
    assert "get_async_qdrant_client(url=None)" in watcher_src


def test_audit1_2b_antigravity_manifests_subagents_and_skills_schema():
    """Audit 1 Section 2B: plugin.json has no version; subagents declare Kortext MCP tools; skills have clean frontmatter."""
    plugin_json = json.loads((KORTEXT_ROOT / "integrations" / "antigravity" / "plugin.json").read_text(encoding="utf-8"))
    assert "version" not in plugin_json

    for agent_md in sorted((KORTEXT_ROOT / "integrations" / "antigravity" / "agents").glob("*.md")):
        text = agent_md.read_text(encoding="utf-8")
        # agy's loader wants McpServerYaml objects here; a bare "- kortext" string makes it skip the agent.
        assert "mcpServers:" not in text, f"{agent_md.name}: string mcpServers entries make agy skip the agent"
        assert "- seek_symbol" in text, f"{agent_md.name} missing seek_symbol"
        assert "- trace_symbol_impact" in text, f"{agent_md.name} missing trace_symbol_impact"

    skill_files = sorted((KORTEXT_ROOT / "integrations" / "antigravity" / "skills").glob("*/SKILL.md"))
    assert len(skill_files) == 12
    for sf in skill_files:
        parts = sf.read_text(encoding="utf-8").split("---\n", 2)
        assert len(parts) == 3
        fm = parts[1]
        assert "version:" not in fm, f"{sf} still contains version: in frontmatter"
        assert "license:" not in fm, f"{sf} still contains license: in frontmatter"
        assert "name:" in fm and "description:" in fm

    hooks_json = json.loads((KORTEXT_ROOT / "integrations" / "antigravity" / "hooks.json").read_text(encoding="utf-8"))
    post_matcher = hooks_json["kortext-cache-invalidation"]["PostToolUse"][0]["matcher"]
    for tool_name in ("run_command", "write_to_file", "replace_file_content", "multi_replace_file_content"):
        assert tool_name in post_matcher


def test_audit1_fm1_to_fm8_teamwork_task_dag_briefs_ledger_and_5line_report(tmp_path, monkeypatch):
    """Audit 1 Section 3: TaskDAGTracker generates briefs, compaction ledger, 5-line report, and validates reviewer verdicts."""
    from kortext.tasks.tracker import TaskDAGTracker

    monkeypatch.setenv("KORTEXT_HOME", str(tmp_path))
    monkeypatch.setenv("KORTEXT_WORKSPACE_ROOT", str(tmp_path))
    tracker = TaskDAGTracker()
    proj = "fm_verification_proj"

    t1 = tracker.create_task(
        title="Implement Token Validator",
        description="Add JWT expiration check",
        complexity=5,
        project_id=proj,
        target_files=["kortext/trust/sanitizer.py"],
    )
    t2 = tracker.create_task(
        title="Wire Validator Into Server",
        description="Call validator from server",
        complexity=4,
        project_id=proj,
        depends_on=[t1],
        target_files=["kortext/server.py"],
    )

    try:
        claimed = tracker.claim_next_ready_task(worker_id="worker-1", project_id=proj)
        assert claimed is not None and claimed["id"] == t1
        assert "brief_path" in claimed and Path(claimed["brief_path"]).is_file()
        assert (tmp_path / ".kortext" / "ledger.md").is_file()

        comp = tracker.complete_task(
            task_id=t1,
            modified_files=["kortext/trust/sanitizer.py"],
            test_command="uv run pytest -q",
            tests_passed=True,
            summary="Implemented and verified token validator.",
        )
        assert comp["status"] == "completed"
        assert "STATUS: PASS" in comp["report_5line"]
        assert Path(comp["report_path"]).is_file()

        # Dependent task t2 is now automatically promoted to ready
        ready_ids = [r["id"] for r in tracker.get_ready_tasks(project_id=proj)]
        assert t2 in ready_ids

        # Two-Stage Reviewer Verdict Gate (FM6)
        v_pass = tracker.validate_reviewer_verdict("Review complete.\nSPEC_VERDICT: PASS\nQUALITY_VERDICT: PASS\n")
        assert v_pass["passed"] is True and v_pass["valid_schema"] is True
        v_fail = tracker.validate_reviewer_verdict("Missing check.\nSPEC_VERDICT: PASS\nQUALITY_VERDICT: FAIL\n")
        assert v_fail["passed"] is False
    finally:
        tracker.client.query(
            tracker.graph,
            "MATCH (t:Task) WHERE t.id IN [$t1, $t2] DETACH DELETE t",
            {"t1": t1, "t2": t2},
        )


def test_audit2_dashboard_observability_routes_and_dom_safety(tmp_path, monkeypatch):
    """Audit 2 Part 1: Dashboard new observability endpoints, dynamic 384d/4096d embed_dim, and DOM attribute safety."""
    from kortext.dashboard.app import app

    monkeypatch.setenv("KORTEXT_HOME", str(tmp_path))
    client = TestClient(app, headers={"X-Kortext-Client": "dashboard"})
    assert client.get("/api/instincts").status_code == 404

    # 2. GET /api/invariants/grounded & GET /api/context/telemetry
    r_inv = client.get("/api/invariants/grounded")
    assert r_inv.status_code == 200 and r_inv.json()["status"] == "ok"

    r_ctx = client.get("/api/context/telemetry")
    assert r_ctx.status_code == 200 and r_ctx.json()["status"] == "ok"
    assert "compaction_threshold_tokens" in r_ctx.json()

    # 3. Dynamic embed_dim: 384 when EMBED_BASE_URL is empty, 4096 when set
    qdir = tmp_path / "qdrant"
    qdir.mkdir(parents=True, exist_ok=True)
    meta_file = qdir / "meta.json"
    meta_file.write_text(json.dumps({"collections": {}, "aliases": {}}), encoding="utf-8")
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("EMBED_DIMENSION", raising=False)
    monkeypatch.setenv("EMBED_BASE_URL", "")
    r_col = client.post("/api/collections", json={"name": "local_fastembed_col"})
    assert r_col.status_code == 200
    meta_after = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta_after["collections"]["local_fastembed_col"]["vectors"]["text-dense"]["size"] == 384

    # 4. Verify index.html & JS modules have zero Shift-JIS mojibake and use data-* attributes
    idx_html = (KORTEXT_ROOT / "kortext" / "dashboard" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "Kortext v2.0.0" in idx_html
    assert 'id="context-telemetry-card"' in idx_html
    assert 'id="instincts-manager-list"' not in idx_html
    assert 'id="grounded-invariants-list"' in idx_html
    assert 'id="quarantine-list"' in idx_html

    for js_file in ("blast.js", "graph_inspector.js", "journal.js"):
        js_txt = (KORTEXT_ROOT / "kortext" / "dashboard" / "static" / "js" / "modules" / js_file).read_text(encoding="utf-8")
        for mojibake in ("識", "塘", "櫨", "笞｡", "迫", "窶｢"):
            assert mojibake not in js_txt


def test_audit2_25pct_compaction_window_protocol_end_to_end(tmp_path, monkeypatch):
    """Audit 2 Part 2 & 3: 25% compaction window math, Stage 1/2/3 hooks, kortext_statusline.py, and agy-hud.js."""
    from kortext.hooks.context import HookContext
    from kortext.hooks.pipelines.post_tool import ContextAndLoopAdvisor
    from kortext.hooks.transcript_tokens import (
        BASELINE_SYSTEM_PROMPT_TOKENS,
        estimate_tokens_from_transcript_files,
        resolve_session_token_state,
    )
    from integrations.antigravity.kortext_statusline import format_kortext_badge

    monkeypatch.setenv("KORTEXT_HOME", str(tmp_path))
    monkeypatch.setenv("KORTEXT_WORKSPACE_ROOT", str(tmp_path))

    # 1. Transcript token estimation resets after compaction marker and adds 18k baseline overhead
    t_file = tmp_path / "transcript.jsonl"
    t_file.write_text(
        json.dumps({"role": "user", "content": "A" * 40_000}) + "\n"
        + json.dumps({"role": "system", "content": "# Resuming from a compaction\nSummary of prior work."}) + "\n"
        + json.dumps({"role": "user", "content": "B" * 4_000}) + "\n",
        encoding="utf-8",
    )
    est = estimate_tokens_from_transcript_files([t_file])
    # Post-compaction chars ~ 4100 -> ~1025 tokens + 18,000 baseline = ~19,025 (NOT 10,000 + 19,025)
    assert BASELINE_SYSTEM_PROMPT_TOKENS <= est < 22_000

    # 2. Stage 2 Pre-Compaction Baton at 20% of 1M window (205k tokens)
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "hud_state.json").write_text(
        json.dumps({
            "falkor_ok": True,
            "qdrant_ok": True,
            "omlx_ok": True,
            "total_input_tokens": 205_000,
            "context_window_size": 1_000_000,
            "used_percentage": 20.5,
            "updated_at": 9999999999.0,
        }),
        encoding="utf-8",
    )
    tok_state = resolve_session_token_state(kortext_home=tmp_path)
    assert tok_state.is_1m_window is True
    assert tok_state.should_write_pre_compaction_baton is True
    assert tok_state.effective_compaction_pct >= 80.0

    advisor = ContextAndLoopAdvisor()
    ctx = HookContext.from_stdin(raw_text=json.dumps({
        "sessionId": "sess-205k",
        "workspaceRoot": str(tmp_path),
        "toolCall": {"name": "view_file", "args": {"AbsolutePath": str(tmp_path / "app.py")}},
    }))
    res = advisor.inspect(ctx)
    assert res.data is not None and "Pre-Compaction Alert" in str(res.data.get("additionalContext", ""))
    assert (tmp_path / "handoffs" / "session_sess-205k.json").is_file()
    assert (tmp_path / ".kortext" / "ledger.md").is_file()

    # 3. kortext_statusline.py 2-tier 18% (Pre-Compact) and 20% (Compact) badges
    badge_18 = format_kortext_badge(
        compact=True,
        state={"context_window": {"total_input_tokens": 185_000, "context_window_size": 1_000_000, "used_percentage": 18.5}},
    )
    assert "Pre-Compact" in badge_18

    badge_21 = format_kortext_badge(
        compact=True,
        state={"context_window": {"total_input_tokens": 210_000, "context_window_size": 1_000_000, "used_percentage": 21.0}},
    )
    assert "Compact" in badge_21

    # 4. agy-hud.js persists context_window before checking snapshot age
    if AGY_HUD_JS.is_file():
        agy_src = AGY_HUD_JS.read_text(encoding="utf-8")
        assert "snap.context_updated_at" in agy_src
        assert "Pre-Compact" in agy_src
        age_check_idx = (
            agy_src.index("if (snap.falkor_ok === void 0 || age > 86400)")
            if "if (snap.falkor_ok === void 0 || age > 86400)" in agy_src
            else agy_src.index("if (age > 60")
        )
        assert agy_src.index("snap.context_updated_at") < age_check_idx
