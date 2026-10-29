"""
Unit tests for the hybrid RRF retrieval pipeline.
"""

from pathlib import Path
import pytest
from kortext.retrieval.engine import (
    RetrievalEngine,
    extract_passage_text,
    K_RRF,
    W_AST,
    W_DENSE,
)


class TestRetrievalPipeline:
    def test_extract_passage_text(self):
        assert extract_passage_text({"text": "hello world"}) == "hello world"
        assert extract_passage_text({"document": "doc body"}) == "doc body"
        assert extract_passage_text({"_node_content": '{"text": "node content"}'}) == "node content"

    def test_symbol_candidate_extraction(self):
        engine = RetrievalEngine()
        query = "Where is the definition of `RetrievalEngine` and what calls SymbolSeekEngine?"
        candidates = engine._extract_symbol_candidates(query)
        assert "RetrievalEngine" in candidates
        assert "SymbolSeekEngine" in candidates

    def test_signal_weights(self):
        """The four default RRF weights sum to 1.0."""
        from kortext.retrieval.engine import W_BM25, W_TEMPORAL
        assert pytest.approx(W_AST + W_BM25 + W_DENSE + W_TEMPORAL) == 1.0

    def test_get_sparse_embedding(self, caplog):
        """Verifies BM25 sparse embedding handles empty input, formats valid output, and falls back gracefully on error."""
        import logging
        from unittest.mock import MagicMock, patch

        engine = RetrievalEngine()

        # 1. Empty or whitespace prompt returns None without calling get_sparse_model
        with patch("kortext.retrieval.engine.get_sparse_model") as mock_get_model:
            assert engine._get_sparse_embedding("") is None
            assert engine._get_sparse_embedding("   ") is None
            mock_get_model.assert_not_called()

        # 2. Valid prompt with mocked model returns formatted indices and values
        mock_model = MagicMock()
        mock_emb = MagicMock()
        mock_emb.indices = [101, 202]
        mock_emb.values = [1.5, 0.8]
        mock_model.query_embed.return_value = [mock_emb]

        with patch("kortext.retrieval.engine.get_sparse_model", return_value=mock_model):
            sparse = engine._get_sparse_embedding("def calculate_metrics():")
            assert sparse is not None
            indices, values = sparse
            assert isinstance(indices, list)
            assert isinstance(values, list)
            assert len(indices) == len(values) == 2
            assert all(isinstance(idx, int) for idx in indices)
            assert all(isinstance(val, float) for val in values)
            assert indices == [101, 202]
            assert values == [1.5, 0.8]
            mock_model.query_embed.assert_called_once_with("def calculate_metrics():")

        # 3. Model returning empty results returns None
        mock_empty_model = MagicMock()
        mock_empty_model.query_embed.return_value = []
        with patch("kortext.retrieval.engine.get_sparse_model", return_value=mock_empty_model):
            assert engine._get_sparse_embedding("def calculate_metrics():") is None

        # 4. Model exception (e.g., offline ConnectError) returns None and logs warning
        mock_err_model = MagicMock()
        mock_err_model.query_embed.side_effect = RuntimeError("Offline connection error")
        with patch("kortext.retrieval.engine.get_sparse_model", return_value=mock_err_model):
            with caplog.at_level(logging.WARNING, logger="kortext.retrieval"):
                caplog.clear()
                assert engine._get_sparse_embedding("def calculate_metrics():") is None
                assert any("Failed to generate sparse embedding" in record.message for record in caplog.records)

        # 5. Model initialization exception (e.g. FastEmbed initialization failure) returns None and logs warning
        with patch("kortext.retrieval.engine.get_sparse_model", side_effect=RuntimeError("FastEmbed init failed")):
            with caplog.at_level(logging.WARNING, logger="kortext.retrieval"):
                caplog.clear()
                assert engine._get_sparse_embedding("def calculate_metrics():") is None
                assert any("Failed to generate sparse embedding" in record.message for record in caplog.records)

    def test_retrieve_qdrant_sparse_points(self):
        """Verifies _retrieve_qdrant_sparse_points formats SparseVector query correctly."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from qdrant_client import models as qmodels

        async def _run():
            engine = RetrievalEngine()
            mock_client = AsyncMock()
            mock_point = MagicMock()
            mock_point.payload = {"text": "Sparse match content"}
            mock_response = MagicMock()
            mock_response.points = [mock_point]
            mock_client.query_points = AsyncMock(return_value=mock_response)
            engine._client = mock_client

            results = await engine._retrieve_qdrant_sparse_points(
                col_name="test_col",
                sparse_indices=[101, 202],
                sparse_values=[1.5, 0.8],
                limit=5,
            )

            assert results == ["Sparse match content"]
            mock_client.query_points.assert_called_once()
            _, kwargs = mock_client.query_points.call_args
            assert kwargs["collection_name"] == "test_col"
            assert kwargs["using"] == "text-sparse-new"
            assert kwargs["limit"] == 5
            query = kwargs["query"]
            assert isinstance(query, qmodels.SparseVector)
            assert query.indices == [101, 202]
            assert query.values == [1.5, 0.8]

        asyncio.run(_run())

    def test_resolve_workspace_root_no_pickle(self):
        """Verifies resolve_workspace_root_for_collection never executes pickle.loads."""
        import asyncio
        from unittest.mock import patch
        from kortext.retrieval.engine import resolve_workspace_root_for_collection, _COLLECTION_WORKSPACE_CACHE

        async def _run():
            _COLLECTION_WORKSPACE_CACHE.clear()

            # Ensure pickle.loads would raise an error if ever invoked
            with patch("pickle.loads", side_effect=RuntimeError("pickle.loads should never be called!")):
                resolved = await resolve_workspace_root_for_collection("kortext_codebase")
                assert resolved is not None
                expected_root = str(Path(__file__).resolve().parent.parent)
                assert resolved == expected_root

        asyncio.run(_run())

    def test_5_signal_rrf_query(self):
        """Verifies RetrievalEngine.query runs dense and sparse in parallel and fuses 5 signals."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        async def _run():
            engine = RetrievalEngine()
            engine._client = AsyncMock()

            with patch.object(engine, "_get_query_embedding", AsyncMock(return_value=[0.1, 0.2])), \
                 patch.object(engine, "_get_sparse_embedding", return_value=([42], [1.0])), \
                 patch.object(engine, "_fetch_ast_matches", return_value=["[AST] Definition"]), \
                 patch.object(engine, "_fetch_temporal_facts", return_value=["[Temporal] Fact"]), \
                 patch.object(engine, "_retrieve_qdrant_points", AsyncMock(return_value=["[Dense] Match"])), \
                 patch.object(engine, "_retrieve_qdrant_sparse_points", AsyncMock(return_value=["[Sparse] BM25 Match"])):

                results = await engine.query("test query", collection="kortext_codebase")
                assert len(results) >= 3
                # Verify that both dense and sparse passages are included in candidate ranking
                combined = " ".join(results)
                assert "[AST] Definition" in combined
                assert "[Dense] Match" in combined
                assert "[Sparse] BM25 Match" in combined

        asyncio.run(_run())

    def test_compute_adaptive_weights_suppress_ast_signal(self):
        """Verifies compute_adaptive_weights zeros out AST signal and re-normalizes weights."""
        from kortext.retrieval.engine import compute_adaptive_weights
        weights = compute_adaptive_weights("def calculate_metrics():", suppress_ast_signal=True)
        assert weights["ast"] == 0.0
        assert weights["bm25"] > 0.0
        assert weights["dense"] > 0.0
        assert pytest.approx(sum(weights.values()), rel=1e-5) == 1.0

        ci_weights = compute_adaptive_weights(
            "def calculate_metrics():",
            ci_mode=True,
            suppress_ast_signal=True,
        )
        assert ci_weights["ast"] == 0.0
        assert ci_weights["dense"] == 0.0
        assert ci_weights["bm25"] > 0.0
        assert pytest.approx(sum(ci_weights.values()), rel=1e-5) == 1.0

    def test_query_suppress_ast_signal(self):
        """Verifies query skips ast_task, zeros out AST weight, and filters out AST symbol passages when suppress_ast_signal=True."""
        import asyncio
        from unittest.mock import AsyncMock, patch, MagicMock

        async def _run():
            engine = RetrievalEngine()
            engine._client = AsyncMock()

            ast_fetch_mock = MagicMock(return_value=["### AST Symbol: `FakeSymbol` (class)"])
            rank_spy = MagicMock(wraps=engine._rank_candidates)
            engine._rank_candidates = rank_spy

            with patch.object(engine, "_get_query_embedding", AsyncMock(return_value=[0.1, 0.2])), \
                 patch.object(engine, "_get_sparse_embedding", return_value=([42], [1.0])), \
                 patch.object(engine, "_fetch_ast_matches", ast_fetch_mock), \
                 patch.object(engine, "_fetch_temporal_facts", return_value=[]), \
                 patch.object(engine, "_retrieve_qdrant_points", AsyncMock(return_value=["### AST Symbol: `StrayAst` (func)", "Real code chunk"])), \
                 patch.object(engine, "_retrieve_qdrant_sparse_points", AsyncMock(return_value=["### ast symbol: stray", "Another code chunk"])):

                results = await engine.query("test query", collection="kortext_codebase", suppress_ast_signal=True)
                # Verify _fetch_ast_matches was NOT called
                ast_fetch_mock.assert_not_called()

                # Verify AST signal in _rank_candidates was empty and weight was 0.0
                rank_spy.assert_called_once()
                signals_passed = rank_spy.call_args[0][0]
                ast_signal_passages, ast_signal_weight = signals_passed[0]
                assert ast_signal_passages == []
                assert ast_signal_weight == 0.0

                # Verify AST formatted passages were stripped
                for r in results:
                    assert not r.startswith("### AST Symbol:")
                    assert "### ast symbol:" not in r.lower()
                assert "Real code chunk" in results or "Another code chunk" in results

        asyncio.run(_run())

    def test_multi_collection_rrf_normalization(self):
        """Verifies multi-collection querying normalizes local RRF scores to avoid collection order bias."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        async def _run():
            engine = RetrievalEngine()
            engine._client = AsyncMock()

            # col1 has dense and sparse candidates; col2 has dense and sparse candidates
            async def fake_retrieve_dense(col, dense_vec, limit=30):
                if col == "col1":
                    return ["col1_dense_top", "col1_dense_low"]
                return ["col2_dense_top", "col2_dense_low"]

            async def fake_retrieve_sparse(col, sparse_indices, sparse_values, limit=30):
                if col == "col1":
                    return ["col1_sparse_top", "col1_dense_top"]
                return ["col2_sparse_top", "col2_dense_top"]

            with patch.object(engine, "_get_query_embedding", AsyncMock(return_value=[0.1, 0.2])), \
                 patch.object(engine, "_get_sparse_embedding", return_value=([1], [1.0])), \
                 patch.object(engine, "_fetch_ast_matches", return_value=[]), \
                 patch.object(engine, "_fetch_temporal_facts", return_value=[]), \
                 patch.object(engine, "_retrieve_qdrant_points", side_effect=fake_retrieve_dense), \
                 patch.object(engine, "_retrieve_qdrant_sparse_points", side_effect=fake_retrieve_sparse):

                results = await engine.query("test query", collection=["col1", "col2"], top_k=6)
                # Both col1_dense_top and col2_dense_top should have top normalized local scores (1.0).
                # col2_dense_top (from collection 2) must appear before lower-ranked candidates of collection 1 (col1_dense_low).
                assert "col1_dense_top" in results
                assert "col2_dense_top" in results
                idx_c2_top = results.index("col2_dense_top")
                if "col1_dense_low" in results:
                    idx_c1_low = results.index("col1_dense_low")
                    assert idx_c2_top < idx_c1_low

        asyncio.run(_run())

    def test_federated_query_detail_levels(self):
        """Verifies detail levels compact, balanced (default), and comprehensive propagate correctly."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        async def _run():
            engine = RetrievalEngine()
            engine._client = AsyncMock()

            seek_calls = []
            def fake_seek(sym, detail_level="interface", **kwargs):
                seek_calls.append(detail_level)
                return {"status": "found", "matches": [{"file": "test.py", "name": sym, "kind": "function"}]}

            engine.symbol_seek.seek_symbol = fake_seek

            long_chunk = "def example_code():\n    return 'x' * 300\n" * 10
            mock_chunks = [f"{long_chunk} // chunk {i}" for i in range(15)]
            mock_facts = [f"Episodic fact {i}" for i in range(25)]

            # 1. Compact: truncates code chunks to 256 chars, omits Tier 3, max 2 chunks, detail_level='skeleton'
            seek_calls.clear()
            with patch.object(engine, "query", AsyncMock(return_value=mock_chunks)), \
                 patch.object(engine.episodic, "search", AsyncMock(return_value=mock_facts)), \
                 patch.object(engine, "_fetch_tier3_overview", AsyncMock(return_value="# Architecture Overview")):

                res_compact = await engine.federated_query("example_code query", token_budget=1500, detail_level="compact")
                assert res_compact["architectural_overview"] is None
                assert len(res_compact["code_chunks"]) <= 2
                for chunk in res_compact["code_chunks"]:
                    assert len(chunk) <= 256
                assert len(res_compact["episodic_history"]) <= 2
                assert all(dl == "skeleton" for dl in seek_calls)

            # 2. Balanced (default): up to 5 chunks, detail_level='interface', Tier 3 included when workspace provided
            seek_calls.clear()
            with patch.object(engine, "query", AsyncMock(return_value=mock_chunks)), \
                 patch.object(engine.episodic, "search", AsyncMock(return_value=mock_facts)), \
                 patch.object(engine, "_fetch_tier3_overview", AsyncMock(return_value="# Balanced Architecture Overview")):

                repo_root = str(Path(__file__).resolve().parent.parent)
                res_balanced = await engine.federated_query(
                    "example_code query",
                    token_budget=1500,
                    workspace_root=repo_root,
                )
                assert len(res_balanced["code_chunks"]) <= 5
                assert len(res_balanced["episodic_history"]) <= 5
                assert res_balanced["architectural_overview"] == "# Balanced Architecture Overview"
                assert all(dl == "interface" for dl in seek_calls)

            # 3. Comprehensive: up to 10 chunks, full AST detail_level='full', full Tier 3 outline
            seek_calls.clear()
            with patch.object(engine, "query", AsyncMock(return_value=mock_chunks)), \
                 patch.object(engine.episodic, "search", AsyncMock(return_value=mock_facts)), \
                 patch.object(engine, "_fetch_tier3_overview", AsyncMock(return_value="# Comprehensive Architecture Outline")):

                res_comp = await engine.federated_query(
                    "example_code query",
                    token_budget=1500,
                    detail_level="comprehensive",
                    workspace_root=repo_root,
                )
                assert len(res_comp["code_chunks"]) <= 10
                assert len(res_comp["episodic_history"]) <= 20
                assert res_comp["architectural_overview"] == "# Comprehensive Architecture Outline"
                assert all(dl == "full" for dl in seek_calls)

        asyncio.run(_run())

    def test_federated_query_knapsack_trims_tier3_first(self):
        """Verifies knapsack trimming trims Tier 3 first when total exceeds token_budget and preserves budget."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        async def _run():
            engine = RetrievalEngine()
            engine._client = AsyncMock()

            # 1. Moderate budget excess: Tier 3 is trimmed (shortened) first
            code_chunks_mod = ["def foo():\n    return 42\n" * 12]  # ~74 tokens -> tier1 caps at 50
            facts_mod = ["Episodic decision recorded in system."]  # ~8 tokens -> tier2 caps at 20
            t3_text_long = "T3 Architecture Overview Section Details " * 6  # ~61 tokens

            with patch.object(engine, "query", AsyncMock(return_value=code_chunks_mod)), \
                 patch.object(engine.episodic, "search", AsyncMock(return_value=facts_mod)), \
                 patch.object(engine, "_fetch_tier3_overview", AsyncMock(return_value=t3_text_long)):

                res_mod = await engine.federated_query(
                    "test query",
                    token_budget=100,
                    detail_level="comprehensive",
                )
                assert res_mod["total_estimated_tokens"] <= 100
                # Tier 3 was trimmed (shortened) to fit budget
                assert res_mod["architectural_overview"] is not None
                assert len(res_mod["architectural_overview"]) < len(t3_text_long)
                # Code chunks and facts preserved
                assert len(res_mod["code_chunks"]) == 1
                assert len(res_mod["episodic_history"]) == 1

            # 2. Tight budget excess: Tier 3 keep_tokens < 15, so Tier 3 is dropped completely first
            code_chunks_c2 = ["def foo():\n    return 42\n" * 7]  # ~45 tokens
            facts_c2 = ["Episodic fact number one.", "Episodic fact number two."]  # ~12 tokens
            t3_text_drop = "T3 Architecture Overview Section Details " * 3  # ~30 tokens
            engine.symbol_seek.seek_symbol = lambda sym, **kw: {
                "status": "found",
                "matches": [{"file": "mod.py", "name": sym, "kind": "function", "code": "def big_func():\n" + "    pass\n" * 7}],
            }

            with patch.object(engine, "query", AsyncMock(return_value=code_chunks_c2)), \
                 patch.object(engine.episodic, "search", AsyncMock(return_value=facts_c2)), \
                 patch.object(engine, "_fetch_tier3_overview", AsyncMock(return_value=t3_text_drop)):

                res_heavy = await engine.federated_query(
                    "find `big_func` implementation",
                    token_budget=100,
                    detail_level="comprehensive",
                )
                assert res_heavy["total_estimated_tokens"] <= 100
                assert res_heavy["architectural_overview"] is None
                assert len(res_heavy["code_chunks"]) == 1
                assert len(res_heavy["episodic_history"]) == 2

        asyncio.run(_run())

    def test_get_query_embedding_delegates_to_dense_embedder(self):
        """Verifies _get_query_embedding delegates to get_dense_embedder().aembed_query()."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        async def _run():
            engine = RetrievalEngine()
            mock_embedder = MagicMock()
            mock_embedder.aembed_query = AsyncMock(return_value=[0.123] * 384)
            mock_embedder.dimension = 384

            with patch("kortext.retrieval.engine.get_dense_embedder", return_value=mock_embedder) as mock_get:
                vec = await engine._get_query_embedding("search query")
                assert vec == [0.123] * 384
                mock_get.assert_called_once()
                mock_embedder.aembed_query.assert_awaited_once_with("search query")

        asyncio.run(_run())

    def test_ensure_collection_resolves_embedder_dimension(self):
        """Verifies _ensure_collection defaults embed_dim to get_dense_embedder().dimension unless overridden."""
        import asyncio
        import os
        from unittest.mock import AsyncMock, MagicMock, patch
        from qdrant_client import models as qmodels

        async def _run():
            engine = RetrievalEngine()
            mock_client = AsyncMock()
            mock_client.collection_exists.return_value = False
            engine._client = mock_client

            mock_embedder = MagicMock()
            mock_embedder.dimension = 384

            # 1. Without EMBED_DIMENSION: defaults to embedder.dimension (384)
            with patch("kortext.retrieval.engine.get_dense_embedder", return_value=mock_embedder), \
                 patch.dict(os.environ, {}, clear=False):
                os.environ.pop("EMBED_DIMENSION", None)
                await engine._ensure_collection("test_col_fastembed")

                mock_client.create_collection.assert_awaited_once()
                call_kwargs = mock_client.create_collection.call_args.kwargs
                assert call_kwargs["collection_name"] == "test_col_fastembed"
                vec_params = call_kwargs["vectors_config"]["text-dense"]
                assert vec_params.size == 384

            # 2. With EMBED_DIMENSION overridden: respects EMBED_DIMENSION
            mock_client.create_collection.reset_mock()
            with patch("kortext.retrieval.engine.get_dense_embedder", return_value=mock_embedder), \
                 patch.dict(os.environ, {"EMBED_DIMENSION": "1024"}):
                await engine._ensure_collection("test_col_custom")

                mock_client.create_collection.assert_awaited_once()
                call_kwargs = mock_client.create_collection.call_args.kwargs
                assert call_kwargs["collection_name"] == "test_col_custom"
                vec_params = call_kwargs["vectors_config"]["text-dense"]
                assert vec_params.size == 1024

        asyncio.run(_run())

