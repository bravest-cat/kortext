"""
Multi-tier federated retrieval coordinator for Kortext.

Orchestrates concurrent retrieval across 4 memory tiers:
  - Tier 0: AST Symbol Seek (Tree-sitter symbol index)
  - Tier 1: Hybrid Codebase Chunks (dense + sparse vector search)
  - Tier 2: Episodic History (bi-temporal facts with Knapsack MMR)
  - Tier 3: Architectural Overview (file skeletons and system summaries)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qmodels

from kortext._env import get_env, get_falkordb_symbols_graph, get_qdrant_collection
from kortext.chunking.token_utils import count_tokens
from kortext.retrieval.fusion import apply_knapsack_mmr, extract_passage_text
from kortext.retrieval.query_parser import (
    is_ci_mode,
    is_file_in_workspace,
    resolve_symbol_graph_for_collection,
    resolve_workspace_root_for_collection,
)
from kortext.symbols.seek import extract_symbol_candidates

logger = logging.getLogger("kortext.retrieval.federated")

DEFAULT_COLLECTION = get_qdrant_collection()
DEFAULT_SYMBOLS_GRAPH = get_falkordb_symbols_graph()

_REDUNDANT_PREAMBLE_RE = re.compile(
    r"^(?:#|//)\s*(?:Last Modified|Depends On|Tests Target):[^\n]*\n?",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_redundant_chunk_preamble(chunk: str) -> str:
    """Strips verbose preamble metadata lines (# Last Modified, # Depends On, # Tests Target) while preserving # File: and # Symbol:."""
    if not chunk or (
        "Last Modified:" not in chunk
        and "Depends On:" not in chunk
        and "Tests Target:" not in chunk
    ):
        return chunk
    return _REDUNDANT_PREAMBLE_RE.sub("", chunk)


def partition_budgets(token_budget: int, dl: str) -> Tuple[Dict[str, int], Dict[str, Any]]:
    """Splits token budget across Tier 0, Tier 1, Tier 2, and Tier 3 based on detail level."""
    if dl == "compact":
        t0 = max(10, int(token_budget * 0.30))
        t1 = max(20, int(token_budget * 0.50))
        t2 = max(10, token_budget - t0 - t1)
        t3 = 0
        return {"t0": t0, "t1": t1, "t2": t2, "t3": t3}, {"ast_dl": "skeleton", "max_t1": 2, "max_t2": 2}

    if dl in ("comprehensive", "full"):
        t0 = max(20, int(token_budget * 0.25))
        t1 = max(50, int(token_budget * 0.35))
        t2 = max(20, int(token_budget * 0.15))
        t3 = max(0, token_budget - t0 - t1 - t2)
        return {"t0": t0, "t1": t1, "t2": t2, "t3": t3}, {"ast_dl": "full", "max_t1": 10, "max_t2": 20}

    # Balanced (default)
    t0 = max(20, int(token_budget * 0.25))
    t1 = max(50, int(token_budget * 0.45))
    t2 = max(20, int(token_budget * 0.20))
    t3 = max(0, token_budget - t0 - t1 - t2)
    return {"t0": t0, "t1": t1, "t2": t2, "t3": t3}, {"ast_dl": "interface", "max_t1": 5, "max_t2": 5}


def resolve_project_scope(
    project: Optional[str],
    collection: Optional[str],
    workspace_root: Optional[str],
) -> str:
    """Determines project namespace for episodic memory scoping."""
    if project is not None:
        p_str = project.strip()
        return "all" if p_str.lower() == "all" else p_str
    if collection and collection.strip().lower() not in (
        "all",
        "default",
        "kortext_codebase",
    ):
        return collection.strip()
    if workspace_root:
        ws_name = Path(workspace_root).name.strip()
        if ws_name and ws_name.lower() not in ("all", "default", "", ".", "/"):
            return ws_name
    return get_env("PROJECT") or get_env("KORTEXT_PROJECT") or "kortext"


def normalize_symbol_item(item: Any, fallback_name: str = "") -> Dict[str, Any]:
    """Normalizes a Tier 0 symbol match (dict or markdown string) into a structured dict."""
    if isinstance(item, dict):
        out = dict(item)
        name = out.get("name") or out.get("symbol") or fallback_name
        file_val = out.get("file_path") or out.get("file") or out.get("path") or ""
        kind_val = out.get("kind") or out.get("type") or "symbol"
        line_val = out.get("line") or out.get("start_line") or 1
        sig_val = out.get("signature") or out.get("skeleton") or name
        out.setdefault("name", name)
        out.setdefault("kind", kind_val)
        out.setdefault("file", file_val)
        out.setdefault("file_path", file_val)
        out.setdefault("line", line_val)
        out.setdefault("signature", sig_val)
        return out

    raw = str(item).strip()
    name = fallback_name
    kind = "symbol"
    file_path = ""
    line = 1
    signature = ""

    m_hdr = re.search(r"`([^`]+)`\s*\(([^)]+)\)", raw)
    if m_hdr:
        name = m_hdr.group(1).strip()
        kind = m_hdr.group(2).strip()
    m_file = re.search(r"File:\s*`?([^`\n(]+)`?(?:\s*\(Lines?\s*(\d+))?", raw, re.IGNORECASE)
    if m_file:
        file_path = m_file.group(1).strip()
        if m_file.group(2):
            try:
                line = int(m_file.group(2))
            except ValueError:
                line = 1
    m_sig = re.search(r"Signature:\s*`([^`]+)`", raw, re.IGNORECASE)
    if m_sig:
        signature = m_sig.group(1).strip()
    elif not signature:
        signature = raw.splitlines()[0][:160] if raw else name

    return {
        "name": name or "symbol",
        "kind": kind,
        "file": file_path,
        "file_path": file_path,
        "line": line,
        "signature": signature,
        "raw": raw,
    }


async def fetch_tier0_symbols(
    symbol_seek: Any,
    query: str,
    graph: str,
    ws_root: Optional[str],
    budget: int,
    ast_dl: str,
) -> List[Dict[str, Any]]:
    """Retrieves AST symbols (Tier 0) within budget constraints."""
    try:
        candidates = extract_symbol_candidates(query)
        if not candidates:
            return []

        async def _seek_one(sym: str) -> Tuple[str, Optional[Any]]:
            try:
                res = await asyncio.to_thread(
                    symbol_seek.seek_symbol,
                    sym,
                    detail_level=ast_dl,
                    graph_name=graph,
                    workspace_root=ws_root,
                )
                return sym, res
            except TypeError:
                res = await asyncio.to_thread(
                    symbol_seek.seek_symbol, sym, detail_level=ast_dl
                )
                return sym, res
            except Exception as exc:
                logger.warning(f"[Tier 0] Error seeking '{sym}': {exc}")
                return sym, None

        results = await asyncio.gather(*[_seek_one(s) for s in candidates[:5]], return_exceptions=True)
        symbols: List[Dict[str, Any]] = []
        cur_tokens = 0
        for entry in results:
            if not entry or isinstance(entry, Exception):
                continue
            sym_name, r = entry
            if not r or isinstance(r, Exception):
                continue
            try:
                parsed = json.loads(r) if isinstance(r, str) else r
            except Exception:
                parsed = r

            if isinstance(parsed, dict):
                if parsed.get("status") in ("not_found", "error") and not parsed.get("matches"):
                    continue
                items = parsed.get("matches", [parsed]) if "matches" in parsed else [parsed]
            elif isinstance(parsed, list):
                items = parsed
            else:
                items = [parsed]

            for item in items:
                f_val = (
                    (item.get("file") or item.get("file_path") or item.get("path"))
                    if isinstance(item, dict)
                    else None
                )
                if ws_root and f_val:
                    if not is_file_in_workspace(f_val, ws_root):
                        continue
                cost = count_tokens(json.dumps(item) if isinstance(item, (dict, list)) else str(item))
                if cur_tokens + cost <= budget or not symbols:
                    symbols.append(item)
                    cur_tokens += cost
                else:
                    break
        return symbols
    except Exception as exc:
        logger.warning(f"[Tier 0/Symbols] Unhandled error: {exc}")
        return []


async def fetch_tier1_chunks(
    query_func: Callable[..., Coroutine[Any, Any, List[str]]],
    query: str,
    col: Optional[str],
    ws_root: Optional[str],
    budget: int,
    max_chunks: int,
    dl: str,
    query_vec: Optional[List[float]] = None,
    suppress_temporal_signal: bool = True,
) -> List[str]:
    """Retrieves code chunks (Tier 1) within budget constraints."""
    try:
        try:
            chunks = await query_func(
                query,
                limit=max_chunks,
                collection=col or DEFAULT_COLLECTION,
                workspace_root=ws_root,
                suppress_ast_signal=True,
                suppress_temporal_signal=suppress_temporal_signal,
                query_vec=query_vec,
            )
        except TypeError:
            try:
                chunks = await query_func(
                    query,
                    limit=max_chunks,
                    collection=col or DEFAULT_COLLECTION,
                    workspace_root=ws_root,
                    suppress_ast_signal=True,
                )
            except TypeError:
                chunks = await query_func(
                    query,
                    limit=max_chunks,
                    collection=col or DEFAULT_COLLECTION,
                    suppress_ast_signal=True,
                )

        if not chunks:
            return []

        selected: List[str] = []
        cur_tokens = 0
        for chunk in chunks[:max_chunks]:
            c_str = _strip_redundant_chunk_preamble(str(chunk))
            if ws_root:
                first_line = c_str.split("\n", 1)[0]
                if first_line.startswith(("# File:", "File:", "// File:")):
                    f_path = re.sub(r":\d+(?:-\d+)?$", "", first_line.split(":", 1)[1].strip())
                    if f_path and not is_file_in_workspace(f_path, ws_root):
                        continue
            if dl == "compact":
                c_str = c_str[:256]
            cost = count_tokens(c_str)
            if cur_tokens + cost <= budget:
                selected.append(c_str)
                cur_tokens += cost
            elif not selected and cost > budget:
                chars = min(256, max(40, budget * 4)) if dl == "compact" else max(40, budget * 4)
                trunc = c_str[:chars]
                selected.append(trunc)
                cur_tokens += count_tokens(trunc)
                break
            else:
                break
        return selected
    except Exception as exc:
        logger.warning(f"[Tier 1/Code] Unhandled error: {exc}")
        return []


async def fetch_tier2_episodic(
    episodic_search_func: Callable[..., Coroutine[Any, Any, List[str]]],
    query: str,
    project: str,
    ws_root: Optional[str],
    col: Optional[str],
    budget: int,
    max_facts: int,
) -> List[str]:
    """Retrieves episodic memory facts and grounded invariants (Tier 2) with Knapsack MMR diversity."""
    try:
        raw_facts = list(
            await episodic_search_func(
                query, limit=max_facts, project=project, workspace_root=ws_root
            )
            or []
        )

        # Surface matching AST-grounded invariants from journal/grounded_invariants.json
        try:
            from kortext.invariants import grounded_read_path

            g_file = grounded_read_path()

            if g_file.is_file():
                g_items = json.loads(g_file.read_text(encoding="utf-8"))
                if isinstance(g_items, list):
                    q_tokens = [t for t in re.split(r"\W+", query.lower()) if len(t) >= 3]
                    for g_inv in g_items[:15]:
                        if not isinstance(g_inv, dict) or g_inv.get("quarantined"):
                            continue
                        sym = str(g_inv.get("symbol_name") or "")
                        rule = str(g_inv.get("rule") or "")
                        dom = str(g_inv.get("domain") or "INV")
                        inv_lower = f"{dom} {sym} {rule}".lower()
                        if q_tokens and any(tok in inv_lower for tok in q_tokens):
                            inv_line = f"[Grounded Invariant:{dom} ({sym})] {rule}"
                            if inv_line not in raw_facts:
                                raw_facts.append(inv_line)
        except Exception as inv_exc:
            logger.debug(f"[Tier 2/Invariants] Lookup skipped: {inv_exc}")

        if not raw_facts:
            return []

        if col or ws_root:
            keywords: List[str] = []
            if col and col.lower() not in ("all", "default", "kortext_codebase"):
                keywords.append(col.lower())
            if ws_root:
                ws_name = Path(ws_root).name.lower()
                if ws_name and ws_name not in ("all", "default"):
                    keywords.append(ws_name)
            if keywords:
                scoped_facts = [f for f in raw_facts if any(k in f.lower() for k in keywords)]
                if scoped_facts:
                    raw_facts = scoped_facts + [f for f in raw_facts if f not in scoped_facts]

        selected_facts = apply_knapsack_mmr(
            raw_facts, query=query, token_budget=budget, lambda_param=0.7
        )
        return selected_facts[:max_facts]
    except Exception as exc:
        logger.warning(f"[Tier 2/Episodic] Unhandled error: {exc}")
        return []


async def fetch_tier3_overview(
    client: Optional[AsyncQdrantClient],
    falkordb: Any,
    query_embedding_func: Callable[[str], Coroutine[Any, Any, List[float]]],
    workspace_root: Optional[str],
    query: str,
    budget: int,
    detail_level: str = "balanced",
    collection: Optional[str] = None,
    query_vec: Optional[List[float]] = None,
) -> Optional[str]:
    """Retrieves architectural file skeletons and high-level module summaries (Tier 3)."""
    if budget <= 0 or detail_level == "compact":
        return None
    if detail_level == "balanced" and not workspace_root:
        return None

    limit = 2 if detail_level == "balanced" else 8
    target_col = collection or DEFAULT_COLLECTION
    skeleton_texts: List[str] = []

    if client is not None:
        try:
            filter_cond = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="chunk_type",
                        match=qmodels.MatchValue(value="ast_skeleton"),
                    )
                ]
            )
            points = []
            dense_vec = query_vec if query_vec is not None else await query_embedding_func(query)
            if dense_vec and not is_ci_mode():
                try:
                    res = await client.query_points(
                        collection_name=target_col,
                        query=dense_vec,
                        query_filter=filter_cond,
                        using="text-dense",
                        limit=limit,
                        with_payload=True,
                    )
                    points = res.points
                except Exception:
                    points = []

            if not points:
                try:
                    res_scroll, _ = await client.scroll(
                        collection_name=target_col,
                        scroll_filter=filter_cond,
                        limit=limit,
                        with_payload=True,
                    )
                    points = res_scroll
                except Exception:
                    points = []

            for pt in points:
                payload = getattr(pt, "payload", {}) or {}
                fp = payload.get("file_path") or payload.get("metadata", {}).get("file_path")
                if workspace_root and fp and not is_file_in_workspace(fp, workspace_root):
                    continue
                text = extract_passage_text(payload)
                if text:
                    skeleton_texts.append(text)
        except Exception as exc:
            logger.debug(f"[Tier 3] Qdrant ast_skeleton retrieval error: {exc}")

    if not skeleton_texts and falkordb:
        try:
            target_graph = resolve_symbol_graph_for_collection(collection) or DEFAULT_SYMBOLS_GRAPH
            cypher = """
            MATCH (f:File)
            OPTIONAL MATCH (s:Symbol)-[d:DEFINED_IN]->(f)
            RETURN f.path AS file, f.summary AS summary,
                   collect({name: s.name, kind: s.kind, signature: d.signature})[..5] AS symbols
            LIMIT 10
            """
            res = falkordb.query(target_graph, cypher)
            if res and len(res) > 1 and res[1]:
                falkor_lines = []
                for row in res[1]:
                    f_path, f_summary, syms = row[0], row[1], row[2]
                    if workspace_root and f_path and not is_file_in_workspace(f_path, workspace_root):
                        continue
                    falkor_lines.append(f"- **File:** `{f_path}`")
                    if f_summary:
                        falkor_lines.append(f"  - Summary: {f_summary}")
                    if syms and isinstance(syms, list):
                        for sym in syms[:4]:
                            if isinstance(sym, dict) and sym.get("name"):
                                sig = sym.get("signature") or sym.get("name")
                                falkor_lines.append(f"  - `{sig}` ({sym.get('kind', 'symbol')})")
                if falkor_lines:
                    skeleton_texts.append("\n".join(falkor_lines))
        except Exception as exc:
            logger.debug(f"[Tier 3] FalkorDB fallback error: {exc}")

    if not skeleton_texts:
        return None

    combined = "\n\n".join(skeleton_texts).strip()
    if not combined:
        return None

    curr_tokens = count_tokens(combined)
    if curr_tokens > budget:
        allowed_chars = max(40, budget * 4)
        combined = combined[:allowed_chars].rstrip()

    return combined if combined else None


