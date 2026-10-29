"""
Retrieval sandbox, graph visualizer queries, and symbol impact/lookup routes.
"""

import asyncio
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from kortext._env import is_qdrant_embedded_mode
from kortext.dashboard.routes.actions import _is_sole_custodian_running
from kortext.dashboard.views import build_graph_view, build_symbol_impact_view
from kortext.falkordb_client import get_shared_falkordb_client
from kortext.retrieval.federated import normalize_symbol_item

router = APIRouter(tags=["query"])

FALKORDB_HOST = os.getenv("FALKORDB_HOST", "127.0.0.1")
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
FALKORDB_DB = os.getenv("KORTEXT_DATABASE", os.getenv("FALKORDB_DATABASE", "kortext"))
FALKORDB_SYMBOLS_DB = os.getenv("KORTEXT_SYMBOLS_GRAPH", os.getenv("FALKORDB_SYMBOLS_GRAPH", "kortext_symbols"))

_shared_retrieval_engine = None
_retrieval_lock = asyncio.Lock()


async def get_retrieval_engine():
    """
    Lazily initializes a RetrievalEngine for the dashboard process.
    Adheres to Invariant 3 (Sole Custodian): if Qdrant is in embedded mode and
    kortext.server is already running, avoids acquiring a competing embedded lock.
    """
    global _shared_retrieval_engine
    if _shared_retrieval_engine is None:
        async with _retrieval_lock:
            if _shared_retrieval_engine is None:
                from kortext.retrieval.engine import RetrievalEngine

                engine = RetrievalEngine()
                if is_qdrant_embedded_mode(probe_local=True) and _is_sole_custodian_running():
                    engine._skip_embedded_qdrant_init = True
                else:
                    await engine.initialize()
                _shared_retrieval_engine = engine
    else:
        if is_qdrant_embedded_mode(probe_local=True) and _is_sole_custodian_running():
            _shared_retrieval_engine._skip_embedded_qdrant_init = True
        else:
            _shared_retrieval_engine._skip_embedded_qdrant_init = False
    return _shared_retrieval_engine


@router.get("/api/graphs")
async def list_graphs():
    """Lists all active graph databases in FalkorDB."""
    try:
        client = get_shared_falkordb_client(host=FALKORDB_HOST, port=FALKORDB_PORT)
        graphs = await asyncio.to_thread(client.get_redis().execute_command, "GRAPH.LIST")
        return {"status": "ok", "graphs": graphs if isinstance(graphs, list) else []}
    except Exception as e:
        return {"status": "error", "message": str(e), "graphs": []}


@router.get("/api/graph")
async def get_graph(
    graph_type: str = "episodic",
    clean: bool = True,
    graph_name: Optional[str] = None,
    project: Optional[str] = None,
):
    """Queries FalkorDB for nodes and relationships formatted for vis-network."""
    resolved_graph_name = graph_name
    if not resolved_graph_name and graph_type == "symbols" and project and project.lower() not in ("all", "kortext"):
        candidate = f"{project}_symbols"
        try:
            client = get_shared_falkordb_client(host=FALKORDB_HOST, port=FALKORDB_PORT)
            graphs = await asyncio.to_thread(client.get_redis().execute_command, "GRAPH.LIST")
            if isinstance(graphs, list) and candidate in [str(g) for g in graphs]:
                resolved_graph_name = candidate
        except Exception:
            pass

    res = await asyncio.to_thread(
        build_graph_view,
        host=FALKORDB_HOST,
        port=FALKORDB_PORT,
        default_db=FALKORDB_DB,
        graph_type=graph_type,
        clean=clean,
        graph_name=resolved_graph_name,
    )
    if project and project.lower() != "all" and isinstance(res, dict) and res.get("status") == "ok":
        proj_lower = project.lower()
        nodes = res.get("nodes", [])
        filtered_nodes = [
            n for n in nodes
            if (
                (n.get("project_id") and str(n.get("project_id")).lower() == proj_lower)
                or (not n.get("project_id") and (proj_lower == "kortext" or proj_lower in str(n.get("id", "")).lower()))
            )
        ]
        if filtered_nodes:
            valid_ids = {n["id"] for n in filtered_nodes if "id" in n}
            res["nodes"] = filtered_nodes
            res["edges"] = [
                e for e in res.get("edges", [])
                if e.get("from") in valid_ids and e.get("to") in valid_ids
            ]
    return res


@router.get("/api/symbols")
async def get_symbols(q: Optional[str] = None, graph_name: Optional[str] = None):
    """Returns symbol graph statistics or symbol references if query 'q' provided."""
    from kortext.symbols.graph import SymbolGraphManager

    try:
        target_graph = graph_name or os.getenv("KORTEXT_SYMBOLS_GRAPH", os.getenv("FALKORDB_SYMBOLS_GRAPH", "kortext_symbols"))
        sym_mgr = SymbolGraphManager(graph_name=target_graph)
        if q:
            res = await asyncio.to_thread(sym_mgr.find_references, q)
            return {"status": "ok", "result": res}
        stats = await asyncio.to_thread(sym_mgr.get_statistics)
        return {"status": "ok", "stats": stats}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/symbols/impact")
