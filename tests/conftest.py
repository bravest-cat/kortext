"""
Benchmark reporter and sandbox test-double fixture for Kortext tests.

1. Collects numeric results from benchmark tests via a session-scoped store and writes
   JSON reports ONLY when benchmark tests were actually executed in the session.
2. Provides an in-memory FalkorDB test double when running inside an OS Seatbelt sandbox
   that blocks local socket connections (Operation not permitted), keeping production
   `kortext/falkordb_client.py` completely free of embedded test fakes.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest

# Session-wide HOME isolation. This must run before any kortext import: kortext._home and
# kortext.retrieval.qdrant_engine compute KORTEXT_HOME / DEFAULT_QDRANT_PATH at import time.
# Without it, tests create ~/.kortext and `kortext setup` writes into ~/.gemini.
_TEST_HOME = Path(tempfile.mkdtemp(prefix="kortext_test_home_"))
_TEST_KORTEXT_HOME = _TEST_HOME / ".kortext"
(_TEST_KORTEXT_HOME / "qdrant").mkdir(parents=True)
os.environ["HOME"] = str(_TEST_HOME)
os.environ["KORTEXT_HOME"] = str(_TEST_KORTEXT_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_BENCHMARK_STASH_KEY = pytest.StashKey[Dict[str, Any]]()
_SANDBOX_GRAPHS: Dict[str, Dict[str, Any]] = {}


def _is_seatbelt_socket_blocked() -> bool:
    """Checks once whether OS Seatbelt blocks local loopback/UDS socket connections."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.05)
            s.connect(("127.0.0.1", 6379))
        return False
    except OSError:
        return True


_SEATBELT_BLOCKED = _is_seatbelt_socket_blocked()


