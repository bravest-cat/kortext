"""
Empirical benchmark testing Reciprocal Rank Fusion deduplication using production rank_candidates_rrf.
"""

from kortext.retrieval.engine import (
    W_AST,
    W_BM25,
    W_DENSE,
    W_TEMPORAL,
)
from kortext.symbols.seek import SymbolSeekEngine


class TestTokenReductionBenchmark:
    def test_rrf_fusion_deduplication_ratio(self):
        """TR-4: 4-signal Reciprocal Rank Fusion deduplication ratio using production rank_candidates_rrf."""
        from kortext.retrieval.fusion import rank_candidates_rrf

        engine = SymbolSeekEngine()
        real_syms = ["RetrievalEngine", "SymbolSeekEngine", "TaskDAGTracker", "CausalSanitizer", "FalkorDBClient", "AgentNativeEpisodicDriver"]
        base_passages = [engine.seek_symbol(s, detail_level="skeleton") for s in real_syms]
        extra_passages = [f"# File: kortext/module_{i}.py\ndef helper_{i}(): pass" for i in range(1, 10)]
        pool = base_passages + extra_passages  # 15 unique real/code passages

        ast_passages = pool[0:6]
        bm25_passages = pool[3:9]
        dense_passages = pool[6:15]
        temporal_passages = [pool[i] for i in (0, 2, 4, 7, 10, 13)]

        all_signals = [
            (ast_passages, W_AST),
            (bm25_passages, W_BM25),
            (dense_passages, W_DENSE),
            (temporal_passages, W_TEMPORAL),
        ]

        total_raw = sum(len(passages) for passages, _ in all_signals)
        assert total_raw == 27

        ranked = rank_candidates_rrf(all_signals)

        unique_count = len(ranked)
        assert unique_count == 15
        deduplication_reduction_ratio = (total_raw - unique_count) / total_raw
        assert deduplication_reduction_ratio >= 0.20
