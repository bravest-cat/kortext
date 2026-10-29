"""
Kortext Standard External Benchmark Suite.

SB-1: Code-Domain Multi-Hop Retrieval (Recall@5, MRR, nDCG@10)
    Modelled on HotpotQA's 2-hop structure but using 50 Kortext codebase
    questions. Gold passages are exact symbol names indexed in FalkorDB.
    Uses AST seek only (offline CI; no dense embedder).

SB-2: LongMemEval v1 Style (5 Abilities: Extraction, Knowledge Updates,
      Temporal, Multi-Session Reasoning, Abstention)
    Facts seeded into live kortext FalkorDB; cleaned up after the class.
    Thresholds calibrated against measured F1 on this fixture.

SB-3: Kortext Domain Golden Set (20 curated codebase queries)
    Gold passages are primary symbol names from the live AST index.
    Recall@5 and Precision@3 measured against exact symbol IDs.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from kortext.benchmarks.ir_metrics import (
    token_f1,
    exact_match,
    recall_at_k,
    precision_at_k,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ─────────────────────────────────────────────────────────────────────────────
# Shared retrieval helper (AST seek over all candidates)
# Returns: deduped list of symbol-name strings
# ─────────────────────────────────────────────────────────────────────────────

def _is_valid_symbol(s: Any) -> bool:
    """Validates whether a candidate string looks like a valid code symbol name."""
    if not isinstance(s, str):
        return False
    s = s.strip()
    if not s or s.lower() in {
        "null", "none", "staticmethod", "classmethod", "property", "self", "cls", "true", "false",
    }:
        return False
    parts = s.split(".")
    return all(part.isidentifier() for part in parts)


def _extract_child_symbols(field_value: Any) -> List[str]:
    """
    Extracts valid child symbol names from callees, callers, or methods fields.
    Handles lists of strings, lists of dicts (e.g. {'callee': ...}), dicts,
    FalkorDB unquoted Cypher map strings, and JSON serialized strings.
    """
    extracted: List[str] = []
    if not field_value:
        return extracted

    if isinstance(field_value, list):
        for item in field_value:
            if isinstance(item, dict):
                for k in ("callee", "caller", "method", "symbol", "name"):
                    v = item.get(k)
                    if _is_valid_symbol(v):
                        extracted.append(v.strip())
            elif isinstance(item, str):
                extracted.extend(_extract_child_symbols(item))
    elif isinstance(field_value, dict):
        for k in ("callee", "caller", "method", "symbol", "name"):
            v = field_value.get(k)
            if _is_valid_symbol(v):
                extracted.append(v.strip())
    elif isinstance(field_value, str):
        s = field_value.strip()
        try:
            parsed = json.loads(s)
            if isinstance(parsed, (list, dict)):
                return _extract_child_symbols(parsed)
        except Exception:
            pass
        # Unquoted cypher map format: [{callee: foo, file: ...}, ...]
        cypher_matches = re.findall(
            r"(?:callee|caller|method|symbol|name)\s*:\s*([A-Za-z_][A-Za-z0-9_]*)",
            s,
        )
        if cypher_matches:
            for m in cypher_matches:
                if _is_valid_symbol(m):
                    extracted.append(m.strip())
        elif _is_valid_symbol(s):
            extracted.append(s)
        elif s.startswith("[") and s.endswith("]"):
            for part in s[1:-1].split(","):
                part = part.strip()
                if _is_valid_symbol(part):
                    extracted.append(part)

    return extracted


def _retrieve_symbol_names(
    query: str,
    top_k: Optional[int] = None,
) -> List[str]:
    """
    Seek every candidate extracted from query, plus callee/caller/method expansion.
    Returns: deduped list of symbol-name strings.
    """
    from kortext.symbols.seek import SymbolSeekEngine, extract_symbol_candidates

    candidates = extract_symbol_candidates(query)
    engine = SymbolSeekEngine()
    names: List[str] = []
    seen: set[str] = set()

    search_targets: List[str] = []
    clean_q = query.strip()
    if clean_q and clean_q.isidentifier() and clean_q not in candidates:
        search_targets.append(clean_q)
    for c in candidates[:8]:
        if c not in search_targets:
            search_targets.append(c)

    for sym in search_targets:
        result = engine.seek_symbol(sym)
        if '"found"' in result:
            try:
                data = json.loads(result)
                for m in data.get("matches", []):
                    s = m.get("symbol") or m.get("name")
                    if s and _is_valid_symbol(s) and s not in seen:
                        names.append(s)
                        seen.add(s)
                    for field in ("callees", "callers", "methods"):
                        for child in _extract_child_symbols(m.get(field)):
                            if child and _is_valid_symbol(child) and child not in seen:
                                names.append(child)
                                seen.add(child)
            except Exception:
                pass  # a malformed seek result retrieves nothing; never echo the query back as an answer

    if top_k is not None:
        return names[:top_k]
    return names


# ─────────────────────────────────────────────────────────────────────────────
# Shared episodic retrieval helper (SB-2)
# ─────────────────────────────────────────────────────────────────────────────

def _query_seeded_facts(subject: str, query_text: str) -> str:
    """
    Answers through the production episodic path (`AgentNativeEpisodicDriver.search`,
    the same call `search_knowledge_and_memory` makes via the federated tier-2 fetch)
    and returns the top-ranked fact text. The subject is part of the query, as an agent
    would write it; `search` has no subject parameter.
    """
    import asyncio
    from kortext.episodic.driver import AgentNativeEpisodicDriver

    facts = asyncio.run(
        AgentNativeEpisodicDriver().search(f"{subject} {query_text}", limit=1)
    )
    return facts[0] if facts else ""


# ─────────────────────────────────────────────────────────────────────────────
# SB-1: Code-Domain Multi-Hop Retrieval  (HotpotQA-style)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def hotpot_samples() -> List[Dict[str, Any]]:
    return json.loads((FIXTURES / "hotpotqa_sample.json").read_text())


@pytest.mark.usefixtures("seeded_symbol_graph")
class TestHotpotQAMultiHop:
    """
    SB-1: 50-question code-domain multi-hop fixture.
    Gold passages are exact symbol names in the FalkorDB AST index.
    Dense signal excluded for offline CI determinism.

    Thresholds (empirically measured, ~0.43/0.47/0.44, safety margin applied):
        Recall@5  >= 0.35
        MRR       >= 0.38
        nDCG@10   >= 0.38
    """

    @staticmethod
    def _mrr(retrieved: List[str], gold: List[str]) -> float:
        gold_set = set(gold)
        for rank, r in enumerate(retrieved, 1):
            if r in gold_set:
                return 1.0 / rank
        return 0.0

    @staticmethod
    def _ndcg(retrieved: List[str], gold: List[str], k: int) -> float:
        gold_set = set(gold)
        gains = [1 if r in gold_set else 0 for r in retrieved[:k]]
        dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
        ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(gold), k)))
        return dcg / ideal if ideal > 0 else 0.0

    def test_hotpotqa_recall_at_5(self, hotpot_samples):
        """SB-1a: Mean Recall@5 >= 0.35 on 50-question code-domain fixture."""
        scores = [
            recall_at_k(_retrieve_symbol_names(item["question"]), item["gold_passages"], k=5)
            for item in hotpot_samples
        ]
        mean_r = sum(scores) / len(scores)
        assert mean_r >= 0.35, (
            f"SB-1a: Multi-Hop Recall@5 = {mean_r:.3f} < 0.35 "
            f"(n={len(scores)}, hits>0: {sum(1 for s in scores if s > 0)})"
        )

    # Alias for multi-hop recall test
    test_hotpot_multi_hop_symbol_recall = test_hotpotqa_recall_at_5

    def test_hotpotqa_mrr(self, hotpot_samples):
        """SB-1b: Mean MRR >= 0.38 on 50-question code-domain fixture."""
        scores = [
            self._mrr(_retrieve_symbol_names(item["question"]), item["gold_passages"])
            for item in hotpot_samples
        ]
        mean_mrr = sum(scores) / len(scores)
        assert mean_mrr >= 0.38, (
            f"SB-1b: Multi-Hop MRR = {mean_mrr:.3f} < 0.38"
        )

    def test_hotpotqa_ndcg_at_10(self, hotpot_samples):
        """SB-1c: Mean nDCG@10 >= 0.38 on 50-question code-domain fixture."""
        scores = [
            self._ndcg(_retrieve_symbol_names(item["question"]), item["gold_passages"], k=10)
            for item in hotpot_samples
        ]
        mean_ndcg = sum(scores) / len(scores)
        assert mean_ndcg >= 0.38, (
            f"SB-1c: Multi-Hop nDCG@10 = {mean_ndcg:.3f} < 0.38"
        )


# ─────────────────────────────────────────────────────────────────────────────
# SB-2: LongMemEval v1 Long-Term Memory Benchmark
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def lme_samples() -> List[Dict[str, Any]]:
    return json.loads((FIXTURES / "longmemeval_sample.json").read_text())


@pytest.fixture(scope="module")
def seeded_lme_sessions(lme_samples) -> List[str]:
    """Seeds all context_facts into live FalkorDB; returns session IDs."""
    from kortext.episodic.driver import AgentNativeEpisodicDriver

    driver = AgentNativeEpisodicDriver()
    sessions: List[str] = []
    for item in lme_samples:
        if not item.get("context_facts"):
            continue
        sid = f"lme_bench_{item['id']}"
        sessions.append(sid)
        for fact in item["context_facts"]:
            driver.record_fact(
                subject=fact["subject"],
                relation=fact["relation"],
                fact=fact["fact"],
                object_name=fact.get("object") or fact["subject"],
                source_session=sid,
            )
    return sessions


@pytest.fixture(scope="module", autouse=True)
def cleanup_lme_sessions(seeded_lme_sessions):
    """Tears down seeded facts after the module finishes."""
    yield
    from kortext.falkordb_client import get_shared_falkordb_client

    client = get_shared_falkordb_client()
    db_name = os.getenv("KORTEXT_DATABASE", os.getenv("FALKORDB_DATABASE", "kortext"))
    for sid in seeded_lme_sessions:
        client.query(
            db_name,
            "MATCH ()-[r:RELATES_TO {source_session: $sid}]->() "
            "SET r.dormant = true, r.valid_to = '2000-01-01T00:00:00+00:00'",
            {"sid": sid},
        )


@pytest.mark.usefixtures("real_falkordb")
class TestLongMemEval:
    """
    SB-2: LongMemEval v1 style - 5 memory abilities over 50 questions, seeded into live FalkorDB.
    Scored as answer containment: the top fact returned by the production `search` path
    contains the gold answer. Search returns stored facts, not generated answers, so token F1
    against a short gold answer mostly measures sentence length.

    Measured 2026-10-29 (before: token-overlap search, after: dense scoring with abstention).
    The abstention threshold was calibrated on this same fixture.
        extraction          0.75 -> 0.83
        knowledge_updates   0.50 -> 0.50
        temporal_reasoning  0.70 -> 0.80
        multi_session       0.10 -> 0.10
        abstention rate     0.00 -> 1.00
    """

    @staticmethod
    def _contains_rate(items) -> float:
        hits = [
            item["gold_answer"].lower() in _query_seeded_facts(item["subject"], item["question"]).lower()
            for item in items
        ]
        return sum(hits) / len(hits)

    @pytest.mark.parametrize(
        "ability,bar",
        [
            ("information_extraction", 0.80),
            ("knowledge_updates", 0.45),
            ("temporal_reasoning", 0.75),
            ("multi_session_reasoning", 0.10),
        ],
    )
    def test_lme_answer_containment(self, lme_samples, seeded_lme_sessions, ability, bar):
        rate = self._contains_rate([s for s in lme_samples if s["ability"] == ability])
        assert rate >= bar, f"SB-2 {ability}: containment {rate:.2f} < {bar}"

    def test_lme_abstention(self, lme_samples, seeded_lme_sessions):
        """SB-2e: Abstention rate >= 0.80 for is_answerable=False questions."""
        items = [s for s in lme_samples if s["ability"] == "abstention"]
        abstained = sum(
            1 for item in items
            if not _query_seeded_facts(item["subject"], item["question"]).strip()
        )
        rate = abstained / len(items)
        assert rate >= 0.80, (
            f"SB-2e: LME Abstention rate = {rate:.2f} < 0.80 "
            f"(abstained={abstained}/{len(items)})"
        )

    def test_lme_temporal_ordering_tie_breaking(self):
        """SB-2f: Validates that most recent valid fact wins when token scores tie."""
        import uuid
        from kortext.episodic.driver import AgentNativeEpisodicDriver
        from kortext.falkordb_client import get_shared_falkordb_client

        driver = AgentNativeEpisodicDriver()
        client = get_shared_falkordb_client()
        subj = f"TemporalTieSubj_{uuid.uuid4().hex[:6]}"
        try:
            # Seed older fact first
            driver.record_fact(
                subject=subj,
                relation="works_as",
                fact=f"{subj} worked as junior engineer in 2022",
                object_name="engineer",
                valid_from="2022-01-01T00:00:00+00:00",
            )
            # Seed newer fact second
            driver.record_fact(
                subject=subj,
                relation="works_as",
                fact=f"{subj} worked as staff engineer in 2024",
                object_name="engineer",
                valid_from="2024-01-01T00:00:00+00:00",
            )

            # Query with token 'engineer' which ties between both facts
            # `search` has no subject filter and decays other facts by age, so rank this
            # subject's facts within a wide result window.
            import asyncio
            ranked = asyncio.run(driver.search(f"{subj} engineer", limit=50))
            own = [f for f in ranked if subj in f]
            assert own and "staff engineer" in own[0], f"Expected newest fact to win tie, got: {own}"
        finally:
            client.query("kortext", "MATCH (s:Entity {name: $sub})-[r]->() DELETE r, s", {"sub": subj})

    def test_query_multi_hop_facts_cross_session_traversal(self):
        """SB-2g: Validates 2-hop multi-session traversal following intermediate bridge entities."""
        import uuid
        from kortext.episodic.driver import AgentNativeEpisodicDriver
        from kortext.falkordb_client import get_shared_falkordb_client

        driver = AgentNativeEpisodicDriver()
        client = get_shared_falkordb_client()
        uid = uuid.uuid4().hex[:6]
        subj = f"UserAlex_{uid}"
        bridge = f"TitanProject_{uid}"
        target = f"FalkorDBGraph_{uid}"

        try:
            # Session 1: UserAlex -> assigned_to -> TitanProject
            driver.record_fact(
                subject=subj,
                relation="assigned_to",
                fact=f"{subj} was assigned as lead architect on {bridge}",
                object_name=bridge,
                source_session=f"session_1_{uid}",
            )
            # Session 2: TitanProject -> uses_substrate -> FalkorDBGraph
            driver.record_fact(
                subject=bridge,
                relation="uses_substrate",
                fact=f"{bridge} uses {target} as its graph memory database",
                object_name=target,
                source_session=f"session_2_{uid}",
            )

            # 1. Direct query_multi_hop_facts
            hop_facts = driver.query_multi_hop_facts(subj, max_hops=2)
            assert len(hop_facts) >= 2, f"Expected at least 2 facts across hops, got: {hop_facts}"

            hops = [f["hops"] for f in hop_facts]
            assert 1 in hops, "Expected 1-hop fact in results"
            assert 2 in hops, "Expected 2-hop fact in results"

            hop2_matches = [f for f in hop_facts if f["hops"] == 2 and target in (f.get("target") or "") or target in (f.get("fact") or "")]
            assert len(hop2_matches) > 0, f"Expected 2-hop fact reaching {target}, got: {hop_facts}"
            assert hop2_matches[0]["bridge_entity"] == bridge

            # 2. _query_seeded_facts falls back to 2-hop traversal when 1-hop has 0 similarity
            result = _query_seeded_facts(subj, target)
            assert target in result, f"Expected 2-hop traversal to retrieve {target}, got: {result}"
        finally:
            client.query("kortext", "MATCH (s:Entity) WHERE s.name IN [$s, $b, $t] DETACH DELETE s", {"s": subj, "b": bridge, "t": target})



# ─────────────────────────────────────────────────────────────────────────────
# SB-3: Kortext Domain-Specific Golden Set Benchmark
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def golden_set() -> List[Dict[str, Any]]:
    return json.loads((FIXTURES / "kortext_golden_set.json").read_text())


@pytest.mark.usefixtures("seeded_symbol_graph")
class TestKortextGoldenSet:
    """
    SB-3: 20-query golden set against the Kortext codebase.
    Gold passages are primary symbol names present in the FalkorDB AST index.

    Thresholds (AST seek only, PPR removed 2026-10-29: R@5 unchanged, MRR 0.900 -> 0.879):
        Recall@5  >= 0.75
        MRR       >= 0.85
    """

    def test_golden_set_recall_at_5(self, golden_set):
        """SB-3a: Recall@5 >= 0.75 on 20-query Kortext golden set."""
        scores = [
            recall_at_k(
                _retrieve_symbol_names(item["query"]),
                item["gold_passages"],
                k=5,
            )
            for item in golden_set
        ]
        mean_r = sum(scores) / len(scores)
        assert mean_r >= 0.75, (
            f"SB-3a: Golden Set Recall@5 = {mean_r:.3f} < 0.75 "
            f"(n={len(scores)}, per-query: {[round(s, 2) for s in scores]})"
        )

    def test_golden_set_mrr(self, golden_set):
        """SB-3b: MRR >= 0.85 on 20-query Kortext golden set."""
        scores = []
        for item in golden_set:
            retrieved = _retrieve_symbol_names(item["query"])
            gold_set = set(item["gold_passages"])
            mrr = next(
                (1.0 / rank for rank, r in enumerate(retrieved, 1) if r in gold_set),
                0.0,
            )
            scores.append(mrr)
        mean_mrr = sum(scores) / len(scores)
        assert mean_mrr >= 0.85, (
            f"SB-3b: Golden Set MRR = {mean_mrr:.3f} < 0.85 "
            f"(n={len(scores)}, per-query: {[round(s, 2) for s in scores]})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# SB-4: Symbol Retrieval Expansion Unit Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSymbolRetrievalExpansion:
    """Unit tests for callee, caller, and method extraction in _retrieve_symbol_names."""

    def test_extract_child_symbols_various_formats(self):
        from tests.test_standard_benchmarks import _extract_child_symbols

        # List of dicts
        lod = [{"callee": "child_func", "file": "a.py"}, {"caller": "parent_caller"}]
        assert _extract_child_symbols(lod) == ["child_func", "parent_caller"]

        # List of strings
        los = ["helper_one", "helper_two"]
        assert _extract_child_symbols(los) == ["helper_one", "helper_two"]

        # FalkorDB unquoted Cypher map string
        cypher_str = "[{callee: staticmethod, file: NULL}, {callee: run_worker, file: /path/to/f.py, line: 10}]"
        # staticmethod and NULL should be filtered out
        assert _extract_child_symbols(cypher_str) == ["run_worker"]

        # Valid JSON string
        json_str = json.dumps([{"method": "execute_step"}, {"name": "aux_func"}])
        assert _extract_child_symbols(json_str) == ["execute_step", "aux_func"]

        # None / empty / invalid inputs
        assert _extract_child_symbols(None) == []
        assert _extract_child_symbols("") == []
        assert _extract_child_symbols("NULL") == []

    def test_retrieve_symbol_names_expands_callees_callers_methods(self, monkeypatch):
        mock_seek_payload = json.dumps({
            "status": "found",
            "matches": [
                {
                    "symbol": "MyPipeline",
                    "callees": [{"callee": "callee_step_a"}],
                    "callers": "[{caller: caller_orch, file: NULL}]",
                    "methods": ["method_run"],
                }
            ],
        })

        from kortext.symbols.seek import SymbolSeekEngine
        monkeypatch.setattr(
            SymbolSeekEngine,
            "seek_symbol",
            lambda self, name, file_hint=None: mock_seek_payload,
        )

        names = _retrieve_symbol_names("MyPipeline")
        assert "MyPipeline" in names
        assert "callee_step_a" in names
        assert "caller_orch" in names
        assert "method_run" in names
        # Order should preserve primary symbol first
        assert names[0] == "MyPipeline"

    def test_retrieve_symbol_names_respects_top_k(self, monkeypatch):
        mock_seek_payload = json.dumps({
            "status": "found",
            "matches": [
                {
                    "symbol": "RootSymbol",
                    "callees": ["c1", "c2", "c3"],
                    "methods": ["m1", "m2"],
                }
            ],
        })

        from kortext.symbols.seek import SymbolSeekEngine
        monkeypatch.setattr(
            SymbolSeekEngine,
            "seek_symbol",
            lambda self, name, file_hint=None: mock_seek_payload,
        )

        names = _retrieve_symbol_names("RootSymbol", top_k=3)
        assert len(names) == 3
        assert names == ["RootSymbol", "c1", "c2"]