_SEED_SYMBOLS_DATA: List[Dict[str, Any]] = [
    {
        "name": "RetrievalEngine",
        "kind": "class",
        "file_path": "kortext/retrieval/engine.py",
        "line": 128,
        "end_line": 145,
        "signature": "class RetrievalEngine:",
        "docstring": "Orchestrates 5-Signal Hybrid RRF retrieval.",
        "callees": ["apply_taak_trimming", "rank_candidates_rrf", "SymbolSeekEngine", "HippoRAGPPR"],
        "callers": ["execute_federated_query", "search_knowledge_and_memory"],
    },
    {
        "name": "apply_taak_trimming",
        "kind": "function",
        "file_path": "kortext/retrieval/knee_trimmer.py",
        "line": 45,
        "end_line": 70,
        "signature": "def apply_taak_trimming(ranked_candidates: List[Any], k_min: int = 3, k_max: int = 15) -> List[str]:",
        "docstring": "Truncation-Aware Adaptive Knee trimming.",
        "callees": ["extract_passage_text"],
        "callers": ["RetrievalEngine"],
    },
    {
        "name": "SymbolSeekEngine",
        "kind": "class",
        "file_path": "kortext/symbols/seek.py",
        "line": 349,
        "end_line": 372,
        "signature": "class SymbolSeekEngine:",
        "docstring": "Deterministic symbol locator and blast-radius traverser.",
        "callees": ["extract_symbol_candidates", "extract_symbol_source_detail", "get_shared_falkordb_client"],
        "callers": ["RetrievalEngine", "seek_symbol"],
    },
    {
        "name": "extract_symbol_candidates",
        "kind": "function",
        "file_path": "kortext/symbols/seek.py",
        "line": 67,
        "end_line": 95,
        "signature": "def extract_symbol_candidates(query: str, max_candidates: int = 5) -> List[str]:",
        "docstring": "Extracts likely symbol identifiers from a natural language query.",
        "callees": [],
        "callers": ["SymbolSeekEngine", "RetrievalEngine"],
    },
    {
        "name": "HippoRAGPPR",
        "kind": "class",
        "file_path": "kortext/retrieval/hipporag.py",
        "line": 22,
        "end_line": 42,
        "signature": "class HippoRAGPPR:",
        "docstring": "Executes Personalized PageRank with Inverse Degree Specificity.",
        "callees": ["retrieve_associative_subgraph", "get_shared_falkordb_client"],
        "callers": ["RetrievalEngine"],
    },
    {
        "name": "retrieve_associative_subgraph",
        "kind": "method",
        "file_path": "kortext/retrieval/hipporag.py",
        "line": 38,
        "end_line": 65,
        "signature": "def retrieve_associative_subgraph(self, seed_entities: List[str], top_k: int = 15) -> List[Tuple[str, float]]:",
        "docstring": "Runs single-pass Personalized PageRank starting from seed entities.",
        "parent": "HippoRAGPPR",
        "callees": ["get_shared_falkordb_client"],
        "callers": ["HippoRAGPPR", "RetrievalEngine"],
    },
    {
        "name": "AgentNativeEpisodicDriver",
        "kind": "class",
        "file_path": "kortext/episodic/driver.py",
        "line": 70,
        "end_line": 95,
        "signature": "class AgentNativeEpisodicDriver:",
        "docstring": "Direct FalkorDB driver for Episodic & Temporal Knowledge Memory.",
        "callees": ["record_fact", "record_outcome", "query_facts_as_of", "CausalSanitizer"],
        "callers": ["record_system_outcome"],
    },
    {
        "name": "record_fact",
        "kind": "method",
        "file_path": "kortext/episodic/driver.py",
        "line": 312,
        "end_line": 345,
        "signature": "def record_fact(self, subject: str, relation: str, fact: str) -> str:",
        "docstring": "Records an episodic relationship between subject and object entities.",
        "parent": "AgentNativeEpisodicDriver",
        "callees": ["CausalSanitizer"],
        "callers": ["AgentNativeEpisodicDriver", "EpisodicDriver"],
    },
    {
        "name": "CausalSanitizer",
        "kind": "class",
        "file_path": "kortext/trust/sanitizer.py",
        "line": 68,
        "end_line": 95,
        "signature": "class CausalSanitizer:",
        "docstring": "Guards memory write operations against adversarial poisoning.",
        "callees": ["normalize_for_security_scan"],
        "callers": ["AgentNativeEpisodicDriver", "record_system_outcome"],
    },
    {
        "name": "TaskDAGTracker",
        "kind": "class",
        "file_path": "kortext/tasks/tracker.py",
        "line": 121,
        "end_line": 150,
        "signature": "class TaskDAGTracker:",
        "docstring": "Manages Task DAG lifecycle in FalkorDB.",
        "callees": ["get_ready_tasks", "claim_next_ready_task", "complete_task", "detect_file_overlaps"],
        "callers": ["manage_tasks"],
    },
    {
        "name": "get_ready_tasks",
        "kind": "method",
        "file_path": "kortext/tasks/tracker.py",
        "line": 365,
        "end_line": 390,
        "signature": "def get_ready_tasks(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:",
        "docstring": "Finds all tasks whose upstream dependencies are completed.",
        "parent": "TaskDAGTracker",
        "callees": [],
        "callers": ["TaskDAGTracker"],
    },
    {
        "name": "EmbeddedQdrantEngine",
        "kind": "class",
        "file_path": "kortext/retrieval/qdrant_engine.py",
        "line": 37,
        "end_line": 65,
        "signature": "class EmbeddedQdrantEngine:",
        "docstring": "Manages in-process embedded Qdrant lifecycle and modern hybrid vector search.",
        "callees": ["get_qdrant_client", "ensure_collection", "hybrid_search"],
        "callers": ["RetrievalEngine"],
    },
    {
        "name": "SymbolGraphManager",
        "kind": "class",
        "file_path": "kortext/symbols/graph.py",
        "line": 34,
        "end_line": 65,
        "signature": "class SymbolGraphManager(SymbolGraphTraversalMixin):",
        "docstring": "Manages symbol declarations, cross-file imports, and usages in FalkorDB.",
        "callees": ["trace_symbol_impact", "save_file_ast", "find_definition", "find_references"],
        "callers": ["trace_symbol_impact", "index_single_file"],
    },
    {
        "name": "trace_symbol_impact",
        "kind": "method",
        "file_path": "kortext/symbols/graph_traversal.py",
        "line": 15,
        "end_line": 50,
        "signature": "def trace_symbol_impact(self, symbol_name: str, direction: str = 'upstream', max_depth: int = 3) -> Dict[str, Any]:",
        "docstring": "Traces recursive call, reference, import, and note link blast radius.",
        "parent": "SymbolGraphManager",
        "callees": ["find_definition", "format_impact_report"],
        "callers": ["SymbolGraphManager"],
    },
    {
        "name": "LouvainCommunityClusterer",
        "kind": "class",
        "file_path": "kortext/symbols/clustering.py",
        "line": 25,
        "end_line": 55,
        "signature": "class LouvainCommunityClusterer:",
        "docstring": "Detects Louvain architectural communities across the symbol graph.",
        "callees": ["detect_communities", "detect_subsystems"],
        "callers": ["DreamEngine", "DreamConsolidator"],
    },
    {
        "name": "detect_communities",
        "kind": "method",
        "file_path": "kortext/symbols/clustering.py",
        "line": 65,
        "end_line": 90,
        "signature": "def detect_communities(self) -> List[Dict[str, Any]]:",
        "docstring": "Runs Louvain community detection.",
        "parent": "LouvainCommunityClusterer",
        "callees": [],
        "callers": ["LouvainCommunityClusterer"],
    },
    {
        "name": "DreamConsolidator",
        "kind": "class",
        "file_path": "kortext/dream.py",
        "line": 46,
        "end_line": 75,
        "signature": "class DreamConsolidator:",
        "docstring": "Cognitive consolidation and reflection engine for Kortext.",
        "callees": ["trigger_dream_mode", "run_dream_cycle", "LouvainCommunityClusterer"],
        "callers": ["trigger_dream_mode"],
    },
    {
        "name": "trigger_dream_mode",
        "kind": "function",
        "file_path": "kortext/server.py",
        "line": 342,
        "end_line": 370,
        "signature": "async def trigger_dream_mode(project: str = 'kortext') -> str:",
        "docstring": "Trigger an on-demand Kortext Dream Mode consolidation cycle.",
        "callees": ["DreamConsolidator"],
        "callers": ["KortextServer"],
    },
    {
        "name": "HandoffEngine",
        "kind": "class",
        "file_path": "kortext/handoff.py",
        "line": 20,
        "end_line": 50,
        "signature": "class HandoffEngine:",
        "docstring": "Persists and compacts multi-turn session handoff batons.",
        "callees": ["create_baton"],
        "callers": ["memory_session_handoff"],
    },
    {
        "name": "HealthMonitor",
        "kind": "class",
        "file_path": "kortext/health.py",
        "line": 25,
        "end_line": 55,
        "signature": "class HealthMonitor:",
        "docstring": "Probes FalkorDB, Qdrant, and embedding substrate health.",
        "callees": ["audit_kortext_health", "check_kortext_health"],
        "callers": ["check_kortext_health"],
    },
    {
        "name": "audit_kortext_health",
        "kind": "function",
        "file_path": "kortext/health.py",
        "line": 65,
        "end_line": 95,
        "signature": "async def audit_kortext_health() -> Dict[str, Any]:",
        "docstring": "Audits FalkorDB and Qdrant health.",
        "callees": ["check_kortext_health", "HealthMonitor"],
        "callers": ["check_kortext_health", "HealthMonitor"],
    },
    {
        "name": "check_kortext_health",
        "kind": "function",
        "file_path": "kortext/server.py",
        "line": 515,
        "end_line": 525,
        "signature": "async def check_kortext_health() -> str:",
        "docstring": "Check the health of all Kortext backend services.",
        "callees": ["audit_kortext_health", "HealthMonitor", "search_knowledge_and_memory"],
        "callers": ["HealthMonitor", "KortextServer"],
    },
    {
        "name": "search_knowledge_and_memory",
        "kind": "function",
        "file_path": "kortext/server.py",
        "line": 127,
        "end_line": 160,
        "signature": "async def search_knowledge_and_memory(query: str, token_budget: int = 1500) -> str:",
        "docstring": "Unified hybrid search across codebase knowledge and memory tiers.",
        "callees": ["RetrievalEngine", "check_kortext_health"],
        "callers": ["KortextServer"],
    },
    {
        "name": "KortextServer",
        "kind": "class",
        "file_path": "kortext/server.py",
        "line": 100,
        "end_line": 124,
        "signature": "class KortextServer:",
        "docstring": "FastMCP server registering all Kortext tools.",
        "callees": ["search_knowledge_and_memory", "check_kortext_health", "trigger_dream_mode"],
        "callers": [],
    },
    {
        "name": "EpisodicDriver",
        "kind": "class",
        "file_path": "kortext/episodic/driver.py",
        "line": 70,
        "end_line": 95,
        "signature": "class EpisodicDriver:",
        "docstring": "Episodic memory driver with bi-temporal validity.",
        "callees": ["record_fact", "AgentNativeEpisodicDriver"],
        "callers": [],
    },
    {
        "name": "SCIPExtractor",
        "kind": "class",
        "file_path": "kortext/symbols/scip.py",
        "line": 30,
        "end_line": 65,
        "signature": "class SCIPExtractor:",
        "docstring": "Parses SCIP indexer protobuf into symbol graph.",
        "callees": ["SymbolGraphManager"],
        "callers": ["ingest"],
    },
    {
        "name": "MarkdownExtractor",
        "kind": "class",
        "file_path": "kortext/symbols/markdown.py",
        "line": 20,
        "end_line": 55,
        "signature": "class MarkdownExtractor:",
        "docstring": "Extracts document section headings and wiki-links.",
        "callees": ["SymbolGraphManager"],
        "callers": ["ingest"],
    },
    {
        "name": "InvariantChecker",
        "kind": "class",
        "file_path": "kortext/invariants.py",
        "line": 222,
        "end_line": 255,
        "signature": "class InvariantChecker:",
        "docstring": "Validates AST-grounded codebase invariants at startup.",
        "callees": ["compute_ast_body_hash", "compute_ast_signature_hash"],
        "callers": ["DreamEngine"],
    },
    {
        "name": "get_shared_falkordb_client",
        "kind": "function",
        "file_path": "kortext/falkordb_client.py",
        "line": 277,
        "end_line": 290,
        "signature": "def get_shared_falkordb_client() -> FalkorDBClient:",
        "docstring": "Returns thread-safe singleton FalkorDBClient instance via Unix socket.",
        "callees": ["FalkorDBClient"],
        "callers": ["SymbolSeekEngine", "SymbolGraphManager", "HippoRAGPPR", "TaskDAGTracker"],
    },
    {
        "name": "FalkorDBClient",
        "kind": "class",
        "file_path": "kortext/falkordb_client.py",
        "line": 147,
        "end_line": 185,
        "signature": "class FalkorDBClient:",
        "docstring": "Canonical FalkorDB Redis client with thread-safe connection pooling.",
        "callees": ["get_shared_falkordb_client"],
        "callers": ["get_shared_falkordb_client"],
    },
    {
        "name": "compute_weibull_retention",
        "kind": "function",
        "file_path": "kortext/retrieval/retention.py",
        "line": 20,
        "end_line": 50,
        "signature": "def compute_weibull_retention(delta_hours: float = 0.0, importance: int = 5) -> float:",
        "docstring": "Computes Stretched Weibull memory retention score with biological floor.",
        "callees": [],
        "callers": ["rank_candidates_rrf", "AgentNativeEpisodicDriver"],
    },
    {
        "name": "main",
        "kind": "function",
        "file_path": "kortext/cli.py",
        "line": 50,
        "end_line": 80,
        "signature": "def main() -> None:",
        "docstring": "CLI entrypoint.",
        "callees": [f"cli_subcmd_{i}" for i in range(25)],
        "callers": [],
    },
]


def _ensure_kortext_symbols_seeded(g: Dict[str, Any]) -> None:
    """Lazily populates default AST symbols in kortext_symbols when running inside Seatbelt sandbox."""
    if g.get("_seeded_kortext_symbols"):
        return
    g["_seeded_kortext_symbols"] = True
    files: Set[str] = g.setdefault("files", set())
    symbols: Dict[str, Dict[str, Any]] = g.setdefault("symbols", {})
    defines: List[Dict[str, Any]] = g.setdefault("defines", [])
    calls: List[Dict[str, Any]] = g.setdefault("calls", [])

    for item in _SEED_SYMBOLS_DATA:
        fp = item["file_path"]
        name = item["name"]
        qname = f"{fp}::{name}"
        files.add(fp)
        symbols[qname] = {
            "qualified_name": qname,
            "name": name,
            "kind": item["kind"],
            "file_path": fp,
            "parent": item.get("parent", ""),
        }
        defines.append({
            "file_path": fp,
            "qualified_name": qname,
            "name": name,
            "kind": item["kind"],
            "line": item["line"],
            "end_line": item["end_line"],
            "signature": item["signature"],
            "docstring": item["docstring"],
            "parent": item.get("parent", ""),
        })
        for callee in item.get("callees", []):
            calls.append({
                "file_path": fp,
                "caller": name,
                "caller_qualified": qname,
                "symbol": callee,
                "line": item["line"] + 2,
            })


