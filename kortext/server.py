"""
Kortext FastMCP Server. One daemon per machine serves every agy window (see kortext.daemon).
Tools:

  1. search_knowledge_and_memory: federated multi-tier memory and codebase retrieval (read)
  2. record_system_outcome: persist directly to FalkorDB KG and journal (write)
  3. trigger_dream_mode: on-demand synaptic pruning and community detection (write)
  4. index_directory: on-demand Qdrant ingest (write)
  5. list_vector_collections: list Qdrant collections and point counts (read)
  6. check_kortext_health: service health dashboard (read)
  7. delete_or_supersede_fact: KG fact management (write)
  8. trace_symbol_impact: blast radius and impact analysis (read)
  9. seek_symbol: deterministic AST symbol locator (read)
  10. manage_tasks: Task DAG project management engine (read/write)
  11. memory_session_handoff: cross-agent session handoff baton (read/write)
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastmcp import Context, FastMCP

logger = logging.getLogger("kortext.server")

# Deterministically load .env from KORTEXT_ROOT with override=True so .env settings
# take precedence over stale environment blocks in mcp_config.json.
KORTEXT_ROOT = Path(__file__).resolve().parent.parent
KORTEXT_ENV_PATH = KORTEXT_ROOT / ".env"
if KORTEXT_ENV_PATH.is_file():
    load_dotenv(KORTEXT_ENV_PATH, override=True)
load_dotenv()

from kortext._env import get_configured_qdrant_url, is_qdrant_embedded_mode
from kortext._home import get_kortext_home
from kortext._qdrant import aclose_qdrant_clients
from kortext.episodic import VALID_CATEGORIES, AgentNativeEpisodicDriver
from kortext.handoff import HandoffManager
from kortext.ingest import ingest as _ingest
from kortext.retrieval import (
    RetrievalEngine,
    discover_vector_collections,
    resolve_workspace_root_for_collection,
)
from kortext.server_helpers import (
    FORBIDDEN_SYSTEM_PREFIXES,
    SENSITIVE_DIR_NAMES,
    _filter_untrusted_injection_chunks,
    _format_federated_markdown,
    _validate_index_path,
)
from kortext.symbols.graph import SymbolGraphManager
from kortext.trust.sanitizer import CausalSanitizer, SecurityViolation

# ── Singletons & Concurrency Guards (shared across tool calls) ─────────────

retrieval = RetrievalEngine()
episodic = AgentNativeEpisodicDriver()
symbol_graph = SymbolGraphManager()

_ACTIVE_INDEX_TASKS: set[asyncio.Task[Any]] = set()
_COLLECTION_INDEX_LOCKS: Dict[str, asyncio.Lock] = {}

JOURNAL_DIR = Path(os.getenv("JOURNAL_DIR", str(KORTEXT_ROOT / "journal")))
JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
HANDOFFS_DIR = get_kortext_home() / "handoffs"
HANDOFFS_DIR.mkdir(parents=True, exist_ok=True)

CATEGORY_DIRS: Dict[str, str] = {
    "decision": "decisions",
    "roadmap": "roadmaps",
    "implementation": "implementations",
    "experiment": "experiments",
    "benchmark": "benchmarks",
    "investigation": "investigations",
    "research": "research",
    "report": "reports",
}


SPOOL_MAX_ATTEMPTS = 5


async def _process_spool_event(ev_file: Path, client: Any = None) -> None:
    """
    Re-indexes the file an invalidation event names. On success the event is deleted. On failure it
    stays in the spool with a backoff; after SPOOL_MAX_ATTEMPTS it moves to spool/failed/ and the
    agent is told on its next turn.
    """
    from kortext.ingest import index_single_file

    try:
        payload = json.loads(ev_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("Unreadable invalidation event %s moved to spool/failed: %s", ev_file.name, exc)
        _dead_letter(ev_file, {"path": None, "last_error": f"unreadable event: {exc}"})
        return
    if float(payload.get("next_attempt_at") or 0) > time.time():
        return
    target_path = payload.get("path")
    if not target_path or not os.path.isfile(target_path):
        ev_file.unlink(missing_ok=True)  # the file is gone; nothing to re-index
        return
    try:
        res = await index_single_file(
            file_path=Path(target_path),
            collection=payload.get("collection") or os.getenv("QDRANT_COLLECTION", "kortext_codebase"),
            target_symbols_db=payload.get("symbols_graph") or os.getenv("FALKORDB_SYMBOLS_GRAPH", "kortext_symbols"),
            client=client,
            symbol_mgr=symbol_graph,
            skip_ast_if_sha=payload.get("ast_synced_sha256"),
        )
        error = res.get("error") if isinstance(res, dict) and res.get("status") == "error" else None
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    if error is None:
        ev_file.unlink(missing_ok=True)
        return

    attempts = int(payload.get("attempts") or 0) + 1
    payload.update(attempts=attempts, last_error=str(error)[:500], last_attempt_at=time.time())
    if attempts >= SPOOL_MAX_ATTEMPTS:
        _dead_letter(ev_file, payload)
        return
    payload["next_attempt_at"] = time.time() + min(600, 5 * 2**attempts)
    logger.warning("Re-index of %s failed (attempt %d/%d), will retry: %s", target_path, attempts, SPOOL_MAX_ATTEMPTS, error)
    ev_file.write_text(json.dumps(payload), encoding="utf-8")


def _dead_letter(ev_file: Path, payload: Dict[str, Any]) -> None:
    failed_dir = ev_file.parent.parent / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    (failed_dir / ev_file.name).write_text(json.dumps(payload), encoding="utf-8")
    ev_file.unlink(missing_ok=True)
    msg = (
        f"kortext could not re-index {payload.get('path')} after {payload.get('attempts', 0)} attempts: "
        f"{payload.get('last_error')}. Search results for that file are stale. "
        f"Events are kept in {failed_dir}; check_kortext_health lists them."
    )
    logger.error(msg)
    try:
        from kortext.hooks.pipelines.post_tool import queue_pending_advisory

        queue_pending_advisory(msg)
    except Exception as exc:
        logger.warning("Could not queue re-index failure advisory: %s", exc)


async def _consume_invalidation_spool_loop() -> None:
    """Drains ~/.kortext/spool/invalidation/event_*.json using the daemon's Qdrant client."""
    spool_dir = get_kortext_home() / "spool" / "invalidation"
    while True:
        try:
            await asyncio.sleep(2.0)
            if not spool_dir.is_dir():
                continue
            for ev_file in sorted(spool_dir.glob("event_*.json"))[:25]:
                await _process_spool_event(ev_file, getattr(retrieval, "_client", None))
        except asyncio.CancelledError:
            break
        except Exception as loop_exc:
            logger.error("Invalidation spool loop error: %s", loop_exc)


