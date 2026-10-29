"""
Unit tests for adaptive RRF weights and CI redistribution in Kortext RetrievalEngine.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from kortext.retrieval.engine import (
    DEFAULT_FUSION_WEIGHTS,
    SYMBOLIC_FUSION_WEIGHTS,
    PROSE_FUSION_WEIGHTS,
    RetrievalEngine,
    compute_adaptive_weights,
    is_ci_mode,
    is_symbolic_query,
    redistribute_ci_weights,
    redistribute_weights,
    W_AST,
    W_BM25,
    W_DENSE,
    W_TEMPORAL,
    K_RRF,
)


class TestQueryClassification:
    def test_detects_snake_case_symbolic_queries(self):
        assert is_symbolic_query("compute_age_hours") is True
        assert is_symbolic_query("where is _rank_candidates defined") is True
        assert is_symbolic_query("how does calculate_metrics_for_eval work") is True
        assert is_symbolic_query("find test_retrieval_pipeline") is True

    def test_detects_camel_case_symbolic_queries(self):
        assert is_symbolic_query("RetrievalEngine") is True
        assert is_symbolic_query("where is RetrievalEngine") is True
        assert is_symbolic_query("getUserProfile") is True
        assert is_symbolic_query("HippoRAGPPR") is True
        assert is_symbolic_query("FastEmbed") is True

    def test_detects_dotted_symbol_paths_and_file_extensions(self):
        assert is_symbolic_query("kortext.retrieval.engine") is True
        assert is_symbolic_query("engine.py") is True
        assert is_symbolic_query("models.SparseVector") is True
        assert is_symbolic_query("os.getenv") is True

    def test_does_not_flag_sentence_ending_period_as_symbolic(self):
        # A normal sentence ending with a period should not be treated as a dotted symbol
        assert is_symbolic_query("What is the architecture of Kortext.") is False
        assert is_symbolic_query("Explain memory consolidation over time.") is False

    def test_detects_parentheses_function_invocations(self):
        assert is_symbolic_query("extract_symbol_candidates()") is True
        assert is_symbolic_query("compute(x, y)") is True
        assert is_symbolic_query("what does run() return") is True

    def test_detects_programming_keywords(self):
        assert is_symbolic_query("def calculate_metrics") is True
        assert is_symbolic_query("class MemoryStore") is True
        assert is_symbolic_query("async def initialize") is True
        assert is_symbolic_query("import httpx") is True
        assert is_symbolic_query("return rrf_scores") is True
        assert is_symbolic_query("yield candidate") is True

    def test_detects_backticks_and_operators(self):
        assert is_symbolic_query("where is `weibull_decay` used") is True
        assert is_symbolic_query("what is Foo::bar") is True
        assert is_symbolic_query("fn -> int") is True

    def test_identifies_prose_and_conceptual_queries(self):
        assert is_symbolic_query("how does memory retention decay work in kortext") is False
        assert is_symbolic_query("explain cognitive dream mode and memory consolidation") is False
        assert is_symbolic_query("what is the biological retention floor") is False
        assert is_symbolic_query("overview of background tasks and memory substrates") is False
        assert is_symbolic_query("how are episodic memories forgotten over time") is False


class TestAdaptiveWeights:
    def test_default_fusion_weights_structure_and_sum(self):
        expected_keys = {"ast", "bm25", "dense", "temporal"}
        assert set(DEFAULT_FUSION_WEIGHTS.keys()) == expected_keys
        assert DEFAULT_FUSION_WEIGHTS["ast"] == W_AST
        assert DEFAULT_FUSION_WEIGHTS["bm25"] == W_BM25
        assert DEFAULT_FUSION_WEIGHTS["dense"] == W_DENSE
        assert DEFAULT_FUSION_WEIGHTS["temporal"] == W_TEMPORAL
        assert pytest.approx(sum(DEFAULT_FUSION_WEIGHTS.values())) == 1.0

    def test_symbolic_query_elevates_ast_and_bm25(self):
        weights = compute_adaptive_weights("def compute_age_hours(ts):", ci_mode=False, dense_available=True)
        # AST weight elevated to >= 0.35
        assert weights["ast"] >= 0.35
        # BM25 elevated above default 0.20
        assert weights["bm25"] > DEFAULT_FUSION_WEIGHTS["bm25"]
        # AST and BM25 dominate
        assert weights["ast"] > weights["dense"]
        assert weights["bm25"] >= DEFAULT_FUSION_WEIGHTS["bm25"]
        assert pytest.approx(sum(weights.values())) == 1.0

    def test_prose_query_elevates_dense_and_facts(self):
        weights = compute_adaptive_weights(
            "explain the architecture of cognitive memory and forgetting",
            ci_mode=False,
            dense_available=True,
        )
        assert weights["temporal"] > DEFAULT_FUSION_WEIGHTS["temporal"]
        assert weights["dense"] > DEFAULT_FUSION_WEIGHTS["dense"]
        # AST is de-emphasized compared to default
        assert weights["ast"] < DEFAULT_FUSION_WEIGHTS["ast"]
        assert pytest.approx(sum(weights.values())) == 1.0


class TestCIRedistribution:
    def test_proportional_redistribution_preserves_relative_ratios(self):
        base_weights = {"ast": 0.30, "bm25": 0.20, "dense": 0.25, "temporal": 0.25}
        redistributed = redistribute_weights(base_weights, inactive_keys=["dense"])
        assert redistributed["dense"] == 0.0

        assert pytest.approx(redistributed["ast"] / redistributed["bm25"]) == 1.5
        assert pytest.approx(redistributed["ast"] / redistributed["temporal"]) == 1.2

        # Total sum must equal 1.0
        assert pytest.approx(sum(redistributed.values())) == 1.0
        assert pytest.approx(redistributed["ast"]) == 0.40

    def test_redistribute_ci_weights_helper(self):
        res = redistribute_ci_weights()
        assert res["dense"] == 0.0
        assert pytest.approx(sum(res.values())) == 1.0
        assert pytest.approx(res["ast"]) == 0.50

    def test_ci_mode_env_var_triggers_redistribution(self):
        with patch.dict(os.environ, {"KORTEXT_CI_MODE": "1", "CI_MODE": "1"}):
            assert is_ci_mode() is True
            weights = compute_adaptive_weights("RetrievalEngine", dense_available=True)
            assert weights["dense"] == 0.0
            assert weights["ast"] > 0.40
            assert pytest.approx(sum(weights.values())) == 1.0

    def test_dense_offline_triggers_redistribution_even_if_not_in_ci(self):
        with patch.dict(os.environ, {"KORTEXT_CI_MODE": "0", "CI_MODE": "0"}):
            assert is_ci_mode() is False
            weights = compute_adaptive_weights("RetrievalEngine", dense_available=False)
            assert weights["dense"] == 0.0
            assert pytest.approx(sum(weights.values())) == 1.0

    def test_retrieval_engine_get_adaptive_weights(self):
        engine = RetrievalEngine()
        # Symbolic query
        w_sym = engine.get_adaptive_weights("def seek_symbol():", dense_available=True, ci_mode=False)
        assert w_sym["ast"] >= 0.35
        assert w_sym["dense"] > 0.0
        assert pytest.approx(sum(w_sym.values())) == 1.0

        # Prose query in CI mode
        w_prose_ci = engine.get_adaptive_weights(
            "explain memory consolidation", dense_available=True, ci_mode=True
        )
        assert w_prose_ci["dense"] == 0.0
        assert w_prose_ci["temporal"] > 0.30
        assert pytest.approx(sum(w_prose_ci.values())) == 1.0


class TestRetrievalEngineQueryIntegration:
    def test_query_adapts_weights_for_symbolic_query(self):
        """Verifies engine.query uses adaptive weights for symbolic query."""
        import asyncio

        async def _run():
            engine = RetrievalEngine()
            engine._client = AsyncMock()

            # Mock out parallel fetchers
            with patch.object(engine, "_extract_symbol_candidates", return_value=["foo_bar"]):
                with patch.object(engine, "_get_query_embedding", AsyncMock(return_value=[0.1, 0.2])):
                    with patch.object(engine, "_get_sparse_embedding", return_value=([1], [0.5])):
                        with patch.object(engine, "_fetch_ast_matches", return_value=["ast_match"]):
                            with patch.object(engine, "_fetch_temporal_facts", return_value=["temporal_match"]):
                                with patch.object(engine, "_retrieve_qdrant_points", AsyncMock(return_value=["dense_match"])):
                                    with patch.object(engine, "_retrieve_qdrant_sparse_points", AsyncMock(return_value=["sparse_match"])):
                                        with patch.object(engine, "_rank_candidates", wraps=engine._rank_candidates) as spy_rank:
                                            results = await engine.query("where is foo_bar defined?")
                                            assert len(results) > 0
                                            # Verify _rank_candidates was called with elevated AST weight
                                            assert spy_rank.called
                                            signals = spy_rank.call_args[0][0]
                                            ast_weight = signals[0][1]
                                            # AST weight for symbolic query is >= 0.35
                                            assert ast_weight >= 0.35

        asyncio.run(_run())

    def test_query_in_ci_mode_redistributes_dense_weight(self):
        """Verifies engine.query in KORTEXT_CI_MODE sets dense weight to 0.0 and redistributes."""
        import asyncio

        async def _run():
            engine = RetrievalEngine()
            engine._client = AsyncMock()

            with patch.dict(os.environ, {"KORTEXT_CI_MODE": "1", "CI_MODE": "1"}):
                with patch.object(engine, "_extract_symbol_candidates", return_value=["foo_bar"]):
                    with patch.object(engine, "_get_query_embedding", AsyncMock(return_value=[])):
                        with patch.object(engine, "_get_sparse_embedding", return_value=([1], [0.5])):
                            with patch.object(engine, "_fetch_ast_matches", return_value=["ast_match"]):
                                with patch.object(engine, "_fetch_temporal_facts", return_value=["temporal_match"]):
                                    with patch.object(engine, "_retrieve_qdrant_points", AsyncMock(return_value=[])):
                                        with patch.object(engine, "_retrieve_qdrant_sparse_points", AsyncMock(return_value=["sparse_match"])):
                                            with patch.object(engine, "_rank_candidates", wraps=engine._rank_candidates) as spy_rank:
                                                results = await engine.query("where is foo_bar defined?")
                                                assert spy_rank.called
                                                signals = spy_rank.call_args[0][0]
                                                # Signal 2 is dense (index 2)
                                                dense_weight = signals[2][1]
                                                assert dense_weight == 0.0
                                                # AST weight has received redistributed mass
                                                ast_weight = signals[0][1]
                                                assert ast_weight > 0.40
                                                total_weight = sum(w for _, w in signals)
                                                assert pytest.approx(total_weight) == 1.0

        asyncio.run(_run())