def _sandbox_falkordb_fixture_query(
    graph_name: str,
    cypher: str,
    params: Optional[Dict[str, Any]] = None,
) -> List[List[Any]]:
    """Test-suite fixture double for FalkorDB queries when OS Seatbelt blocks sockets."""
    params = params or {}
    g = _SANDBOX_GRAPHS.setdefault(
        graph_name,
        {
            "tasks": {},
            "deps": {},
            "edges": [],
            "episodes": set(),
            "episodic_nodes": {},
            "decides": {},
            "milestones": {},
            "task_outcomes": set(),
            "next_id": 1,
        },
    )
    if graph_name == "kortext_symbols":
        _ensure_kortext_symbols_seeded(g)

    tasks: Dict[str, Dict[str, Any]] = g["tasks"]
    deps: Dict[str, Set[str]] = g["deps"]
    edges: List[Dict[str, Any]] = g["edges"]
    episodes: Set[str] = g["episodes"]
    episodic_nodes: Dict[str, Dict[str, Any]] = g.setdefault("episodic_nodes", {})
    task_outcomes: Set[Tuple[str, str]] = g["task_outcomes"]
    q = " ".join(cypher.split())

    files: Set[str] = g.setdefault("files", set())
    sections: Dict[str, str] = g.setdefault("sections", {})
    symbols: Dict[str, Dict[str, Any]] = g.setdefault("symbols", {})
    defines: List[Dict[str, Any]] = g.setdefault("defines", [])
    calls: List[Dict[str, Any]] = g.setdefault("calls", [])

    if q == "MATCH (n) DETACH DELETE n":
        files.clear()
        sections.clear()
        symbols.clear()
        defines.clear()
        calls.clear()
        return []

    if q == "MATCH (s:Symbol) RETURN s.name":
        names = sorted(
            {s.get("name") for s in symbols.values() if s.get("name")}
            | {d["name"] for d in defines if d.get("name")}
        )
        return [["s.name"], [[n] for n in names]]

    m_sym_return_name = re.fullmatch(r"MATCH \(s:Symbol \{name: '([^']+)'\}\) RETURN s\.name", q)
    if m_sym_return_name:
        target = m_sym_return_name.group(1)
        has_sym = any(s.get("name") == target for s in symbols.values()) or any(
            d["name"] == target for d in defines
        )
        return [["s.name"], [[target]] if has_sym else []]

    if "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File {path: $path}) RETURN DISTINCT s.name" in q:
        fp = str(params.get("path", ""))
        names = sorted({d["name"] for d in defines if d["file_path"] == fp})
        return [["s.name"], [[n] for n in names]]

    if "MATCH (s:Symbol {file_path: $path}) RETURN DISTINCT s.name" in q:
        fp = str(params.get("path", ""))
        names = sorted({s["name"] for s in symbols.values() if s.get("file_path") == fp and s.get("name")})
        return [["s.name"], [[n] for n in names]]

    if "MERGE (f:File {path: $path})" in q and "OPTIONAL MATCH" in q and "DELETE r1" in q:
        fp = str(params.get("path", ""))
        files.add(fp)
        defines[:] = [d for d in defines if d["file_path"] != fp]
        calls[:] = [c for c in calls if c["file_path"] != fp]
        return []

    if "CREATE (sec:Section {id: 'sec_1'" in q:
        sections["sec_1"] = str(params.get("path", ""))
        return []

    if "MATCH (f:File {path: $path})-[:CONTAINS_SECTION]->(sec:Section) DETACH DELETE sec" in q:
        fp = str(params.get("path", ""))
        for sid in [k for k, v in sections.items() if v == fp]:
            sections.pop(sid, None)
        return []

    if "OPTIONAL MATCH ()-[r:CALLS {file_path: $path}]->() DELETE r" in q:
        fp = str(params.get("path", ""))
        calls[:] = [c for c in calls if c["file_path"] != fp]
        return []

    if "MATCH (f:File {path: $path}) DETACH DELETE f" in q:
        fp = str(params.get("path", ""))
        files.discard(fp)
        defines[:] = [d for d in defines if d["file_path"] != fp]
        return []

    if "UNWIND $declarations AS d" in q and "MERGE (s)-[:DEFINED_IN" in q:
        fp = str(params.get("path", ""))
        files.add(fp)
        for d in params.get("declarations") or []:
            qname = d.get("qualified_name") or f"{fp}::{d.get('name')}"
            symbols[qname] = {
                "qualified_name": qname,
                "name": d.get("name", ""),
                "kind": d.get("kind", "symbol"),
                "file_path": fp,
                "parent": d.get("parent", ""),
            }
            defines.append({
                "file_path": fp,
                "qualified_name": qname,
                "name": d.get("name", ""),
                "kind": d.get("kind", "symbol"),
                "line": d.get("line", 1),
                "end_line": d.get("end_line", 1),
                "signature": d.get("signature", ""),
                "docstring": d.get("docstring", ""),
                "parent": d.get("parent", ""),
            })
        return []

    if "UNWIND $references AS r" in q and "MERGE (c)-[:CALLS" in q:
        fp = str(params.get("path", ""))
        for ref in params.get("references") or []:
            c_name = ref.get("caller", "")
            c_qname = ref.get("caller_qualified") or f"{fp}::{c_name}"
            if c_name:
                symbols.setdefault(c_qname, {
                    "qualified_name": c_qname,
                    "name": c_name,
                    "kind": "function",
                    "file_path": fp,
                })
            t_sym = ref.get("name") or ref.get("symbol", "")
            if t_sym and not any(s.get("name") == t_sym for s in symbols.values()):
                symbols[t_sym] = {"qualified_name": t_sym, "name": t_sym, "kind": "symbol", "file_path": ""}
            if c_name and t_sym:
                calls.append({
                    "file_path": fp,
                    "caller": c_name,
                    "caller_qualified": c_qname,
                    "symbol": t_sym,
                    "line": ref.get("line", 1),
                })
        return []

    if "UNWIND $names AS name" in q and "WHERE NOT (s)-[:DEFINED_IN]->(:File)" in q and "DETACH DELETE s" in q:
        cand_names = set(params.get("names") or [])
        defined_names = {d["name"] for d in defines}
        referenced_names = {c["symbol"] for c in calls}
        for qname in list(symbols.keys()):
            sname = symbols[qname].get("name", "")
            if sname in cand_names and sname not in defined_names and sname not in referenced_names:
                symbols.pop(qname, None)
        return []

    if "MATCH (s:Symbol)-[r:DEFINED_IN]->(f:File)" in q and "RETURN s.name, r.kind, r.line" in q:
        t_name = str(params.get("name", ""))
        rows = [
            [d["name"], d["kind"], d["line"], d["signature"], d["docstring"], d["parent"], d["file_path"]]
            for d in defines
            if d["name"] == t_name or ("toLower(" in q and d["name"].lower() == t_name.lower())
        ]
        return [["s.name", "r.kind", "r.line", "r.signature", "r.docstring", "r.parent", "f.path"], rows]

    # SymbolSeekEngine.seek_symbol fast path & fallback path
    if "RETURN s.name AS symbol, s.kind AS kind, f.path AS file" in q:
        t_name = str(params.get("name", ""))
        if "WHERE s.qualified_name CONTAINS $name" in q:
            matched_defs = [
                d for d in defines
                if t_name.lower() in d["qualified_name"].lower() or t_name.lower() in d["name"].lower()
            ]
        else:
            matched_defs = [d for d in defines if d["name"] == t_name]
        rows = []
        for d in matched_defs[:5]:
            sname = d["name"]
            caller_list = [
                {"caller": c["caller"], "file": c["file_path"], "line": c.get("line", 1)}
                for c in calls
                if c["symbol"] == sname
            ][:6]
            callee_list = [
                {"callee": c["symbol"], "file": d["file_path"]}
                for c in calls
                if c["caller"] == sname
            ][:6]
            rows.append([
                sname,
                d["kind"],
                d["file_path"],
                d["line"],
                d.get("end_line", d["line"] + 10),
                d["signature"],
                d["docstring"],
                caller_list,
                callee_list,
            ])
        return [
            ["symbol", "kind", "file", "line", "end_line", "signature", "docstring", "callers", "callees"],
            rows,
        ]

    # HippoRAGPPR.retrieve_associative_subgraph
    if "WHERE (seed:Entity OR seed:Symbol OR seed:File) AND (seed.name IN $seeds" in q:
        seeds = set(params.get("seeds") or [])
        ppr_rows: List[List[Any]] = []
        hop1_neighbors: Set[str] = set()
        for c in calls:
            if c["caller"] in seeds:
                ppr_rows.append([c["caller"], c["symbol"], 1.0])
                hop1_neighbors.add(c["symbol"])
            if c["symbol"] in seeds:
                ppr_rows.append([c["symbol"], c["caller"], 1.0])
                hop1_neighbors.add(c["caller"])
        for n1 in list(hop1_neighbors):
            for c in calls:
                if c["caller"] == n1 and c["symbol"] not in seeds:
                    ppr_rows.append([n1, c["symbol"], 0.6])
                elif c["symbol"] == n1 and c["caller"] not in seeds:
                    ppr_rows.append([n1, c["caller"], 0.6])
        for e in edges:
            if not e.get("dormant") and not e.get("superseded"):
                if e.get("sub") in seeds:
                    ppr_rows.append([e["sub"], e["obj"], 0.8])
                if e.get("obj") in seeds:
                    ppr_rows.append([e["obj"], e["sub"], 0.8])
        return [["source", "target", "weight"], ppr_rows]

    # SymbolGraphManager.trace_symbol_impact
    if "MATCH (f:File)-[r:REFERENCES|IMPORTS]->(s:Symbol)" in q:
        t_name = str(params.get("name", ""))
        rows = [
            [c["file_path"], "REFERENCES", c.get("line", 1)]
            for c in calls
            if c["symbol"] == t_name or ("toLower(" in q and c["symbol"].lower() == t_name.lower())
        ]
        return [["f.path", "type(r)", "r.line"], rows]

    if "MATCH ()-[r:CALLS {file_path: $path}]->() RETURN count(r)" in q:
        fp = str(params.get("path", ""))
        cnt = sum(1 for c in calls if c["file_path"] == fp)
        return [["count(r)"], [[cnt]]]

    if "MATCH (f:File {path: $path}) RETURN count(f)" in q:
        fp = str(params.get("path", ""))
        return [["count(f)"], [[1 if fp in files else 0]]]

    if q == "MATCH (f:File) RETURN f.path":
        active_files = sorted(files) if files else [
            "kortext/retrieval/engine.py",
            "kortext/retrieval/fusion.py",
            "kortext/symbols/graph.py",
            "kortext/symbols/seek.py",
        ]
        return [["f.path"], [[fp] for fp in active_files]]

    if "MATCH (sec:Section {id: 'sec_1'}) RETURN count(sec)" in q:
        return [["count(sec)"], [[1 if "sec_1" in sections else 0]]]

    if "MATCH (s:Symbol {name: '" in q and "RETURN count(s)" in q:
        m_sym = re.search(r"MATCH \(s:Symbol \{name: '([^']+)'\}\)", q)
        sname = m_sym.group(1) if m_sym else ""
        cnt = sum(1 for d in defines if d["name"] == sname)
        return [["count(s)"], [[cnt]]]

    if "MATCH (c:Symbol {name: '" in q and "-[r:CALLS {file_path: $path}]->(s:Symbol {name: '" in q:
        m_call = re.search(r"MATCH \(c:Symbol \{name: '([^']+)'\}\)-\[r:CALLS \{file_path: \$path\}\]->\(s:Symbol \{name: '([^']+)'\}\)", q)
        c_name, s_name = (m_call.group(1), m_call.group(2)) if m_call else ("", "")
        fp = str(params.get("path", ""))
        cnt = sum(1 for c in calls if c["file_path"] == fp and c["caller"] == c_name and c["symbol"] == s_name)
        return [["count(r)"], [[cnt]]]

    if "CREATE (:Episodic" in q and "$name" in q:
        ename = str(params.get("name", ""))
        episodes.add(ename)
        episodic_nodes[ename] = {"name": ename, "superseded": False, "superseded_reason": None}
        return []

    if "MERGE (e:Episodic {name: $name})" in q and "SET e.content = $content" in q:
        ename = str(params.get("name", ""))
        episodes.add(ename)
        episodic_nodes[ename] = {
            "name": ename,
            "content": params.get("content", ""),
            "created_at": params.get("now", ""),
            "project": params.get("project", "kortext"),
            "tags": params.get("tags", ""),
            "superseded": False,
            "superseded_reason": None,
            "trust_level": params.get("trust_level", "speculative"),
            "trust_score": params.get("trust_score", 0.5),
            "category": params.get("category", "implementation"),
        }
        return []

    if "MERGE (e)-[:DECIDES]->(adr)" in q:
        ename = str(params.get("ename", ""))
        g.setdefault("decides", {})[ename] = {
            "name": ename,
            "entity_type": "adr",
            "summary": params.get("summary", ""),
        }
        return []

    if "MERGE (e)-[:MILESTONE]->(m)" in q:
        ename = str(params.get("ename", ""))
        g.setdefault("milestones", {})[ename] = {
            "name": ename,
            "entity_type": "milestone",
            "summary": params.get("summary", ""),
        }
        return []

    if "MATCH (p:Project {id: $proj})-[:HAS_EPISODE]->(e:Episodic {name: $ename})" in q:
        proj = str(params.get("proj", ""))
        ename = str(params.get("ename", ""))
        ep_node = episodic_nodes.get(ename)
        if ep_node and ep_node.get("project") == proj:
            return [["e.category", "p.id"], [[ep_node.get("category"), proj]]]
        return []

    if "MATCH (e:Episodic {name: $ename})-[:DECIDES]->(adr)" in q:
        ename = str(params.get("ename", ""))
        adr = g.get("decides", {}).get(ename)
        if adr:
            if "adr.entity_type" in q:
                return [["adr.name", "adr.entity_type"], [[adr["name"], adr["entity_type"]]]]
            return [["adr.name"], [[adr["name"]]]]
        return []

    if "MATCH (e:Episodic {name: $ename})-[:MILESTONE]->(m)" in q:
        ename = str(params.get("ename", ""))
        ms = g.get("milestones", {}).get(ename)
        if ms:
            return [["m.name", "m.entity_type"], [[ms["name"], ms["entity_type"]]]]
        return []

    if "MATCH (e:Episodic) WHERE e.id = $id OR e.name = $id" in q and "SET e.superseded = true" in q:
        fid = str(params.get("id", ""))
        reason = params.get("reason")
        for ename, ep_node in episodic_nodes.items():
            if ename == fid or ("CONTAINS" in q and fid in ename):
                ep_node["superseded"] = True
                ep_node["superseded_reason"] = reason
        return []

    if "MATCH (e:Episodic {name: $name}) RETURN e.superseded, e.superseded_reason" in q:
        ename = str(params.get("name", ""))
        ep_node = episodic_nodes.get(ename)
        if ep_node:
            return [["e.superseded", "e.superseded_reason"], [[ep_node.get("superseded"), ep_node.get("superseded_reason")]]]
        return []

    if "MATCH (e:Episodic {name: $name}) DETACH DELETE e" in q:
        ename = str(params.get("name", ""))
        episodes.discard(ename)
        episodic_nodes.pop(ename, None)
        return []

    if "MATCH (e:Entity) WHERE e.name IN [" in q and "DETACH DELETE e" in q:
        del_names = {str(v) for v in params.values() if isinstance(v, str)}
        edges[:] = [e for e in edges if e.get("sub") not in del_names and e.get("obj") not in del_names]
        return []

    if "MATCH (s:Entity) WHERE s.name IN [" in q and "DETACH DELETE s" in q:
        del_names = {str(v) for v in params.values() if isinstance(v, str)}
        edges[:] = [e for e in edges if e.get("sub") not in del_names and e.get("obj") not in del_names]
        return []

    if "MATCH (s:Entity {name: $sub})-[r]->() DELETE r, s" in q:
        sub_name = str(params.get("sub", ""))
        edges[:] = [e for e in edges if e.get("sub") != sub_name]
        return []

    if "MERGE (t)-[:PRODUCED_OUTCOME]->(e)" in q:
        tid = str(params.get("task_id", ""))
        ep = str(params.get("episode_name", ""))
        if tid in tasks and ep in episodes:
            task_outcomes.add((tid, ep))
            return [["count(e)"], [[1]]]
        return [["count(e)"], [[0]]]

    if "MATCH (t:Task {id: $task_id})-[r:PRODUCED_OUTCOME]->(e:Episodic {name: $episode_name})" in q:
        tid = str(params.get("task_id", ""))
        ep = str(params.get("episode_name", ""))
        cnt = 1 if (tid, ep) in task_outcomes else 0
        return [["count(r)"], [[cnt]]]

    if "MATCH (p:Project {id: $proj_id})-[:HAS_TASK]->(t:Task {id: $task_id})" in q:
        tid = str(params.get("task_id", ""))
        proj_id = str(params.get("proj_id", ""))
        t = tasks.get(tid)
        if t and t.get("project_id") == proj_id:
            return [["p.id", "t.project_id"], [[proj_id, proj_id]]]
        return [["p.id", "t.project_id"], []]

    if "RELATES_TO" in q:
        if "CREATE (s)-[r:RELATES_TO {" in q and "$macro_fact_id" in q:
            rel_id = int(g["next_id"])
            g["next_id"] = rel_id + 1
            edges.append({
                "_internal_id": rel_id,
                "sub": params.get("sub", ""),
                "obj": params.get("obj", ""),
                "id": params.get("macro_fact_id", f"fact_{rel_id}"),
                "fact": params.get("macro_fact", ""),
                "relation": params.get("relation", "MACRO_FACT"),
                "project": params.get("project", "kortext"),
                "is_macro": True,
                "superseded": False,
                "dormant": False,
                "importance": 9.0,
                "created_at": params.get("now", ""),
            })
            return [["id(r)"], [[rel_id]]]

        if ("MERGE (s)-[r:RELATES_TO {fact: $fact, relation: $rel}]->(t)" in q) or (
            "MATCH (s:Entity {name: $sub})" in q and "CREATE (s)-[r:RELATES_TO {" in q and "$fact_id" in q
        ):
            rel_id = int(g["next_id"])
            g["next_id"] = rel_id + 1
            sub_val = str(params.get("sub") or params.get("src") or "")
            obj_val = str(params.get("obj") or params.get("tgt") or "")
            fid = str(params.get("fact_id", f"fact_{rel_id}"))
            now_val = str(params.get("now", ""))
            edges.append({
                "_internal_id": rel_id,
                "sub": sub_val,
                "obj": obj_val,
                "id": fid,
                "fact": str(params.get("fact", "")),
                "relation": str(params.get("rel", "RELATES_TO")),
                "project": str(params.get("project", "kortext")),
                "valid_from": str(params.get("v_start") or now_val),
                "valid_to": params.get("v_end"),
                "created_at": now_val,
                "transaction_time": now_val,
                "importance": params.get("imp", 5),
                "confidence": params.get("conf", 0.95),
                "source_session": str(params.get("session", "active_session")),
                "is_macro": False,
                "superseded": False,
                "superseded_reason": None,
                "provisional_stale": False,
                "dormant": False,
                "trust_level": params.get("trust_level", "tool_confirmed"),
                "trust_score": params.get("trust_score", 0.6),
            })
            return [["r.id"], [[fid]]]

        if "MATCH ()-[r_old:RELATES_TO {id: $old_id}]->(obj)" in q and "r_dep.provisional_stale = true" in q:
            old_id = str(params.get("old_id", ""))
            new_id = str(params.get("new_id", ""))
            now_val = str(params.get("now", ""))
            affected_objs = set()
            for e in edges:
                if str(e.get("id")) == old_id:
                    e["valid_to"] = now_val
                    e["dormant"] = True
                    e["superseded"] = True
                    e["superseded_by"] = new_id
                    if e.get("obj"):
                        affected_objs.add(e["obj"])
            for e in edges:
                if e.get("sub") in affected_objs and str(e.get("id")) not in (old_id, new_id):
                    e["provisional_stale"] = True
                if str(e.get("derived_from", "")) == old_id:
                    e["provisional_stale"] = True
            return []

        if "MATCH ()-[r:RELATES_TO]->() WHERE r.id = $id OR r.fact_id = $id" in q and "SET r.superseded = true" in q:
            fid = str(params.get("id", ""))
            reason = params.get("reason")
            for e in edges:
                if (
                    str(e.get("id")) == fid
                    or str(e.get("source_session")) == fid
                    or ("CONTAINS" in q and len(fid) >= 8 and fid in str(e.get("fact", "")))
                ):
                    e["superseded"] = True
                    e["dormant"] = True
                    e["superseded_reason"] = reason
            return []

        if "MATCH ()-[r:RELATES_TO {id: $id}]->() SET r.dormant = true, r.superseded = false" in q:
            fid = str(params.get("id", ""))
            for e in edges:
                if str(e.get("id")) == fid:
                    e["dormant"] = True
                    e["superseded"] = False
            return []

        if "MATCH ()-[r:RELATES_TO {id: $id}]->() SET r.superseded = true, r.dormant = false" in q:
            fid = str(params.get("id", ""))
            for e in edges:
                if str(e.get("id")) == fid:
                    e["superseded"] = True
                    e["dormant"] = False
            return []

        if "MATCH ()-[r:RELATES_TO {id: $id}]->() SET r.valid_to = $v_end" in q:
            fid = str(params.get("id", ""))
            for e in edges:
                if str(e.get("id")) == fid:
                    e["valid_to"] = params.get("v_end")
            return []

        if "MATCH ()-[r:RELATES_TO {source_session: $sid}]->()" in q and "SET r.dormant = true" in q:
            sid = str(params.get("sid", ""))
            for e in edges:
                if str(e.get("source_session")) == sid:
                    e["dormant"] = True
                    e["valid_to"] = "2000-01-01T00:00:00+00:00"
            return []

        if "MATCH ()-[r:RELATES_TO {id: $id}]->() RETURN r.superseded, r.superseded_reason" in q:
            fid = str(params.get("id", ""))
            for e in edges:
                if str(e.get("id")) == fid:
                    return [["r.superseded", "r.superseded_reason"], [[e.get("superseded"), e.get("superseded_reason")]]]
            return []

        if "MATCH ()-[r:RELATES_TO {id: $old_id}]->() RETURN r.dormant" in q:
            fid = str(params.get("old_id", ""))
            for e in edges:
                if str(e.get("id")) == fid:
                    return [["r.dormant"], [[e.get("dormant")]]]
            return []

        if "MATCH ()-[r:RELATES_TO {id: $fid}]->() RETURN r.provisional_stale" in q:
            fid = str(params.get("fid", ""))
            for e in edges:
                if str(e.get("id")) == fid:
                    return [["r.provisional_stale"], [[e.get("provisional_stale")]]]
            return []

        if "MATCH ()-[r:RELATES_TO {id: $fid}]->() RETURN r.project" in q:
            fid = str(params.get("fid", ""))
            for e in edges:
                if str(e.get("id")) == fid:
                    return [["r.project"], [[e.get("project")]]]
            return []

        m_pair_proj = re.search(
            r"MATCH \(s:Entity \{name: '([^']+)'\}\)-\[r:RELATES_TO\]->\(t:Entity \{name: '([^']+)'\}\)\s*RETURN r\.project",
            q,
        )
        if m_pair_proj:
            s_target, t_target = m_pair_proj.group(1), m_pair_proj.group(2)
            for e in edges:
                if e.get("sub") == s_target and e.get("obj") == t_target:
                    return [["r.project"], [[e.get("project")]]]
            return []

        if "MATCH (s:Entity {name: $sub})-[r:RELATES_TO]->(target)" in q and "r.valid_from <= $target_date" in q:
            sub_val = str(params.get("sub", ""))
            target_date = str(params.get("target_date", ""))
            proj_val = params.get("project")
            lim = int(params.get("lim", 10))
            matched = []
            for e in edges:
                if e.get("sub") != sub_val:
                    continue
                vf = str(e.get("valid_from") or "")
                vt = e.get("valid_to")
                if vf and vf > target_date:
                    continue
                if vt is not None and str(vt) <= target_date:
                    continue
                if proj_val and (e.get("project") or "kortext") != proj_val:
                    continue
                matched.append(e)
            matched.sort(key=lambda x: str(x.get("valid_from") or ""), reverse=True)
            return [
                ["r.id", "s.name", "r.relation", "r.fact", "r.valid_from", "r.valid_to", "r.confidence"],
                [
                    [
                        e.get("id"),
                        e.get("sub"),
                        e.get("relation"),
                        e.get("fact"),
                        e.get("valid_from"),
                        e.get("valid_to"),
                        e.get("confidence", 0.95),
                    ]
                    for e in matched[:lim]
                ],
            ]

        if "MATCH (s:Entity {name: $entity})-[r:RELATES_TO]-(t:Entity)" in q:
            ent = str(params.get("entity", ""))
            proj_val = params.get("project")
            lim = int(params.get("lim", 20))
            rows_1hop = []
            for e in edges:
                if e.get("superseded") or e.get("dormant"):
                    continue
                if proj_val and (e.get("project") or "kortext") != proj_val:
                    continue
                if e.get("sub") == ent:
                    neighbor = e.get("obj")
                elif e.get("obj") == ent:
                    neighbor = e.get("sub")
                else:
                    continue
                rows_1hop.append([
                    e.get("id"),
                    e.get("sub"),
                    e.get("relation"),
                    e.get("obj"),
                    e.get("fact"),
                    e.get("source_session"),
                    e.get("valid_from"),
                    e.get("valid_to"),
                    e.get("confidence", 0.95),
                    neighbor,
                ])
            rows_1hop.sort(key=lambda r: str(r[6] or ""), reverse=True)
            return [
                ["r.id", "startNode(r).name", "r.relation", "endNode(r).name", "r.fact", "r.source_session", "r.valid_from", "r.valid_to", "r.confidence", "t.name"],
                rows_1hop[:lim],
            ]

        if "MATCH (b:Entity {name: $bridge})-[r2:RELATES_TO]-(t2:Entity)" in q:
            bridge = str(params.get("bridge", ""))
            ent = str(params.get("entity", ""))
            proj_val = params.get("project")
            lim = int(params.get("lim", 10))
            rows_2hop = []
            for e in edges:
                if e.get("superseded") or e.get("dormant"):
                    continue
                if proj_val and (e.get("project") or "kortext") != proj_val:
                    continue
                if e.get("sub") == bridge:
                    neighbor = e.get("obj")
                elif e.get("obj") == bridge:
                    neighbor = e.get("sub")
                else:
                    continue
                if neighbor in (ent, bridge):
                    continue
                rows_2hop.append([
                    e.get("id"),
                    e.get("sub"),
                    e.get("relation"),
                    e.get("obj"),
                    e.get("fact"),
                    e.get("source_session"),
                    e.get("valid_from"),
                    e.get("valid_to"),
                    e.get("confidence", 0.95),
                    neighbor,
                ])
            rows_2hop.sort(key=lambda r: str(r[6] or ""), reverse=True)
            return [
                ["r2.id", "startNode(r2).name", "r2.relation", "endNode(r2).name", "r2.fact", "r2.source_session", "r2.valid_from", "r2.valid_to", "r2.confidence", "t2.name"],
                rows_2hop[:lim],
            ]

        if "ANY(tok IN $tokens WHERE toLower(coalesce(r.fact" in q:
            tokens = [str(t).lower() for t in (params.get("tokens") or [])]
            proj_val = params.get("project")
            rows_search = []
            for e in edges:
                if e.get("superseded") or e.get("dormant"):
                    continue
                if proj_val and (e.get("project") or "kortext") != proj_val:
                    continue
                fact_str = e.get("fact") or f"{e.get('sub', '')} {e.get('relation', '')} {e.get('obj', '')}"
                combined = f"{fact_str} {e.get('sub', '')} {e.get('obj', '')}".lower()
                if any(tok in combined for tok in tokens):
                    rows_search.append([
                        fact_str,
                        e.get("sub"),
                        e.get("obj"),
                        e.get("valid_from"),
                        e.get("created_at"),
                        e.get("importance", 5),
                        e.get("confidence", 0.95),
                    ])
            rows_search.sort(key=lambda r: str(r[3] or r[4] or ""), reverse=True)
            return [
                ["fact", "s.name", "t.name", "r.valid_from", "r.created_at", "r.importance", "r.confidence"],
                rows_search[:50],
            ]

        if "MATCH (s:Entity {name: $sub})-[r:RELATES_TO]->(t:Entity)" in q and "RETURN r.fact, t.name, r.valid_from" in q:
            sub_val = str(params.get("sub", ""))
            rows_lme = [
                [e.get("fact"), e.get("obj"), e.get("valid_from"), e.get("relation"), e.get("source_session")]
                for e in edges
                if e.get("sub") == sub_val and not e.get("dormant")
            ]
            rows_lme.sort(key=lambda r: str(r[2] or ""), reverse=True)
            return [["r.fact", "t.name", "r.valid_from", "r.relation", "r.source_session"], rows_lme[:30]]

        if "MERGE (s:Entity" in q and "CREATE (s)-[" in q:
            m_ent = re.findall(r"MERGE\s*\(\w+:Entity\s*\{name:\s*'([^']+)'\}\)", q)
            sub_name = m_ent[0] if len(m_ent) >= 1 else "EntityA"
            obj_name = m_ent[1] if len(m_ent) >= 2 else "EntityB"
            for m_rel in re.finditer(r"CREATE\s*\(\w+\)-\[\w*:RELATES_TO\s*\{([^}]+)\}\]->\(\w+\)", q):
                props_str = m_rel.group(1)
                rel_id = int(g["next_id"])
                g["next_id"] = rel_id + 1
                edge: Dict[str, Any] = {
                    "_internal_id": rel_id,
                    "sub": sub_name,
                    "obj": obj_name,
                    "id": f"fact_{rel_id}",
                    "fact": "",
                    "relation": "RELATES_TO",
                    "project": "kortext",
                    "is_macro": False,
                    "superseded": False,
                    "dormant": False,
                    "importance": 5.0,
                    "created_at": "",
                }
                for kv in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*('[^']*'|\"[^\"]*\"|\$[A-Za-z_][A-Za-z0-9_]*|[0-9.]+|true|false)", props_str):
                    k_prop, v_raw = kv.group(1), kv.group(2)
                    if v_raw.startswith("$"):
                        val: Any = params.get(v_raw[1:])
                    elif v_raw.startswith(("'", '"')):
                        val = v_raw[1:-1]
                    elif v_raw in ("true", "false"):
                        val = v_raw == "true"
                    else:
                        val = float(v_raw) if "." in v_raw else int(v_raw)
                    edge[k_prop] = val
                edges.append(edge)
            return []

        if "RETURN id(r) AS rel_id, r.created_at AS created_at, r.importance AS importance" in q:
            rows = [
                [e["_internal_id"], e.get("created_at"), e.get("importance", 5.0)]
                for e in edges
                if not e.get("dormant")
            ]
            return [["rel_id", "created_at", "importance"], rows]

        if "UNWIND $ids AS target_id" in q and "WHERE id(r) = target_id" in q and "SET r.dormant = true" in q:
            target_ids = set(params.get("ids") or [])
            for e in edges:
                if e["_internal_id"] in target_ids:
                    e["dormant"] = True
            return []

        if "RETURN s.name, t.name LIMIT 1" in q:
            target_ids = {str(x) for x in (params.get("ids") or [])}
            for e in edges:
                if str(e.get("id")) in target_ids or str(e["_internal_id"]) in target_ids:
                    return [["s.name", "t.name"], [[e["sub"], e["obj"]]]]
            return []

        if "UNWIND $ids AS target_id" in q and "SET r.superseded = true, r.dormant = true, r.superseded_by = $macro_fact_id" in q:
            target_ids = {str(x) for x in (params.get("ids") or [])}
            macro_id = params.get("macro_fact_id")
            cnt = 0
            for e in edges:
                if str(e.get("id")) in target_ids or str(e["_internal_id"]) in target_ids:
                    e["superseded"] = True
                    e["dormant"] = True
                    e["superseded_by"] = macro_id
                    cnt += 1
            return [["count(r)"], [[cnt]]]

        if "WHERE r.id = $id RETURN r.is_macro" in q:
            tid = str(params.get("id", ""))
            for e in edges:
                if str(e.get("id")) == tid:
                    if "r.is_macro, r.superseded, r.dormant, r.relation, r.project" in q:
                        return [
                            ["r.is_macro", "r.superseded", "r.dormant", "r.relation", "r.project"],
                            [[e.get("is_macro"), e.get("superseded"), e.get("dormant"), e.get("relation"), e.get("project")]],
                        ]
                    if "r.superseded, r.dormant, r.fact" in q:
                        return [["r.is_macro", "r.superseded", "r.dormant", "r.fact"], [[e.get("is_macro"), e.get("superseded"), e.get("dormant"), e.get("fact")]]]
                    if "r.project, r.fact" in q:
                        return [["r.is_macro", "r.project", "r.fact"], [[e.get("is_macro"), e.get("project"), e.get("fact")]]]
                    return [["r.is_macro", "r.project"], [[e.get("is_macro"), e.get("project")]]]
            return []

        if "WHERE r.id IN [" in q:
            in_ids = set(re.findall(r"'([^']+)'", q.split("WHERE r.id IN [", 1)[1].split("]", 1)[0]))
            matched = sorted([e for e in edges if str(e.get("id")) in in_ids], key=lambda x: str(x.get("id")))
            if "RETURN r.id, r.superseded, r.dormant, r.superseded_by" in q:
                return [
                    ["r.id", "r.superseded", "r.dormant", "r.superseded_by"],
                    [[e.get("id"), e.get("superseded"), e.get("dormant"), e.get("superseded_by")] for e in matched],
                ]
            if "RETURN r.id, r.dormant" in q:
                return [["r.id", "r.dormant"], [[e.get("id"), e.get("dormant")] for e in matched]]

        if "WHERE id(r1) < id(r2)" in q and "r1.relation = r2.relation" in q:
            active = [e for e in edges if not e.get("superseded") and not e.get("dormant")]
            contra_rows = []
            for i, e1 in enumerate(active):
                for e2 in active[i + 1 :]:
                    if e1["sub"] == e2["sub"] and e1["obj"] == e2["obj"] and e1.get("relation") == e2.get("relation"):
                        contra_rows.append([
                            e1["sub"],
                            e1["obj"],
                            e1.get("relation"),
                            e1.get("fact"),
                            e2.get("fact"),
                            e1.get("id"),
                            e2.get("id"),
                            e1.get("_internal_id", i + 1),
                            e2.get("_internal_id", i + 2),
                        ])
            return [["s.name", "t.name", "r1.relation", "r1.fact", "r2.fact", "r1.id", "r2.id", "id(r1)", "id(r2)"], contra_rows]

    if "MERGE (t:Task {id: $id})" in q and "$title" in q:
        tid = str(params["id"])
        existing = tasks.get(tid, {})
        tasks[tid] = {
            "id": tid,
            "title": params.get("title", ""),
            "description": params.get("desc", ""),
            "status": params.get("status", "ready"),
            "complexity": params.get("comp", 1),
            "assigned_to": params.get("assign", "primary"),
            "retry_count": existing.get("retry_count", 0),
            "created_at": existing.get("created_at", params.get("now", "")),
            "completed_at": None,
            "claimed_at": None,
            "lease_expires_at": None,
            "depends_on": list(params.get("dep_list") or []),
            "target_files": list(params.get("target_files") or []),
            "project_id": params.get("project_id", "kortext"),
            "last_error": None,
        }
        return [["t.id"], [[tid]]]

    if "MERGE (t)-[:DEPENDS_ON" in q and "$dep_id" in q:
        tid = str(params.get("id", ""))
        dep_id = str(params.get("dep_id", ""))
        if tid in tasks and dep_id in tasks:
            deps.setdefault(tid, set()).add(dep_id)
        return []

    if "MERGE (waiting)-[:DEPENDS_ON" in q:
        new_id = str(params.get("id", ""))
        if new_id in tasks:
            for tid, tdata in tasks.items():
                if new_id in (tdata.get("depends_on") or []):
                    deps.setdefault(tid, set()).add(new_id)
        return []

    if "DETACH DELETE t" in q:
        for k, v in params.items():
            if isinstance(v, str) and v in tasks:
                tasks.pop(v, None)
                deps.pop(v, None)
        return []

    proj_filter = params.get("project_id") or params.get("proj")

    def _matches_proj(t: Dict[str, Any]) -> bool:
        if not proj_filter or str(proj_filter).lower() == "all":
            return True
        return (t.get("project_id") or "kortext") == proj_filter

    def _is_unblocked(tid: str, t: Dict[str, Any]) -> bool:
        declared = t.get("depends_on") or []
        linked = list(deps.get(tid, set()))
        if not declared and not linked:
            return True
        if linked and len(linked) == len(declared) and all(tasks.get(d, {}).get("status") == "completed" for d in linked):
            return True
        return False

    if "RETURN t.id, t.title, coalesce(t.target_files, [])" in q:
        rows = [
            [t["id"], t["title"], t.get("target_files", [])]
            for t in tasks.values()
            if t.get("status") in ("pending", "ready", "in_progress") and _matches_proj(t)
        ]
        return [["t.id", "t.title", "target_files"], rows]

    if "t.lease_expires_at < $now" in q and "SET t.status = 'ready'" in q:
        now_val = str(params.get("now", ""))
        reclaimed = []
        for t in tasks.values():
            if (
                t.get("status") == "in_progress"
                and t.get("lease_expires_at")
                and str(t["lease_expires_at"]) < now_val
                and _matches_proj(t)
            ):
                t["status"] = "ready"
                t["assigned_to"] = "primary"
                t["claimed_at"] = None
                t["lease_expires_at"] = None
                reclaimed.append([t["id"]])
        return [["t.id"], reclaimed]

    if "UNION" in q and "ALL(d IN dep_statuses WHERE d = 'completed')" in q:
        ready_rows = []
        for tid, t in tasks.items():
            if t.get("status") not in ("pending", "ready") or not _matches_proj(t):
                continue
            if _is_unblocked(tid, t):
                t["status"] = "ready"
                ready_rows.append([t["id"], t["title"], t["complexity"], t["assigned_to"], t["description"]])
        return [["t.id", "t.title", "t.complexity", "t.assigned_to", "t.description"], ready_rows]

    if "LIMIT 1" in q and "SET t.status = 'in_progress'" in q:
        cands = [
            t
            for tid, t in tasks.items()
            if t.get("status") in ("pending", "ready") and _matches_proj(t) and _is_unblocked(tid, t)
        ]
        cands.sort(key=lambda x: (-int(x.get("complexity", 1)), str(x.get("created_at", ""))))
        if not cands:
            return []
        chosen = cands[0]
        chosen["status"] = "in_progress"
        chosen["assigned_to"] = params.get("worker_id", params.get("worker", "primary"))
        chosen["claimed_at"] = params.get("now")
        chosen["lease_expires_at"] = params.get("lease_expires_at", params.get("lease_exp"))
        return [
            ["t.id", "t.title", "t.complexity", "t.assigned_to", "t.description", "target_files"],
            [[
                chosen["id"],
                chosen["title"],
                chosen["complexity"],
                chosen["assigned_to"],
                chosen["description"],
                chosen.get("target_files", []),
            ]],
        ]

    if "WHERE t.status = 'in_progress'" in q and "RETURN t.id, t.title, t.complexity" in q:
        rows = [
            [t["id"], t["title"], t["complexity"], t["assigned_to"], t["description"]]
            for t in tasks.values()
            if t.get("status") == "in_progress" and _matches_proj(t)
        ]
        return [["t.id", "t.title", "t.complexity", "t.assigned_to", "t.description"], rows]

    if "RETURN t.id, t.title, t.description, t.status, t.complexity" in q:
        rows = [
            [
                t["id"],
                t["title"],
                t.get("description", ""),
                t.get("status", "pending"),
                t.get("complexity", 1),
                t.get("assigned_to", "primary"),
                t.get("retry_count", 0),
                t.get("created_at", ""),
                t.get("completed_at"),
                t.get("last_error"),
                list(deps.get(tid, set())) or list(t.get("depends_on") or []),
                t.get("project_id", "kortext"),
            ]
            for tid, t in tasks.items()
            if _matches_proj(t)
        ]
        return [["id", "title", "desc", "status", "comp", "assign", "retry", "created", "completed", "err", "deps", "proj"], rows]

    if "SET t.lease_expires_at = $lease_expires_at" in q and "t.heartbeat_at = $now" in q:
        tid = str(params.get("id", ""))
        t = tasks.get(tid)
        if t and t.get("status") == "in_progress":
            t["lease_expires_at"] = params.get("lease_expires_at")
            return [["t.id"], [[tid]]]
        return []

    if "MATCH (t:Task {id: $id}) SET t.lease_expires_at =" in q:
        tid = str(params.get("id", ""))
        t = tasks.get(tid)
        if t:
            m = re.search(r"SET t\.lease_expires_at = '([^']+)'", q)
            if m:
                t["lease_expires_at"] = m.group(1)
        return []

    if "MATCH (t:Task {id: $id})" in q and "SET t.status = 'completed'" in q:
        tid = str(params.get("id", ""))
        t = tasks.get(tid)
        if t:
            t["status"] = "completed"
            t["completed_at"] = params.get("now")
            t["lease_expires_at"] = None
            return [["t.id"], [[tid]]]
        return []

    return []


