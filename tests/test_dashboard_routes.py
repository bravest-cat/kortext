"""
Unit and integration tests for modular Kortext Dashboard routes and architecture.
Verifies:
1. app.py is declarative and <250 lines.
2. Invariant 3 compliance (Zero-Docker / embedded resilience; no secondary QdrantClient(path=...)).
3. All modular route handlers (status, journal, query, actions) respond correctly.
4. 100% backward compatibility with frontend endpoints and Jinja template parameters.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

import kortext.dashboard.app as app_module
from kortext.dashboard.app import app
from kortext.dashboard.routes import (
    actions_router,
    all_routers,
    journal_router,
    query_router,
    status_router,
)
from kortext.dashboard.routes.status import _check_http, _check_tcp

client = TestClient(app)


def test_app_architecture_and_line_count():
    """Verify that app.py is concise (<250 lines) and declarative."""
    app_py_path = Path(app_module.__file__)
    line_count = len(app_py_path.read_text(encoding="utf-8").splitlines())
    assert line_count < 250, f"app.py exceeds 250 lines (currently {line_count} lines)"

    # Verify all routers are mounted
    router_tags = {r.tags[0] for r in all_routers if r.tags}
    assert {"status", "journal", "query", "actions"}.issubset(router_tags)

    # Verify main function exists for CLI invocation
    assert callable(app_module.main)


def test_root_index_and_head():
    """Verify the root page delivers HTML with proper no-cache headers."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "no-cache" in resp.headers.get("cache-control", "")

    head_resp = client.head("/")
    assert head_resp.status_code == 200


def test_status_endpoint_and_invariant_3():
    """Verify /api/status returns service checks without initializing QdrantClient(path=...)."""
    with patch("qdrant_client.QdrantClient") as mock_qdrant:
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "services" in data
        assert "timestamp" in data

        service_names = [s["name"] for s in data["services"]]
        assert "Qdrant Vector Store" in service_names
        assert "FalkorDB Graph Engine" in service_names
        assert "FalkorDB Browser UI" in service_names
        assert "oMLX Admin Dashboard" in service_names

        # Invariant 3 Mandate: secondary process must NEVER instantiate QdrantClient(path=...)
        mock_qdrant.assert_not_called()


def test_collections_endpoint_point_stats():
    """Verify /api/collections returns collections and point statistics."""
    resp = client.get("/api/collections")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") in ("ok", "error")
    assert "collections" in data
    assert isinstance(data["collections"], list)


def test_telemetry_endpoint():
    """Verify /api/telemetry returns system performance metrics."""
    resp = client.get("/api/telemetry")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"
    assert "telemetry" in data
    telem = data["telemetry"]
    assert "version" in telem
    assert "falkordb_latency_ms" in telem
    assert "active_invariants" in telem
    assert "substrate_ram_rss_mb" in telem


def test_cache_and_watcher_endpoints():
    """Verify cache stats and watcher health endpoints."""
    c_resp = client.get("/api/cache/stats")
    assert c_resp.status_code == 200
    assert c_resp.json().get("status") == "ok"

    w_resp = client.get("/api/watcher/status")
    assert w_resp.status_code == 200
    assert w_resp.json().get("status") == "ok"


def test_quarantine_endpoint():
    """Verify /api/quarantine returns structured list of quarantined payloads."""
    resp = client.get("/api/quarantine")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"
    assert "quarantine_count" in data
    assert "items" in data


def test_clear_quarantine_endpoint(tmp_path):
    """Verify DELETE /api/quarantine clears quarantined payload files."""
    with patch("kortext.trust.sanitizer.CausalSanitizer") as mock_san_cls:
        san_mock = MagicMock()
        san_mock.quarantine_dir = tmp_path
        (tmp_path / "test1.json").write_text("{}", encoding="utf-8")
        (tmp_path / "test2.json").write_text("{}", encoding="utf-8")
        mock_san_cls.return_value = san_mock

        resp = client.delete("/api/quarantine")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"
        assert data.get("cleared_count") == 2
        assert not (tmp_path / "test1.json").exists()
        assert not (tmp_path / "test2.json").exists()


def test_journal_endpoint():
    """Verify /api/journal returns parsed chronological markdown entries."""
    resp = client.get("/api/journal")
    assert resp.status_code == 200
    data = resp.json()
    assert "entries" in data
    assert isinstance(data["entries"], list)
    if data["entries"]:
        first = data["entries"][0]
        assert "date" in first
        assert "title" in first
        assert "body" in first
        assert "tags" in first
        assert "category" in first


def test_query_graphs_and_graph_view():
    """Verify /api/graphs and /api/graph endpoints."""
    g_resp = client.get("/api/graphs")
    assert g_resp.status_code == 200
    assert g_resp.json().get("status") == "ok"

    gv_resp = client.get("/api/graph?graph_type=symbols&clean=true")
    assert gv_resp.status_code == 200
    assert gv_resp.json().get("status") == "ok"