@asynccontextmanager
async def lifespan(app):
    """Graceful FastMCP lifecycle manager ensuring clean substrate and WAL teardown."""
    spool_task: Optional[asyncio.Task[Any]] = None
    try:
        await retrieval.initialize()
        if hasattr(episodic, "initialize"):
            await episodic.initialize()
        spool_task = asyncio.create_task(_consume_invalidation_spool_loop())
        yield
    finally:
        if spool_task is not None:
            spool_task.cancel()
            await asyncio.gather(spool_task, return_exceptions=True)
        if _ACTIVE_INDEX_TASKS:
            for t in list(_ACTIVE_INDEX_TASKS):
                t.cancel()
            await asyncio.gather(*_ACTIVE_INDEX_TASKS, return_exceptions=True)
            _ACTIVE_INDEX_TASKS.clear()

        try:
            if hasattr(episodic, "close"):
                await episodic.close()
        except Exception as exc:
            logger.debug("Episodic driver close ignored: %s", exc)

        try:
            if hasattr(retrieval, "close"):
                await retrieval.close()
        except Exception as exc:
            logger.debug("Retrieval engine close ignored: %s", exc)

        try:
            await aclose_qdrant_clients()
        except Exception as exc:
            logger.debug("Qdrant client pool close ignored: %s", exc)


mcp = FastMCP(
    name="kortext",
    instructions=(
        "Kortext Knowledge OS is your grounded memory, code intelligence, and retrieval base with four knowledge tiers: "
        "(0) AST Symbols & Backlinks, (1) Operational Facts & Decisions, (2) Viking Code Structure, (3) Qdrant Vectors. "
        "Prefer seek_symbol for known symbol definitions, trace_symbol_impact before refactoring shared interfaces, "
        "search_knowledge_and_memory for conceptual or cross-tier context, and record_system_outcome after significant changes."
    ),
    lifespan=lifespan,
)


# ── Tool 1: Retrieve ───────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Unified hybrid search across codebase knowledge, architecture outlines, episodic outcomes, "
        "and AST symbols. Queries all memory tiers in parallel and returns structured context "
        "strictly respecting the token budget and detail level."
    ),
    meta={"antigravity/alwaysLoad": True},
)
async def search_knowledge_and_memory(
    query: str,
    collection: Optional[str] = None,
    workspace_root: Optional[str] = None,
    token_budget: int = 1500,
    detail_level: str = "balanced",
    project: Optional[str] = None,
    include_markdown: bool = False,
) -> str:
    """
    Args:
        query:            The coding task, architectural question, or query to retrieve context for.
        collection:       Optional Qdrant collection name to search within (defaults to 'kortext_codebase', or 'all' for federation).
        workspace_root:   Optional workspace path to scope hierarchical structure to.
        token_budget:     Maximum total estimated tokens across all memory tiers (default: 1500).
        detail_level:     Context detail level: 'compact', 'balanced', or 'comprehensive'.
        project:          Optional project scope, e.g. 'kortext', 'my_app', or 'all' to query across all projects.
        include_markdown: If True, also includes a pre-rendered 'markdown' string alongside the JSON arrays (default False to avoid token duplication).
    Returns:
        Compact JSON envelope with 4 in-budget memory tiers (Tier 0: symbolic_context,
        Tier 1: code_chunks, Tier 2: episodic_history, Tier 3: architectural_overview) and total_estimated_tokens.
    """
    if not workspace_root and collection and collection.strip().lower() != "all":
        workspace_root = await resolve_workspace_root_for_collection(collection)

    res = await retrieval.federated_query(
        query=query,
        token_budget=token_budget,
        collection=collection,
        workspace_root=workspace_root,
        detail_level=detail_level,
        project=project,
    )

    res_copy = _filter_untrusted_injection_chunks(res)
    if include_markdown:
        res_copy["markdown"] = _format_federated_markdown(res_copy, query=query, token_budget=token_budget)
    return json.dumps(res_copy, separators=(",", ":"))


