"""
Unit tests for consolidated substrate health checks in kortext.health.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from kortext.health import audit_kortext_health


@pytest.mark.anyio
async def test_audit_health_defaults():
    """Verify default parameters (include_browser_ui=False, include_invariants=True)."""
    with patch("kortext.invariants.InvariantGroundingEngine.audit_invariant_drift") as mock_audit:
        mock_audit.return_value = {
            "verified_count": 10,
            "drifted_count": 0,
            "orphaned_count": 0,
            "total_grounded": 10,
        }
        res = await audit_kortext_health(include_browser_ui=False, include_invariants=True)
        assert isinstance(res, dict)
        assert "qdrant" in res
        assert "falkordb" in res
        assert "inference_server" in res
        assert "kortext_dashboard" in res
        assert "invariants" in res
        assert "10 verified" in res["invariants"]
        assert "services" in res
        assert "timestamp" in res
        assert "recommendation" in res

        # FalkorDB Browser UI should not be in services when include_browser_ui=False
        service_names = [s["name"] for s in res["services"]]
        assert "FalkorDB Browser UI" not in service_names
        assert "Qdrant Vector Store" in service_names
        assert "FalkorDB Graph Engine" in service_names
        assert "oMLX Admin Dashboard" in service_names


@pytest.mark.anyio
async def test_audit_health_with_browser_ui():
    """Verify include_browser_ui=True probes FalkorDB browser UI."""
    with patch("kortext.invariants.InvariantGroundingEngine.audit_invariant_drift") as mock_audit:
        mock_audit.return_value = {
            "verified_count": 1,
            "drifted_count": 0,
            "orphaned_count": 0,
            "total_grounded": 1,
        }
        res = await audit_kortext_health(include_browser_ui=True, include_invariants=False)
        assert "invariants" not in res
        service_names = [s["name"] for s in res["services"]]
        assert "FalkorDB Browser UI" in service_names
        assert "Qdrant Vector Store" in service_names
        assert "FalkorDB Graph Engine" in service_names
        assert "oMLX Admin Dashboard" in service_names


@pytest.mark.anyio
async def test_audit_health_invariant_drift_recommendation():
    """Verify recommendation warning when architectural invariants drift."""
    with patch("kortext.invariants.InvariantGroundingEngine.audit_invariant_drift") as mock_audit:
        mock_audit.return_value = {
            "verified_count": 8,
            "drifted_count": 2,
            "orphaned_count": 0,
            "total_grounded": 10,
        }
        res = await audit_kortext_health(include_browser_ui=False, include_invariants=True)
        assert "2 drifted" in res["invariants"]
        assert "trigger_dream_mode" in res["recommendation"]


@pytest.mark.anyio
async def test_audit_health_remote_qdrant(monkeypatch):
    """Verify remote Qdrant healthz probe."""
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:9999")
    res = await audit_kortext_health(include_browser_ui=False, include_invariants=False)
    assert "qdrant" in res
    # Should report DOWN for dead port
    assert "DOWN" in res["qdrant"]





def test_kortext_incremental_sync_initialization():
    """Verify that instantiating KortextIncrementalSync succeeds."""
    from kortext.watcher import KortextIncrementalSync

    with patch("kortext.watcher.SymbolGraphManager"), \
         patch("kortext.watcher.ChunkEmbeddingCache"), \
         patch("kortext.watcher.IngestCache"):
        syncer = KortextIncrementalSync()
        assert isinstance(syncer, KortextIncrementalSync)