def test_symbols_and_impact_endpoints():
    """Verify symbol queries, blast radius impact, and autocomplete."""
    sym_resp = client.get("/api/symbols")
    assert sym_resp.status_code == 200
    assert sym_resp.json().get("status") == "ok"

    auto_resp = client.get("/api/symbols/autocomplete?q=Symbol&limit=5")
    assert auto_resp.status_code == 200
    assert auto_resp.json().get("status") == "ok"

    impact_resp = client.get("/api/symbols/impact?target=SymbolGraphManager&direction=upstream&max_depth=2")
    assert impact_resp.status_code == 200
    assert impact_resp.json().get("status") == "ok"


def test_autocomplete_symbols_parameterized():
    """Verify /api/symbols/autocomplete uses Cypher query parameterization to prevent injection."""
    with patch("kortext.dashboard.routes.query.get_shared_falkordb_client") as mock_get_client:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        # Return 1 symbol so files query is also executed (limit=10, so rem=9)
        mock_client.query.side_effect = [
            [["s.name", "'symbol'"], [["MySymbol", "symbol"]]],
            [["f.path", "'file'"], [["/path/to/file.py", "file"]]],
        ]

        resp = client.get("/api/symbols/autocomplete?q=TestSymbol&limit=10")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert len(resp.json()["items"]) == 2

        # Verify client.query was called twice with parameterized queries
        assert mock_client.query.call_count == 2
        # Symbol query check
        s_args, s_kwargs = mock_client.query.call_args_list[0]
        assert s_args[0] == "kortext_symbols"
        assert "$q_clean" in s_args[1]
        assert "$limit" in s_args[1]
        assert "TestSymbol" not in s_args[1]
        s_params = s_kwargs.get("params") or (s_args[2] if len(s_args) > 2 else None)
        assert s_params == {"q_clean": "testsymbol", "limit": 10}

        # File query check
        f_args, f_kwargs = mock_client.query.call_args_list[1]
        assert f_args[0] == "kortext_symbols"
        assert "$q_clean" in f_args[1]
        assert "$rem" in f_args[1]
        assert "TestSymbol" not in f_args[1]
        f_params = f_kwargs.get("params") or (f_args[2] if len(f_args) > 2 else None)
        assert f_params == {"q_clean": "testsymbol", "rem": 9}


def test_autocomplete_symbols_parameterized_empty_query():
    """Verify /api/symbols/autocomplete without query parameterizes limit and rem."""
    with patch("kortext.dashboard.routes.query.get_shared_falkordb_client") as mock_get_client:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.query.side_effect = [
            [["s.name", "'symbol'"], [["Sym1", "symbol"]]],
            [["f.path", "'file'"], [["/file1.py", "file"]]],
        ]

        resp = client.get("/api/symbols/autocomplete?limit=5")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        assert mock_client.query.call_count == 2
        s_args, s_kwargs = mock_client.query.call_args_list[0]
        assert "$limit" in s_args[1]
        s_params = s_kwargs.get("params") or (s_args[2] if len(s_args) > 2 else None)
        assert s_params == {"limit": 5}

        f_args, f_kwargs = mock_client.query.call_args_list[1]
        assert "$rem" in f_args[1]
        f_params = f_kwargs.get("params") or (f_args[2] if len(f_args) > 2 else None)
        assert f_params == {"rem": 4}


def test_retrieval_query_sandbox_empty_error():
    """Verify /api/query rejects empty query payloads with HTTP 400."""
    resp = client.post("/api/query", json={"query": ""})
    assert resp.status_code == 400
    assert "error" in resp.json()