# ── Tool 2: Record ─────────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Record a significant technical decision, configuration change, or work outcome. "
        "Persists to: (1) FalkorDB episodic knowledge graph directly without intermediate LLM proxy, "
        "and (2) a dated Markdown journal file for human-readable audit trail. Old conflicting facts "
        "are superseded, not deleted."
    ),
    meta={"antigravity/alwaysLoad": True},
)
async def record_system_outcome(
    episode_name: str,
    content: str,
    category: str = "implementation",
    tags: Optional[list[str]] = None,
    project: Optional[str] = None,
    entities: Optional[list[Any]] = None,
    facts: Optional[list[dict[str, str]]] = None,
    supersedes: Optional[list[str]] = None,
    trust_level: str = "speculative",
    ctx: Optional[Context] = None,
) -> str:
    """
    Args:
        episode_name: Short title (5-10 words), e.g. 'Switched embed server to port 8001'
        content:      Full description of the outcome, decision, or change.
        category:     Mandatory category: 'decision', 'roadmap', 'implementation', 'experiment', 'benchmark', 'investigation', 'research', 'report'
        tags:         Optional list of lowercase keyword tags, e.g. ['embedding', 'ports']
        project:      Optional project label, e.g. 'kortext' or 'big-je'
        entities:     Optional structured entities [{'name': '...', 'type': '...', 'summary': '...'}]
        facts:        Optional structured facts [{'source': '...', 'relation': '...', 'target': '...', 'fact': '...'}]
        supersedes:   Optional list of patterns/facts superseded by this outcome
        trust_level:  Trust anchor level: 'speculative' (0.50), 'tool_confirmed' (0.60), 'verified' (0.80), 'user_approved' (1.00), or 'system' (1.00)
        ctx:          Optional FastMCP request context carrying Antigravity metadata (_meta['antigravity.google/root_conversation_id']).
    Returns:
        Confirmation with paths to KG and journal.
    """
    root_conv_id: Optional[str] = None
    if ctx is not None:
        try:
            req_ctx = getattr(ctx, "request_context", None)
            meta = getattr(req_ctx, "meta", None)
            if isinstance(meta, dict):
                root_conv_id = meta.get("antigravity.google/root_conversation_id")
            elif meta is not None:
                root_conv_id = getattr(meta, "root_conversation_id", None) or getattr(
                    getattr(meta, "model_extra", {}), "get", lambda *_: None
                )("antigravity.google/root_conversation_id")
        except Exception:
            root_conv_id = None
    if root_conv_id:
        tags = list(tags or [])
        conv_tag = f"root_conv:{root_conv_id}"
        if conv_tag not in tags:
            tags.append(conv_tag)
    if not isinstance(category, str) or category.lower().strip() not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category '{category}'. Must be one of: {VALID_CATEGORIES}")
    cat = category.lower().strip()

    full_content = content
    if project:
        full_content = f"[Project: {project}]\n\n{content}"

    sanitizer = CausalSanitizer()
    norm_trust = trust_level.lower().strip() if isinstance(trust_level, str) else "speculative"
    if norm_trust == "tool_confirmed" and os.getenv("KORTEXT_VERIFY_TEST_PROOF", "0") == "1":
        from kortext.hooks.pipelines.post_tool import has_recent_passing_test_run

        if not has_recent_passing_test_run():
            norm_trust = "speculative"
            trust_level = "speculative"

    if norm_trust in ("user_approved", "system"):
        source_tier = 1
    elif norm_trust in ("tool_confirmed", "verified"):
        source_tier = 2
    else:
        source_tier = 3

    try:
        sanitizer.sanitize(
            subject=episode_name,
            relation="RECORDED",
            fact=full_content,
            source_tier=source_tier,
        )
        if entities:
            for ent in entities:
                if isinstance(ent, str):
                    sanitizer.sanitize(
                        subject=ent,
                        relation="ENTITY",
                        fact=ent,
                        source_tier=source_tier,
                    )
                elif isinstance(ent, dict):
                    sanitizer.sanitize(
                        subject=str(ent.get("name", episode_name)),
                        relation=str(ent.get("type", "ENTITY")),
                        fact=str(ent.get("summary") or ent.get("name") or ""),
                        source_tier=source_tier,
                    )
        if facts:
            for f_item in facts:
                if isinstance(f_item, dict):
                    src = str(f_item.get("source") or f_item.get("subject") or episode_name)
                    rel = str(f_item.get("relation", "RELATES_TO"))
                    tgt = str(f_item.get("target") or f_item.get("object") or "")
                    f_text = str(f_item.get("fact") or f"{src} {rel} {tgt}")
                    sanitizer.sanitize(
                        subject=src,
                        relation=rel,
                        fact=f"{tgt}: {f_text}" if tgt else f_text,
                        source_tier=source_tier,
                    )
    except SecurityViolation as sv:
        return f"🚨 BLOCKED BY MINJA CAUSAL SANITIZER: {sv}. Write operation rejected and quarantined."

    safe_title = re.sub(r"[^\w\-]+", "_", episode_name.strip().lower()).strip("_") or "outcome"
    today = datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.now().strftime("%H:%M")

    cat_dir_name = CATEGORY_DIRS[cat]
    category_dir = JOURNAL_DIR / cat_dir_name
    outcome_file = category_dir / f"{today}_{safe_title}.md"

    outcome_lines = [
        f"# {episode_name}\n",
        f"- **Date:** {today} {now_time}",
        f"- **Category:** {cat}",
        f"- **Project:** {project or 'kortext'}",
        f"- **Trust Level:** {trust_level}",
    ]
    if tags:
        outcome_lines.append(f"- **Tags:** {', '.join(tags)}")
    if supersedes:
        outcome_lines.append(f"- **Supersedes:** {', '.join(supersedes)}")
    outcome_lines.extend(["\n## Outcome Description\n", full_content, "\n"])
    outcome_content = "\n".join(outcome_lines)

    journal_file = JOURNAL_DIR / f"{today}.md"
    entry_lines = [f"\n## {now_time} - {episode_name}\n", full_content]
    entry_lines.append(f"\n**Category:** {cat}")
    if tags:
        entry_lines.append(f"\n**Tags:** {', '.join(tags)}")
    if project:
        entry_lines.append(f"\n**Project:** {project}")
    if supersedes:
        entry_lines.append(f"\n**Supersedes:** {', '.join(supersedes)}")
    if trust_level:
        entry_lines.append(f"\n**Trust Level:** {trust_level}")
    entry_lines.append("\n")

    def _write_journal():
        category_dir.mkdir(parents=True, exist_ok=True)
        with open(outcome_file, "w", encoding="utf-8") as f:
            f.write(outcome_content)
        with open(journal_file, "a", encoding="utf-8") as f:
            f.write("\n".join(entry_lines))

    await asyncio.to_thread(_write_journal)

    kg_status = "✅ FalkorDB Episodic Memory"
    try:
        res = await episodic.record_outcome(
            episode_name=episode_name,
            content=full_content,
            tags=tags,
            project=project,
            entities=entities,
            facts=facts,
            supersedes=supersedes,
            trust_level=trust_level,
            category=cat,
        )
        kg_status = (
            f"✅ FalkorDB KG ({res.get('entities_written', 0)} entities, "
            f"{res.get('facts_written', 0)} facts, {res.get('superseded_count', 0)} superseded, "
            f"trust: {res.get('trust_level', 'speculative')} [{res.get('trust_score', 0.5)}])"
        )
    except Exception as e:
        kg_status = f"⚠️ FalkorDB KG skipped ({type(e).__name__}: {e})"

    return (
        f"✅ Recorded: '{episode_name}'\n"
        f"   → Category: {outcome_file}\n"
        f"   → Journal: {journal_file}\n"
        f"   → {kg_status}"
    )