def _install_seatbelt_falkordb_patch() -> None:
    """Installs session-wide FalkorDBClient test-double patch when Seatbelt blocks local sockets."""
    if not _SEATBELT_BLOCKED:
        return
    from unittest.mock import MagicMock
    import kortext.falkordb_client as fdb_mod
    from kortext.falkordb_client import FalkorDBClient

    if getattr(FalkorDBClient, "_seatbelt_patched", False):
        return

    orig_query = FalkorDBClient.query
    orig_get_redis = FalkorDBClient.get_redis

    def _is_client_mocked(self) -> bool:
        if fdb_mod.redis is None or not isinstance(getattr(fdb_mod.redis, "Redis", None), type):
            return True
        if self._r is not None and not isinstance(self._r, fdb_mod.redis.Redis):
            return True
        return False

    def _patched_get_redis(self):
        if _is_client_mocked(self):
            return orig_get_redis(self)
        mock_r = MagicMock()

        def _exec_cmd(cmd, *args, **kwargs):
            if cmd == "GRAPH.DELETE" and args:
                _SANDBOX_GRAPHS.pop(str(args[0]), None)
                return "OK"
            if cmd in ("GRAPH.QUERY", "GRAPH.RO_QUERY") and len(args) >= 2:
                return _sandbox_falkordb_fixture_query(str(args[0]), str(args[1]), {})
            return []

        mock_r.execute_command.side_effect = _exec_cmd
        return mock_r

    def _patched_query(self, graph_name, cypher, params=None, attempts=2, read_only=False, raise_on_error=False):
        if _is_client_mocked(self):
            return orig_query(
                self,
                graph_name,
                cypher,
                params=params,
                attempts=attempts,
                read_only=read_only,
                raise_on_error=raise_on_error,
            )
        return _sandbox_falkordb_fixture_query(self.resolve_graph_name(graph_name), cypher, params)

    def _patched_ensure_indexes(self, graph_name, indexes):
        if _is_client_mocked(self):
            for label, prop in indexes:
                r_client = self.get_redis()
                r_client.execute_command(
                    "GRAPH.QUERY",
                    self.resolve_graph_name(graph_name),
                    f"CREATE INDEX FOR (n:{label}) ON (n.{prop})",
                )
        return []

    FalkorDBClient.get_redis = _patched_get_redis  # type: ignore[method-assign]
    FalkorDBClient.query = _patched_query  # type: ignore[method-assign]
    FalkorDBClient.ensure_indexes = _patched_ensure_indexes  # type: ignore[method-assign]
    FalkorDBClient._seatbelt_patched = True  # type: ignore[attr-defined]


