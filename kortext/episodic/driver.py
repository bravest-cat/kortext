"""
Episodic memory driver for FalkorDB.

The agent supplies entities, facts and supersessions directly; nothing calls an LLM.
Search scores current facts by dense similarity (lexical coverage when no embedder is
available) and abstains when nothing clears the threshold.
"""
from __future__ import annotations

import logging
import math
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import redis

from kortext.falkordb_client import FalkorDBClient, FALKORDB_HOST, FALKORDB_PORT, FALKORDB_PASS
from kortext.trust.sanitizer import CausalSanitizer, fence_memory_by_provenance

logger = logging.getLogger(__name__)


_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "what", "where", "when", "which", "who",
    "whom", "whose", "how", "why", "are", "was", "were", "has", "have", "had", "does", "did", "can",
    "could", "would", "should", "into", "about", "over", "after", "before", "between", "under",
    "their", "there", "they", "them", "his", "her", "its", "our", "your", "any", "all", "is", "do",
})
# Minimum score for a result. Calibrated for BAAI/bge-small-en-v1.5 on tests/fixtures/longmemeval_sample.json
# (unanswerable questions topped out at 0.405, answerable ones started at 0.444).
# shortcut: one threshold per process, recalibrate with KORTEXT_EPISODIC_MIN_SCORE when EMBED_BASE_URL points at another model.
DENSE_MIN_SCORE = float(os.getenv("KORTEXT_EPISODIC_MIN_SCORE", "0.42"))
LEXICAL_MIN_SCORE = float(os.getenv("KORTEXT_EPISODIC_MIN_LEXICAL", "0.25"))
# Results this close to the best score count as ties and are ordered newest first, so an update outranks the fact it replaced.
RECENCY_TIE_MARGIN = 0.02
_CANDIDATE_LIMIT = 300


def _content_tokens(text: str) -> List[str]:
    return [w for w in re.split(r"\W+", (text or "").lower()) if len(w) > 2 and w not in _STOPWORDS]


