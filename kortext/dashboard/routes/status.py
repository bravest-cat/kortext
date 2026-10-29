"""
Status, health checks, substrate telemetry, TCP/HTTP checkers, and collection point stats.
Invariant 3 compliance: Read-only inspection of embedded Qdrant files; no secondary QdrantClient(path=...).
"""
import logging
logger = logging.getLogger(__name__)

import asyncio
import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import psutil
from fastapi import APIRouter

from kortext.falkordb_client import get_shared_falkordb_client
from kortext.retrieval.discovery import discover_vector_collections

router = APIRouter(tags=["status"])

FALKORDB_HOST = os.getenv("FALKORDB_HOST", "127.0.0.1")
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
FALKORDB_DB = os.getenv("KORTEXT_DATABASE", os.getenv("FALKORDB_DATABASE", "kortext"))


def _check_tcp(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _check_http(url: str, timeout: float = 2.0) -> tuple[bool, Optional[dict]]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            if resp.status_code < 400:
                try:
                    return True, resp.json()
                except Exception:
                    return True, None
            return False, None
    except Exception:
        return False, None


@router.get("/api/status")
async def get_status():
    """Health check across Qdrant, FalkorDB, browser UI, and oMLX."""
    from kortext.health import audit_kortext_health

    return await audit_kortext_health(include_browser_ui=True, include_invariants=False)


@router.get("/api/collections")
async def list_collections():
    """List Qdrant collections with point counts and configs."""
    try:
        collections = await asyncio.to_thread(discover_vector_collections)
        return {"status": "ok", "collections": collections}
    except Exception as e:
        return {"status": "error", "message": str(e), "collections": []}



@router.get("/api/cache/stats")
async def get_cache_stats():
    """Returns chunk embedding cache and file ingestion cache metrics."""
    from kortext.cache import ChunkEmbeddingCache, IngestCache
    try:
        chunk_cache = ChunkEmbeddingCache()
        chunk_stats = chunk_cache.get_stats()
        ingest_cache = IngestCache()
        default_col = os.getenv("KORTEXT_COLLECTION", os.getenv("QDRANT_COLLECTION", "kortext_codebase"))
        all_files = ingest_cache.get_all_cached_files(default_col)
        return {
            "status": "ok",
            "chunk_cache": chunk_stats,
            "total_indexed_files": len(all_files),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/watcher/status")
async def get_watcher_endpoint():
    """Returns real-time watcher daemon health and queue statistics."""
    from kortext.watcher import get_watcher_status
    try:
        st = get_watcher_status()
        return {"status": "ok", "watcher": st}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/quarantine")
async def get_quarantine():
    """Lists quarantined adversarial payloads blocked by CausalSanitizer."""
    from kortext.trust.sanitizer import CausalSanitizer
    try:
        sanitizer = CausalSanitizer()
        q_files = sorted(sanitizer.quarantine_dir.glob("*.json"), reverse=True)
        items = []
        for f in q_files[:50]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                items.append({
                    "filename": f.name,
                    "timestamp": data.get("timestamp"),
                    "reason": data.get("reason"),
                    "subject": data.get("subject"),
                    "relation": data.get("relation"),
                    "fact": data.get("fact"),
                })
            except Exception:
                continue
        return {"status": "ok", "quarantine_count": len(q_files), "items": items}
    except Exception as e:
        return {"status": "error", "message": str(e), "quarantine_count": 0, "items": []}


@router.delete("/api/quarantine")
async def clear_quarantine():
    from kortext.trust.sanitizer import CausalSanitizer
    try:
        sanitizer = CausalSanitizer()
        q_files = list(sanitizer.quarantine_dir.glob("*.json"))
        count = 0
        for f in q_files:
            try:
                f.unlink(missing_ok=True)
                count += 1
            except Exception:
                logger.debug("Suppressed non-fatal exception")
        return {"status": "ok", "cleared_count": count}
    except Exception as e:
        return {"status": "error", "message": str(e), "cleared_count": 0}


@router.get("/api/invariants/grounded")
async def get_grounded_invariants():
    """Returns AST-grounded invariants and their live SHA-256 AST body hash drift status."""
    try:
        from kortext.invariants import InvariantGroundingEngine, grounded_read_path

        engine = InvariantGroundingEngine(host=FALKORDB_HOST, port=FALKORDB_PORT, db_name=FALKORDB_DB)
        audit = await asyncio.to_thread(engine.audit_invariant_drift, False)
        grounded_file = grounded_read_path()
        records: List[Dict[str, Any]] = []
        if grounded_file.is_file():
            try:
                raw = json.loads(grounded_file.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    records = raw
            except Exception:
                records = []
        return {
            "status": "ok",
            "drift_audit": audit,
            "grounded_invariants": records,
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "drift_audit": {}, "grounded_invariants": []}


@router.get("/api/context/telemetry")
async def get_context_telemetry():
    """
    Returns Live Session Context & Token Burn Telemetry:
    - Real-time context window usage vs 25% compaction ceiling (~200k-250k tokens on 1M window)
    - Token savings estimator (seek_symbol vs raw grep/cat)
    - Active pre-compaction handoff batons (~/.kortext/handoffs/*.json)
    """
    from kortext._home import get_kortext_home
    from kortext.hooks.transcript_tokens import resolve_session_token_state

    try:
        home = get_kortext_home()
        tok = resolve_session_token_state(kortext_home=home)
        tok_dict = tok.to_dict()

        seek_calls = 0
        telemetry_jsonl = home / "telemetry" / "tools.jsonl"
        if telemetry_jsonl.is_file():
            try:
                raw_lines = telemetry_jsonl.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in raw_lines[-500:]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        t_name = str(d.get("tool_name") or "").lower()
                        if "seek_symbol" in t_name or "trace_symbol_impact" in t_name:
                            seek_calls += 1
                    except Exception:
                        continue
            except Exception:
                pass

        spool_dir = home / "spool" / "telemetry"
        if spool_dir.is_dir():
            for f in list(spool_dir.glob("*.json"))[-100:]:
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    t_name = str(d.get("tool_name") or "").lower()
                    if "seek_symbol" in t_name or "trace_symbol_impact" in t_name:
                        seek_calls += 1
                except Exception:
                    continue

        handoffs_dir = home / "handoffs"
        batons: List[Dict[str, Any]] = []
        if handoffs_dir.is_dir():
            for hf in sorted(handoffs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
                try:
                    b_data = json.loads(hf.read_text(encoding="utf-8"))
                    batons.append({
                        "filename": hf.name,
                        "task_id": b_data.get("task_id") or hf.stem,
                        "target_agent": b_data.get("target_agent", "orchestrator"),
                        "current_scope": str(b_data.get("current_scope") or b_data.get("summary") or "")[:160],
                        "completed_at": b_data.get("completed_at") or b_data.get("created_at"),
                    })
                except Exception:
                    continue

        estimated_tokens_saved = seek_calls * 1850
        return {
            "status": "ok",
            "context_window": tok_dict,
            "compaction_threshold_tokens": 200_000,
            "soft_budget_tokens": 150_000,
            "seek_symbol_calls": seek_calls,
            "estimated_tokens_saved": estimated_tokens_saved,
            "handoff_batons": batons,
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "context_window": {}, "handoff_batons": []}


@router.get("/api/telemetry")
async def get_telemetry():
    """Returns real-time system telemetry and performance benchmarks."""
    lat_ms = 0.0
    falkor_ok = False
    try:
        client = get_shared_falkordb_client(host=FALKORDB_HOST, port=FALKORDB_PORT)
        t0 = time.perf_counter()
        client.query(FALKORDB_DB, "RETURN 1")
        lat_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        falkor_ok = True
    except Exception:
        logger.debug("Suppressed non-fatal exception")

    from kortext._home import get_kortext_home
    kortext_home = get_kortext_home()
    q_dir = kortext_home / "quarantine"
    q_count = len(list(q_dir.glob("*.json"))) if q_dir.exists() else 0

    rss_mb = 0.0
    try:
        proc = psutil.Process(os.getpid())
        rss_mb = round(proc.memory_info().rss / (1024 * 1024), 1)
    except Exception:
        logger.debug("Suppressed non-fatal exception")

    embed_url = os.getenv("EMBED_BASE_URL")
    embedder_desc = f"Remote API ({embed_url})" if embed_url else "FastEmbed (Local 384d)"

    # Live Tier 0 AST B-Tree Symbol Index Introspection
    sym_count = 0
    file_count = 0
    sub_count = 0
    ep_count = 0
    rel_count = 0
    tier0_status = "Active" if falkor_ok else "Offline"
    tier2_status = "Active" if falkor_ok else "Offline"
    tier4_status = "Active" if falkor_ok else "Offline"

    if falkor_ok:
        try:
            client = get_shared_falkordb_client(host=FALKORDB_HOST, port=FALKORDB_PORT)
            # Symbols & Files
            s_res = client.query("kortext_symbols", "MATCH (s:Symbol) RETURN count(s)", read_only=True)
            if s_res and len(s_res) > 1 and s_res[1]:
                sym_count = int(s_res[1][0][0])
            f_res = client.query("kortext_symbols", "MATCH (f:File) RETURN count(f)", read_only=True)
            if f_res and len(f_res) > 1 and f_res[1]:
                file_count = int(f_res[1][0][0])
            # Subsystems
            sub_res = client.query("kortext_symbols", "MATCH (sub:Subsystem) RETURN count(sub)", read_only=True)
            if sub_res and len(sub_res) > 1 and sub_res[1]:
                sub_count = int(sub_res[1][0][0])
            # Episodic outcomes and facts
            e_res = client.query(FALKORDB_DB, "MATCH (e:Episodic) RETURN count(e)", read_only=True)
            if e_res and len(e_res) > 1 and e_res[1]:
                ep_count = int(e_res[1][0][0])
            rel_res = client.query(FALKORDB_DB, "MATCH ()-[r:RELATES_TO]->() RETURN count(r)", read_only=True)
            if rel_res and len(rel_res) > 1 and rel_res[1]:
                rel_count = int(rel_res[1][0][0])
        except Exception:
            tier0_status = "Degraded"
            tier2_status = "Degraded"

    # Live Tier 1 Qdrant Vector Points Introspection
    collections = []
    total_vectors = 0
    qdrant_online = False
    try:
        from kortext._qdrant import is_qdrant_online
        qdrant_online = is_qdrant_online()
        collections = discover_vector_collections()
        total_vectors = sum(c.get("points_count", 0) for c in collections)
    except Exception:
        logger.debug("Suppressed non-fatal exception")
    tier1_status = "Active" if qdrant_online else "Offline"
    tier3_status = "Active" if (falkor_ok and qdrant_online) else "Degraded"

    # Dynamic Next-Gen Hooks Introspection
    active_hooks: List[Dict[str, str]] = []
    try:
        from kortext.hooks.pipelines.pre_tool import PreToolPipeline
        from kortext.hooks.pipelines.post_tool import PostToolPipeline
        from kortext.hooks.pipelines.pre_invoke import PreInvokePipeline
        from kortext.hooks.pipelines.post_invoke import PostInvokePipeline
        from kortext.hooks.pipelines.stop import StopPipeline

        pipelines = [
            ("pre_tool", PreToolPipeline()),
            ("post_tool", PostToolPipeline()),
            ("pre_invoke", PreInvokePipeline()),
            ("post_invoke", PostInvokePipeline()),
            ("stop", StopPipeline()),
        ]
        for stage, p in pipelines:
            items = getattr(p, "guards", getattr(p, "interceptors", ()))
            for item in items:
                name = type(item).__name__
                doc = (item.__doc__ or "").strip().split("\n")[0] if hasattr(item, "__doc__") and item.__doc__ else ""
                active_hooks.append({
                    "name": name,
                    "pipeline": stage,
                    "description": doc,
                })
    except Exception:
        logger.debug("Suppressed non-fatal exception")

    # Dynamic Invariants Count
    active_inv_count = 0
    try:
        journal_root = Path(__file__).resolve().parents[3] / "journal"
        inv_file = journal_root / "invariants.md"
        if inv_file.is_file():
            inv_text = inv_file.read_text(encoding="utf-8")
            active_inv_count += len(re.findall(r"###\s+Invariant\s+\[", inv_text))
        from kortext.invariants import grounded_read_path

        grounded_file = grounded_read_path()
        if grounded_file.is_file():
            try:
                g_data = json.loads(grounded_file.read_text(encoding="utf-8"))
                if isinstance(g_data, list):
                    active_inv_count += len(g_data)
            except Exception:
                logger.debug("Suppressed non-fatal exception")
    except Exception as exc:
        logger.warning("Invariant count unavailable: %s", exc)
        active_inv_count = None

    # Version
    version_str = "2.0.0"
    try:
        import importlib.metadata
        version_str = importlib.metadata.version("kortext")
    except Exception:
        logger.debug("Suppressed non-fatal exception")

    # Live context window telemetry
    context_telemetry: Dict[str, Any] = {}
    try:
        from kortext.hooks.transcript_tokens import resolve_session_token_state

        tok_state = resolve_session_token_state(kortext_home=kortext_home)
        context_telemetry = tok_state.to_dict()
    except Exception:
        logger.debug("Context telemetry resolution skipped")

    from kortext.health import reindex_spool_status

    reindex = reindex_spool_status()

    return {
        "status": "ok",
        "telemetry": {
            "version": version_str,
            "substrate_ram_rss_mb": rss_mb,
            "falkordb_latency_ms": lat_ms,
            "falkordb_healthy": falkor_ok,
            "quarantine_count": q_count,
            "reindex": reindex,
            "context_window": context_telemetry,
            "active_invariants": active_inv_count,
            "memory_tiers": {
                "tier_0": {
                    "name": "Tree-sitter AST B-Tree",
                    "status": tier0_status,
                    "metrics": f"{sym_count:,} symbols, {file_count:,} files",
                    "type": "Deterministic Tree-sitter AST Index",
                },
                "tier_1": {
                    "name": "Qdrant Vectors",
                    "status": tier1_status,
                    "metrics": f"{total_vectors:,} vectors ({len(collections)} collections)",
                    "embedder": embedder_desc,
                },
                "tier_2": {
                    "name": "FalkorDB Episodic Graph",
                    "status": tier2_status,
                    "metrics": f"{ep_count:,} episodes, {rel_count:,} facts",
                    "type": "Temporal & Causal Knowledge Graph",
                },
                "tier_3": {
                    "name": "Federated Hybrid Engine",
                    "status": tier3_status,
                    "metrics": "Weighted RRF over AST, BM25, dense and facts",
                    "type": "Dense + Sparse + Graph Federation",
                },
                "tier_4": {
                    "name": "Cognitive Dream Mode",
                    "status": tier4_status,
                    "metrics": f"{sub_count} Louvain subsystems",
                    "type": "Pruning & Community Clustering",
                },
            },
            "nextgen_hooks": active_hooks,
        },
    }