def pack_federated_result(
    query: str,
    symbolic_context: List[Any],
    code_chunks: List[str],
    episodic_history: List[str],
    architectural_overview: Optional[str],
    token_budget: int,
) -> Dict[str, Any]:
    """Applies unified knapsack trimming across all tiers to respect strict token budget."""
    def _cost(x: Any) -> int:
        return count_tokens(json.dumps(x) if isinstance(x, (dict, list)) else str(x))

    t3_tokens = count_tokens(architectural_overview) if architectural_overview else 0
    total_tokens = (
        sum(_cost(x) for x in symbolic_context)
        + sum(count_tokens(x) for x in code_chunks)
        + sum(count_tokens(x) for x in episodic_history)
        + t3_tokens
    )

    if total_tokens > token_budget and architectural_overview:
        excess = total_tokens - token_budget
        if t3_tokens <= excess:
            total_tokens -= t3_tokens
            architectural_overview = None
        else:
            keep = max(0, t3_tokens - excess)
            if keep < 15:
                total_tokens -= t3_tokens
                architectural_overview = None
            else:
                architectural_overview = architectural_overview[:keep * 4].rstrip()
                total_tokens = (
                    sum(_cost(x) for x in symbolic_context)
                    + sum(count_tokens(x) for x in code_chunks)
                    + sum(count_tokens(x) for x in episodic_history)
                    + count_tokens(architectural_overview)
                )

    while total_tokens > token_budget and (code_chunks or episodic_history or symbolic_context):
        if len(code_chunks) > 1:
            total_tokens -= count_tokens(code_chunks.pop())
        elif len(episodic_history) > 1:
            total_tokens -= count_tokens(episodic_history.pop())
        elif len(symbolic_context) > 1:
            total_tokens -= _cost(symbolic_context.pop())
        elif code_chunks:
            t3_curr = count_tokens(architectural_overview) if architectural_overview else 0
            rem = max(
                10,
                token_budget
                - sum(_cost(x) for x in symbolic_context)
                - sum(count_tokens(x) for x in episodic_history)
                - t3_curr,
            )
            if rem < count_tokens(code_chunks[0]):
                code_chunks[0] = code_chunks[0][:rem * 4]
            break
        else:
            break
        total_tokens = (
            sum(_cost(x) for x in symbolic_context)
            + sum(count_tokens(x) for x in code_chunks)
            + sum(count_tokens(x) for x in episodic_history)
            + (count_tokens(architectural_overview) if architectural_overview else 0)
        )

    structured_symbols = [
        normalize_symbol_item(s) for s in symbolic_context if s
    ]

    if structured_symbols or (code_chunks and len(code_chunks) >= 2) or (code_chunks and episodic_history):
        confidence_verdict = "strong"
        confidence_guidance = "Grounded in verified AST symbols and multi-tier memory."
    elif code_chunks or episodic_history or architectural_overview:
        confidence_verdict = "weak"
        confidence_guidance = "Partial context retrieved; verify critical signatures via seek_symbol before editing."
    else:
        confidence_verdict = "none"
        confidence_guidance = "Memory is silent on this topic — do not guess; inspect files directly."

    return {
        "query": query,
        "confidence_verdict": confidence_verdict,
        "confidence_guidance": confidence_guidance,
        "symbolic_context": symbolic_context,
        "symbols": structured_symbols,
        "code_chunks": code_chunks,
        "episodic_history": episodic_history,
        "architectural_overview": architectural_overview,
        "total_estimated_tokens": total_tokens,
        "tokens_used": total_tokens,
    }