# ── Tool 3: Dream Mode ─────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Trigger an on-demand Kortext Dream Mode consolidation cycle. Executes: "
        "(1) Synaptic pruning & stale fact contradiction resolution, archiving invalid facts to disk, "
        "(2) Graph community detection (Louvain algorithm) clustering code symbols into subsystems, "
        "(3) Abstract invariant induction across codebase decisions, and "
        "(4) Generates a dated dream report in journal/dreams/."
    ),
    meta={"antigravity/alwaysLoad": False},
)
async def trigger_dream_mode(
    project: str = "kortext",
    purge_stale: bool = False,
    invariants: Optional[List[Dict[str, str]]] = None,
    stale_fact_names: Optional[List[str]] = None,
    consolidations: Optional[List[Dict[str, Any]]] = None,
    distilled_skills: Optional[List[Dict[str, Any]]] = None,
    global_skills: bool = False,
) -> str:
    """
    Args:
        project:          Target project label (default: 'kortext').
        purge_stale:      If True, physically removes superseded facts after archiving. Default False (soft-prune).
        invariants:       Optional list of newly induced architectural invariants: [{'domain': '...', 'rule': '...', 'rationale': '...'}].
        stale_fact_names: Optional list of older fact names to supersede during pruning.
        consolidations:   Optional list of macro-truth facts to consolidate [{'macro_fact': '...', 'superseded_fact_ids': [...], 'subject': '...', 'object': '...'}]; subject and object are required when the facts span more than one entity pair
        distilled_skills: Optional list of skills to distill into SKILL.md [{'name': '...', 'description': '...', 'content': '...'}]
        global_skills:    Whether to persist distilled skills globally to ~/.gemini/config/plugins/kortext/skills/ instead of .agents/skills/
    Returns:
        Structured summary of the dream consolidation report.
    """
    from kortext.dream import KortextDreamEngine

    dream_engine = KortextDreamEngine()
    report = await dream_engine.run_dream_cycle(
        project=project,
        purge_stale=purge_stale,
        invariants=invariants,
        stale_fact_names=stale_fact_names,
        consolidations=consolidations,
        distilled_skills=distilled_skills,
        global_skills=global_skills,
    )
    return (
        f"🌙 Dream Mode completed for project '{project}':\n"
        f"- Active Facts Remaining: {report['pruned_facts']['active_facts_remaining']}\n"
        f"- Superseded Facts Pruned: {report['pruned_facts']['superseded_facts_pruned']}\n"
        f"- Contradictions Flagged: {len(report['pruned_facts']['contradictions_flagged'])}\n"
        f"- Subsystem Clusters Found: {report['subsystem_clusters']}\n"
        f"- Invariants Active: {report['invariants_inducted']}\n"
        f"- Macro Facts Consolidated: {report.get('consolidated_facts_count', 0)}\n"
        f"- Skills Distilled: {report.get('distilled_skills_count', 0)}\n"
        f"- Dream Report Written: {report['dream_report_path']}\n"
        f"- Invariants Log: {report['invariants_path']}"
    )


