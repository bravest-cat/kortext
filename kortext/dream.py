"""
Dream mode: offline memory reflection, synaptic pruning, and subsystem synthesis.

100% Agent-Native:
- Contradiction auditing and invariant induction are driven directly by the frontier Antigravity agent.
- No local LLM calls.
- Louvain Subsystem Community Detection is fully deterministic and runs in <5ms.
- Synaptic pruning archives superseded FalkorDB nodes and RELATES_TO edges into markdown and purges them.

Can be triggered on-demand via:
1. CLI: python -m kortext.dream [--project kortext] [--purge-stale] [--dry-run]
2. Antigravity Agent MCP Tool: trigger_dream_mode(project, purge_stale, dry_run, invariants, stale_fact_names)
3. Kortext Dashboard: 'Trigger Dream Mode' button on http://localhost:8004
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional
import uuid

import redis

from kortext.falkordb_client import (
    FALKORDB_HOST,
    FALKORDB_PASS,
    FALKORDB_PORT,
    FalkorDBClient,
    get_shared_falkordb_client,
)
from kortext.invariants import InvariantGroundingEngine
from kortext.symbols.clustering import SubsystemClusterer

logger = logging.getLogger(__name__)

FALKORDB_DB = os.getenv("KORTEXT_DATABASE", os.getenv("FALKORDB_DATABASE", "kortext"))
DEFAULT_SYMBOLS_DB = os.getenv("KORTEXT_SYMBOLS_GRAPH", os.getenv("FALKORDB_SYMBOLS_GRAPH", "kortext_symbols"))

KORTEXT_ROOT = Path(__file__).resolve().parent.parent
JOURNAL_DIR = Path(os.getenv("JOURNAL_DIR", str(KORTEXT_ROOT / "journal")))
ARCHIVE_DIR = JOURNAL_DIR / "archive"
DREAMS_DIR = JOURNAL_DIR / "dreams"
REPORTS_DIR = JOURNAL_DIR / "reports"
INVARIANTS_FILE = JOURNAL_DIR / "invariants.md"

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,62}[a-z0-9]$")


class DreamEngine:
    """The cognitive consolidation and reflection engine for Kortext (Agent-Native)."""

    def __init__(
        self,
        host: str = FALKORDB_HOST,
        port: int = FALKORDB_PORT,
        password: Optional[str] = FALKORDB_PASS,
        episodic_db: str = FALKORDB_DB,
        symbols_db: str = DEFAULT_SYMBOLS_DB,
        client: Optional[FalkorDBClient] = None,
    ):
        self.host = host
        self.port = port
        self.password = password or None
        self.episodic_db = episodic_db
        self.symbols_db = symbols_db
        self.client = (
            client
            if client is not None
            else get_shared_falkordb_client(host=self.host, port=self.port, password=self.password)
        )
        self.invariant_engine = InvariantGroundingEngine(
            host=self.host,
            port=self.port,
            password=self.password,
            db_name=self.episodic_db,
            client=self.client,
        )

        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        DREAMS_DIR.mkdir(parents=True, exist_ok=True)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    def _get_client(self) -> redis.Redis:
        return self.client.get_redis()

    def _query(self, db_name: str, cypher: str, params: Optional[Dict[str, Any]] = None) -> List[List[Any]]:
        return self.client.query(db_name, cypher, params)

    # ── Phase 1: Synaptic Pruning & Stale Fact Cleanup ─────────────────────────

    async def prune_stale_facts(
        self,
        purge_stale: bool = True,
        dry_run: bool = False,
        stale_fact_names: Optional[List[str]] = None,
        projects: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Audits both (:Episodic) nodes and (:Entity)-[r:RELATES_TO]->(:Entity) edges
        for superseded state, archives them to markdown, and purges them.
        Only facts of `projects` are touched; None means every project.
        """
        scope = {"projects": list(projects)} if projects else {}
        node_scope = "AND coalesce(e.project, 'kortext') IN $projects " if projects else ""
        edge_scope = "AND coalesce(r.project, 'kortext') IN $projects " if projects else ""
        now = datetime.now(timezone.utc).isoformat()
        today_str = datetime.now().strftime("%Y-%m-%d")
        superseded_details: List[Dict[str, str]] = []
        archived_entries: List[str] = []

        if stale_fact_names:
            for s_name in stale_fact_names:
                superseded_details.append({
                    "stale_name": s_name,
                    "superseded_by": "Agent Reflection",
                    "reason": "Superseded during cognitive consolidation",
                })
                if not dry_run:
                    self._query(
                        self.episodic_db,
                        "MATCH (e:Episodic) WHERE e.name = $name " + node_scope +
                        "SET e.superseded = true, e.superseded_by = 'Agent Reflection', "
                        "    e.superseded_at = $now, e.superseded_reason = 'Superseded during cognitive consolidation'",
                        {"name": s_name, "now": now, **scope},
                    )
                    self._query(
                        self.episodic_db,
                        "MATCH ()-[r:RELATES_TO]->() "
                        "WHERE (r.episode_name = $name OR r.id = $name OR r.fact_id = $name) " + edge_scope +
                        "SET r.superseded = true, r.dormant = true, r.superseded_by = 'Agent Reflection', "
                        "    r.superseded_at = $now, r.superseded_reason = 'Superseded during cognitive consolidation'",
                        {"name": s_name, "now": now, **scope},
                    )

        # 1. Collect superseded (:Episodic) nodes
        stale_node_names: List[str] = []
        superseded_nodes = self._query(
            self.episodic_db,
            "MATCH (e:Episodic) WHERE e.superseded = true " + node_scope +
            "RETURN e.name, e.content, e.created_at, e.superseded_by, e.superseded_reason",
            scope,
        )
        if superseded_nodes and len(superseded_nodes) > 1 and isinstance(superseded_nodes[1], list):
            for r in superseded_nodes[1]:
                if len(r) >= 3:
                    s_name = str(r[0])
                    s_content = str(r[1] or "")
                    s_created = str(r[2] or "")
                    s_by = str(r[3] or "Newer configuration") if len(r) > 3 and r[3] else "Newer configuration"
                    s_reason = str(r[4] or "Outdated operational state") if len(r) > 4 and r[4] else "Outdated operational state"
                    if not any(d["stale_name"] == s_name for d in superseded_details):
                        superseded_details.append({
                            "stale_name": s_name,
                            "superseded_by": s_by,
                            "reason": s_reason,
                        })
                    if purge_stale and not dry_run:
                        archived_entries.append(
                            f"### Episode: {s_name} (Created: {s_created})\n"
                            f"**Superseded By:** {s_by}\n"
                            f"**Reason:** {s_reason}\n\n"
                            f"{s_content}\n"
                        )
                        stale_node_names.append(s_name)

        # 2. Collect and purge superseded [r:RELATES_TO] edges
        superseded_edges = self._query(
            self.episodic_db,
            "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) WHERE r.superseded = true " + edge_scope +
            "RETURN coalesce(r.id, r.fact_id, toString(id(r))), r.fact, r.created_at, r.superseded_by, r.superseded_reason, id(r)",
            scope,
        )
        edge_internal_ids: List[int] = []
        if superseded_edges and len(superseded_edges) > 1 and isinstance(superseded_edges[1], list):
            for r in superseded_edges[1]:
                if len(r) >= 6:
                    f_id = str(r[0])
                    f_text = str(r[1] or "")
                    f_created = str(r[2] or "")
                    f_by = str(r[3] or "Consolidated Macro Fact")
                    f_reason = str(r[4] or "Superseded relationship")
                    edge_internal_ids.append(int(r[5]))
                    if not any(d["stale_name"] == f_id for d in superseded_details):
                        superseded_details.append({
                            "stale_name": f_id,
                            "superseded_by": f_by,
                            "reason": f_reason,
                        })
                    if purge_stale and not dry_run:
                        archived_entries.append(
                            f"### Fact: {f_id} (Created: {f_created})\n"
                            f"**Superseded By:** {f_by}\n"
                            f"**Reason:** {f_reason}\n\n"
                            f"{f_text}\n"
                        )

        # Archive first: if the write fails, nothing is deleted.
        if archived_entries and not dry_run:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            archive_file = ARCHIVE_DIR / f"{today_str}-archived.md"
            with open(archive_file, "a", encoding="utf-8") as f:
                f.write("\n\n" + "\n".join(archived_entries))

        for s_name in stale_node_names:
            self._query(
                self.episodic_db,
                "MATCH (e:Episodic {name: $name}) DETACH DELETE e",
                {"name": s_name},
            )
        if edge_internal_ids and purge_stale and not dry_run:
            self._query(
                self.episodic_db,
                "UNWIND $ids AS rel_id MATCH ()-[r:RELATES_TO]->() WHERE id(r) = rel_id DELETE r",
                {"ids": edge_internal_ids},
            )

        active_count = 0
        active_rows = self._query(
            self.episodic_db,
            "MATCH (e:Episodic) WHERE (e.superseded IS NULL OR e.superseded = false) " + node_scope + "RETURN count(e)",
            scope,
        )
        if active_rows and len(active_rows) > 1 and isinstance(active_rows[1], list) and active_rows[1]:
            try:
                active_count = int(active_rows[1][0][0])
            except (ValueError, TypeError, IndexError):
                active_count = 0

        return {
            "superseded_count": len(superseded_details),
            "archived_count": len(archived_entries),
            "active_count": active_count,
            "contradictions": [d["stale_name"] for d in superseded_details],
            "details": superseded_details,
        }

    @staticmethod
    def _token_shingles(text: str) -> set[str]:
        """Extracts normalized 2-word and 1-word token shingles for fast LSH/Jaccard blocking."""
        words = [w for w in re.split(r"\W+", (text or "").lower()) if len(w) > 1]
        if len(words) < 2:
            return set(words)
        return set(words) | {f"{words[i]}_{words[i + 1]}" for i in range(len(words) - 1)}

    def deduplicate_near_identical_facts(
        self,
        project: str = "kortext",
        similarity_threshold: float = 0.85,
        dry_run: bool = False,
    ) -> int:
        """
        Tier-1 Deterministic Neighborhood + Shingle-Jaccard Deduplication (MEX Cognitive Architecture):
        Groups active facts by (subject, object, relation) neighborhood and deterministically
        merges near-identical duplicates (Jaccard >= similarity_threshold) in <2ms without LLM calls.
        """
        cypher = """
        MATCH (s:Entity)-[r1:RELATES_TO]->(t:Entity)
        WHERE (r1.superseded IS NULL OR r1.superseded = false) AND (r1.dormant IS NULL OR r1.dormant = false)
        MATCH (s)-[r2:RELATES_TO]->(t)
        WHERE id(r1) < id(r2) AND (r2.superseded IS NULL OR r2.superseded = false) AND (r2.dormant IS NULL OR r2.dormant = false)
        AND r1.relation = r2.relation
        RETURN s.name, t.name, r1.relation, r1.fact, r2.fact, coalesce(r1.id, toString(id(r1))), coalesce(r2.id, toString(id(r2))), id(r1), id(r2)
        LIMIT 100
        """
        raw = self._query(self.episodic_db, cypher)
        if not raw or len(raw) < 2 or not raw[1]:
            return 0

        duplicate_edge_ids: List[int] = []
        seen_removed: set[int] = set()
        for row in raw[1]:
            if len(row) < 9:
                continue
            fact_a = str(row[3] or "")
            fact_b = str(row[4] or "")
            id_r1 = int(row[7])
            id_r2 = int(row[8])
            if id_r1 in seen_removed or id_r2 in seen_removed:
                continue
            sh_a = self._token_shingles(fact_a)
            sh_b = self._token_shingles(fact_b)
            if not sh_a or not sh_b:
                continue
            jaccard = len(sh_a & sh_b) / float(len(sh_a | sh_b))
            if jaccard >= similarity_threshold:
                duplicate_edge_ids.append(id_r1)
                seen_removed.add(id_r1)

        if duplicate_edge_ids and not dry_run:
            self._query(
                self.episodic_db,
                "UNWIND $ids AS target_id MATCH ()-[r:RELATES_TO]->() WHERE id(r) = target_id "
                "SET r.superseded = true, r.dormant = true, r.superseded_by = 'Tier1_Deterministic_Dedup'",
                {"ids": duplicate_edge_ids},
            )
        merged_entities = self.disambiguate_entities(project=project, dry_run=dry_run)
        return len(duplicate_edge_ids) + merged_entities

    def disambiguate_entities(
        self,
        project: str = "kortext",
        similarity_threshold: float = 0.88,
        dry_run: bool = False,
    ) -> int:
        """
        Union-Find + Jaro-Winkler + Normalized Identifier Clustering across :Entity nodes:
        Merges differently-named synonymous entities (e.g. 'auth-service' vs 'AuthService')
        and re-points RELATES_TO and MENTIONS edges to the canonical entity node.
        """
        from kortext.episodic.driver import jaro_winkler_similarity

        raw = self._query(self.episodic_db, "MATCH (n:Entity) RETURN n.name LIMIT 500")
        if not raw or len(raw) < 2 or not isinstance(raw[1], list):
            return 0

        names = [str(r[0]).strip() for r in raw[1] if r and r[0] and str(r[0]).strip()]
        if len(names) < 2:
            return 0

        def _norm_key(s: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", s.lower())

        parent = {n: n for n in names}

        def _find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def _union(a: str, b: str) -> None:
            ra, rb = _find(a), _find(b)
            if ra == rb:
                return
            # Prefer PascalCase / mixed-case canonical name over all-lowercase/hyphenated
            score_a = (1 if any(c.isupper() for c in ra) else 0, -len(ra))
            score_b = (1 if any(c.isupper() for c in rb) else 0, -len(rb))
            if score_b > score_a:
                ra, rb = rb, ra
            parent[rb] = ra

        norm_buckets: Dict[str, List[str]] = {}
        for n in names:
            nk = _norm_key(n)
            if len(nk) >= 3:
                norm_buckets.setdefault(nk, []).append(n)

        # 1. Exact alphanumeric normalization match ('auth-service' == 'AuthService' == 'auth_service')
        for bucket in norm_buckets.values():
            if len(bucket) > 1:
                first = bucket[0]
                for other in bucket[1:]:
                    _union(first, other)

        # 2. Jaro-Winkler + Token Jaccard match within 2-char prefix blocks
        prefix_buckets: Dict[str, List[str]] = {}
        for n in names:
            nk = _norm_key(n)
            if len(nk) >= 3:
                prefix_buckets.setdefault(nk[:2], []).append(n)

        for bucket in prefix_buckets.values():
            if len(bucket) < 2 or len(bucket) > 40:
                continue
            for i in range(len(bucket)):
                for j in range(i + 1, len(bucket)):
                    a, b = bucket[i], bucket[j]
                    if _find(a) == _find(b):
                        continue
                    jw = jaro_winkler_similarity(a, b)
                    if jw >= similarity_threshold:
                        toks_a = set(re.split(r"[^a-z0-9]+", a.lower())) - {""}
                        toks_b = set(re.split(r"[^a-z0-9]+", b.lower())) - {""}
                        union_sz = len(toks_a | toks_b)
                        jac = (len(toks_a & toks_b) / union_sz) if union_sz else 0.0
                        if 0.75 * jw + 0.25 * jac >= similarity_threshold:
                            _union(a, b)

        merges: List[Dict[str, str]] = []
        for n in names:
            canon = _find(n)
            if canon != n:
                merges.append({"alias": n, "canonical": canon})

        if merges and not dry_run:
            for m in merges:
                try:
                    self._query(
                        self.episodic_db,
                        "MATCH (alias:Entity {name: $alias}), (canon:Entity {name: $canonical}) "
                        "OPTIONAL MATCH (alias)-[r_out:RELATES_TO]->(tgt:Entity) "
                        "WHERE tgt.name <> $canonical "
                        "FOREACH (_ IN CASE WHEN r_out IS NOT NULL THEN [1] ELSE [] END | "
                        "  MERGE (canon)-[r_new:RELATES_TO {fact: coalesce(r_out.fact, ''), relation: coalesce(r_out.relation, 'RELATES_TO')}]->(tgt) "
                        "  SET r_new.id = coalesce(r_out.id, r_new.id), r_new.project = coalesce(r_out.project, $proj), "
                        "      r_new.valid_from = r_out.valid_from, r_new.created_at = r_out.created_at, "
                        "      r_new.importance = coalesce(r_out.importance, 5), r_new.confidence = coalesce(r_out.confidence, 0.9) "
                        ") "
                        "WITH alias, canon "
                        "DETACH DELETE alias",
                        {"alias": m["alias"], "canonical": m["canonical"], "proj": project},
                    )
                except Exception as exc:
                    logger.debug("Entity merge skipped for %s -> %s: %s", m["alias"], m["canonical"], exc)

        return len(merges)

    def surface_potential_contradictions(self, project: str = "kortext") -> List[Dict[str, Any]]:
        """
        Surfaces candidate contradictions and redundant episodic facts:
        Finds active facts sharing the same subject and relation with diverging content.
        These are staged as proposals for agent-supervised review rather than auto-pruned.
        """
        cypher = """
        MATCH (s:Entity)-[r1:RELATES_TO]->(t:Entity)
        WHERE (r1.superseded IS NULL OR r1.superseded = false) AND (r1.dormant IS NULL OR r1.dormant = false)
        MATCH (s)-[r2:RELATES_TO]->(t)
        WHERE id(r1) < id(r2) AND (r2.superseded IS NULL OR r2.superseded = false) AND (r2.dormant IS NULL OR r2.dormant = false)
        AND r1.relation = r2.relation
        RETURN s.name, t.name, r1.relation, r1.fact, r2.fact, r1.id, r2.id
        LIMIT 50
        """
        raw = self._query(self.episodic_db, cypher)
        if not raw or len(raw) < 2 or not raw[1]:
            return []

        candidates: List[Dict[str, Any]] = []
        for row in raw[1]:
            if len(row) >= 5:
                candidates.append({
                    "subject": str(row[0]),
                    "object": str(row[1]),
                    "relation": str(row[2]),
                    "fact_a": str(row[3]),
                    "fact_b": str(row[4]),
                    "id_a": str(row[5] if len(row) > 5 and row[5] is not None else ""),
                    "id_b": str(row[6] if len(row) > 6 and row[6] is not None else ""),
                })
        return candidates

    # ── Phase 2: Louvain Subsystem Community Detection ────────────────────────

    def discover_active_projects(self) -> List[str]:
        """Discovers all active project IDs across FalkorDB (:Project, :Episodic)."""
        projects: List[str] = ["kortext"]
        try:
            p_rows = self._query(self.episodic_db, "MATCH (p:Project) RETURN DISTINCT p.id LIMIT 50")
            if p_rows and len(p_rows) > 1 and isinstance(p_rows[1], list):
                for r in p_rows[1]:
                    if r and r[0] and str(r[0]).strip() not in projects:
                        projects.append(str(r[0]).strip())
            e_rows = self._query(self.episodic_db, "MATCH (e:Episodic) RETURN DISTINCT e.project LIMIT 50")
            if e_rows and len(e_rows) > 1 and isinstance(e_rows[1], list):
                for r in e_rows[1]:
                    if r and r[0] and str(r[0]).strip() not in projects:
                        projects.append(str(r[0]).strip())
        except Exception as exc:
            logger.debug("Active project discovery fallback: %s", exc)
        return [p for p in projects if p and p.lower() != "all"] or ["kortext"]

    async def cluster_subsystems(self, persist: bool = True, force: bool = False) -> List[Dict[str, Any]]:
        """Executes Louvain Community Detection (skipping re-clustering when file/edge counts are unchanged unless force=True)."""
        from kortext._home import get_kortext_home

        wm_file = get_kortext_home() / "run" / "louvain_graph_watermark.json"
        file_cnt = -1
        edge_cnt = -1
        if not force and getattr(self.client, "_is_live_falkor_client", False) is True:
            try:
                f_res = self._query(self.symbols_db, "MATCH (f:File) RETURN count(f)")
                e_res = self._query(self.symbols_db, "MATCH ()-[r:CALLS|IMPORTS|REFERENCES]->() RETURN count(r)")
                if f_res and len(f_res) > 1 and f_res[1]:
                    file_cnt = int(f_res[1][0][0] or 0)
                if e_res and len(e_res) > 1 and e_res[1]:
                    edge_cnt = int(e_res[1][0][0] or 0)
                if file_cnt >= 0 and edge_cnt >= 0 and wm_file.is_file():
                    wm = json.loads(wm_file.read_text(encoding="utf-8"))
                    if (
                        isinstance(wm, dict)
                        and wm.get("symbols_db") == self.symbols_db
                        and wm.get("file_count") == file_cnt
                        and wm.get("edge_count") == edge_cnt
                        and isinstance(wm.get("subsystems"), list)
                        and wm["subsystems"]
                    ):
                        return wm["subsystems"]
            except Exception as wm_exc:
                logger.debug("Louvain watermark check skipped: %s", wm_exc)

        clusterer = SubsystemClusterer(
            host=self.host,
            port=self.port,
            password=self.password,
            graph_name=self.symbols_db,
        )
        subsystems = await clusterer.detect_subsystems(persist=persist)
        if persist and file_cnt >= 0 and edge_cnt >= 0 and subsystems:
            try:
                wm_file.parent.mkdir(parents=True, exist_ok=True)
                wm_file.write_text(
                    json.dumps({
                        "symbols_db": self.symbols_db,
                        "file_count": file_cnt,
                        "edge_count": edge_cnt,
                        "subsystems": subsystems,
                    }),
                    encoding="utf-8",
                )
            except Exception as wm_err:
                logger.debug("Louvain watermark write skipped: %s", wm_err)
        return subsystems

    # ── Phase 3: Abstract Invariant Induction & AST Grounding ──────────────

    async def induce_invariants(
        self,
        invariants: Optional[List[Dict[str, str]]] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Agent-Native invariant consolidation and AST body grounding:
        If `invariants` are passed, persists them as probationary invariants,
        grounds them to AST body hashes, and audits existing invariants for drift.
        """
        now = datetime.now(timezone.utc).isoformat()
        inducted: List[Dict[str, Any]] = []

        if invariants is None and not dry_run:
            try:
                existing_text = INVARIANTS_FILE.read_text(encoding="utf-8") if INVARIANTS_FILE.exists() else ""
                auto_invs: List[Dict[str, str]] = []
                # Induce invariants from high-trust decision episodes in FalkorDB
                if getattr(self.client, "_is_live_falkor_client", False) is True:
                    ep_rows = self._query(
                        self.episodic_db,
                        "MATCH (e:Episodic) WHERE coalesce(e.superseded, false) = false "
                        "AND e.category = 'decision' AND coalesce(e.trust_score, 0.5) >= 0.6 "
                        "RETURN e.name, e.content, coalesce(e.trust_level, 'tool_confirmed') LIMIT 5",
                    )
                    if ep_rows and len(ep_rows) > 1 and isinstance(ep_rows[1], list):
                        for r in ep_rows[1]:
                            if len(r) >= 2 and r[0] and r[1]:
                                ep_title = str(r[0]).strip()
                                ep_body = str(r[1]).strip().splitlines()[0][:200]
                                if ep_title and ep_title not in existing_text and ep_body not in existing_text:
                                    auto_invs.append({
                                        "domain": "DECISION",
                                        "rule": f"{ep_title}: {ep_body}",
                                        "rationale": "Auto-induced from verified architectural decision episode.",
                                        "trust_level": str(r[2] or "tool_confirmed"),
                                    })
                if auto_invs:
                    invariants = auto_invs
            except Exception as auto_exc:
                logger.debug("Auto invariant induction from decision episodes skipped: %s", auto_exc)

        if invariants and not dry_run:
            # 1. Update journal/invariants.md
            lines = [f"\n## Updated: {now[:10]} (Probationary Invariant Induction)\n"]
            for inv in invariants:
                domain = inv.get("domain", "General").upper()
                rule = inv.get("rule", "")
                rationale = inv.get("rationale", "")
                lines.append(f"### Invariant [{domain}] (Status: Probationary)")
                lines.append(f"**Rule:** {rule}")
                lines.append(f"**Rationale:** {rationale}\n")

                # Ground to AST if symbol & file provided
                sym = inv.get("symbol")
                fp = inv.get("file_path")
                grounded_rec = None
                if sym and fp:
                    grounded_rec = self.invariant_engine.ground_invariant(
                        domain=domain,
                        rule=rule,
                        rationale=rationale,
                        symbol_name=sym,
                        file_path=fp,
                        trust_level=inv.get("trust_level", "speculative"),
                    )

                inducted.append({
                    "domain": domain,
                    "rule": rule,
                    "rationale": rationale,
                    "status": "probationary",
                    "trust_level": inv.get("trust_level", "speculative"),
                    "ast_hash": grounded_rec.get("ast_hash") if grounded_rec else None,
                })

            with open(INVARIANTS_FILE, "a", encoding="utf-8") as f:
                f.write("\n".join(lines))

            # 2. Persist to FalkorDB
            for item in inducted:
                self._query(
                    self.episodic_db,
                    "MERGE (i:Invariant {rule: $rule}) "
                    "SET i.domain = $domain, i.rationale = $rationale, i.status = $status, "
                    "    i.trust_level = $trust, i.updated_at = $now",
                    {
                        "rule": item.get("rule", ""),
                        "domain": item.get("domain", ""),
                        "rationale": item.get("rationale", ""),
                        "status": item.get("status", "probationary"),
                        "trust": item.get("trust_level", "speculative"),
                        "now": now,
                    },
                )

        # Audit live invariant AST body drift
        drift_report = self.invariant_engine.audit_invariant_drift(quarantine_drifted=(not dry_run))

        # Parse existing invariants from file if none passed
        existing_invariants: List[Dict[str, str]] = []
        if INVARIANTS_FILE.exists():
            content = INVARIANTS_FILE.read_text(encoding="utf-8")
            pattern = re.compile(
                r"###\s+Invariant\s+\[(.*?)\](?:\s*\(.*?\))?\s*\n\*\*Rule:\*\*\s*(.*?)\n\*\*Rationale:\*\*\s*(.*?)(?=\n###|\Z)",
                re.DOTALL,
            )
            for match in pattern.finditer(content):
                domain = match.group(1).strip()
                rule = match.group(2).strip()
                rationale = match.group(3).strip()
                existing_invariants.append({
                    "domain": domain,
                    "rule": rule,
                    "rationale": rationale,
                })

        return {
            "inducted": inducted,
            "active": existing_invariants,
            "drift_audit": drift_report,
        }

    # ── Phase 4: Fact Consolidation (Macro-Truth Induction) ───────────────────

    def consolidate_facts(
        self,
        macro_fact: str,
        superseded_fact_ids: List[str],
        project: str = "kortext",
        subject: Optional[str] = None,
        object_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Consolidates fine-grained or redundant episodic facts into a single authoritative macro-truth.
        Creates a new macro-truth fact relationship in FalkorDB with r.is_macro = true, r.project = $project.
        Marks candidate facts in superseded_fact_ids with SET r.superseded = true, r.dormant = true, r.superseded_by = $macro_fact_id.
        The macro fact links `subject` to `object_name`; when they are omitted, every superseded fact
        must share one (subject, object) pair, otherwise nothing is written.
        """
        macro_fact_id = f"fact_macro_{uuid.uuid4().hex[:10]}"
        now = datetime.now(timezone.utc).isoformat()

        updated_count = 0
        skipped = {
            "status": "skipped",
            "macro_fact_id": None,
            "macro_fact": macro_fact,
            "project": project,
            "superseded_count": 0,
            "superseded_fact_ids": list(superseded_fact_ids),
        }

        if superseded_fact_ids:
            if not (subject and object_name):
                pairs = self._query(
                    self.episodic_db,
                    "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) "
                    "WHERE (r.id IN $ids OR r.fact_id IN $ids OR toString(id(r)) IN $ids) "
                    "AND coalesce(r.project, 'kortext') = $project "
                    "RETURN DISTINCT s.name, t.name",
                    {"ids": list(superseded_fact_ids), "project": project},
                )
                rows = pairs[1] if pairs and len(pairs) > 1 and pairs[1] else []
                if len(rows) != 1:
                    logger.warning(
                        "[DreamEngine] Macro fact for '%s' needs subject and object: the %d facts span %d entity pairs.",
                        project, len(superseded_fact_ids), len(rows),
                    )
                    return {**skipped, "reason": f"facts span {len(rows)} entity pairs; pass subject and object"}
                subject, object_name = str(rows[0][0]), str(rows[0][1])

            update_cypher = """
            UNWIND $ids AS target_id
            MATCH ()-[r:RELATES_TO]->()
            WHERE (r.id = target_id OR r.fact_id = target_id OR toString(id(r)) = toString(target_id))
              AND coalesce(r.project, 'kortext') = $project
            SET r.superseded = true, r.dormant = true, r.superseded_by = $macro_fact_id,
                r.superseded_at = $now, r.valid_to = coalesce(r.valid_to, $now)
            RETURN count(r)
            """
            res = self._query(
                self.episodic_db,
                update_cypher,
                {"ids": list(superseded_fact_ids), "macro_fact_id": macro_fact_id, "project": project, "now": now},
            )
            if res and len(res) > 1 and res[1] and res[1][0]:
                try:
                    updated_count = int(res[1][0][0])
                except (ValueError, TypeError):
                    updated_count = 0
            else:
                updated_count = 0

            if updated_count == 0:
                logger.warning(
                    "[DreamEngine] Aborting MACRO_FACT creation for '%s': 0 of %d target facts matched.",
                    project,
                    len(superseded_fact_ids),
                )
                return skipped

        sub_name = subject or project
        obj_name = object_name or "MacroTruth"

        create_cypher = """
        MERGE (s:Entity {name: $sub})
        MERGE (t:Entity {name: $obj})
        CREATE (s)-[r:RELATES_TO {
            id: $macro_fact_id,
            fact: $macro_fact,
            relation: $relation,
            project: $project,
            is_macro: true,
            superseded: false,
            dormant: false,
            importance: 9,
            confidence: 0.98,
            created_at: $now,
            valid_from: $now,
            transaction_time: $now
        }]->(t)
        RETURN id(r)
        """
        self._query(
            self.episodic_db,
            create_cypher,
            {
                "sub": sub_name,
                "obj": obj_name,
                "macro_fact_id": macro_fact_id,
                "macro_fact": macro_fact,
                "relation": "MACRO_FACT",
                "project": project,
                "now": now,
            },
        )

        logger.info(
            "[DreamEngine] Consolidated %d facts into macro fact '%s' for project '%s'.",
            updated_count,
            macro_fact_id,
            project,
        )

        return {
            "status": "ok",
            "macro_fact_id": macro_fact_id,
            "macro_fact": macro_fact,
            "project": project,
            "superseded_count": updated_count,
            "superseded_fact_ids": list(superseded_fact_ids),
        }

    # ── Phase 5: Skill Distillation ───────────────────────────────────────────

    def distill_skill(
        self,
        skill_name: str,
        description: str,
        skill_content: str,
        global_scope: bool = False,
        workspace_root: Optional[str] = None,
        trust_level: str = "tool_confirmed",
    ) -> Path:
        """Safely distills consolidated procedures into a validated, sanitized SKILL.md specification."""
        from kortext.trust.sanitizer import CausalSanitizer

        clean_name = skill_name.strip().lower()
        if not _SKILL_NAME_RE.fullmatch(clean_name):
            raise ValueError(
                f"Invalid skill_name {skill_name!r}: must match ^[a-z0-9][a-z0-9-_]{{0,62}}[a-z0-9]$ (no path separators)."
            )

        sanitizer = CausalSanitizer()
        sanitizer.sanitize(
            subject=clean_name,
            relation="DISTILL_SKILL",
            fact=f"{description}\n{skill_content}",
            source_tier=2 if trust_level in ("tool_confirmed", "verified", "user_approved", "system") else 3,
            trust_level=trust_level,
        )

        if global_scope:
            skills_root = (Path.home() / ".gemini" / "config" / "plugins" / "kortext" / "skills").resolve()
        else:
            base_dir = Path(workspace_root).expanduser().resolve() if workspace_root else KORTEXT_ROOT.resolve()
            skills_root = (base_dir / ".agents" / "skills").resolve()

        dest_dir = (skills_root / clean_name).resolve()
        if not dest_dir.is_relative_to(skills_root):
            raise ValueError(f"Path traversal blocked: {dest_dir} is outside {skills_root}")

        dest_dir.mkdir(parents=True, exist_ok=True)
        skill_file = dest_dir / "SKILL.md"

        stripped_content = skill_content.strip()
        if stripped_content.startswith("---"):
            full_content = stripped_content + "\n"
        else:
            clean_desc = re.sub(r"[\r\n]+", " ", description.strip())[:240]
            full_content = (
                f"---\n"
                f"name: {clean_name}\n"
                f"description: {clean_desc}\n"
                f"---\n\n"
                f"{stripped_content}\n"
            )

        skill_file.write_text(full_content, encoding="utf-8")
        logger.info("[DreamEngine] Distilled skill '%s' to: %s", clean_name, skill_file)
        return skill_file

    # ── Full Dream Cycle & Report Generation ─────────────────────────────────

    async def run_dream_cycle(
        self,
        project: str = "kortext",
        purge_stale: bool = True,
        dry_run: bool = False,
        invariants: Optional[List[Dict[str, str]]] = None,
        stale_fact_names: Optional[List[str]] = None,
        consolidations: Optional[List[Dict[str, Any]]] = None,
        distilled_skills: Optional[List[Dict[str, Any]]] = None,
        global_skills: bool = False,
        workspace_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Runs cognitive reflection phases, consolidation, and skill distillation (100% Agent-Native)."""
        start_time = datetime.now()
        date_str = start_time.strftime("%Y-%m-%d")
        target_projects = (
            self.discover_active_projects()
            if not project or project.strip().lower() in ("all", "*")
            else [project.strip()]
        )

        logger.info(
            "[DreamEngine] Initiating Dream Mode for project '%s' (projects=%s, dry_run=%s, purge_stale=%s)...",
            project,
            target_projects,
            dry_run,
            purge_stale,
        )

        # Phase 1: Synaptic Pruning, Entity Disambiguation & Stale Fact Cleanup
        prune_res = await self.prune_stale_facts(
            purge_stale=purge_stale,
            dry_run=dry_run,
            stale_fact_names=stale_fact_names,
            projects=target_projects,
        )
        dedup_count = sum(self.deduplicate_near_identical_facts(project=p, dry_run=dry_run) for p in target_projects)
        prune_res["deduplicated_facts_count"] = dedup_count
        logger.info(
            "[DreamEngine] Phase 1 complete: %d facts superseded, %d archived, %d deduplicated.",
            prune_res["superseded_count"],
            prune_res["archived_count"],
            dedup_count,
        )

        # Phase 2: Louvain Subsystem Community Detection
        subsystems = await self.cluster_subsystems(persist=(not dry_run))
        logger.info("[DreamEngine] Phase 2 complete: %d architectural subsystems clustered.", len(subsystems))

        # Phase 3: Abstract Invariant Induction & AST Drift Audit
        invariant_results = await self.induce_invariants(invariants=invariants, dry_run=dry_run)
        active_invariants = invariant_results["active"]
        drift_audit = invariant_results["drift_audit"]
        logger.info(
            "[DreamEngine] Phase 3 complete: %d active invariants, %d drifted.",
            len(active_invariants),
            drift_audit.get("drifted_count", 0),
        )

        # Phase 4: Fact Consolidation
        consolidation_results: List[Dict[str, Any]] = []
        if consolidations:
            for item in consolidations:
                macro_fact = item.get("macro_fact") or item.get("fact")
                superseded_ids = item.get("superseded_fact_ids") or item.get("supersedes") or []
                item_project = item.get("project", project)
                if macro_fact:
                    if not dry_run:
                        res = self.consolidate_facts(
                            macro_fact=macro_fact,
                            superseded_fact_ids=superseded_ids,
                            project=item_project,
                            subject=item.get("subject"),
                            object_name=item.get("object"),
                        )
                        consolidation_results.append(res)
                    else:
                        consolidation_results.append({
                            "status": "dry_run",
                            "macro_fact": macro_fact,
                            "superseded_count": len(superseded_ids),
                            "superseded_fact_ids": list(superseded_ids),
                            "project": item_project,
                        })
            logger.info("[DreamEngine] Phase 4 complete: %d macro facts consolidated.", len(consolidation_results))

        # Phase 5: Skill Distillation (agent-supplied skills only)
        distilled_skills_results: List[Dict[str, Any]] = []
        effective_skills: List[Dict[str, Any]] = list(distilled_skills or [])
        if effective_skills:
            for s in effective_skills:
                s_name = (s.get("name") or s.get("skill_name") or "unnamed-skill").strip().lower()
                s_desc = s.get("description", "")
                s_content = s.get("content") or s.get("skill_content") or ""
                s_global = s.get("global_scope", global_skills)
                s_ws = s.get("workspace_root", workspace_root)
                s_trust = str(s.get("trust_level") or "tool_confirmed")
                if not dry_run:
                    created_path = self.distill_skill(
                        skill_name=s_name,
                        description=s_desc,
                        skill_content=s_content,
                        global_scope=s_global,
                        workspace_root=s_ws,
                        trust_level=s_trust,
                    )
                    distilled_skills_results.append({
                        "name": s_name,
                        "description": s_desc,
                        "path": str(created_path),
                        "global_scope": s_global,
                    })
                else:
                    dest_base = (
                        (Path.home() / ".gemini" / "config" / "plugins" / "kortext" / "skills" / s_name)
                        if s_global
                        else ((Path(s_ws) if s_ws else KORTEXT_ROOT) / ".agents" / "skills" / s_name)
                    )
                    distilled_skills_results.append({
                        "name": s_name,
                        "description": s_desc,
                        "path": str(dest_base / "SKILL.md"),
                        "global_scope": s_global,
                        "status": "dry_run",
                    })
            logger.info("[DreamEngine] Phase 5 complete: %d skills distilled.", len(distilled_skills_results))

        # Phase 6: Automated Contradiction Surfacing & Proposal Staging
        staged_contradictions = self.surface_potential_contradictions(project=project)
        staged_proposals = {
            "contradictions": staged_contradictions,
            "macro_truth_candidates": consolidation_results,
        }

        # Dream Synthesis Report Generation
        duration = (datetime.now() - start_time).total_seconds()
        report_lines = [
            f"# Kortext Dream Report: {date_str}",
            f"**Execution Mode:** {'Dry Run' if dry_run else 'Live Consolidation'} | **Duration:** {duration:.2f}s | **Project:** {project}\n",
            "## 1. Synaptic Pruning & Fact Cleanup",
            f"- **Facts Superseded:** {prune_res['superseded_count']}",
            f"- **Stale Nodes Purged:** {prune_res['archived_count']}",
        ]
        if prune_res["details"]:
            for d in prune_res["details"]:
                report_lines.append(f"  - *{d['stale_name']}* -> Replaced by **{d['superseded_by']}** ({d['reason']})")
        else:
            report_lines.append("  - No conflicting or stale operational facts detected. Knowledge base is high-density.")

        report_lines.append("\n## 2. Synthesized Architectural Subsystems (Louvain Modularity)")
        for sub in subsystems:
            report_lines.append(f"### Subsystem: {sub['name']} ({sub['file_count']} files)")
            report_lines.append(f"*{sub['description']}*")
            sample_files = [Path(f).name for f in sub["files"][:4]]
            report_lines.append(
                f"- Member files: `{', '.join(sample_files)}`"
                + (f" and {len(sub['files'])-4} more..." if len(sub["files"]) > 4 else "")
            )

        report_lines.append("\n## 3. Induced Codebase Invariants")
        if active_invariants:
            for inv in active_invariants:
                report_lines.append(f"- **[{inv.get('domain', 'Core').upper()}]**: {inv.get('rule')}")
                report_lines.append(f"  *Rationale: {inv.get('rationale')}*")
        else:
            report_lines.append("- Existing invariants up-to-date in `journal/invariants.md`.")

        report_lines.append("\n## 4. AST Grounding & Invariant Drift Audit")
        report_lines.append(f"- Grounded invariants: {drift_audit.get('total_grounded', 0)}")
        report_lines.append(f"- Verified: {drift_audit.get('verified_count', 0)}")
        report_lines.append(f"- Drifted / Stale: {drift_audit.get('drifted_count', 0)}")
        report_lines.append(f"- Orphaned: {drift_audit.get('orphaned_count', 0)}")
        if drift_audit.get("drifted"):
            for dr in drift_audit["drifted"]:
                report_lines.append(
                    f"  - [DRIFTED] `{dr.get('symbol_name')}` in `{Path(dr.get('file_path', '')).name}`: rule '{dr.get('rule')[:60]}...'"
                )

        report_lines.append("\n## 5. Consolidated Macro Facts")
        if consolidation_results:
            for c in consolidation_results:
                report_lines.append(f"- **Macro Fact:** {c.get('macro_fact')} (Superseded {c.get('superseded_count', 0)} facts)")
        else:
            report_lines.append("- No fact consolidations performed.")

        report_lines.append("\n## 6. Distilled Agent Skills")
        if distilled_skills_results:
            for s in distilled_skills_results:
                report_lines.append(f"- **Skill:** {s.get('name')} -> `{s.get('path')}`")
        else:
            report_lines.append("- No agent skills distilled.")

        report_lines.append("\n## 7. Staged Proposals for Agent Review & Reflection")
        if staged_contradictions:
            report_lines.append("*The following potential contradictions were surfaced and staged for agent reflection before committing:*")
            for sc in staged_contradictions:
                report_lines.append(f"- **[{sc['relation']}]** on `{sc['subject']}` -> `{sc['object']}`:")
                report_lines.append(f"  - Option A: \"{sc['fact_a']}\"")
                report_lines.append(f"  - Option B: \"{sc['fact_b']}\"")
        else:
            report_lines.append("- No candidate contradictions staged.")

        report_md = "\n".join(report_lines)

        report_path_str = "dry-run"
        reports_path_str = "dry-run"
        if not dry_run:
            if workspace_root:
                ws_journal = Path(workspace_root) / "journal"
                target_dreams_dir = ws_journal / "dreams"
                target_reports_dir = ws_journal / "reports"
            else:
                target_dreams_dir = DREAMS_DIR
                target_reports_dir = REPORTS_DIR

            target_dreams_dir.mkdir(parents=True, exist_ok=True)
            target_reports_dir.mkdir(parents=True, exist_ok=True)

            dream_file = target_dreams_dir / f"{date_str}-dream.md"
            with open(dream_file, "w", encoding="utf-8") as f:
                f.write(report_md)
            report_path_str = str(dream_file)

            report_file = target_reports_dir / f"{date_str}-dream.md"
            with open(report_file, "w", encoding="utf-8") as f:
                f.write(report_md)
            reports_path_str = str(report_file)

            logger.info("[DreamEngine] Dream Report persisted to: %s", dream_file)

        return {
            "status": "ok",
            "duration_seconds": duration,
            "superseded_count": prune_res.get("superseded_count", 0),
            "archived_count": prune_res.get("archived_count", 0),
            "subsystems_count": len(subsystems),
            "subsystem_clusters": len(subsystems),
            "invariants_count": len(active_invariants),
            "invariants_inducted": len(invariant_results.get("inducted", [])),
            "invariant_drift": drift_audit,
            "consolidations": consolidation_results,
            "consolidated_facts_count": len(consolidation_results),
            "distilled_skills": distilled_skills_results,
            "distilled_skills_count": len(distilled_skills_results),
            "staged_proposals": staged_proposals,
            "markdown_report": report_md,
            "dream_report_path": report_path_str,
            "reports_path": reports_path_str,
            "invariants_path": str(INVARIANTS_FILE),
            "pruned_facts": {
                "active_facts_remaining": prune_res.get("active_count", 0),
                "superseded_facts_pruned": prune_res.get("superseded_count", 0),
                "contradictions_flagged": prune_res.get("contradictions", []),
            },
        }


KortextDreamEngine = DreamEngine


def main():
    default_proj = (
        os.getenv("KORTEXT_PROJECT_ID")
        or os.getenv("KORTEXT_PROJECT")
        or Path.cwd().name
        or "kortext"
    )
    parser = argparse.ArgumentParser(description="Kortext: dream mode consolidation engine")
    parser.add_argument("--project", default=default_proj, help="Project label")
    parser.add_argument("--purge-stale", action="store_true", help="Purge stale facts")
    parser.add_argument("--dry-run", action="store_true", help="Preview without modifying")
    args = parser.parse_args()

    engine = DreamEngine()
    res = asyncio.run(
        engine.run_dream_cycle(
            project=args.project,
            purge_stale=args.purge_stale,
            dry_run=args.dry_run,
        )
    )
    sys.stdout.write(res["markdown_report"] + "\n")


if __name__ == "__main__":
    main()
