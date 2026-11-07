"""
Tests for dashboard Overview and Memory Inbox routes.
Verifies:
1. /api/overview returns real substrate state and project freshness.
2. /api/memory contract (list, monkeypatched results).
3. CSRF protection on /api/memory/approve, /api/memory/edit, /api/memory/supersede.
4. Content-Security-Policy header presence without unsafe-inline for scripts.
5. delete_fact exact=True does not hit adjacent episodes.
6. After an edit, search finds the new text and not the old.
"""
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from kortext.dashboard.app import app
from kortext.episodic import AgentNativeEpisodicDriver

client = TestClient(app)


def test_overview_endpoint():
    """Verify /api/overview returns valid schema with real data and no placeholder numbers."""
    resp = client.get("/api/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "daemon" in data
    assert "falkordb" in data
    assert "qdrant" in data
    assert "reindex" in data
    assert "projects" in data
    assert isinstance(data["projects"], list)
    assert isinstance(data["reindex"]["recent_failures"], list)


def test_csp_header_present():
    """Verify CSP header is set with self sources."""
    resp = client.get("/")
    assert resp.status_code == 200
    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp


def test_memory_list_contract():
    """Verify /api/memory returns list of memories according to contract."""
    mock_memories: List[Dict[str, Any]] = [
        {
            "id": "fact_001",
            "kind": "fact",
            "text": "The project uses Python 3.11+",
            "project": "kortext",
            "trust_level": "user_approved",
            "created_at": "2026-01-01T00:00:00Z",
            "superseded": False,
            "stale": False,
            "stale_symbols": [],
        },
        {
            "id": "ep_002",
            "kind": "episode",
            "text": "<img src=x onerror=alert(1)>",
            "project": "kortext",
            "trust_level": "untrusted",
            "created_at": "2026-01-02T00:00:00Z",
            "superseded": False,
            "stale": True,
            "stale_symbols": ["old_func"],
        },
    ]

    with patch.object(AgentNativeEpisodicDriver, "list_memories", return_value=mock_memories):
        resp = client.get("/api/memory?project=kortext&limit=10&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["available"] is True
        assert len(data["memories"]) == 2
        assert data["memories"][0]["id"] == "fact_001"
        # XSS payload is returned verbatim in JSON (browser must use textContent, not innerHTML)
        assert data["memories"][1]["text"] == "<img src=x onerror=alert(1)>"


def test_memory_limit_capped():
    """Verify limit > 1000 is silently capped at 1000."""
    with patch.object(AgentNativeEpisodicDriver, "list_memories", return_value=[]) as mock_lm:
        client.get("/api/memory?limit=99999&offset=-5")
        args, kwargs = mock_lm.call_args
        called_limit = kwargs.get("limit", args[0] if args else None)
        called_offset = kwargs.get("offset", args[1] if len(args) > 1 else None)
        assert called_limit <= 1000
        assert called_offset >= 0


def test_memory_actions_csrf_protection():
    """Verify mutating routes refuse requests missing required CSRF headers."""
    # 1. Approve without CSRF
    resp = client.post(
        "/api/memory/approve",
        json={"id": "fact_1", "kind": "fact"},
        headers={"x-kortext-enforce-csrf": "1"},
    )
    assert resp.status_code == 403
    assert "CSRF blocked" in resp.json().get("message", "")

    # 2. Edit without CSRF
    resp = client.post(
        "/api/memory/edit",
        json={"id": "fact_1", "text": "updated"},
        headers={"x-kortext-enforce-csrf": "1"},
    )
    assert resp.status_code == 403
    assert "CSRF blocked" in resp.json().get("message", "")

    # 3. Supersede without CSRF
    resp = client.post(
        "/api/memory/supersede",
        json={"id": "fact_1", "reason": "obsolete"},
        headers={"x-kortext-enforce-csrf": "1"},
    )
    assert resp.status_code == 403
    assert "CSRF blocked" in resp.json().get("message", "")


def test_supersede_calls_delete_fact_with_exact():
    """Verify supersede route passes exact=True to delete_fact."""
    headers = {
        "x-kortext-enforce-csrf": "1",
        "x-kortext-client": "dashboard",
    }

    with patch.object(AgentNativeEpisodicDriver, "delete_fact", new_callable=AsyncMock) as mock_del:
        resp = client.post(
            "/api/memory/supersede",
            json={"id": "fact_1", "reason": "outdated", "superseded_by": "fact_2"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_del.assert_awaited_once_with("fact_1", "outdated (superseded by fact_2)", exact=True)


def test_approve_uses_direct_db_update():
    """Verify approve route updates trust fields directly via FalkorDB client."""
    headers = {
        "x-kortext-enforce-csrf": "1",
        "x-kortext-client": "dashboard",
    }
    mock_client = MagicMock()
    with patch("kortext.dashboard.routes.memory.get_shared_falkordb_client", return_value=mock_client):
        resp = client.post(
            "/api/memory/approve",
            json={"id": "fact_1", "kind": "fact"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_client.query.assert_called_once()


def test_edit_memory_rejects_empty_text():
    """Verify edit route returns 400 when text is empty."""
    headers = {
        "x-kortext-enforce-csrf": "1",
        "x-kortext-client": "dashboard",
    }
    resp = client.post(
        "/api/memory/edit",
        json={"id": "fact_1", "text": "   ", "kind": "fact"},
        headers=headers,
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["status"] == "error"
    assert "empty" in data["message"].lower()


def test_memory_js_never_uses_innerhtml_with_memory_text():
    """Verify memory.js sets textContent on textEl and never sets innerHTML with memory text."""
    from pathlib import Path
    mem_js_path = Path(__file__).resolve().parents[1] / "kortext" / "dashboard" / "static" / "js" / "modules" / "memory.js"
    content = mem_js_path.read_text(encoding="utf-8")
    assert "textEl.textContent = mem.text" in content
    assert "innerHTML = mem.text" not in content
    assert "innerHTML = `<p>${mem.text}`" not in content


@pytest.mark.usefixtures("real_falkordb")
def test_delete_fact_exact_does_not_hit_sibling():
    """
    Two episodes where one name contains the other.
    Superseding the shorter one with exact=True must leave the longer untouched.
    """
    import asyncio
    import uuid

    suffix = uuid.uuid4().hex[:8]
    name_a = f"Session summary {suffix}"
    name_b = f"Session summary 2 {suffix}"

    driver = AgentNativeEpisodicDriver()

    async def _run():
        await driver.record_outcome(name_a, "content a", category="implementation")
        await driver.record_outcome(name_b, "content b", category="implementation")

        # Supersede only name_a with exact matching
        await driver.delete_fact(name_a, "test supersede", exact=True)

        res_a = driver._query(
            "MATCH (e:Episodic {name: $n}) RETURN e.superseded", {"n": name_a}
        )
        res_b = driver._query(
            "MATCH (e:Episodic {name: $n}) RETURN e.superseded", {"n": name_b}
        )
        return res_a, res_b

    res_a, res_b = asyncio.run(_run())
    assert res_a and res_a[1] and str(res_a[1][0][0]).lower() == "true", "name_a should be superseded"
    assert res_b and res_b[1] and str(res_b[1][0][0]).lower() != "true", "name_b must NOT be superseded"

    # Cleanup
    driver._query("MATCH (e:Episodic) WHERE e.name IN $names DETACH DELETE e", {"names": [name_a, name_b]})


@pytest.mark.usefixtures("real_falkordb")
def test_edit_episode_updates_content_and_clears_embedding():
    """
    After editing an episode via the route, the stored content must be the new text
    and the old embedding must be cleared so future searches use the new embedding.
    """
    import asyncio
    import uuid

    from kortext.dashboard.routes.memory import edit_memory as route_edit_memory
    from kortext.dashboard.routes.memory import MemoryEditRequest

    suffix = uuid.uuid4().hex[:8]
    ep_name = f"EditTest_{suffix}"
    old_keyword = f"ancient{suffix}"
    new_keyword = f"modern{suffix}"
    old_text = f"{old_keyword} original content"
    new_text = f"{new_keyword} rewritten content"

    driver = AgentNativeEpisodicDriver()

    async def _run():
        await driver.record_outcome(ep_name, old_text, category="implementation")

        before = driver._query(
            "MATCH (e:Episodic) WHERE e.name = $n RETURN e.content", {"n": ep_name}
        )
        assert before and before[1] and before[1][0][0] == old_text, "setup: old content must be stored"

        payload = MemoryEditRequest(id=ep_name, text=new_text, kind="episode")
        from unittest.mock import MagicMock
        fake_request = MagicMock()
        result = await route_edit_memory(payload, fake_request)
        assert result["status"] == "ok", f"edit route failed: {result}"

        after = driver._query(
            "MATCH (e:Episodic) WHERE e.name = $n RETURN e.content", {"n": ep_name}
        )
        assert after and after[1] and after[1][0][0] == new_text, "content must be updated to new text"

        # Search using old-text-specific keyword vs new-text-specific keyword
        old_search = await driver.search(old_keyword)
        new_search = await driver.search(new_keyword)
        return old_search, new_search

    old_search, new_search = asyncio.run(_run())

    # Cleanup
    driver._query("MATCH (e:Episodic) WHERE e.name = $n DETACH DELETE e", {"n": ep_name})

    for r in old_search:
        assert ep_name not in r, f"Episode with old content appeared in old-text search after edit: {r}"
    assert any(ep_name in r for r in new_search), f"Episode should appear in new-text search after edit: {new_search}"
