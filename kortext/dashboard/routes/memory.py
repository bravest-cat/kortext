"""
Memory inbox routes: inspect, approve, edit, and supersede episodic memories and facts.
Complies with Invariant 3 (Substrate Resilience) and FastMCP security checks.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
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
    List memories via AgentNativeEpisodicDriver.list_memories contract.
    Returns guarded fallback {"available": False, "memories": []} when list_memories is missing.
    """
    driver = AgentNativeEpisodicDriver()
    if not hasattr(driver, "list_memories"):
        return {
            "status": "ok",
            "available": False,
            "memories": [],
            "total": 0,
        }

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
        if hasattr(driver, "approve_memory"):
            if asyncio.iscoroutinefunction(driver.approve_memory):
                await driver.approve_memory(mem_id, kind=kind)
            else:
                await asyncio.to_thread(driver.approve_memory, mem_id, kind=kind)
            return {"status": "ok", "message": f"Memory '{mem_id}' approved as user_approved", "id": mem_id}

        # FalkorDB direct update fallback
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
    Subject to CSRF and token verification middleware.
    """
    driver = AgentNativeEpisodicDriver()
    mem_id = payload.id.strip()
    new_text = payload.text
    kind = (payload.kind or "fact").strip().lower()

    if not mem_id:
        return {"status": "error", "message": "Memory id is required"}

    try:
        if hasattr(driver, "edit_memory"):
            if asyncio.iscoroutinefunction(driver.edit_memory):
                await driver.edit_memory(mem_id, new_text, kind=kind)
            else:
                await asyncio.to_thread(driver.edit_memory, mem_id, new_text, kind=kind)
            return {"status": "ok", "message": f"Memory '{mem_id}' text updated", "id": mem_id}

        client = get_shared_falkordb_client(host=FALKORDB_HOST, port=FALKORDB_PORT)
        now = datetime.now(timezone.utc).isoformat()
        if kind == "episode":
            client.query(
                FALKORDB_DB,
                "MATCH (e:Episodic) WHERE e.id = $id OR e.name = $id "
                "SET e.content = $text, e.updated_at = $now",
                {"id": mem_id, "text": new_text, "now": now},
            )
        else:
            client.query(
                FALKORDB_DB,
                "MATCH ()-[r:RELATES_TO]->() WHERE r.id = $id OR r.fact_id = $id "
                "SET r.fact = $text, r.updated_at = $now",
                {"id": mem_id, "text": new_text, "now": now},
            )
        return {"status": "ok", "message": f"Memory '{mem_id}' text updated", "id": mem_id}
    except Exception as exc:
        logger.error("Failed to edit memory %s: %s", mem_id, exc)
        return {"status": "error", "message": str(exc)}


@router.post("/api/memory/supersede")
async def supersede_memory(payload: MemorySupersedeRequest, request: Request):
    """
    Supersedes a memory, keeping history via driver.delete_fact (same as delete_or_supersede_fact).
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
        await driver.delete_fact(mem_id, full_reason)
        return {"status": "ok", "message": f"Memory '{mem_id}' marked as superseded", "id": mem_id}
    except Exception as exc:
        logger.error("Failed to supersede memory %s: %s", mem_id, exc)
        return {"status": "error", "message": str(exc)}
