"""
Subsystem Community Detection via Louvain Modularity on FalkorDB Dependency Graphs.

Clusters collaborating files into high-cohesion architectural subsystems,
generating executive architectural descriptions and persisting (:Subsystem) nodes
and [:BELONGS_TO] edges directly into FalkorDB.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import logging
import os
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Set
try:
    import networkx as nx
    from networkx.algorithms.community import louvain_communities
except Exception:
    nx = None  # type: ignore
    louvain_communities = None  # type: ignore
try:
    import redis
except Exception:
    redis = None  # type: ignore

from kortext.falkordb_client import build_cypher_params_header, get_shared_falkordb_client

logger = logging.getLogger(__name__)

FALKORDB_HOST = os.getenv("FALKORDB_HOST", "127.0.0.1")
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
FALKORDB_PASS = os.getenv("FALKORDB_PASSWORD", "")

CODE_EXTENSIONS = frozenset({".py", ".ts", ".js", ".go", ".rs", ".java", ".c", ".cpp", ".cs"})
EXCLUDED_DIR_NAMES = frozenset({
    "journal", "docs", "archive", "rules", "skills", "agents",
    "tests", ".gemini", ".git", ".pytest_cache", ".venv", "node_modules",
})

DOMAIN_TITLES: Dict[str, str] = {
    "retrieval": "Retrieval & HippoRAG",
    "symbols": "AST Symbols & Code Intelligence",
    "chunking": "AST Chunking & Ingestion",
    "dashboard": "Interactive Dashboard",
    "episodic": "Episodic Memory & Grounding",
    "trust": "Causal Trust & MINJA Defense",
    "benchmarks": "Benchmarks & Metrics",
    "hooks": "Antigravity Lifecycle Hooks",
    "tasks": "Task DAG Engine",
    "core": "Core Orchestration & Services",
}


def is_code_file(path_str: str) -> bool:
    """
    Validates if a file is a production source code file eligible for architectural subsystem clustering.
    Excludes documentation, journals, test suites, and metadata files.
    """
    norm_str = str(path_str).replace("\\", "/")
    p = PurePosixPath(norm_str)
    if p.suffix.lower() not in CODE_EXTENSIONS:
        return False

    parts_lower = {part.lower() for part in p.parts}
    if parts_lower.intersection(EXCLUDED_DIR_NAMES):
        return False

    # Exclude test files
    name_lower = p.name.lower()
    if name_lower.startswith("test_") or name_lower.endswith("_test.py") or name_lower == "conftest.py":
        return False

    return True


def get_relative_module(path_str: str) -> str:
    """
    Extracts the relative architectural module/package from a file path.
    Maps known internal directories or returns 'core' for entrypoint scripts.
    """
    norm_str = str(path_str).replace("\\", "/")
    p = PurePosixPath(norm_str)
    parts = p.parts
    lower_parts = [part.lower() for part in parts]

    # Look for innermost root package 'kortext' (or legacy 'cortex')
    for pkg in ("kortext", "cortex"):
        if pkg in lower_parts:
            idx = len(lower_parts) - 1 - lower_parts[::-1].index(pkg)
            rel_parts = parts[idx + 1:]
            if len(rel_parts) > 1:
                sub_mod = rel_parts[0].lower()
                if sub_mod in DOMAIN_TITLES:
                    return sub_mod
                return sub_mod
            return "core"

    # Fallback to parent directory name if not inside known package
    parent_name = p.parent.name.lower()
    if parent_name in ("cortex", "kortext", "scripts", "bin", ""):
        return "core"
    if parent_name in DOMAIN_TITLES:
        return parent_name
    return "core"


def resolve_subsystem_title(
    files: List[str],
    existing_titles: Optional[Set[str]] = None,
) -> str:
    """
    Derives a crisp, architectural subsystem title based on dominant domain modules.
    Guarantees no raw filesystem paths or username prefixes leak into titles.
    """
    if not files:
        return "Core Services"

    modules = [get_relative_module(f) for f in files]
    counts = Counter(modules)
    dominant_mod, _ = counts.most_common(1)[0]

    base_title = DOMAIN_TITLES.get(dominant_mod, dominant_mod.capitalize())

    # Disambiguate if base title is already used
    if existing_titles and base_title in existing_titles:
        # Check second most common or distinct sub-directory
        sub_dirs = [
            PurePosixPath(str(f).replace("\\", "/")).parent.name.capitalize()
            for f in files
            if PurePosixPath(str(f).replace("\\", "/")).parent.name.lower() != dominant_mod
        ]
        if sub_dirs:
            candidate = f"{base_title} ({Counter(sub_dirs).most_common(1)[0][0]})"
            if candidate not in existing_titles:
                return candidate

        # Numerical suffix fallback
        counter = 2
        candidate = f"{base_title} {counter}"
        while candidate in existing_titles:
            counter += 1
            candidate = f"{base_title} {counter}"
        return candidate

    return base_title


def merge_orphan_singletons(communities: List[Set[str]]) -> List[Set[str]]:
    """
    Consolidates 1-file orphan singletons into matching multi-file architectural subsystems.
    Any remaining unmerged entrypoints or standalone utilities are grouped into a unified Core subsystem.
    """
    multi_comms: List[Set[str]] = [set(c) for c in communities if len(c) > 1]
    singletons: List[str] = [list(c)[0] for c in communities if len(c) == 1]

    if not singletons:
        return multi_comms

    # If all communities were singletons
    if not multi_comms:
        return [set(singletons)]

    # Compute dominant module for each multi-file community
    comm_modules: List[str] = []
    for comm in multi_comms:
        mods = [get_relative_module(f) for f in comm]
        dom_mod, _ = Counter(mods).most_common(1)[0]
        comm_modules.append(dom_mod)

    unmerged: List[str] = []
    for singleton in singletons:
        s_mod = get_relative_module(singleton)
        matched = False
        for idx, dom_mod in enumerate(comm_modules):
            if s_mod == dom_mod and dom_mod != "core":
                multi_comms[idx].add(singleton)
                matched = True
                break
        if not matched:
            unmerged.append(singleton)

    # Group remaining unmerged singletons into a unified Core subsystem
    if unmerged:
        # If there's an existing 'core' dominant community, merge into it
        merged_into_core = False
        for idx, dom_mod in enumerate(comm_modules):
            if dom_mod == "core":
                multi_comms[idx].update(unmerged)
                merged_into_core = True
                break
        if not merged_into_core:
            multi_comms.append(set(unmerged))

    return multi_comms


class SubsystemClusterer:
    """Detects and persists architectural subsystems using Louvain community detection."""

    def __init__(
        self,
        host: str = FALKORDB_HOST,
        port: int = FALKORDB_PORT,
        password: Optional[str] = FALKORDB_PASS,
        graph_name: str = "kortext_symbols",
    ):
        self.host = host
        self.port = port
        self.password = password or None
        self.client = get_shared_falkordb_client(host=self.host, port=self.port, password=self.password)
        if hasattr(self.client, "resolve_graph_name"):
            self.graph_name = self.client.resolve_graph_name(graph_name)
        else:
            self.graph_name = graph_name

    def _get_client(self) -> redis.Redis:
        return self.client.get_redis()

    def _query(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> List[List[Any]]:
        try:
            if hasattr(self.client, "query") and callable(self.client.query):
                res = self.client.query(self.graph_name, cypher, params)
                if isinstance(res, list):
                    return res
            client = self._get_client()
            query_str = (build_cypher_params_header(params) + cypher) if params else cypher
            res = client.execute_command("GRAPH.QUERY", self.graph_name, query_str)
            return res if res and isinstance(res, list) else []
        except Exception as e:
            logger.error(f"[SubsystemClusterer] Cypher error: {e}")
            return []

    def _generate_subsystem_summary(self, name: str, files: List[str], custom_summary: Optional[str] = None) -> str:
        """
        Synthesizes a crisp, factual architectural description deterministically
        from member files and primary exported symbols without local LLM overhead.
        """
        if custom_summary:
            return custom_summary

        file_basenames = [PurePosixPath(str(f).replace("\\", "/")).name for f in files]
        top_symbols: List[str] = []
        try:
            for f in files[:4]:
                rows = self._query(
                    "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File {path: $f}) RETURN s.name LIMIT 3",
                    {"f": f},
                )
                if rows and len(rows) > 1 and isinstance(rows[1], list):
                    for r in rows[1]:
                        if r and len(r) >= 1:
                            top_symbols.append(str(r[0]))
        except Exception:
            logger.debug("Suppressed non-fatal exception")

        files_str = ", ".join(file_basenames[:4])
        if len(file_basenames) > 4:
            files_str += f" (+{len(file_basenames) - 4} more)"

        if top_symbols:
            unique_syms = list(dict.fromkeys(top_symbols))[:5]
            syms_str = ", ".join(unique_syms)
            return f"Architectural subsystem '{name}' comprising {len(files)} components ({files_str}) exposing primary symbols: {syms_str}."
        return f"Architectural subsystem '{name}' encapsulating {len(files)} components ({files_str})."

    async def detect_subsystems(self, persist: bool = True) -> List[Dict[str, Any]]:
        """
        Runs Louvain community detection on production code dependencies, merges singletons,
        and optionally persists (:Subsystem) nodes and [:BELONGS_TO] edges into FalkorDB.
        """
        # 1. Fetch file dependency matrix
        cypher_edges = (
            "MATCH (f1:File)-[r:IMPORTS|REFERENCES]->(s:Symbol)-[:DEFINED_IN]->(f2:File) "
            "WHERE f1.path <> f2.path "
            "RETURN f1.path, f2.path, count(r)"
        )
        edge_rows = self._query(cypher_edges)

        G = nx.Graph()

        # Add all production code files (filtering out markdown, documentation, tests, and ghost legacy paths)
        file_rows = self._query("MATCH (f:File) RETURN f.path")
        stale_graph_files: List[str] = []
        if file_rows and len(file_rows) > 1 and isinstance(file_rows[1], list):
            for row in file_rows[1]:
                if row:
                    path_str = str(row[0]).replace("\\", "/")
                    p_obj = Path(path_str)
                    if "/cortex/" in path_str.lower() or (p_obj.is_absolute() and not p_obj.exists()):
                        stale_graph_files.append(str(row[0]))
                        continue
                    if not is_code_file(path_str):
                        continue
                    G.add_node(path_str)

        if persist and stale_graph_files:
            self._query(
                "UNWIND $stale_files AS fp "
                "MATCH (f:File {path: fp}) OPTIONAL MATCH (s:Symbol)-[:DEFINED_IN]->(f) DETACH DELETE s, f",
                {"stale_files": stale_graph_files},
            )

        # Add dependency edges between eligible code files
        if edge_rows and len(edge_rows) > 1 and isinstance(edge_rows[1], list):
            for row in edge_rows[1]:
                if len(row) >= 3:
                    u, v, w = str(row[0]).replace("\\", "/"), str(row[1]).replace("\\", "/"), int(row[2])
                    if is_code_file(u) and is_code_file(v):
                        if G.has_edge(u, v):
                            G[u][v]["weight"] += w
                        else:
                            G.add_edge(u, v, weight=w)

        if len(G.nodes) == 0:
            return []

        # 2. Compute Louvain communities with invariant resolution = 1.0 (Invariant D22)
        raw_communities = louvain_communities(G, weight="weight", resolution=1.0, seed=42)

        # 3. Merge orphan singletons into cohesive architectural subsystems
        consolidated_communities = merge_orphan_singletons([set(c) for c in raw_communities])

        # 4. Label and synthesize descriptions
        now = datetime.now(timezone.utc).isoformat()
        subsystems: List[Dict[str, Any]] = []
        assigned_titles: Set[str] = set()

        # Clear existing subsystem nodes first if persisting
        if persist:
            self._query("MATCH (sub:Subsystem) DETACH DELETE sub")

        for idx, comm in enumerate(consolidated_communities):
            file_list = sorted(list(comm))
            sub_name = resolve_subsystem_title(file_list, existing_titles=assigned_titles)
            assigned_titles.add(sub_name)

            summary = self._generate_subsystem_summary(sub_name, file_list)

            subsystems.append({
                "id": f"subsystem_{idx+1}",
                "name": sub_name,
                "description": summary,
                "file_count": len(file_list),
                "files": file_list,
            })

            if persist:
                self._query(
                    "MERGE (sub:Subsystem {id: $id, name: $name}) "
                    "SET sub.description = $desc, sub.file_count = $count, sub.updated_at = $now "
                    "WITH sub "
                    "UNWIND $files AS fp "
                    "MATCH (f:File {path: fp}) "
                    "MERGE (f)-[:BELONGS_TO]->(sub)",
                    {
                        "id": f"subsystem_{idx+1}",
                        "name": sub_name,
                        "desc": summary,
                        "count": len(file_list),
                        "now": now,
                        "files": file_list,
                    },
                )

        return subsystems

    def detect_shallow_modules(self, max_results: int = 10) -> List[Dict[str, Any]]:
        """
        Identifies shallow or high-coupling modules (John Ousterhout's A Philosophy of Software Design)
        by comparing public symbol interface count + external cross-file callers against implementation depth.
        """
        cypher = (
            "MATCH (s:Symbol)-[d:DEFINED_IN]->(f:File) "
            "OPTIONAL MATCH (caller_f:File)-[r:IMPORTS|REFERENCES]->(s) WHERE caller_f.path <> f.path "
            "RETURN f.path, count(DISTINCT s) AS sym_count, "
            "sum(coalesce(d.end_line, d.line, 1) - coalesce(d.line, 1) + 1) AS total_span, "
            "count(DISTINCT caller_f) AS ext_callers"
        )
        rows = self._query(cypher)
        candidates: List[Dict[str, Any]] = []
        if rows and len(rows) > 1 and isinstance(rows[1], list):
            for row in rows[1]:
                if not row or len(row) < 4:
                    continue
                fp = str(row[0] or "").replace("\\", "/")
                if not is_code_file(fp):
                    continue
                sym_count = max(1, int(row[1] or 1))
                total_span = max(1, int(row[2] or 1))
                ext_callers = int(row[3] or 0)
                avg_lines_per_symbol = round(total_span / sym_count, 1)
                shallowness_ratio = round((sym_count + ext_callers) / max(10.0, float(total_span)), 3)
                if avg_lines_per_symbol <= 12.0 and sym_count >= 3:
                    candidates.append({
                        "file_path": fp,
                        "module": get_relative_module(fp),
                        "exported_symbols": sym_count,
                        "total_symbol_lines": total_span,
                        "avg_lines_per_symbol": avg_lines_per_symbol,
                        "external_caller_files": ext_callers,
                        "shallowness_score": shallowness_ratio,
                        "recommendation": (
                            f"Consolidate shallow interface in `{PurePosixPath(fp).name}` "
                            f"({sym_count} symbols averaging {avg_lines_per_symbol} lines) into a deeper module."
                        ),
                    })
        candidates.sort(key=lambda x: x["shallowness_score"], reverse=True)
        return candidates[:max_results]