async def get_symbol_impact(
    target: Optional[str] = None,
    file_path: Optional[str] = None,
    symbol: Optional[str] = None,
    direction: str = "upstream",
    max_depth: int = 3,
    graph_name: Optional[str] = None,
    project: Optional[str] = None,
):
    """
    Performs blast-radius and impact analysis for a symbol or file.
    Returns structured nodes, edges, risk scores, and tree hierarchies for interactive visualization.
    """
    resolved_graph = graph_name
    if not resolved_graph and project and project.lower() not in ("all", "kortext"):
        candidate = f"{project}_symbols"
        try:
            client = get_shared_falkordb_client(host=FALKORDB_HOST, port=FALKORDB_PORT)
            graphs = await asyncio.to_thread(client.get_redis().execute_command, "GRAPH.LIST")
            if isinstance(graphs, list) and candidate in [str(g) for g in graphs]:
                resolved_graph = candidate
        except Exception:
            pass

    return await asyncio.to_thread(
        build_symbol_impact_view,
        target=target,
        file_path=file_path,
        symbol=symbol,
        direction=direction,
        max_depth=max_depth,
        host=FALKORDB_HOST,
        port=FALKORDB_PORT,
        graph_name=resolved_graph,
    )


@router.get("/api/symbols/autocomplete")
async def autocomplete_symbols(q: str = "", limit: int = 30):
    """Fast autocomplete search for symbols and files."""
    try:
        client = get_shared_falkordb_client(host=FALKORDB_HOST, port=FALKORDB_PORT)
        q_clean = q.strip().replace("'", "").replace('"', "")
        if q_clean:
            cypher = (
                "MATCH (s:Symbol) WHERE toLower(s.name) CONTAINS $q_clean "
                "RETURN DISTINCT s.name, 'symbol' ORDER BY size(s.name) LIMIT $limit"
            )
            params = {"q_clean": q_clean.lower(), "limit": limit}
        else:
            cypher = "MATCH (s:Symbol) RETURN DISTINCT s.name, 'symbol' ORDER BY s.name LIMIT $limit"
            params = {"limit": limit}

        res = await asyncio.to_thread(client.query, FALKORDB_SYMBOLS_DB, cypher, params)
        items = []
        if res and len(res) > 1 and isinstance(res[1], list):
            for row in res[1]:
                items.append({"name": str(row[0]), "type": str(row[1])})

        if len(items) < limit:
            rem = limit - len(items)
            if q_clean:
                f_cypher = (
                    "MATCH (f:File) WHERE toLower(f.path) CONTAINS $q_clean "
                    "RETURN DISTINCT f.path, 'file' LIMIT $rem"
                )
                f_params = {"q_clean": q_clean.lower(), "rem": rem}
            else:
                f_cypher = "MATCH (f:File) RETURN DISTINCT f.path, 'file' LIMIT $rem"
                f_params = {"rem": rem}

            f_res = await asyncio.to_thread(client.query, FALKORDB_SYMBOLS_DB, f_cypher, f_params)
            if f_res and len(f_res) > 1 and isinstance(f_res[1], list):
                for row in f_res[1]:
                    items.append({"name": str(row[0]), "type": "file"})

        return {"status": "ok", "items": items}
    except Exception as e:
        return {"status": "error", "message": str(e), "items": []}


@router.post("/api/query")
async def run_query(request: Request):
    """Federated multi-tier retrieval endpoint via RetrievalEngine.federated_query."""
    body = await request.json()
    query_text = body.get("query", "").strip()
    collection = body.get("collection")
    col = None if collection == "all" or not collection else collection

    token_budget_raw = body.get("token_budget")
    if token_budget_raw is None:
        token_budget_raw = body.get("budget")
    if token_budget_raw is not None:
        try:
            token_budget = int(token_budget_raw)
        except (ValueError, TypeError):
            token_budget = 1500
    else:
        token_budget = 1500

    project = body.get("project")

    if not query_text:
        return JSONResponse(status_code=400, content={"error": "query cannot be empty"})

    try:
        fed_kwargs: Dict[str, Any] = {
            "query": query_text,
            "collection": col,
            "token_budget": token_budget,
        }
        if project and str(project).strip().lower() != "all":
            fed_kwargs["project"] = str(project).strip()

        from kortext import daemon

        if is_qdrant_embedded_mode(probe_local=True) and _is_sole_custodian_running() and daemon.is_up():
            # The daemon owns the vectors; asking it gives dense and BM25 results this process cannot read.
            import json

            res = await daemon.call_tool("search_knowledge_and_memory", {k: v for k, v in fed_kwargs.items() if v is not None})
            result = json.loads(res.content[0].text)
        else:
            engine = await get_retrieval_engine()
            result = await engine.federated_query(**fed_kwargs)
        raw_symbolic = result.get("symbolic_context", [])
        symbols = result.get("symbols")
        if not isinstance(symbols, list):
            symbols = [normalize_symbol_item(s) for s in raw_symbolic if s]

        tokens_est = int(result.get("total_estimated_tokens", result.get("tokens_used", 0)) or 0)
        return {
            "status": "ok",
            "query": query_text,
            "collection": col or "all",
            "symbolic_context": raw_symbolic,
            "symbols": symbols,
            "code_chunks": result.get("code_chunks", []),
            "episodic_history": result.get("episodic_history", []),
            "total_estimated_tokens": tokens_est,
            "tokens_used": tokens_est,
            "token_count": tokens_est,
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": str(e),
                "query": query_text,
                "collection": col or "all",
                "symbolic_context": [],
                "symbols": [],
                "code_chunks": [],
                "episodic_history": [],
                "total_estimated_tokens": 0,
                "tokens_used": 0,
                "token_count": 0,
            },
        )
