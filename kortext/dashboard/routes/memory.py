"""
Memory inbox routes: inspect, approve, edit, and supersede episodic memories and facts.
Complies with Invariant 3 (Substrate Resilience) and FastMCP security checks.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kortext.episodic import AgentNativeEpisodicDriver
from kortext.falkordb_client import (
    FALKORDB_HOST,
    FALKORDB_PORT,
    get_shared_falkordb_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["memory"])

FALKORDB_DB = os.getenv("KORTEXT_DATABASE", os.getenv("FALKORDB_DATABASE", "kortext"))

_LIMIT_MAX = 1000
_OFFSET_MIN = 0


class MemoryApproveRequest(BaseModel):
    id: str
    kind: Optional[str] = "fact"


class MemoryEditRequest(BaseModel):
    id: str
    text: str
    kind: Optional[str] = "fact"


class MemorySupersedeRequest(BaseModel):
    id: str
    reason: str
    superseded_by: Optional[str] = None


@router.get("/api/memory")
async def get_memories(
    project: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
):
    """
    List memories via AgentNativeEpisodicDriver.list_memories.
    limit is capped at 1000; offset is floored at 0.
    """
    driver = AgentNativeEpisodicDriver()
    limit = min(max(1, limit), _LIMIT_MAX)
    offset = max(_OFFSET_MIN, offset)

    try:
        norm_project = None if not project or project.strip().lower() in ("all", "") else project.strip()
        if asyncio.iscoroutinefunction(driver.list_memories):
            memories = await driver.list_memories(project=norm_project, limit=limit, offset=offset)
        else:
            memories = await asyncio.to_thread(driver.list_memories, project=norm_project, limit=limit, offset=offset)

        if not isinstance(memories, list):
            memories = []

        return {
            "status": "ok",
            "available": True,
            "memories": memories,
            "total": len(memories),
        }
    except Exception as exc:
        logger.warning("AgentNativeEpisodicDriver.list_memories failed: %s", exc)
        return {
            "status": "error",
            "message": str(exc),
            "available": False,
            "memories": [],
            "total": 0,
        }


@router.post("/api/memory/approve")
async def approve_memory(payload: MemoryApproveRequest, request: Request):
    """
    Approves a memory, raising trust level to 'user_approved' with trust_score 1.0.
    Subject to CSRF and token verification middleware.
    """
    driver = AgentNativeEpisodicDriver()
    mem_id = payload.id.strip()
    kind = (payload.kind or "fact").strip().lower()

    if not mem_id:
        return {"status": "error", "message": "Memory id is required"}

    try:
        # FalkorDB direct update
        client = get_shared_falkordb_client(host=FALKORDB_HOST, port=FALKORDB_PORT)
        now = datetime.now(timezone.utc).isoformat()
        if kind == "episode":
            client.query(
                FALKORDB_DB,
                "MATCH (e:Episodic) WHERE e.id = $id OR e.name = $id "
                "SET e.trust_level = 'user_approved', e.trust_score = 1.0, e.approved_at = $now",
                {"id": mem_id, "now": now},
            )
        else:
            client.query(
                FALKORDB_DB,
                "MATCH ()-[r:RELATES_TO]->() WHERE r.id = $id OR r.fact_id = $id "
                "SET r.trust_level = 'user_approved', r.trust_score = 1.0, r.approved_at = $now",
                {"id": mem_id, "now": now},
            )
        return {"status": "ok", "message": f"Memory '{mem_id}' approved as user_approved", "id": mem_id}
    except Exception as exc:
        logger.error("Failed to approve memory %s: %s", mem_id, exc)
        return {"status": "error", "message": str(exc)}


@router.post("/api/memory/edit")
async def edit_memory(payload: MemoryEditRequest, request: Request):
    """
    Updates the text content of a memory.
    For facts: records a new fact via record_fact (user_approved) and supersedes the old one.
    For episodes: updates content and recomputes the stored embedding.
    Subject to CSRF and token verification middleware.
    """
    driver = AgentNativeEpisodicDriver()
    mem_id = payload.id.strip()
    new_text = payload.text
    kind = (payload.kind or "fact").strip().lower()

    if not mem_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Memory id is required"})
    if not new_text or not new_text.strip():
        return JSONResponse(status_code=400, content={"status": "error", "message": "Text cannot be empty"})

    try:
        now = datetime.now(timezone.utc).isoformat()
        if kind == "episode":
            # Update episode content and recompute embedding the same way record_outcome does
            client = get_shared_falkordb_client(host=FALKORDB_HOST, port=FALKORDB_PORT)
            # Compute embedding for new text
            new_emb = None
            try:
                from kortext.episodic.driver import _dense_embedder
                embedder = _dense_embedder()
                if embedder is not None:
                    vecs = embedder.embed_documents([new_text])
                    if vecs:
                        new_emb = [float(x) for x in vecs[0]]
            except Exception as emb_exc:
                logger.debug("Could not compute embedding for edited episode: %s", emb_exc)

            if new_emb is not None:
                client.query(
                    FALKORDB_DB,
                    "MATCH (e:Episodic) WHERE e.id = $id OR e.name = $id "
                    "SET e.content = $text, e.updated_at = $now, e.emb = $emb",
                    {"id": mem_id, "text": new_text, "now": now, "emb": new_emb},
                )
            else:
                client.query(
                    FALKORDB_DB,
                    "MATCH (e:Episodic) WHERE e.id = $id OR e.name = $id "
                    "SET e.content = $text, e.updated_at = $now, e.emb = null",
                    {"id": mem_id, "text": new_text, "now": now},
                )
            return {"status": "ok", "message": f"Memory '{mem_id}' text updated", "id": mem_id}

        else:
            # For facts: find the existing fact to get subject/object, record a new fact, supersede old
            client = get_shared_falkordb_client(host=FALKORDB_HOST, port=FALKORDB_PORT)
            res = client.query(
                FALKORDB_DB,
                "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) "
                "WHERE r.id = $id OR r.fact_id = $id "
                "RETURN s.name, r.relation, t.name, coalesce(r.project, 'kortext'), r.source_session "
                "LIMIT 1",
                {"id": mem_id},
            )
            if not res or len(res) < 2 or not res[1]:
                return JSONResponse(
                    status_code=404,
                    content={"status": "error", "message": f"Fact '{mem_id}' not found"},
                )
            row = res[1][0]
            subject = str(row[0]) if row[0] else ""
            relation = str(row[1]) if row[1] else "RELATES_TO"
            object_name = str(row[2]) if row[2] else ""
            project = str(row[3]) if row[3] else "kortext"
            source_session = str(row[4]) if len(row) > 4 and row[4] else None

            new_fact_id = driver.record_fact(
                subject=subject,
                relation=relation,
                fact=new_text,
                object_name=object_name,
                project=project,
                source_session=source_session,
                trust_level="user_approved",
                supersedes=mem_id,
            )
            return {"status": "ok", "message": f"Memory '{mem_id}' text updated", "id": new_fact_id}
    except Exception as exc:
        logger.error("Failed to edit memory %s: %s", mem_id, exc)
        return {"status": "error", "message": str(exc)}


@router.post("/api/memory/supersede")
async def supersede_memory(payload: MemorySupersedeRequest, request: Request):
    """
    Supersedes a memory, keeping history via driver.delete_fact with exact=True.
    Subject to CSRF and token verification middleware.
    """
    driver = AgentNativeEpisodicDriver()
    mem_id = payload.id.strip()
    reason = payload.reason.strip()
    superseded_by = (payload.superseded_by or "").strip() or None

    if not mem_id:
        return {"status": "error", "message": "Memory id is required"}
    if not reason:
        return {"status": "error", "message": "Reason is required to supersede memory"}

    full_reason = f"{reason} (superseded by {superseded_by})" if superseded_by else reason
    try:
        await driver.delete_fact(mem_id, full_reason, exact=True)
        return {"status": "ok", "message": f"Memory '{mem_id}' marked as superseded", "id": mem_id}
    except Exception as exc:
        logger.error("Failed to supersede memory %s: %s", mem_id, exc)
        return {"status": "error", "message": str(exc)}
