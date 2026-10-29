"""
Action triggers: dream consolidation, collection creation, cache clearing,
SCIP/symbol indexing, and Task DAG management.
"""
import logging
logger = logging.getLogger(__name__)

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Request

router = APIRouter(tags=["actions"])

from kortext._home import get_kortext_home

KORTEXT_ROOT = Path(__file__).resolve().parents[3]


@router.post("/api/dream")
async def trigger_dream(request: Request):
    """Triggers on-demand Kortext Dream Mode consolidation cycle."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    project = body.get("project", "kortext")
    purge_stale = bool(body.get("purge_stale", False))

    from kortext.dream import KortextDreamEngine

    engine = KortextDreamEngine()
    try:
        if hasattr(engine, "run_dream_cycle"):
            report = await engine.run_dream_cycle(project=project, purge_stale=purge_stale)
        else:
            report = await engine.dream(project=project, purge_stale=purge_stale)
        return {"status": "ok", "report": report}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _is_sole_custodian_running() -> bool:
    """True when another process (normally the kortext daemon) holds the embedded Qdrant lock."""
    from kortext._qdrant import is_embedded_qdrant_locked

    return is_embedded_qdrant_locked()


async def _check_remote_qdrant(url: str = "http://127.0.0.1:6333") -> bool:
    """Checks if a remote/local Qdrant HTTP service is reachable."""
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            resp = await client.get(f"{url.rstrip('/')}/collections")
            return resp.status_code == 200
    except Exception:
        return False


def _get_default_collection_schema(embed_dim: int = 4096) -> Dict[str, Any]:
    """Generates the embedded Qdrant schema matching QdrantClient create_collection format."""
    return {
        "vectors": {
            "text-dense": {
                "size": embed_dim,
                "distance": "Cosine",
                "hnsw_config": None,
                "quantization_config": None,
                "on_disk": True,
                "memory": None,
                "datatype": None,
                "multivector_config": None,
            }
        },
        "shard_number": None,
        "sharding_method": None,
        "replication_factor": None,
        "write_consistency_factor": None,
        "on_disk_payload": None,
        "payload": None,
        "hnsw_config": None,
        "wal_config": None,
        "optimizers_config": None,
        "quantization_config": None,
        "sparse_vectors": {
            "text-sparse-new": {
                "index": {
                    "full_scan_threshold": None,
                    "on_disk": True,
                    "memory": None,
                    "datatype": None,
                },
                "modifier": None,
            }
        },
        "strict_mode_config": None,
        "metadata": None,
    }


def _init_collection_sqlite(col_dir: Path) -> None:
    """Initializes Qdrant Local collection storage.sqlite so embedded Qdrant loads cleanly."""
    col_dir.mkdir(parents=True, exist_ok=True)
    sql_path = col_dir / "storage.sqlite"
    with sqlite3.connect(str(sql_path), timeout=5.0) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS points (id TEXT PRIMARY KEY, point BLOB)")
        conn.commit()


@router.post("/api/collections")
async def create_collection(request: Request):
    """Creates a new Qdrant collection with named hybrid dense + sparse BM25 vectors."""
    try:
        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            return {"status": "error", "message": "Collection name is required"}
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            return {"status": "error", "message": "Collection name may only use letters, digits, '_' and '-'"}

        raw_dim = body.get("dimension") or os.getenv("EMBED_DIMENSION")
        if raw_dim:
            embed_dim = int(raw_dim)
        else:
            from kortext._env import get_embed_dimension

            embed_dim = get_embed_dimension()

        http_payload = {
            "vectors": {
                "text-dense": {
                    "size": embed_dim,
                    "distance": "Cosine",
                }
            },
            "sparse_vectors": {
                "text-sparse-new": {},
            },
        }

        # 1. If explicit QDRANT_URL is set, provision via remote HTTP endpoint
        qdrant_url = os.getenv("QDRANT_URL")
        if qdrant_url:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.put(f"{qdrant_url.rstrip('/')}/collections/{name}", json=http_payload)
                if resp.status_code in [200, 201]:
                    return {"status": "ok", "message": f"Collection '{name}' created successfully."}
                return {"status": "error", "message": resp.text}

        # 2. Zero-Docker mode (when Qdrant HTTP is unavailable)
        # Invariant 3 Mandate: Do NOT instantiate QdrantClient(path=...) in secondary dashboard process
        # if the store is open or sole custodian daemon is running.
        kortext_home = get_kortext_home()
        qpath = kortext_home / "qdrant"
        meta_file = qpath / "meta.json"
        custodian_running = _is_sole_custodian_running()

        if meta_file.is_file() or custodian_running:
            try:
                qpath.mkdir(parents=True, exist_ok=True)
                if meta_file.is_file():
                    try:
                        meta_data = json.loads(meta_file.read_text(encoding="utf-8"))
                    except Exception:
                        meta_data = {"collections": {}, "aliases": {}}
                else:
                    meta_data = {"collections": {}, "aliases": {}}

                collections = meta_data.setdefault("collections", {})
                if name not in collections:
                    collections[name] = _get_default_collection_schema(embed_dim=embed_dim)
                    col_dir = qpath / "collection" / name
                    _init_collection_sqlite(col_dir)

                    # Atomic write to meta.json
                    tmp_file = meta_file.with_suffix(f".tmp.{os.getpid()}")
                    tmp_file.write_text(json.dumps(meta_data, indent=2), encoding="utf-8")
                    tmp_file.replace(meta_file)

                return {"status": "ok", "message": f"Collection '{name}' created successfully in native embedded Qdrant."}
            except Exception as e:
                return {"status": "error", "message": f"Failed to safely update meta.json: {e}"}

        # 4. Fallback to EmbeddedQdrantEngine guarded with exception handling (no daemon is holding lock)
        try:
            from kortext.retrieval.qdrant_engine import EmbeddedQdrantEngine

            engine = EmbeddedQdrantEngine()
            engine.ensure_collection(name, embed_dim=embed_dim)
            return {"status": "ok", "message": f"Collection '{name}' created successfully in native embedded Qdrant."}
        except Exception as e:
            err_msg = str(e)
            if "lock" in err_msg.lower() or "operationalerror" in err_msg.lower() or "resource temporarily unavailable" in err_msg.lower():
                return {
                    "status": "error",
                    "message": f"Embedded Qdrant store is locked by sole custodian daemon ({err_msg}). Provisioning aborted to preserve Invariant 3.",
                }
            return {"status": "error", "message": f"Embedded Qdrant provisioning failed: {err_msg}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/api/cache/clear")
async def clear_cache():
    """Clears the chunk embedding SQLite cache."""
    from kortext.cache import ChunkEmbeddingCache

    try:
        chunk_cache = ChunkEmbeddingCache()
        purged_count = await asyncio.to_thread(chunk_cache.clear)
        return {"status": "ok", "purged_chunks": purged_count}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/api/scip/auto")
@router.post("/api/symbols/index")
async def run_scip_auto(request: Request = None):  # type: ignore[assignment]
    """Triggers automated SCIP indexing and ingestion after path validation."""
    from fastapi.responses import JSONResponse
    from kortext.server_helpers import _validate_index_path

    body: Dict[str, Any] = {}
    if request is not None:
        try:
            body = await request.json()
        except Exception:
            body = {}
    raw_ws = str(body.get("workspace_root") or KORTEXT_ROOT)
    valid, err_msg, validated_root = _validate_index_path(raw_ws)
    if not valid or validated_root is None:
        return JSONResponse(status_code=400, content={"status": "error", "message": err_msg})
    ws_root = validated_root
    graph_name = body.get("graph_name")
    scip_path = ws_root / "index.scip"
    scip_py = shutil.which("scip-python")
    if scip_path.exists():
        from kortext.symbols.scip import SCIPImporter

        importer = SCIPImporter(graph_name=graph_name) if graph_name else SCIPImporter()
        stats = await asyncio.to_thread(importer.ingest_index, str(scip_path))
        return {"status": "ok", "message": "Ingested existing index.scip", "stats": stats}
    elif scip_py:
        try:
            cmd = [scip_py, "index", "--project-root", str(ws_root), "--output", str(scip_path)]
            p = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=60)
            if p.returncode == 0 and scip_path.exists():
                from kortext.symbols.scip import SCIPImporter

                importer = SCIPImporter(graph_name=graph_name) if graph_name else SCIPImporter()
                stats = await asyncio.to_thread(importer.ingest_index, str(scip_path))
                return {"status": "ok", "message": "Generated and ingested index.scip", "stats": stats}
            return {"status": "error", "message": f"scip-python failed: {p.stderr}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    else:
        if body.get("fallback_ast", True):
            from kortext.symbols.scip import auto_index_and_ingest

            auto_res = await asyncio.to_thread(auto_index_and_ingest, str(ws_root), graph_name=graph_name)
            return {
                "status": "not_installed",
                "message": "Neither index.scip nor scip-python CLI tool was found. Run `npm install -g @sourcegraph/scip-python` or generate index.scip.",
                "fallback": auto_res,
            }
        return {
            "status": "not_installed",
            "message": "Neither index.scip nor scip-python CLI tool was found. Run `npm install -g @sourcegraph/scip-python` or generate index.scip.",
        }


def _fetch_tasks_sync(include_graph: bool, project_id: Optional[str]) -> Dict[str, Any]:
    from kortext.tasks.tracker import TaskDAGTracker

    tracker = TaskDAGTracker()
    data = (
        tracker.get_graph_data(project_id=project_id)
        if include_graph
        else {"tasks": tracker.get_all_tasks(project_id=project_id), "nodes": [], "edges": []}
    )
    all_tasks = tracker.get_all_tasks(project_id=project_id)
    ready = tracker.get_ready_tasks(project_id=project_id)
    projects = list(set([t.get("project_id", "kortext") for t in all_tasks if t.get("project_id")]))
    if "all" not in projects:
        projects.insert(0, "all")
    if "kortext" not in projects:
        projects.append("kortext")

    return {
        "status": "ok",
        "tasks": all_tasks,
        "nodes": data.get("nodes", []),
        "edges": data.get("edges", []),
        "ready_tasks": ready,
        "projects": projects,
    }


@router.get("/api/tasks")
async def get_tasks(include_graph: bool = True, project_id: Optional[str] = None):
    """Returns Task DAG tasks and dependency graph formatted for Vis.js."""
    try:
        return await asyncio.to_thread(_fetch_tasks_sync, include_graph, project_id)
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "tasks": [],
            "nodes": [],
            "edges": [],
            "ready_tasks": [],
            "projects": ["all", "kortext"],
        }


def _manage_task_sync(body: Dict[str, Any]) -> Dict[str, Any]:
    from kortext.tasks.tracker import TaskDAGTracker

    action = body.get("action", "create")
    tracker = TaskDAGTracker()
    if action == "create":
        title = body.get("title")
        if not title:
            return {"status": "error", "message": "Task title is required"}
        tid = tracker.create_task(
            title=title,
            description=body.get("description", ""),
            complexity=int(body.get("complexity", 1)),
            assigned_to=body.get("assigned_to", "primary"),
            depends_on=body.get("depends_on") or body.get("dependencies"),
            project_id=body.get("project_id", "kortext"),
            task_id=body.get("task_id"),
        )
        return {"status": "ok", "task_id": tid}
    elif action in ("claim", "claim_ready"):
        claimed = tracker.claim_next_ready_task(
            worker_id=body.get("assigned_to", "kortext-task-runner"),
            project_id=body.get("project_id"),
        )
        return {"status": "ok", "claimed_task": claimed}
    elif action == "update_status":
        tid = body.get("task_id")
        st = body.get("status")
        if not tid or not st:
            return {"status": "error", "message": "task_id and status are required"}
        err = body.get("error")
        tracker.update_task_status(tid, status=st, error=err)
        ep_name = body.get("episode_name")
        if ep_name and st == "completed":
            tracker.link_task_to_outcome(tid, ep_name)
        return {"status": "ok", "task_id": tid, "updated_status": st}
    return {"status": "error", "message": f"Unknown action: {action}"}


@router.post("/api/tasks")
async def manage_task_endpoint(request: Request):
    """Creates a new Task, claims a ready task, or updates existing task state in FalkorDB."""
    try:
        body = await request.json()
        return await asyncio.to_thread(_manage_task_sync, body)
    except Exception as e:
        return {"status": "error", "message": str(e)}
