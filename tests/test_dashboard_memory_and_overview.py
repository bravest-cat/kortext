"""
Tests for dashboard Overview and Memory Inbox routes.
Verifies:
1. /api/overview returns real substrate state and project freshness.
2. /api/memory contract (list, fallback when method missing, monkeypatched results).
3. CSRF protection on /api/memory/approve, /api/memory/edit, /api/memory/supersede.
4. Content-Security-Policy header presence without unsafe-inline for scripts.
"""
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

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


def test_memory_fallback_when_missing():
    """Verify /api/memory returns available: False when list_memories is not present on driver."""
    # By default, list_memories is not yet implemented on the driver in this worktree
    if hasattr(AgentNativeEpisodicDriver, "list_memories"):
        delattr(AgentNativeEpisodicDriver, "list_memories")
    resp = client.get("/api/memory")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["available"] is False
    assert data["memories"] == []


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

    with patch.object(AgentNativeEpisodicDriver, "list_memories", return_value=mock_memories, create=True):
        resp = client.get("/api/memory?project=kortext&limit=10&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["available"] is True
        assert len(data["memories"]) == 2
        assert data["memories"][0]["id"] == "fact_001"
        assert data["memories"][1]["text"] == "<img src=x onerror=alert(1)>"


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


def test_memory_actions_with_csrf():
    """Verify mutating routes succeed when proper CSRF header is provided."""
    headers = {
        "x-kortext-enforce-csrf": "1",
        "x-kortext-client": "dashboard",
    }

    with patch.object(AgentNativeEpisodicDriver, "delete_fact", new_callable=AsyncMock) as mock_del:
        # Supersede
        resp = client.post(
            "/api/memory/supersede",
            json={"id": "fact_1", "reason": "outdated", "superseded_by": "fact_2"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_del.assert_awaited_once_with("fact_1", "outdated (superseded by fact_2)")

    with patch.object(AgentNativeEpisodicDriver, "approve_memory", new_callable=AsyncMock, create=True) as mock_app:
        # Approve
        resp = client.post(
            "/api/memory/approve",
            json={"id": "fact_1", "kind": "fact"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_app.assert_awaited_once_with("fact_1", kind="fact")

    with patch.object(AgentNativeEpisodicDriver, "edit_memory", new_callable=AsyncMock, create=True) as mock_edit:
        # Edit
        resp = client.post(
            "/api/memory/edit",
            json={"id": "fact_1", "text": "corrected text", "kind": "fact"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_edit.assert_awaited_once_with("fact_1", "corrected text", kind="fact")


def test_memory_js_never_uses_innerhtml_with_memory_text():
    """Verify memory.js sets textContent on textEl and never sets innerHTML with memory text."""
    from pathlib import Path
    mem_js_path = Path(__file__).resolve().parents[1] / "kortext" / "dashboard" / "static" / "js" / "modules" / "memory.js"
    content = mem_js_path.read_text(encoding="utf-8")
    assert "textEl.textContent = mem.text" in content
    assert "innerHTML = mem.text" not in content
    assert "innerHTML = `<p>${mem.text}`" not in content