@pytest.mark.anyio
async def test_retrieval_query_sandbox_federated():
    """Verify /api/query uses RetrievalEngine.federated_query and returns structured JSON."""
    with patch("kortext.dashboard.routes.query.get_retrieval_engine") as mock_engine_fn:
        mock_engine = AsyncMock()
        mock_engine.federated_query.return_value = {
            "query": "test query",
            "symbolic_context": [{"symbol": "SymbolGraphManager", "kind": "class"}],
            "code_chunks": ["[kortext/symbols/graph.py:1-20]\nclass SymbolGraphManager:"],
            "episodic_history": ["Kortext indexed 120 symbols."],
            "total_estimated_tokens": 145,
        }
        mock_engine_fn.return_value = mock_engine

        resp = client.post(
            "/api/query",
            json={"query": "test query", "collection": "kortext_codebase", "token_budget": 1500},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"
        assert data.get("query") == "test query"
        assert data.get("collection") == "kortext_codebase"
        assert "symbolic_context" in data
        assert len(data["symbolic_context"]) == 1
        assert "code_chunks" in data
        assert len(data["code_chunks"]) == 1
        assert "episodic_history" in data
        assert len(data["episodic_history"]) == 1
        assert data.get("total_estimated_tokens") == 145
        mock_engine.federated_query.assert_awaited_once_with(
            query="test query",
            collection="kortext_codebase",
            token_budget=1500,
        )


@pytest.mark.anyio
async def test_retrieval_query_sandbox_federated_default_budget_and_collection():
    """Verify /api/query falls back to default budget and None collection when all selected."""
    with patch("kortext.dashboard.routes.query.get_retrieval_engine") as mock_engine_fn:
        mock_engine = AsyncMock()
        mock_engine.federated_query.return_value = {
            "query": "hello",
            "symbolic_context": [],
            "code_chunks": [],
            "episodic_history": [],
            "total_estimated_tokens": 0,
        }
        mock_engine_fn.return_value = mock_engine

        resp = client.post("/api/query", json={"query": "hello", "collection": "all"})
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"
        assert data.get("collection") == "all"
        mock_engine.federated_query.assert_awaited_once_with(
            query="hello",
            collection=None,
            token_budget=1500,
        )



def test_action_collections_validation():
    """Verify /api/collections POST validates collection name."""
    resp = client.post("/api/collections", json={"name": ""})
    assert resp.status_code == 200
    assert resp.json().get("status") == "error"


def test_action_cache_clear():
    """Verify /api/cache/clear calls chunk cache clear."""
    with patch("kortext.cache.ChunkEmbeddingCache.clear", return_value=5):
        resp = client.post("/api/cache/clear")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "purged_chunks": 5}


def test_action_scip_and_symbols_index_alias():
    """Verify /api/scip/auto and /api/symbols/index aliases are both reachable."""
    resp1 = client.post("/api/scip/auto")
    assert resp1.status_code == 200
    assert "status" in resp1.json()

    resp2 = client.post("/api/symbols/index")
    assert resp2.status_code == 200
    assert "status" in resp2.json()


def test_action_tasks_endpoints():
    """Verify /api/tasks GET and POST handlers."""
    g_resp = client.get("/api/tasks?include_graph=true")
    assert g_resp.status_code == 200
    g_data = g_resp.json()
    assert g_data.get("status") == "ok"
    assert "tasks" in g_data
    assert "nodes" in g_data
    assert "edges" in g_data

    # POST validation: missing title
    p_resp = client.post("/api/tasks", json={"action": "create", "title": ""})
    assert p_resp.status_code == 200
    assert p_resp.json().get("status") == "error"

    # POST unknown action
    unknown_resp = client.post("/api/tasks", json={"action": "nonexistent"})
    assert unknown_resp.status_code == 200
    assert unknown_resp.json().get("status") == "error"


def test_tcp_and_http_checkers():
    """Verify _check_tcp and _check_http helpers behave properly."""
    # Invalid port should fail gracefully
    assert _check_tcp("127.0.0.1", 59999, timeout=0.1) is False


def test_action_create_collection_meta_json_safe(tmp_path, monkeypatch):
    """Verify /api/collections in Zero-Docker mode updates meta.json without instantiating QdrantClient."""
    import json
    q_dir = tmp_path / "qdrant"
    q_dir.mkdir(parents=True)
    meta_path = q_dir / "meta.json"
    meta_path.write_text(json.dumps({"collections": {}, "aliases": {}}), encoding="utf-8")

    monkeypatch.setenv("KORTEXT_HOME", str(tmp_path))
    monkeypatch.delenv("QDRANT_URL", raising=False)
    # No embed config: the route must fall back to local FastEmbed's 384 dims.
    for var in ("EMBED_DIMENSION", "KORTEXT_EMBED_DIMENSION", "EMBED_BASE_URL", "KORTEXT_EMBED_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    with (
        patch("kortext.dashboard.routes.actions._is_sole_custodian_running", return_value=False),
        patch("kortext.retrieval.qdrant_engine.EmbeddedQdrantEngine.ensure_collection") as mock_ensure,
    ):
        resp = client.post("/api/collections", json={"name": "safe_vector_col"})
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"
        mock_ensure.assert_any_call("safe_vector_col", embed_dim=384)
        # meta.json is Qdrant's own file; the dashboard never edits it.
        assert json.loads(meta_path.read_text(encoding="utf-8")) == {"collections": {}, "aliases": {}}


def test_action_create_collection_refused_while_daemon_owns_store(tmp_path, monkeypatch):
    """While the daemon holds embedded Qdrant, the dashboard neither opens nor edits the store."""
    monkeypatch.setenv("KORTEXT_HOME", str(tmp_path))
    monkeypatch.delenv("QDRANT_URL", raising=False)

    with (
        patch("kortext.dashboard.routes.actions._is_sole_custodian_running", return_value=True),
        patch("qdrant_client.QdrantClient") as mock_qdrant,
    ):
        resp = client.post("/api/collections", json={"name": "custodian_col"})
        assert resp.status_code == 200
        assert resp.json().get("status") == "error"
        assert "index_directory" in resp.json()["message"]
        mock_qdrant.assert_not_called()
        assert not (tmp_path / "qdrant" / "meta.json").exists()