# ── Tool 4: Index ──────────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Index a directory into the Qdrant vector store so its files become "
        "retrievable by search_knowledge_and_memory. Uses upsert semantics: "
        "existing vectors are updated, not destroyed. Safe to run multiple times."
    ),
    meta={"antigravity/alwaysLoad": False},
)
async def index_directory(
    path: str,
    collection: Optional[str] = None,
    symbols_graph: Optional[str] = None,
    exclude_patterns: Optional[list[str]] = None,
    auto_dream: bool = False,
) -> str:
    """
    Args:
        path:             Absolute path to the directory to index.
        collection:       Optional Qdrant collection name (defaults to 'kortext_codebase').
        symbols_graph:    Optional FalkorDB symbol graph name (defaults to '<collection>_symbols').
        exclude_patterns: Additional glob patterns to exclude (e.g. ['tests/', '*.spec.ts']).
        auto_dream:       Whether to automatically consolidate memory via DreamEngine after ingest (default False).
    Returns:
        Confirmation with background task details.
    """
    is_valid, err_msg, resolved_path = _validate_index_path(path)
    if not is_valid or resolved_path is None:
        return err_msg

    target_col = collection or os.getenv("KORTEXT_COLLECTION", os.getenv("QDRANT_COLLECTION", "kortext_codebase"))
    default_sym = os.getenv("KORTEXT_SYMBOLS_GRAPH", os.getenv("FALKORDB_SYMBOLS_GRAPH", "kortext_symbols"))
    target_sym = symbols_graph or (f"{target_col}_symbols" if target_col != "kortext_codebase" else default_sym)

    lock = _COLLECTION_INDEX_LOCKS.setdefault(target_col, asyncio.Lock())
    if lock.locked():
        return f"⏳ Indexing is already in progress for collection '{target_col}'."

    async def _run_indexing() -> None:
        async with lock:
            try:
                ingest_kwargs: Dict[str, Any] = {
                    "extra_excludes": exclude_patterns or [],
                    "collection": target_col,
                    "symbols_graph": target_sym,
                }
                shared_client = getattr(retrieval, "_client", None)
                if shared_client is not None:
                    ingest_kwargs["client"] = shared_client
                await _ingest(str(resolved_path), **ingest_kwargs)
                if auto_dream:
                    from kortext.dream import DreamEngine

                    engine = DreamEngine()
                    await engine.run_dream_cycle(project=target_col)
            except Exception as exc:
                logger.error("Background indexing failed for '%s': %s", path, exc)

    task = asyncio.create_task(_run_indexing())
    _ACTIVE_INDEX_TASKS.add(task)
    task.add_done_callback(_ACTIVE_INDEX_TASKS.discard)
    return f"🚀 Indexing started in background for '{path}' into Qdrant collection '{target_col}' and FalkorDB graph '{target_sym}'."


# ── Tool 4b: List Collections ───────────────────────────────────────────────