async def execute_federated_query(
    engine: Any,
    query: str,
    token_budget: int = 1500,
    collection: Optional[str] = None,
    workspace_root: Optional[str] = None,
    detail_level: str = "balanced",
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """Main execution loop for 4-tier federated retrieval."""
    if token_budget <= 0:
        return {
            "query": query,
            "confidence_verdict": "none",
            "confidence_guidance": "Memory is silent on this topic — do not guess; inspect files directly.",
            "symbolic_context": [],
            "symbols": [],
            "code_chunks": [],
            "episodic_history": [],
            "architectural_overview": None,
            "total_estimated_tokens": 0,
            "tokens_used": 0,
        }

    if collection and not workspace_root:
        import kortext.retrieval.engine as _engine_mod

        workspace_root = await _engine_mod.resolve_workspace_root_for_collection(collection)

    target_sym_graph = resolve_symbol_graph_for_collection(collection)
    resolved_project = resolve_project_scope(project, collection, workspace_root)

    dl = detail_level.lower().strip() if isinstance(detail_level, str) else "balanced"
    budgets, limits = partition_budgets(token_budget, dl)

    # Compute dense query vector once if needed by Tier 1 and Tier 3
    shared_query_vec: Optional[List[float]] = None
    if hasattr(engine, "_get_query_embedding") and callable(engine._get_query_embedding) and not is_ci_mode():
        try:
            shared_query_vec = await engine._get_query_embedding(query)
        except Exception as exc:
            logger.debug(f"Shared query embedding precomputation fallback: {exc}")
            shared_query_vec = None

    # 1. Fetch Tier 0, Tier 1, Tier 2
    tier0_task = asyncio.create_task(
        fetch_tier0_symbols(
            symbol_seek=engine.symbol_seek,
            query=query,
            graph=target_sym_graph,
            ws_root=workspace_root,
            budget=budgets["t0"],
            ast_dl=limits["ast_dl"],
        )
    )
    tier1_task = asyncio.create_task(
        fetch_tier1_chunks(
            query_func=engine.query,
            query=query,
            col=collection,
            ws_root=workspace_root,
            budget=budgets["t1"],
            max_chunks=limits["max_t1"],
            dl=dl,
            query_vec=shared_query_vec,
            suppress_temporal_signal=True,
        )
    )
    tier2_task = asyncio.create_task(
        fetch_tier2_episodic(
            episodic_search_func=engine.episodic.search,
            query=query,
            project=resolved_project,
            ws_root=workspace_root,
            col=collection,
            budget=budgets["t2"],
            max_facts=limits["max_t2"],
        )
    )

    # 2. Fetch Tier 3 (Omit completely for compact or zero-budget)
    if dl == "compact" or budgets["t3"] <= 0:
        tier3_task = asyncio.create_task(asyncio.sleep(0, result=None))
    else:
        async def _call_tier3():
            try:
                return await engine._fetch_tier3_overview(
                    workspace_root=workspace_root,
                    query=query,
                    budget=budgets["t3"],
                    detail_level=dl,
                    collection=collection,
                    query_vec=shared_query_vec,
                )
            except TypeError:
                return await engine._fetch_tier3_overview(
                    workspace_root=workspace_root,
                    query=query,
                    budget=budgets["t3"],
                    detail_level=dl,
                    collection=collection,
                )

        tier3_task = asyncio.create_task(_call_tier3())

    t0_res, t1_res, t2_res, t3_res = await asyncio.gather(
        tier0_task, tier1_task, tier2_task, tier3_task, return_exceptions=True
    )

    symbolic_context = t0_res if isinstance(t0_res, list) else []
    code_chunks = t1_res if isinstance(t1_res, list) else []
    episodic_history = t2_res if isinstance(t2_res, list) else []
    architectural_overview = t3_res if isinstance(t3_res, str) and t3_res.strip() else None

    return pack_federated_result(
        query=query,
        symbolic_context=symbolic_context,
        code_chunks=code_chunks,
        episodic_history=episodic_history,
        architectural_overview=architectural_overview,
        token_budget=token_budget,
    )