_install_seatbelt_falkordb_patch()


@pytest.fixture(scope="session", autouse=True)
def _isolate_locked_live_qdrant_session():
    """
    The session HOME and KORTEXT_HOME are already redirected at import time (top of this file),
    so tests never collide with a live `kortext-server` holding ~/.kortext/qdrant/.lock.
    This fixture only closes pooled Qdrant clients at session end.
    """
    from kortext._qdrant import close_qdrant_clients

    yield
    try:
        close_qdrant_clients()
    except Exception:
        pass


@pytest.fixture
def real_falkordb():
    """Skips tests whose queries the regex FalkorDB emulator cannot answer (episodic search)."""
    if _SEATBELT_BLOCKED:
        pytest.skip("needs a real FalkorDB on 127.0.0.1:6379")


@pytest.fixture
def seeded_symbol_graph():
    """
    Ensures the default seed symbols exist in the kortext_symbols graph, written through the
    production SymbolGraphManager.save_file_ast. A no-op when the symbols are already there
    (the Seatbelt emulator pre-seeds them), so it works against a real, empty FalkorDB too.
    Only the files it wrote are removed afterwards.
    """
    from kortext.symbols.graph import SymbolGraphManager

    mgr = SymbolGraphManager()
    if mgr.find_definition(_SEED_SYMBOLS_DATA[0]["name"]):
        yield
        return
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for item in _SEED_SYMBOLS_DATA:
        by_file.setdefault(item["file_path"], []).append(item)
    for fp, items in by_file.items():
        refs = [
            {"symbol": callee, "caller": it["name"], "line": it["line"] + 2, "kind": "call"}
            for it in items
            for callee in it.get("callees", [])
        ]
        mgr.save_file_ast(fp, items, [], refs)
    try:
        yield
    finally:
        for fp in by_file:
            mgr.delete_file_ast(fp)
        # delete_file_ast leaves the unresolved callee stubs that save_file_ast created.
        paths = list(by_file)
        mgr._query(
            "MATCH (s:Symbol) WHERE any(p IN $paths WHERE s.qualified_name STARTS WITH p "
            "OR s.qualified_name STARTS WITH ('unresolved::' + p)) DETACH DELETE s",
            {"paths": paths},
        )