def _strip_names(text: str, names: List[str]) -> str:
    """Removes entity names so similarity measures what is said about an entity, not who it is."""
    for name in sorted({n for n in names if n and len(n) > 2}, key=len, reverse=True):
        text = re.sub(re.escape(name) + r"(?:'s)?", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _dense_embedder() -> Optional[Any]:
    """The configured dense embedder, or None when only the hash fallback is available."""
    try:
        from kortext.embeddings import _OfflineDeterministicEmbeddingModel, get_dense_embedder

        emb = get_dense_embedder()
        model = getattr(emb, "model", None)
        if isinstance(model, _OfflineDeterministicEmbeddingModel):
            return None
        return emb
    except Exception as exc:
        logger.warning("Dense embedder unavailable for episodic search, using lexical scoring: %s", exc)
        return None


def jaro_winkler_similarity(s1: str, s2: str, p: float = 0.1) -> float:
    """Computes Jaro-Winkler similarity in [0.0, 1.0] for fast deterministic entity resolution."""
    a = (s1 or "").strip().lower()
    b = (s2 or "").strip().lower()
    if a == b:
        return 1.0 if a else 0.0
    len_a, len_b = len(a), len(b)
    if len_a == 0 or len_b == 0:
        return 0.0

    match_dist = max(0, (max(len_a, len_b) // 2) - 1)
    a_matches = [False] * len_a
    b_matches = [False] * len_b
    matches = 0
    transpositions = 0

    for i in range(len_a):
        start = max(0, i - match_dist)
        end = min(i + match_dist + 1, len_b)
        for j in range(start, end):
            if b_matches[j] or a[i] != b[j]:
                continue
            a_matches[i] = True
            b_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(len_a):
        if not a_matches[i]:
            continue
        while not b_matches[k]:
            k += 1
        if a[i] != b[k]:
            transpositions += 1
        k += 1

    jaro = (
        (matches / len_a)
        + (matches / len_b)
        + ((matches - transpositions / 2.0) / matches)
    ) / 3.0

    prefix = 0
    for ch_a, ch_b in zip(a[:4], b[:4]):
        if ch_a == ch_b:
            prefix += 1
        else:
            break

    return jaro + prefix * p * (1.0 - jaro)

FALKORDB_DB   = os.getenv("KORTEXT_DATABASE", os.getenv("FALKORDB_DATABASE", "kortext"))


TRUST_ANCHOR_LEVELS: Dict[str, float] = {
    "speculative": 0.50,
    "untrusted": 0.30,
    "tool_confirmed": 0.60,
    "verified": 0.80,
    "user_approved": 1.00,
    "system": 1.00,
}

VALID_CATEGORIES = (
    "decision",
    "roadmap",
    "implementation",
    "experiment",
    "benchmark",
    "investigation",
    "research",
    "report",
)


class AgentNativeEpisodicDriver:
    """Direct FalkorDB driver for Episodic & Temporal Knowledge Memory."""

    def __init__(
        self,
        host: str = FALKORDB_HOST,
        port: int = FALKORDB_PORT,
        password: Optional[str] = FALKORDB_PASS,
        db_name: str = FALKORDB_DB,
    ):
        self.host = host
        self.port = port
        self.password = password or None
        self.db_name = db_name
        self.client = FalkorDBClient(host=self.host, port=self.port, password=self.password)
        self.sanitizer = CausalSanitizer()
        self._entity_cache: Dict[str, str] = {}

    def _get_client(self) -> redis.Redis:
        return self.client.get_redis()

    def _query(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> List[List[Any]]:
        return self.client.query(self.db_name, cypher, params)

    def resolve_canonical_entity(self, raw_name: str, threshold: float = 0.88) -> str:
        """
        Resolves near-duplicate entity names against existing :Entity nodes in FalkorDB
        using Jaro-Winkler similarity (>= 0.88) and token Jaccard overlap so variants
        like 'FalkorDB' and 'falkordb' merge into a single canonical node.
        Uses an in-memory cache to avoid N+1 roundtrips during batch outcomes.
        """
        cleaned = (raw_name or "").strip()
        if not cleaned or len(cleaned) < 3:
            return cleaned
        cache_key = cleaned.lower()
        if cache_key in self._entity_cache:
            return self._entity_cache[cache_key]
        try:
            if getattr(self.client, "_is_live_falkor_client", False) is not True:
                self._entity_cache[cache_key] = cleaned
                return cleaned
            cand_tokens = [
                t for t in cleaned.lower().replace("_", " ").replace("-", " ").split()
                if len(t) >= 3
            ]
            res = self._query(
                "MATCH (n:Entity) "
                "WHERE toLower(n.name) STARTS WITH toLower($prefix) "
                "OR ANY(tok IN $tokens WHERE toLower(n.name) CONTAINS tok) "
                "RETURN n.name LIMIT 35",
                {"prefix": cleaned[:2], "tokens": cand_tokens[:4]},
            )
            if not res or len(res) < 2 or not isinstance(res[1], list):
                self._entity_cache[cache_key] = cleaned
                return cleaned
            best_name = cleaned
            best_score = 0.0
            cand_subtokens = set(re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+", cleaned))
            cand_token_set = {t.lower() for t in cand_subtokens if len(t) >= 2} or set(
                cleaned.lower().replace("_", " ").replace("-", " ").split()
            )
            for row in res[1]:
                if not row or not row[0]:
                    continue
                existing_name = str(row[0]).strip()
                if existing_name.lower() == cleaned.lower():
                    self._entity_cache[cache_key] = existing_name
                    return existing_name
                jw = jaro_winkler_similarity(cleaned, existing_name)
                ex_subtokens = set(re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+", existing_name))
                ex_tokens = {t.lower() for t in ex_subtokens if len(t) >= 2} or set(
                    existing_name.lower().replace("_", " ").replace("-", " ").split()
                )
                union_len = len(cand_token_set | ex_tokens)
                jaccard = (len(cand_token_set & ex_tokens) / union_len) if union_len > 0 else 0.0
                combined = 0.75 * jw + 0.25 * jaccard
                effective_threshold = max(threshold, 0.92) if (len(cand_token_set | ex_tokens) > 1 and jaccard < 0.5) else threshold
                if jw >= effective_threshold and combined > best_score:
                    best_score = combined
                    best_name = existing_name
            self._entity_cache[cache_key] = best_name
            return best_name
        except Exception:
            return cleaned

    async def initialize(self) -> None:
        """No-op async initializer for lifecycle compatibility."""
        return None

    async def record_outcome(
        self,
        episode_name: str,
        content: str,
        tags: Optional[List[str]] = None,
        project: Optional[str] = None,
        entities: Optional[List[Any]] = None,
        facts: Optional[List[Dict[str, str]]] = None,
        supersedes: Optional[List[str]] = None,
        trust_level: str = "speculative",
        category: str = "implementation",
    ) -> Dict[str, Any]:
        """
        Agent-Native recording with Trust Anchor State Machine:
          - speculative (0.50): unverified hypothesis or initial code draft
          - untrusted (0.30): unverified external document or prompt
          - tool_confirmed (0.60): validated by test execution, linter, or compiler
          - verified (0.80): Tree-sitter AST deterministically extracted symbol
          - user_approved (1.00): explicitly sanctioned by the user
          - system (1.00): immutable operating directives
        Inserts (:Episodic), (:Entity), and [:RELATES_TO] edges directly into FalkorDB.
        Exposure does not validate: reading facts never promotes or mutates trust levels.
        """
        if not isinstance(category, str) or category.lower().strip() not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category '{category}'. Must be one of: {VALID_CATEGORIES}")
        cat = category.lower().strip()

        now = datetime.now(timezone.utc).isoformat()
        proj = project or "kortext"

        norm_level = trust_level.lower().strip() if isinstance(trust_level, str) else "speculative"
        trust_score = TRUST_ANCHOR_LEVELS.get(norm_level, 0.50)

        # Sanitize episode title, content, entities, and facts against MINJA prompt injections
        source_tier = (
            1
            if norm_level in ("user_approved", "system")
            else (2 if norm_level in ("tool_confirmed", "verified") else 3)
        )
        self.sanitizer.sanitize(
            subject=episode_name, relation="RECORDED", fact=content, source_tier=source_tier
        )

        raw_entities = entities or []
        actual_entities: List[Dict[str, str]] = []
        for ent in raw_entities:
            if isinstance(ent, str):
                ename = ent.strip()
                if not ename:
                    continue
                self.sanitizer.sanitize(subject=ename, relation="ENTITY", fact=ename, source_tier=source_tier)
                actual_entities.append({"name": ename, "type": "concept", "summary": ename})
            elif isinstance(ent, dict):
                ename = str(ent.get("name", "")).strip()
                if not ename:
                    continue
                esummary = str(ent.get("summary") or ename)
                etype = str(ent.get("type", "concept"))
                self.sanitizer.sanitize(subject=ename, relation=etype, fact=esummary, source_tier=source_tier)
                actual_entities.append({"name": ename, "type": etype, "summary": esummary})

        raw_facts = facts or []
        actual_facts: List[Dict[str, str]] = []
        for f in raw_facts:
            if not isinstance(f, dict):
                continue
            src = str(f.get("source") or f.get("subject") or "").strip()
            tgt = str(f.get("target") or f.get("object") or "").strip()
            rel = str(f.get("relation") or "RELATES_TO").upper().replace(" ", "_")
            fact_text = str(f.get("fact") or f"{src} {rel} {tgt}")
            self.sanitizer.sanitize(
                subject=src or episode_name,
                relation=rel,
                fact=f"{tgt}: {fact_text}" if tgt else fact_text,
                source_tier=source_tier,
            )
            actual_facts.append({"source": src, "target": tgt, "relation": rel, "fact": fact_text})

        # 1. Supersede older conflicting facts if specified
        superseded_count = 0
        if supersedes:
            for pattern in supersedes:
                res = self._query(
                    "MATCH (old:Episodic) WHERE (old.name CONTAINS $pat OR old.content CONTAINS $pat) "
                    "AND (old.superseded IS NULL OR old.superseded = false) "
                    "SET old.superseded = true, old.superseded_by = $by, old.superseded_at = $now, "
                    "old.superseded_reason = 'Superseded by ' + $by "
                    "RETURN count(old)",
                    {"pat": pattern, "by": episode_name, "now": now},
                )
                if res and len(res) > 1 and res[1]:
                    try:
                        superseded_count += int(res[1][0][0])
                    except Exception:
                        logger.debug("Suppressed non-fatal exception")

        # 2. Persist (:Episodic) Node with Trust Anchor and link to Project
        tags_str = ", ".join(tags) if tags else ""
        self._query(
            "MERGE (e:Episodic {name: $name}) "
            "SET e.content = $content, e.created_at = $now, e.project = $project, "
            "e.tags = $tags, e.superseded = false, e.trust_level = $trust_level, "
            "e.trust_score = $trust_score, e.category = $category "
            "MERGE (p:Project {id: $project}) "
            "ON CREATE SET p.name = $project "
            "MERGE (p)-[:HAS_EPISODE]->(e)",
            {
                "name": episode_name,
                "content": content,
                "now": now,
                "project": proj,
                "tags": tags_str,
                "trust_level": norm_level,
                "trust_score": trust_score,
                "category": cat,
            },
        )

        # Write Entities (resolved via Jaro-Winkler + Token Jaccard entity disambiguation)
        for ent in actual_entities:
            raw_ename = ent.get("name", "").strip()
            if not raw_ename:
                continue
            ename = self.resolve_canonical_entity(raw_ename)
            esummary = ent.get("summary") or ename
            etype = ent.get("type", "concept")

            self._query(
                "MERGE (n:Entity {name: $name}) "
                "SET n.summary = $summary, n.entity_type = $type, n.updated_at = $now "
                "WITH n "
                "MATCH (e:Episodic {name: $ename}) "
                "MERGE (e)-[:MENTIONS]->(n)",
                {
                    "name": ename,
                    "summary": esummary,
                    "type": etype,
                    "now": now,
                    "ename": episode_name,
                },
            )

        # Write Fact Edges with Trust Anchor
        for f in actual_facts:
            raw_src = f.get("source", "").strip()
            raw_tgt = f.get("target", "").strip()
            src = self.resolve_canonical_entity(raw_src) if raw_src else ""
            tgt = self.resolve_canonical_entity(raw_tgt) if raw_tgt else ""
            rel = (f.get("relation") or "RELATES_TO").upper().replace(" ", "_")
            fact_text = f.get("fact") or f"{src} {rel} {tgt}"

            if src and tgt:
                fact_id = f"fact_{uuid.uuid4().hex[:10]}"
                self._query(
                    "MERGE (s:Entity {name: $src}) "
                    "MERGE (t:Entity {name: $tgt}) "
                    "MERGE (s)-[r:RELATES_TO {fact: $fact, relation: $rel}]->(t) "
                    "SET r.id = $fact_id, r.created_at = $now, r.valid_from = $now, r.valid_to = null, "
                    "r.transaction_time = $now, r.source_session = $session, r.importance = 5, "
                    "r.confidence = 0.95, r.dormant = false, r.superseded = false, "
                    "r.trust_level = $trust_level, r.trust_score = $trust_score, "
                    "r.project = $project",
                    {
                        "src": src,
                        "tgt": tgt,
                        "fact": fact_text,
                        "rel": rel,
                        "now": now,
                        "fact_id": fact_id,
                        "session": episode_name,
                        "trust_level": norm_level,
                        "trust_score": trust_score,
                        "project": proj,
                    },
                )

        auto_entities = 0
        if cat == "decision" and not actual_entities:
            self._query(
                "MATCH (e:Episodic {name: $ename}) "
                "MERGE (adr:Entity:ADR {name: $ename}) "
                "SET adr.summary = $summary, adr.entity_type = 'adr', adr.updated_at = $now "
                "MERGE (e)-[:DECIDES]->(adr)",
                {
                    "ename": episode_name,
                    "summary": content,
                    "now": now,
                },
            )
            auto_entities += 1
        elif cat == "roadmap":
            self._query(
                "MATCH (e:Episodic {name: $ename}) "
                "MERGE (m:Entity:Milestone {name: $ename}) "
                "SET m.summary = $summary, m.entity_type = 'milestone', m.updated_at = $now "
                "MERGE (e)-[:MILESTONE]->(m)",
                {
                    "ename": episode_name,
                    "summary": content,
                    "now": now,
                },
            )
            auto_entities += 1

        return {
            "status": "ok",
            "episode": episode_name,
            "category": cat,
            "trust_level": norm_level,
            "trust_score": trust_score,
            "entities_written": len(actual_entities) + auto_entities,
            "facts_written": len(actual_facts),
            "superseded_count": superseded_count,
        }

    def record_fact(
        self,
        subject: str,
        relation: str,
        fact: str,
        object_name: Optional[str] = None,
        importance: int = 5,
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
        supersedes: Optional[str] = None,
        confidence: float = 0.95,
        source_session: Optional[str] = None,
        source_tier: int = 2,
        project: Optional[str] = None,
        trust_level: Optional[str] = None,
    ) -> str:
        """
        Records an episodic relationship between subject and object entities with bi-temporal attribution.
        Automatically resolves conflicting facts by materializing [:SUPERSEDES] edges (Section 53, Invariant D5, D30).
        """
        # Sanitize untrusted fact text against MINJA prompt injections before persisting
        self.sanitizer.sanitize(subject=subject, relation=relation, fact=fact, source_tier=source_tier)
        from kortext.trust.sanitizer import strip_invisible_unicode

        subject = strip_invisible_unicode(subject).strip()
        relation = strip_invisible_unicode(relation).strip()
        fact = strip_invisible_unicode(fact).strip()
        if object_name:
            object_name = strip_invisible_unicode(object_name).strip()

        fact_id = f"fact_{uuid.uuid4().hex[:10]}"
        now = datetime.now(timezone.utc).isoformat()
        v_start = valid_from or now
        obj = (object_name or relation).strip()
        proj = project or getattr(self, "db_name", None) or "kortext"

        if trust_level:
            norm_level = trust_level.lower().strip()
        elif source_tier == 1:
            norm_level = "user_approved"
        elif source_tier == 2:
            norm_level = "tool_confirmed"
        else:
            norm_level = "untrusted"
        trust_score = TRUST_ANCHOR_LEVELS.get(norm_level, 0.60)

        # 1. Ensure Subject and Object Nodes Exist
        merge_nodes_cypher = """
        MERGE (s:Entity {name: $sub})
        ON CREATE SET s.first_seen = $now, s.trust_tier = 2
        MERGE (t:Entity {name: $obj})
        ON CREATE SET t.first_seen = $now, t.trust_tier = 2
        """
        self._query(merge_nodes_cypher, {"sub": subject, "obj": obj, "now": now})

        # 2. Materialize Directed Bi-Temporal Relationship
        rel_cypher = """
        MATCH (s:Entity {name: $sub})
        MATCH (t:Entity {name: $obj})
        CREATE (s)-[r:RELATES_TO {
            id: $fact_id,
            fact: $fact,
            relation: $rel,
            project: $project,
            valid_from: $v_start,
            valid_to: $v_end,
            created_at: $now,
            transaction_time: $now,
            importance: $imp,
            confidence: $conf,
            source_session: $session,
            dormant: false,
            superseded: false,
            trust_level: $trust_level,
            trust_score: $trust_score
        }]->(t)
        RETURN r.id
        """
        params = {
            "sub": subject,
            "obj": obj,
            "rel": relation,
            "fact_id": fact_id,
            "fact": fact,
            "v_start": v_start,
            "v_end": valid_to,
            "now": now,
            "imp": importance,
            "conf": confidence,
            "session": source_session or "active_session",
            "project": proj,
            "trust_level": norm_level,
            "trust_score": trust_score,
        }
        self._query(rel_cypher, params)

        # 3. Handle Superseded Fact Invalidations
        if supersedes:
            self._invalidate_and_link_superseded(supersedes, fact_id, now)

        return fact_id

    def _invalidate_and_link_superseded(self, old_fact_id: str, new_fact_id: str, timestamp: str):
        """
        Closes the validity window of the old fact, marks it dormant and superseded,
        and sets provisional_stale=true on 1-hop outgoing facts derived from or connected to the superseded fact/object.
        """
        cypher = """
        MATCH ()-[r_old:RELATES_TO {id: $old_id}]->(obj)
        SET r_old.valid_to = $now, r_old.superseded_at = $now, r_old.dormant = true, r_old.superseded = true, r_old.superseded_by = $new_id
        WITH DISTINCT obj
        OPTIONAL MATCH (obj)-[r_dep:RELATES_TO]->()
        WHERE r_dep.id <> $new_id AND r_dep.id <> $old_id
        SET r_dep.provisional_stale = true
        WITH DISTINCT obj
        OPTIONAL MATCH ()-[r_derived:RELATES_TO]->()
        WHERE r_derived.derived_from = $old_id
        SET r_derived.provisional_stale = true
        """
        self._query(cypher, {
            "old_id": old_fact_id,
            "new_id": new_fact_id,
            "now": timestamp,
        })

    def query_facts_as_of(
        self,
        subject: str,
        as_of_date: str,
        limit: int = 10,
        project: Optional[str] = None,
        as_of_asserted_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Executes a point-in-time bi-temporal query returning facts valid on `as_of_date` (World Timeline T)
        and optionally known to the system as of `as_of_asserted_date` (System Timeline T', Invariant D5).
        """
        target_project = project.strip() if isinstance(project, str) else None
        filter_project = bool(target_project and target_project.lower() != "all")
        proj_filter = "  AND coalesce(r.project, 'kortext') = $project\n" if filter_project else ""
        sys_time_filter = (
            "  AND coalesce(r.transaction_time, r.created_at, $asserted_date) <= $asserted_date\n"
            "  AND (r.superseded_at IS NULL OR r.superseded_at > $asserted_date)\n"
            if as_of_asserted_date
            else ""
        )

        cypher = f"""
        MATCH (s:Entity {{name: $sub}})-[r:RELATES_TO]->(target)
        WHERE r.valid_from <= $target_date
          AND (r.valid_to IS NULL OR r.valid_to > $target_date)
        {sys_time_filter}{proj_filter}RETURN r.id, s.name, r.relation, r.fact, r.valid_from, r.valid_to, r.confidence
        ORDER BY r.valid_from DESC
        LIMIT $lim
        """
        params: Dict[str, Any] = {
            "sub": subject,
            "target_date": as_of_date,
            "lim": limit,
        }
        if as_of_asserted_date:
            params["asserted_date"] = as_of_asserted_date
        if filter_project:
            params["project"] = target_project
        raw = self._query(cypher, params)

        if not raw or len(raw) < 2 or not raw[1]:
            return []

        results = []
        for row in raw[1]:
            results.append({
                "fact_id": row[0],
                "subject": row[1],
                "relation": row[2],
                "fact": row[3],
                "valid_from": row[4],
                "valid_to": row[5],
                "confidence": row[6],
            })
        return results

    def query_multi_hop_facts(
        self,
        entity_name: str,
        max_hops: int = 2,
        limit: int = 20,
        project: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Traverses episodic memory up to max_hops (default 2) starting from entity_name.
        Discovers connected facts across different sessions via intermediate bridge entities.
        Filters out dormant and superseded relationships.
        """
        if not entity_name or max_hops < 1:
            return []

        target_project = project.strip() if isinstance(project, str) else None
        filter_project = bool(target_project and target_project.lower() != "all")
        proj_filter_1hop = "  AND coalesce(r.project, 'kortext') = $project\n" if filter_project else ""

        # 1. Fetch 1-hop facts connected to entity_name (both outgoing and incoming)
        cypher_1hop = f"""
        MATCH (s:Entity {{name: $entity}})-[r:RELATES_TO]-(t:Entity)
        WHERE coalesce(r.superseded, false) = false
          AND coalesce(r.dormant, false) = false
        {proj_filter_1hop}RETURN r.id, startNode(r).name, r.relation, endNode(r).name, r.fact, r.source_session,
               r.valid_from, r.valid_to, r.confidence, t.name
        ORDER BY coalesce(r.valid_from, r.transaction_time, '') DESC
        LIMIT $lim
        """
        params_1hop: Dict[str, Any] = {"entity": entity_name, "lim": limit}
        if filter_project:
            params_1hop["project"] = target_project
        raw_1hop = self._query(cypher_1hop, params_1hop)

        results: List[Dict[str, Any]] = []
        seen_fact_ids: set[str] = set()
        seen_signatures: set[tuple[str, str, str, str]] = set()
        bridge_entities: List[str] = []

        if raw_1hop and len(raw_1hop) > 1 and raw_1hop[1]:
            for row in raw_1hop[1]:
                fid = str(row[0]) if row[0] else ""
                s_name = str(row[1]) if row[1] else ""
                rel = str(row[2]) if row[2] else ""
                o_name = str(row[3]) if row[3] else ""
                fact_text = str(row[4]) if row[4] else f"{s_name} {rel} {o_name}"
                sess = str(row[5]) if len(row) > 5 and row[5] else None
                v_from = str(row[6]) if len(row) > 6 and row[6] else None
                v_to = str(row[7]) if len(row) > 7 and row[7] else None
                conf = float(row[8]) if len(row) > 8 and row[8] is not None else 0.95
                neighbor = str(row[9]) if len(row) > 9 and row[9] else ""

                sig = (s_name, rel, o_name, fact_text)
                if (fid and fid in seen_fact_ids) or sig in seen_signatures:
                    continue
                if fid:
                    seen_fact_ids.add(fid)
                seen_signatures.add(sig)

                results.append({
                    "fact_id": fid,
                    "subject": s_name,
                    "relation": rel,
                    "target": o_name,
                    "object": o_name,
                    "fact": fact_text,
                    "source_session": sess,
                    "valid_from": v_from,
                    "valid_to": v_to,
                    "confidence": conf,
                    "hops": 1,
                    "bridge_entity": None,
                    "path": [entity_name, neighbor] if neighbor else [entity_name],
                })

                if neighbor and neighbor != entity_name and neighbor not in bridge_entities:
                    bridge_entities.append(neighbor)

        # 2. Fetch 2-hop facts connected via intermediate bridge entities
        if max_hops >= 2 and bridge_entities:
            hop2_limit = max(5, limit // 2)
            proj_filter_2hop = "  AND coalesce(r2.project, 'kortext') = $project\n" if filter_project else ""
            for bridge in bridge_entities[:10]:
                cypher_2hop = f"""
                MATCH (b:Entity {{name: $bridge}})-[r2:RELATES_TO]-(t2:Entity)
                WHERE coalesce(r2.superseded, false) = false
                  AND coalesce(r2.dormant, false) = false
                  AND t2.name <> $entity AND t2.name <> $bridge
                {proj_filter_2hop}RETURN r2.id, startNode(r2).name, r2.relation, endNode(r2).name, r2.fact, r2.source_session,
                       r2.valid_from, r2.valid_to, r2.confidence, t2.name
                ORDER BY coalesce(r2.valid_from, r2.transaction_time, '') DESC
                LIMIT $lim
                """
                params_2hop: Dict[str, Any] = {
                    "bridge": bridge,
                    "entity": entity_name,
                    "lim": hop2_limit,
                }
                if filter_project:
                    params_2hop["project"] = target_project
                raw_2hop = self._query(cypher_2hop, params_2hop)
                if raw_2hop and len(raw_2hop) > 1 and raw_2hop[1]:
                    for row in raw_2hop[1]:
                        fid = str(row[0]) if row[0] else ""
                        s_name = str(row[1]) if row[1] else ""
                        rel = str(row[2]) if row[2] else ""
                        o_name = str(row[3]) if row[3] else ""
                        fact_text = str(row[4]) if row[4] else f"{s_name} {rel} {o_name}"
                        sess = str(row[5]) if len(row) > 5 and row[5] else None
                        v_from = str(row[6]) if len(row) > 6 and row[6] else None
                        v_to = str(row[7]) if len(row) > 7 and row[7] else None
                        conf = float(row[8]) if len(row) > 8 and row[8] is not None else 0.95
                        neighbor = str(row[9]) if len(row) > 9 and row[9] else ""

                        sig = (s_name, rel, o_name, fact_text)
                        if (fid and fid in seen_fact_ids) or sig in seen_signatures:
                            continue
                        if fid:
                            seen_fact_ids.add(fid)
                        seen_signatures.add(sig)

                        results.append({
                            "fact_id": fid,
                            "subject": s_name,
                            "relation": rel,
                            "target": o_name,
                            "object": o_name,
                            "fact": fact_text,
                            "source_session": sess,
                            "valid_from": v_from,
                            "valid_to": v_to,
                            "confidence": conf,
                            "hops": 2,
                            "bridge_entity": bridge,
                            "path": [entity_name, bridge, neighbor] if neighbor else [entity_name, bridge],
                        })
                        if len(results) >= limit:
                            break
                if len(results) >= limit:
                    break

        return results

    async def search(
        self,
        query: str,
        limit: int = 5,
        project: Optional[str] = None,
        workspace_root: Optional[str] = None,
        **kwargs: Any,
    ) -> List[str]:
        """
        Returns up to `limit` current facts and episodes relevant to `query`, best first.
        Superseded, dormant and expired facts are excluded. Returns [] when nothing scores
        above the threshold, so an unanswerable question gets no answer.
        """
        q_tokens = _content_tokens(query)
        if not q_tokens:
            return []
        if not project and isinstance(kwargs.get("collection"), str):
            project = kwargs["collection"]
        target_project = project.strip() if isinstance(project, str) else None
        filter_project = bool(target_project and target_project.lower() != "all")
        now = datetime.now(timezone.utc).isoformat()
        params: Dict[str, Any] = {"tokens": q_tokens, "now": now, "lim": _CANDIDATE_LIMIT}
        if filter_project:
            params["project"] = target_project

        fact_rows = self._query(
            "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) "
            "WHERE coalesce(r.superseded, false) = false AND coalesce(r.dormant, false) = false "
            "AND (r.valid_to IS NULL OR r.valid_to > $now) "
            + ("AND coalesce(r.project, 'kortext') = $project " if filter_project else "")
            + "AND ANY(tok IN $tokens WHERE toLower(coalesce(r.fact, '')) CONTAINS tok "
            "OR toLower(s.name) CONTAINS tok OR toLower(t.name) CONTAINS tok) "
            "RETURN r.id, coalesce(r.fact, s.name + ' ' + r.relation + ' ' + t.name), s.name, t.name, "
            "coalesce(r.valid_from, r.created_at, ''), r.emb, r.relation, coalesce(r.trust_level, 'tool_confirmed') "
            "ORDER BY coalesce(r.valid_from, r.created_at, '') DESC LIMIT $lim",
            params,
        )
        ep_rows = self._query(
            "MATCH (e:Episodic) WHERE coalesce(e.superseded, false) = false "
            + ("AND coalesce(e.project, 'kortext') = $project " if filter_project else "")
            + "AND ANY(tok IN $tokens WHERE toLower(e.name + ' ' + coalesce(e.content, '')) CONTAINS tok) "
            "RETURN e.name, e.content, coalesce(e.created_at, ''), e.emb, coalesce(e.trust_level, 'speculative') "
            "ORDER BY coalesce(e.created_at, '') DESC LIMIT $lim",
            params,
        )

        # (kind, key, display text, text to score, names, timestamp, stored embedding)
        cands: List[Tuple[str, str, str, str, List[str], str, Any]] = []
        slots: List[Optional[Tuple[str, str]]] = []
        for row in (fact_rows[1] if fact_rows and len(fact_rows) > 1 else []) or []:
            fid, fact, sub, obj, ts, emb, rel, trust = (list(row) + [None] * 8)[:8]
            shown = fence_memory_by_provenance(str(fact), trust_level=str(trust or "speculative"))
            cands.append(("fact", str(fid or ""), shown, str(fact), [str(sub or "")], str(ts or ""), emb))
            slots.append((str(sub or "").lower(), str(rel or "").lower()))
        for row in (ep_rows[1] if ep_rows and len(ep_rows) > 1 else []) or []:
            name, content, ts, emb, trust = (list(row) + [None] * 5)[:5]
            text = f"{name}: {content or ''}"
            shown = fence_memory_by_provenance(f"[{name}]: {str(content or '')[:200]}", trust_level=str(trust or "speculative"))
            cands.append(("episode", str(name), shown, text, [], str(ts or ""), emb))
            slots.append(None)
        if not cands:
            return []

        names = sorted({n for c in cands for n in c[4] if n})
        stripped_query = _strip_names(query, names) or query
        scores = self._score_candidates(stripped_query, cands)

        scored = [(sc, c[5], c[2], slot) for sc, c, slot in zip(scores, cands, slots) if sc is not None]
        if not scored:
            return []
        best = max(x[0] for x in scored)
        ties = sorted((x for x in scored if x[0] >= best - RECENCY_TIE_MARGIN), key=lambda x: x[1], reverse=True)
        rest = sorted((x for x in scored if x[0] < best - RECENCY_TIE_MARGIN), key=lambda x: x[0], reverse=True)
        ordered = ties + rest
        # Facts sharing a (subject, relation) slot keep their slot's positions but go newest first:
        # a later value for the same relation replaces the earlier one at the top.
        by_slot: Dict[Tuple[str, str], List[int]] = {}
        for i, x in enumerate(ordered):
            if x[3] is not None:
                by_slot.setdefault(x[3], []).append(i)
        for positions in by_slot.values():
            newest_first = sorted((ordered[i] for i in positions), key=lambda x: x[1], reverse=True)
            for i, x in zip(positions, newest_first):
                ordered[i] = x
        results: List[str] = []
        for _, _, text, _ in ordered:
            if text not in results:
                results.append(text)
            if len(results) >= limit:
                break
        return results

    def _score_candidates(self, query: str, cands: List[Tuple[str, str, str, str, List[str], str, Any]]) -> List[Optional[float]]:
        """Scores each candidate; None for those under the threshold."""
        q_tokens = set(_content_tokens(query))
        doc_tokens = [set(_content_tokens(_strip_names(c[3], c[4]))) for c in cands]
        # IDF over the candidate pool, so a word every candidate shares counts for little.
        n = len(cands)
        idf = {t: math.log(1 + n / (1 + sum(1 for d in doc_tokens if t in d))) for t in q_tokens}
        q_mass = sum(idf.values()) or 1.0
        lexical = [sum(idf[t] for t in q_tokens & d) / q_mass for d in doc_tokens]

        embedder = _dense_embedder()
        if embedder is None:
            logger.warning("Episodic search is using lexical scoring only; recall on paraphrased questions is low.")
            return [s if s >= LEXICAL_MIN_SCORE else None for s in lexical]

        q_vec = np.asarray(embedder.embed_query(query), dtype=np.float32)
        vecs: List[Optional[np.ndarray]] = [
            np.asarray(c[6], dtype=np.float32) if isinstance(c[6], list) and len(c[6]) == len(q_vec) else None
            for c in cands
        ]
        missing = [i for i, v in enumerate(vecs) if v is None]
        if missing:
            fresh = embedder.embed_documents([_strip_names(cands[i][3], cands[i][4]) or cands[i][3] for i in missing])
            for i, vec in zip(missing, fresh):
                vecs[i] = np.asarray(vec, dtype=np.float32)
                self._store_embedding(cands[i][0], cands[i][1], [float(x) for x in vec])
        q_norm = float(np.linalg.norm(q_vec)) or 1.0
        out: List[Optional[float]] = []
        for v, lex in zip(vecs, lexical):
            cos = float(np.dot(q_vec, v) / (q_norm * (float(np.linalg.norm(v)) or 1.0)))
            score = 0.8 * cos + 0.2 * lex
            out.append(score if score >= DENSE_MIN_SCORE else None)
        return out

    def _store_embedding(self, kind: str, key: str, vec: List[float]) -> None:
        """Caches a candidate's embedding on its edge or node so later searches skip the model."""
        if not key:
            return
        try:
            if kind == "fact":
                self._query("MATCH ()-[r:RELATES_TO {id: $id}]->() SET r.emb = $emb", {"id": key, "emb": vec})
            else:
                self._query("MATCH (e:Episodic {name: $id}) SET e.emb = $emb", {"id": key, "emb": vec})
        except Exception as exc:
            logger.debug("Embedding cache write skipped for %s: %s", key, exc)

    async def delete_fact(self, fact_id: str, reason: str, match_substring: bool = False) -> None:
        """
        Marks an episodic node or fact relation as superseded.
        Requires >=12 chars (or match_substring=True) before falling back to substring matching
        so short generic words never mass-purge the graph.
        """
        now = datetime.now(timezone.utc).isoformat()
        resolved_id = (fact_id or "").strip()
        allow_sub = bool(match_substring or len(resolved_id) >= 12)

        self._query(
            "MATCH (e:Episodic) WHERE e.id = $id OR e.name = $id OR ($allow_sub AND e.name CONTAINS $id) "
            "SET e.superseded = true, e.superseded_reason = $reason, e.superseded_at = $now",
            {"id": resolved_id, "reason": reason, "now": now, "allow_sub": allow_sub},
        )
        self._query(
            "MATCH ()-[r:RELATES_TO]->() WHERE r.id = $id OR r.fact_id = $id OR r.source_session = $id "
            "OR ($allow_sub AND r.fact CONTAINS $id) "
            "SET r.superseded = true, r.dormant = true, r.superseded_reason = $reason, r.superseded_at = $now",
            {"id": resolved_id, "reason": reason, "now": now, "allow_sub": allow_sub},
        )

    async def close(self) -> None:
        """Close FalkorDB connection."""
        if hasattr(self, "client") and self.client is not None:
            try:
                r = self.client.get_redis()
                if r is not None:
                    r.close()
            except Exception:
                logger.debug("Suppressed non-fatal exception")