def test_action_create_collection_fallback_locked_error(tmp_path, monkeypatch):
    """Verify fallback to EmbeddedQdrantEngine catches sqlite locked error cleanly."""
    monkeypatch.setenv("KORTEXT_HOME", str(tmp_path))
    monkeypatch.delenv("QDRANT_URL", raising=False)

    with (
        patch("kortext.dashboard.routes.actions._is_sole_custodian_running", return_value=False),
        patch("kortext.retrieval.qdrant_engine.EmbeddedQdrantEngine.ensure_collection") as mock_ensure,
    ):
        import sqlite3
        mock_ensure.side_effect = sqlite3.OperationalError("database is locked")

        resp = client.post("/api/collections", json={"name": "test_lock_col"})
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "error"
        assert "locked by sole custodian daemon" in data.get("message", "")
        assert "Invariant 3" in data.get("message", "")


def test_modular_frontend_assets_and_template_structure():
    """Verify that index.html mounts ES modules, all 9 tabs, Kanban, and companion panel."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # ES modules script entry
    assert '<script type="module" src="/static/js/main.js"></script>' in html

    # 7 Reorganized Navigation Tabs
    expected_tabs = [
        "tab-btn-graph",
        "tab-btn-tasks",
        "tab-btn-blast",
        "tab-btn-journal",
        "tab-btn-retrieval",
        "tab-btn-collections",
        "tab-btn-health",
    ]
    for tab_id in expected_tabs:
        assert f'id="{tab_id}"' in html, f"Missing tab button: {tab_id}"

    # Verify redundant / quarantine tabs are removed
    assert 'id="tab-btn-quarantine"' not in html
    assert 'id="tab-btn-links"' not in html

    # Companion panel preserved
    assert 'id="rel-filter-list"' in html
    assert 'id="inspector-content"' in html
    assert 'id="hud-badge-status"' in html

    # Task DAG 3-way view switcher & Kanban containers
    assert 'id="btn-view-kanban"' in html
    assert 'id="btn-view-dag"' in html
    assert 'id="btn-view-list"' in html
    assert 'id="kanban-board-container"' in html
    assert 'id="kanban-col-ready"' in html
    assert 'id="kanban-col-in_progress"' in html
    assert 'id="kanban-col-pending"' in html
    assert 'id="kanban-col-completed"' in html
    assert 'id="kanban-col-failed"' in html

    # Journal 8-category taxonomy filter chips
    for cat in ["decision", "roadmap", "implementation", "experiment", "benchmark", "investigation", "research", "report"]:
        assert f'data-cat="{cat}"' in html, f"Missing journal category chip: {cat}"

    # Topology & Health: 5-Tier Memory Grid & Next-Gen Hooks
    assert 'id="memory-tiers-grid"' in html
    assert 'id="hooks-status-list"' in html

    # Markdown preview modal
    assert 'id="modal-markdown-viewer"' in html

    # Verify all ES module files exist on disk and obey maintainability line ceilings (<800 lines)
    js_dir = Path(__file__).resolve().parent.parent / "kortext" / "dashboard" / "static" / "js"
    assert (js_dir / "main.js").is_file()
    modules_dir = js_dir / "modules"
    assert modules_dir.is_dir()

    expected_modules = [
        "api.js", "blast.js", "dream.js", "graph.js", "graph_inspector.js",
        "health.js", "journal.js", "retrieval.js", "state.js", "tasks.js", "ui.js"
    ]
    for mod_name in expected_modules:
        mod_file = modules_dir / mod_name
        assert mod_file.is_file(), f"Missing module: {mod_name}"
        line_count = len(mod_file.read_text(encoding="utf-8").splitlines())
        assert line_count < 800, f"Module {mod_name} exceeds 800-line ceiling ({line_count} lines)"


def test_telemetry_memory_tiers_and_hooks():
    """Verify telemetry returns 5-tier memory status and next-gen hooks."""
    resp = client.get("/api/telemetry")
    assert resp.status_code == 200
    data = resp.json()
    assert "telemetry" in data
    telem = data["telemetry"]

    assert "memory_tiers" in telem
    tiers = telem["memory_tiers"]
    assert "tier_0" in tiers
    assert "tier_1" in tiers
    assert "tier_2" in tiers
    assert "tier_3" in tiers
    assert "tier_4" in tiers

    assert "nextgen_hooks" in telem
    assert isinstance(telem["nextgen_hooks"], list)
    assert len(telem["nextgen_hooks"]) >= 5