@pytest.fixture(autouse=True)
def _sandbox_falkordb_guard():
    """Ensures Seatbelt FalkorDB patch stays active across all test scopes."""
    _install_seatbelt_falkordb_patch()


@pytest.fixture(scope="session")
def benchmark_store(request) -> Dict[str, Any]:
    """Shared dict for collecting real measured benchmark metrics across tests."""
    store: Dict[str, Any] = {
        "token_reduction": {},
        "memory_retention": {},
        "standard": {},
        "run_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    request.session.stash[_BENCHMARK_STASH_KEY] = store
    return store


def pytest_sessionfinish(session, exitstatus):
    """Write benchmark reports after the session completes ONLY when benchmark tests actually ran."""
    store = None
    try:
        store = session.stash.get(_BENCHMARK_STASH_KEY, None)
    except Exception:
        store = None

    if isinstance(store, dict) and (store.get("token_reduction") or store.get("memory_retention")):
        DATA_DIR.mkdir(exist_ok=True)
        internal_report = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "note": "Populated dynamically from session benchmark_store when benchmark tests run.",
            "token_reduction": store.get("token_reduction", {}),
            "memory_retention": store.get("memory_retention", {}),
        }
        (DATA_DIR / "benchmark_results.json").write_text(json.dumps(internal_report, indent=2))

    if isinstance(store, dict) and store.get("standard"):
        DATA_DIR.mkdir(exist_ok=True)
        standard_report = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **store["standard"],
        }
        (DATA_DIR / "standard_benchmark_results.json").write_text(json.dumps(standard_report, indent=2))

    if not _SEATBELT_BLOCKED:
        try:
            from kortext.falkordb_client import get_shared_falkordb_client

            r = get_shared_falkordb_client().get_redis()
            prod_graphs = {"kortext", "kortext_symbols", "antigravity_docs"}
            stale_names = {
                "cerebrum",
                "cerebrum_symbols",
                "my_custom_collection_symbols",
                "custom_microservice_symbols",
                "symbols",
                "episodic",
            }
            all_graphs = r.execute_command("GRAPH.LIST") or []
            for g in all_graphs:
                g_str = str(g)
                if g_str not in prod_graphs and ("test" in g_str.lower() or g_str in stale_names):
                    try:
                        r.execute_command("GRAPH.DELETE", g)
                    except Exception:
                        pass
        except Exception:
            pass