@mcp.tool(
    description=(
        "List all available Qdrant vector collections with their point counts and indexing status. "
        "Call this to discover active knowledge collections (e.g. 'kortext_codebase', 'Antigravity_Docs') "
        "before querying search_knowledge_and_memory with the 'collection' argument."
    ),
    meta={"antigravity/alwaysLoad": False},
)
async def list_vector_collections() -> Dict[str, Any]:
    """
    Returns a dictionary of collections, point counts, vector configurations, and total collections.
    """
    if is_qdrant_embedded_mode(probe_local=False):
        try:
            collections_data = await asyncio.to_thread(discover_vector_collections)
            return {
                "status": "embedded",
                "collections": collections_data,
                "total_collections": len(collections_data),
            }
        except Exception as e:
            return {
                "status": "embedded",
                "error": str(e),
                "collections": [],
                "total_collections": 0,
            }

    import httpx

    qdrant_url = (get_configured_qdrant_url(probe_local=False) or "http://127.0.0.1:6333").rstrip("/")
    collections_data = []

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{qdrant_url}/collections")
            if resp.status_code == 200:
                raw_cols = resp.json().get("result", {}).get("collections", [])

                async def _get_info(col_dict: dict):
                    c_name = col_dict.get("name")
                    try:
                        info_resp = await client.get(f"{qdrant_url}/collections/{c_name}")
                        if info_resp.status_code == 200:
                            res = info_resp.json().get("result", {})
                            points_count = res.get("points_count", 0)
                            indexed_vectors = res.get("indexed_vectors_count", points_count)
                            status_str = res.get("status", "green")
                            return {
                                "name": c_name,
                                "points_count": points_count,
                                "indexed_vectors_count": indexed_vectors,
                                "status": status_str,
                            }
                    except Exception as exc:
                        logger.debug("Collection info fetch failed for '%s': %s", c_name, exc)
                    return {"name": c_name, "points_count": "unknown"}

                collections_data = await asyncio.gather(*[_get_info(c) for c in raw_cols])
                return {
                    "status": "ok",
                    "collections": list(collections_data),
                    "total_collections": len(collections_data),
                }
    except Exception as e:
        logger.warning(f"Could not list collections via HTTP: {e}")

    client = None
    try:
        from qdrant_client import AsyncQdrantClient

        client = AsyncQdrantClient(url=qdrant_url)
        cols = await client.get_collections()

        async def _client_info(c):
            try:
                info = await client.get_collection(c.name)
                return {
                    "name": c.name,
                    "points_count": info.points_count,
                    "status": str(info.status),
                }
            except Exception as exc:
                logger.debug("AsyncQdrantClient get_collection failed for '%s': %s", c.name, exc)
                return {"name": c.name, "points_count": "unknown"}

        collections_data = await asyncio.gather(*[_client_info(c) for c in cols.collections])
        return {
            "status": "ok",
            "collections": list(collections_data),
            "total_collections": len(collections_data),
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to list collections: {e}", "collections": []}
    finally:
        if client:
            try:
                await client.close()
            except Exception as exc:
                logger.debug("AsyncQdrantClient close ignored: %s", exc)


# ── Tool 5: Health ─────────────────────────────────────────────────────────

@mcp.tool(
    description=(
        "Check the health of all Kortext backend services. Returns a JSON status "
        "report for Qdrant, FalkorDB, the embedding server, and the dashboard. "
        "Call this if other Kortext tools return errors or time out."
    ),
    meta={"antigravity/alwaysLoad": False},
)
async def check_kortext_health() -> str:
    """Returns compact JSON with status of each service."""
    from kortext.health import audit_kortext_health

    report = await audit_kortext_health(include_browser_ui=False, include_invariants=True)
    return json.dumps(report, separators=(",", ":"))


# ── Tool 6: Delete / Supersede ─────────────────────────────────────────────

@mcp.tool(
    description=(
        "Mark a specific Graphiti/FalkorDB fact as superseded (soft-delete). Use when a previously "
        "recorded fact is no longer valid, such as when a port changed, a dependency was removed, "
        "or a past decision was reversed. The fact is marked invalid with your reason, "
        "preserving audit history."
    ),
    meta={"antigravity/alwaysLoad": False},
)
async def delete_or_supersede_fact(
    fact_id: str,
    reason: str,
    superseded_by: Optional[str] = None,
) -> str:
    """
    Args:
        fact_id:       The episode name or ID of the fact to supersede.
        reason:        Why this fact is no longer valid.
        superseded_by: Optional ID or name of the new fact that supersedes it.
    Returns:
        Confirmation message.
    """
    full_reason = f"{reason} (superseded by {superseded_by})" if superseded_by else reason
    await episodic.delete_fact(fact_id, full_reason)
    return f"✅ Fact '{fact_id}' marked as superseded.\nReason recorded: {full_reason}"


# ── Tool 7: Blast-Radius / Impact Analysis ─────────────────────────────────

@mcp.tool(
    description=(
        "Trace the upstream callers or downstream dependencies of a symbol across the codebase. "
        "Performs fast graph path traversal in FalkorDB over REFERENCES, IMPORTS, CALLS, and "
        "IMPLEMENTS edges with adaptive token budgeting (L1 signatures for 1-hop, L0 for 2-hop, "
        "aggregated counts for transitive). CALL THIS before refactoring or deleting a symbol."
    ),
    meta={"antigravity/alwaysLoad": True},
)
async def trace_symbol_impact(
    symbol: Optional[str] = None,
    direction: str = "upstream",
    max_depth: int = 3,
    symbols_graph: Optional[str] = None,
    token_budget: Optional[int] = 1500,
    symbol_name: Optional[str] = None,
) -> str:
    """
    Args:
        symbol:        Name of the function, class, method, or struct to analyze.
        direction:     'upstream' (dependents/callers who will break) or 'downstream' (callees/dependencies).
        max_depth:     Graph search depth hops (1 to 5, default: 3).
        symbols_graph: Optional FalkorDB graph name (defaults to 'kortext_symbols').
        token_budget:  Optional max tokens for the response (default: 1500).
        symbol_name:   Optional alias for `symbol`.
    Returns:
        Compact JSON report showing in-budget impacted files, lines, relationships, and progressive summary.
    """
    target_symbol = (symbol or symbol_name or "").strip()
    if not target_symbol:
        raise ValueError("Either 'symbol' or 'symbol_name' must be provided.")

    def _do_trace() -> str:
        target_graph = symbols_graph or os.getenv("KORTEXT_SYMBOLS_GRAPH", os.getenv("FALKORDB_SYMBOLS_GRAPH", "kortext_symbols"))
        mgr = symbol_graph if target_graph == symbol_graph.graph_name else SymbolGraphManager(graph_name=target_graph)
        impact = mgr.trace_symbol_impact(target_symbol, direction=direction, max_depth=max_depth, token_budget=token_budget)
        return json.dumps(impact, separators=(",", ":"))

    return await asyncio.to_thread(_do_trace)


# ── Tool 8: Anti-Ripgrep Deterministic Symbol Seek ───────────────────────

@mcp.tool(
    description=(
        "Deterministic symbol locator replacing brute-force ripgrep loops. "
        "Looks up exact symbol declaration, signature, docstring, callers, and callees "
        "directly from the compiler AST knowledge graph in FalkorDB in <2ms, consuming <150 tokens."
    ),
    meta={"antigravity/alwaysLoad": True},
)
async def seek_symbol(
    symbol_name: str,
    detail_level: str = "skeleton",
    file_hint: Optional[str] = None,
) -> str:
    """
    Args:
        symbol_name: Name or qualified name of the symbol (function, class, struct, method).
        detail_level: Progressive detail level: 'skeleton' (signature, docstring, types, stubbed body),
                      'interface' (skeleton + callers, callees, methods), or 'full' (complete implementation).
        file_hint: Optional filename or path substring to disambiguate identical symbols.
    Returns:
        Structured JSON declaration and immediate call graph.
    """
    def _do_seek() -> str:
        from kortext.symbols.seek import SymbolSeekEngine

        seeker = SymbolSeekEngine()
        if detail_level != "skeleton":
            return seeker.seek_symbol(symbol_name, detail_level=detail_level, file_hint=file_hint)
        return seeker.seek_symbol(symbol_name, file_hint=file_hint)

    return await asyncio.to_thread(_do_seek)


# ── Tool 9: Task DAG Project Management Engine ───────────────────────────

@mcp.tool(
    description=(
        "Graph-backed Task DAG state machine for multi-agent project decomposition. "
        "Allows creating tasks, declaring strict dependencies, claiming ready leaf tasks "
        "atomically for subagent delegation, and recording execution state."
    ),
    meta={"antigravity/alwaysLoad": True},
)
async def manage_tasks(
    action: str = "get_ready",
    title: Optional[str] = None,
    description: Optional[str] = None,
    complexity: int = 1,
    assigned_to: str = "primary",
    depends_on: Optional[list[str]] = None,
    dependencies: Optional[list[str]] = None,
    task_id: Optional[str] = None,
    status: Optional[str] = None,
    error: Optional[str] = None,
    project_id: Optional[str] = None,
    episode_name: Optional[str] = None,
    tasks: Optional[list[dict[str, Any]]] = None,
    target_files: Optional[list[str]] = None,
    files_to_modify: Optional[list[str]] = None,
    modified_files: Optional[list[str]] = None,
    test_command: Optional[str] = None,
    tests_passed: Optional[bool] = None,
    base_commit: Optional[str] = None,
    milestone_name: Optional[str] = None,
    reviewer_output: Optional[str] = None,
    token_budget_spent: Optional[int] = None,
    ctx: Optional[Context] = None,
) -> str:
    """Manage Task DAG: create (single or batch `tasks`), get_ready, claim, claim_files (exclusive file ownership for an in_progress task), heartbeat, complete_task, milestone_handoff, update_status, list_all."""
    resolved_target_files = target_files if target_files is not None else files_to_modify

    def _do_manage() -> str:
        from kortext.tasks.tracker import TaskDAGTracker
        return TaskDAGTracker().execute_action(
            action=action, title=title, description=description, complexity=complexity,
            assigned_to=assigned_to, depends_on=depends_on, dependencies=dependencies,
            task_id=task_id, status=status, error=error, project_id=project_id,
            episode_name=episode_name, tasks=tasks, target_files=resolved_target_files,
            modified_files=modified_files, test_command=test_command,
            tests_passed=tests_passed, base_commit=base_commit,
            milestone_name=milestone_name, reviewer_output=reviewer_output,
            token_budget_spent=token_budget_spent,
        )

    return await asyncio.to_thread(_do_manage)


# ── Tool 10: Cross-Agent Session Handoff Baton ────────────────────────────

@mcp.tool(
    description=(
        "Manage cross-agent context handoff batons across session and agent boundaries. "
        "Allows Antigravity, Claude Code, Codex, and peer subagents to exchange "
        "open hypotheses, unresolved questions, active blast radius, and current scope without context loss."
    ),
    meta={"antigravity/alwaysLoad": False},
)
async def memory_session_handoff(
    action: str = "create",
    target_agent: Optional[str] = None,
    current_scope: Optional[str] = None,
    open_hypotheses: Optional[list[str]] = None,
    unresolved_questions: Optional[list[str]] = None,
    active_blast_radius: Optional[list[str]] = None,
    baton_id: Optional[str] = None,
) -> str:
    """
    Args:
        action:               'create', 'read', 'list', or 'complete'.
        target_agent:         Target agent identifier (e.g. 'claude', 'codex', 'kortext-architect', or 'any').
        current_scope:        Concise summary of the active task and architectural state.
        open_hypotheses:      List of active debugging or implementation hypotheses.
        unresolved_questions: List of open questions or blockers.
        active_blast_radius:  List of modified or impacted files/symbols.
        baton_id:             Specific baton ID to read or mark completed.
    Returns:
        Formatted confirmation or JSON baton payload.
    """
    def _do_handoff() -> str:
        manager = HandoffManager(HANDOFFS_DIR)
        return manager.execute(
            action=action,
            target_agent=target_agent,
            current_scope=current_scope,
            open_hypotheses=open_hypotheses,
            unresolved_questions=unresolved_questions,
            active_blast_radius=active_blast_radius,
            baton_id=baton_id,
        )

    return await asyncio.to_thread(_do_handoff)


# ── Tool 11: Grounded Prompt Boost (/kortext-boost) ──────────────────────

@mcp.tool(
    description=(
        "Pre-execution prompt enhancer (/kortext-boost) that transforms an ambiguous or short task prompt "
        "into a compact, AST-grounded execution brief (<600 tokens) with exact symbol signatures, "
        "1-hop blast-radius callers, active invariants, and recommended multi-agent execution pattern."
    ),
    meta={"antigravity/alwaysLoad": False},
)
async def boost_prompt_context(
    prompt: str,
    workspace_root: Optional[str] = None,
    token_budget: int = 600,
    project: Optional[str] = None,
) -> str:
    """
    Args:
        prompt:         The raw user prompt or feature/bug description to ground.
        workspace_root: Optional workspace directory path.
        token_budget:   Maximum token budget for the grounded execution brief (default 600).
        project:        Optional project identifier (default 'kortext').
    Returns:
        JSON execution brief with target_symbols, blast_radius, active_invariants, and recommended_pattern.
    """
    def _do_boost() -> str:
        from kortext.chunking.ast_splitter import ASTCodeSplitter, LANG_MAP, PY_LANG
        from kortext.invariants import InvariantGroundingEngine
        from kortext.symbols.seek import SymbolSeekEngine, extract_symbol_candidates
        from kortext.tasks.tracker import TaskDAGTracker

        effective_ws = workspace_root or os.getcwd()
        candidates = extract_symbol_candidates(prompt)
        seeker = SymbolSeekEngine()
        matched_symbols: List[Dict[str, Any]] = []
        target_files: List[str] = []
        file_skeletons: Dict[str, List[str]] = {}
        callers_summary: List[str] = []

        # 1. Resolve @file mentions and <file>:<line> comments (/codesearch & /diff) in prompt
        at_mentions = re.findall(r"@([A-Za-z0-9_./\-]+\.[A-Za-z0-9]+)", prompt)
        line_Refs = re.findall(r"([A-Za-z0-9_./\-]+\.[A-Za-z0-9]+):(\d+)", prompt)
        file_line_map: Dict[str, int] = {}
        for ref_fp, ref_ln in line_Refs[:5]:
            if ref_fp not in at_mentions:
                at_mentions.append(ref_fp)
            try:
                file_line_map[ref_fp] = int(ref_ln)
            except ValueError:
                pass

        for mention in at_mentions[:5]:
            cand_path = Path(mention)
            if not cand_path.is_Absolute() if hasattr(cand_path, "is_Absolute") else not cand_path.is_absolute():
                cand_path = (Path(effective_ws) / mention).resolve()
            if cand_path.is_file():
                fp_str = str(cand_path)
                if fp_str not in target_files:
                    target_files.append(fp_str)
                ext = cand_path.suffix.lower()
                if ext in LANG_MAP:
                    try:
                        src = cand_path.read_text(encoding="utf-8", errors="ignore")
                        splitter = ASTCodeSplitter(language=LANG_MAP.get(ext, PY_LANG))
                        ast_info = splitter.extract_symbols_and_references(src, file_path=fp_str)
                        decls = ast_info.get("declarations") or []
                        sigs = [
                            str(d.get("signature") or d.get("name"))
                            for d in decls[:8]
                            if d.get("name")
                        ]
                        if sigs:
                            file_skeletons[fp_str] = sigs
                        target_ln = file_line_map.get(mention)
                        if target_ln:
                            for d in decls:
                                d_start = int(d.get("line", 1))
                                d_end = int(d.get("end_line", d_start))
                                if d_start <= target_ln <= d_end and d.get("name"):
                                    sym_n = str(d["name"])
                                    if sym_n not in candidates:
                                        candidates.insert(0, sym_n)
                    except Exception:
                        pass

        # 2. Resolve AST symbol candidates + 1-hop callers
        for sym in candidates[:5]:
            try:
                res = json.loads(seeker.seek_symbol(sym, detail_level="skeleton", workspace_root=effective_ws))
                for m in res.get("matches", [])[:2]:
                    fp = str(m.get("file") or m.get("file_path") or "")
                    if fp and fp not in target_files:
                        target_files.append(fp)
                    ln = m.get("lines", [m.get("line", 1)])[0] if isinstance(m.get("lines"), list) and m.get("lines") else m.get("line", 1)
                    callers = m.get("callers") or []
                    for c_item in callers[:3]:
                        if isinstance(c_item, dict) and c_item.get("caller"):
                            c_str = f"{c_item.get('caller')} ({Path(str(c_item.get('file') or '')).name}:{c_item.get('line', 1)})"
                            if c_str not in callers_summary:
                                callers_summary.append(c_str)
                    matched_symbols.append({
                        "symbol": m.get("symbol") or sym,
                        "kind": m.get("kind", "symbol"),
                        "file": fp,
                        "line": ln,
                        "signature": m.get("signature", ""),
                        "callers": callers[:4],
                    })
            except Exception:
                continue

        resolved_proj = project or (Path(effective_ws).name if effective_ws else "kortext") or "kortext"
        tracker = TaskDAGTracker()
        routing = tracker.recommend_execution_pattern(
            symbols=[m["symbol"] for m in matched_symbols] or candidates[:4],
            target_files=target_files,
            complexity=5 if len(target_files) > 2 else 2,
            project_id=resolved_proj,
        )

        invariants_summary: List[str] = []
        try:
            ige = InvariantGroundingEngine()
            drift = ige.audit_invariant_drift(quarantine_drifted=False)
            for item in drift.get("verified", [])[:4]:
                invariants_summary.append(f"[{item.get('domain', 'INV')}] {item.get('rule', '')}")
        except Exception:
            pass

        ws_path = Path(effective_ws) if effective_ws else KORTEXT_ROOT
        if (ws_path / "Cargo.toml").is_file():
            verify_cmd = "cargo test"
        elif (ws_path / "go.mod").is_file():
            verify_cmd = "go test ./..."
        elif (ws_path / "package.json").is_file() and not (ws_path / "pyproject.toml").is_file():
            verify_cmd = "npm test"
        else:
            verify_cmd = "uv run pytest -q"

        md_lines = [
            f"## Objective\n{prompt.strip()}",
            "\n## Grounded AST Context & Target Files",
        ]
        if matched_symbols:
            for ms in matched_symbols[:5]:
                md_lines.append(f"- `{ms['signature'] or ms['symbol']}` → `{ms['file']}:{ms['line']}`")
        if file_skeletons:
            for fp_k, sig_list in file_skeletons.items():
                md_lines.append(f"- File `{Path(fp_k).name}` skeleton: `{', '.join(sig_list[:5])}`")
        if callers_summary:
            md_lines.append(f"- 1-Hop Callers: {', '.join(callers_summary[:5])}")
        if invariants_summary:
            md_lines.append("\n## Active Grounded Invariants")
            for inv_s in invariants_summary[:3]:
                md_lines.append(f"- {inv_s}")
        md_lines.append(
            f"\n## Execution & Verification\n"
            f"- Recommended Pattern: `{routing['recommended_pattern']}` (workspace: `{routing['recommended_workspace_mode']}`)\n"
            f"- Verification Command: `{verify_cmd}`"
        )

        brief = {
            "original_prompt": prompt,
            "boosted_prompt": "\n".join(md_lines),
            "target_symbols": matched_symbols,
            "target_files": target_files,
            "file_skeletons": file_skeletons,
            "blast_radius_callers": callers_summary,
            "execution_routing": routing,
            "active_invariants": invariants_summary,
            "verification_command": verify_cmd,
        }
        return json.dumps(brief, separators=(",", ":"))

    return await asyncio.to_thread(_do_boost)


def main():
    """
    `kortext-server` (what agy launches): stdio proxy to the shared daemon, starting it if needed.
    `kortext-server --daemon`: the daemon itself. KORTEXT_SERVER_MODE=inprocess serves stdio directly,
    which is also the fallback when the daemon cannot start.
    """
    import sys

    from kortext import daemon

    if "--daemon" in sys.argv[1:]:
        daemon.serve(mcp)
        return
    if os.getenv("KORTEXT_SERVER_MODE", "").strip().lower() != "inprocess":
        if daemon.run_proxy():
            return
        sys.stderr.write("kortext: daemon unavailable, serving this window in-process\n")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
