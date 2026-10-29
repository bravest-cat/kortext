"""Graph traversal, impact analysis, and scope affinity mixin for SymbolGraphManager."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from kortext.symbols.formatter import format_impact_report

logger = logging.getLogger(__name__)


class SymbolGraphTraversalMixin:
    """Mixin providing multi-hop impact tracing and scope affinity scoring."""

    def trace_symbol_impact(
        self,
        symbol_name: str,
        direction: str = "upstream",
        max_depth: int = 3,
        token_budget: Optional[int] = 1500,
    ) -> Dict[str, Any]:
        """
        Traces recursive call, reference, import, type implementation, and note link blast radius.
        Applies dynamic token budgeting and progressive level-of-detail (0/1 Knapsack style):
          - 1-hop callers / definitions: Full signature and file path (L1 detail)
          - 2-hop callers: Compact path:line (L0 detail)
          - 3+ hop or budget overflow: Compressed count summary with impacted file list
        Args:
            symbol_name: Target symbol or document.
            direction: 'upstream' (dependents/callers/backlinks) or 'downstream' (callees/dependencies/outlinks).
            max_depth: Traversal depth hops (1-5).
            token_budget: Max approximate tokens for the returned impact trace (default 1500).
        Returns:
            Structured dict with definitions, direct call sites/imports/links, type hierarchies,
            progressive degradation statistics, and formatted markdown summary.
        """
        clean_name = symbol_name.strip()
        depth = max(1, min(max_depth, 5))
        definitions = self.find_definition(clean_name)

        impact_nodes: List[Dict[str, Any]] = []
        transitive_paths: List[Dict[str, Any]] = []
        transitive_nodes: List[Dict[str, Any]] = []

        exact_match = bool(definitions)

        if direction.lower() == "upstream":
            # 1. Direct incoming references and imports (1-hop)
            if exact_match:
                direct_res = self._query(
                    "MATCH (f:File)-[r:REFERENCES|IMPORTS]->(s:Symbol) "
                    "WHERE s.name = $name "
                    "RETURN DISTINCT f.path, type(r), r.line",
                    {"name": clean_name},
                )
            else:
                direct_res = self._query(
                    "MATCH (f:File)-[r:REFERENCES|IMPORTS]->(s:Symbol) "
                    "WHERE toLower(s.name) = toLower($name) "
                    "RETURN DISTINCT f.path, type(r), r.line",
                    {"name": clean_name},
                )
            if direct_res and len(direct_res) > 1 and isinstance(direct_res[1], list):
                for row in direct_res[1]:
                    if len(row) >= 3:
                        impact_nodes.append({
                            "source_file": str(row[0]),
                            "relation": str(row[1]),
                            "line": int(row[2]) if str(row[2]).isdigit() else 1,
                            "target_symbol": clean_name,
                            "depth": 1,
                        })

            # 2. Incoming note links (backlinks)
            if exact_match:
                link_res = self._query(
                    "MATCH (d:Document)-[r:LINKS_TO]->(target) "
                    "WHERE (target:File AND target.path ENDS WITH $name) "
                    "   OR (target:Document AND target.title = $name) "
                    "   OR (target:Concept AND target.name = $name) "
                    "   OR (target:Symbol AND target.name = $name) "
                    "RETURN DISTINCT d.path, coalesce(r.link_type, 'LINKS_TO'), r.line",
                    {"name": clean_name},
                )
            else:
                link_res = self._query(
                    "MATCH (d:Document)-[r:LINKS_TO]->(target) "
                    "WHERE (target:File AND toLower(target.path) ENDS WITH toLower($name)) "
                    "   OR (target:Document AND toLower(target.title) = toLower($name)) "
                    "   OR (target:Concept AND toLower(target.name) = toLower($name)) "
                    "   OR (target:Symbol AND toLower(target.name) = toLower($name)) "
                    "RETURN DISTINCT d.path, coalesce(r.link_type, 'LINKS_TO'), r.line",
                    {"name": clean_name},
                )
            if link_res and len(link_res) > 1 and isinstance(link_res[1], list):
                for row in link_res[1]:
                    if len(row) >= 3:
                        impact_nodes.append({
                            "source_file": str(row[0]),
                            "relation": str(row[1]),
                            "line": int(row[2]) if str(row[2]).isdigit() else 1,
                            "target_symbol": clean_name,
                            "depth": 1,
                        })

            # 3. Transitive symbol-to-symbol relationships (IMPLEMENTS, TYPE_OF)
            if exact_match:
                trans_res = self._query(
                    "MATCH (caller:Symbol)-[r:IMPLEMENTS|TYPE_OF]->(target:Symbol) "
                    "WHERE target.name = $name "
                    "OPTIONAL MATCH (caller)-[:DEFINED_IN]->(f:File) "
                    "RETURN caller.name, type(r), coalesce(f.path, 'unknown')",
                    {"name": clean_name},
                )
            else:
                trans_res = self._query(
                    "MATCH (caller:Symbol)-[r:IMPLEMENTS|TYPE_OF]->(target:Symbol) "
                    "WHERE toLower(target.name) = toLower($name) "
                    "OPTIONAL MATCH (caller)-[:DEFINED_IN]->(f:File) "
                    "RETURN caller.name, type(r), coalesce(f.path, 'unknown')",
                    {"name": clean_name},
                )
            if trans_res and len(trans_res) > 1 and isinstance(trans_res[1], list):
                for row in trans_res[1]:
                    if len(row) >= 3:
                        transitive_paths.append({
                            "symbol": str(row[0]),
                            "relation": str(row[1]),
                            "file_path": str(row[2]),
                            "depth": 1,
                        })

            # 4. Multi-hop transitive callers if depth > 1
            if depth > 1:
                if exact_match:
                    cypher_trans = (
                        f"MATCH path = (caller:Symbol)-[:CALLS|REFERENCES*2..{depth}]->(target:Symbol) "
                        "WHERE target.name = $name "
                        "OPTIONAL MATCH (caller)-[:DEFINED_IN]->(f:File) "
                        "RETURN DISTINCT caller.name, length(path), coalesce(f.path, 'unknown')"
                    )
                else:
                    cypher_trans = (
                        f"MATCH path = (caller:Symbol)-[:CALLS|REFERENCES*2..{depth}]->(target:Symbol) "
                        "WHERE toLower(target.name) = toLower($name) "
                        "OPTIONAL MATCH (caller)-[:DEFINED_IN]->(f:File) "
                        "RETURN DISTINCT caller.name, length(path), coalesce(f.path, 'unknown')"
                    )
                multi_res = self._query(cypher_trans, {"name": clean_name})
                if multi_res and len(multi_res) > 1 and isinstance(multi_res[1], list):
                    for row in multi_res[1]:
                        if len(row) >= 3:
                            transitive_nodes.append({
                                "symbol": str(row[0]),
                                "depth": int(row[1]),
                                "source_file": str(row[2]),
                                "relation": "TRANSITIVE_CALLER",
                            })

        else:  # downstream
            # 1. Outgoing dependencies for files defining this symbol
            down_res = self._query(
                "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File)-[r:IMPORTS|REFERENCES]->(target:Symbol) "
                "WHERE toLower(s.name) = toLower($name) "
                "RETURN DISTINCT target.name, type(r), r.line, f.path",
                {"name": clean_name},
            )
            if down_res and len(down_res) > 1 and isinstance(down_res[1], list):
                for row in down_res[1]:
                    if len(row) >= 4:
                        impact_nodes.append({
                            "source_file": str(row[3]),
                            "relation": str(row[1]),
                            "line": int(row[2]) if str(row[2]).isdigit() else 1,
                            "target_symbol": str(row[0]),
                            "depth": 1,
                        })

            # 2. Outgoing note links from this document
            out_link_res = self._query(
                "MATCH (d:Document)-[r:LINKS_TO]->(target) "
                "WHERE toLower(d.title) = toLower($name) OR toLower(d.path) ENDS WITH toLower($name) "
                "RETURN DISTINCT coalesce(target.title, target.name, target.path), coalesce(r.link_type, 'LINKS_TO'), r.line, d.path",
                {"name": clean_name},
            )
            if out_link_res and len(out_link_res) > 1 and isinstance(out_link_res[1], list):
                for row in out_link_res[1]:
                    if len(row) >= 4:
                        impact_nodes.append({
                            "source_file": str(row[3]),
                            "relation": str(row[1]),
                            "line": int(row[2]) if str(row[2]).isdigit() else 1,
                            "target_symbol": str(row[0]),
                            "depth": 1,
                        })

            # 3. Type relations (what does this symbol implement or extend?)
            trans_res = self._query(
                "MATCH (s:Symbol)-[r:IMPLEMENTS|TYPE_OF]->(target:Symbol) "
                "WHERE toLower(s.name) = toLower($name) "
                "OPTIONAL MATCH (target)-[:DEFINED_IN]->(f:File) "
                "RETURN target.name, type(r), coalesce(f.path, 'unknown')",
                {"name": clean_name},
            )
            if trans_res and len(trans_res) > 1 and isinstance(trans_res[1], list):
                for row in trans_res[1]:
                    if len(row) >= 3:
                        transitive_paths.append({
                            "symbol": str(row[0]),
                            "relation": str(row[1]),
                            "file_path": str(row[2]),
                            "depth": 1,
                        })

            # 4. Multi-hop transitive dependencies if depth > 1
            if depth > 1:
                cypher_down_trans = (
                    f"MATCH path = (s:Symbol)-[:CALLS|REFERENCES*2..{depth}]->(target:Symbol) "
                    "WHERE toLower(s.name) = toLower($name) "
                    "OPTIONAL MATCH (target)-[:DEFINED_IN]->(f:File) "
                    "RETURN DISTINCT target.name, length(path), coalesce(f.path, 'unknown')"
                )
                multi_down = self._query(cypher_down_trans, {"name": clean_name})
                if multi_down and len(multi_down) > 1 and isinstance(multi_down[1], list):
                    for row in multi_down[1]:
                        if len(row) >= 3:
                            transitive_nodes.append({
                                "symbol": str(row[0]),
                                "depth": int(row[1]),
                                "source_file": str(row[2]),
                                "relation": "TRANSITIVE_CALLEE",
                            })

        return format_impact_report(
            clean_name=clean_name,
            direction=direction,
            depth=depth,
            definitions=definitions,
            direct_impacts=impact_nodes,
            type_hierarchies=transitive_paths,
            transitive_impacts=transitive_nodes,
            token_budget=token_budget,
        )

    def get_companion_symbols(self, symbol_name: str) -> List[str]:
        """
        Returns companion symbols defined in the same file as the given symbol.
        """
        clean_name = symbol_name.strip()
        cypher = (
            "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File)<-[:DEFINED_IN]-(companion:Symbol) "
            "WHERE toLower(s.name) = toLower($name) AND toLower(companion.name) <> toLower($name) "
            "RETURN DISTINCT companion.name"
        )
        try:
            res = self._query(cypher, {"name": clean_name})
            companions: List[str] = []
            if res and len(res) > 1 and isinstance(res[1], list):
                for row in res[1]:
                    if row and row[0]:
                        companions.append(str(row[0]))
            return companions
        except Exception as e:
            logger.debug(f"Failed to fetch companion symbols for '{clean_name}': {e}")
            return []

    def get_hop_symbols(self, symbol_name: str) -> List[str]:
        """
        Returns direct 1-hop callees and callers of the given symbol.
        """
        clean_name = symbol_name.strip()
        cypher = (
            "MATCH (s:Symbol)-[:CALLS]-(hop:Symbol) "
            "WHERE toLower(s.name) = toLower($name) AND toLower(hop.name) <> toLower($name) "
            "RETURN DISTINCT hop.name"
        )
        try:
            res = self._query(cypher, {"name": clean_name})
            hops: List[str] = []
            if res and len(res) > 1 and isinstance(res[1], list):
                for row in res[1]:
                    if row and row[0]:
                        hops.append(str(row[0]))
            return hops
        except Exception as e:
            logger.debug(f"Failed to fetch hop symbols for '{clean_name}': {e}")
            return []

    def apply_scope_affinity(
        self,
        candidates: List[Dict[str, Any]],
        confidence_threshold: float = 0.85,
        companion_boost: float = 0.15,
        hop_boost: float = 0.10,
    ) -> List[Dict[str, Any]]:
        """
        Applies Bidirectional AST Scope Affinity re-ranking.
        When a Rank-1 symbol is found with high confidence (>0.85),
        boosts companion symbols defined in the same file by +0.15,
        and direct 1-hop callees/callers by +0.10.
        """
        if not candidates:
            return []

        # Identify Rank-1 candidate
        rank_1 = max(candidates, key=lambda x: x.get("score", 0.0))
        rank_1_score = rank_1.get("score", 0.0)
        rank_1_symbol = rank_1.get("symbol")

        if rank_1_score <= confidence_threshold or not rank_1_symbol:
            return [dict(c) for c in candidates]

        companion_symbols = set(self.get_companion_symbols(rank_1_symbol))
        hop_symbols = set(self.get_hop_symbols(rank_1_symbol))

        reranked: List[Dict[str, Any]] = []
        for c in candidates:
            item = dict(c)
            sym = item.get("symbol")
            if sym and sym != rank_1_symbol:
                boost = 0.0
                if sym in companion_symbols:
                    boost += companion_boost
                if sym in hop_symbols:
                    boost += hop_boost
                item["score"] = round(item.get("score", 0.0) + boost, 4)
            reranked.append(item)

        reranked.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return reranked
