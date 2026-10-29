"""
4-signal Reciprocal Rank Fusion (RRF), adaptive weights, and Knapsack MMR diversity.

Combines deterministic AST symbols, lexical BM25, dense semantics and current episodic facts.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from kortext.chunking.token_utils import count_tokens
from kortext.retrieval.query_parser import is_ci_mode, is_symbolic_query

logger = logging.getLogger("kortext.retrieval.fusion")

W_AST: float = 0.35
W_BM25: float = 0.25
W_DENSE: float = 0.30
W_TEMPORAL: float = 0.10
K_RRF: int = 60

DEFAULT_FUSION_WEIGHTS: Dict[str, float] = {"ast": W_AST, "bm25": W_BM25, "dense": W_DENSE, "temporal": W_TEMPORAL}
# Code queries favour exact symbols and lexical matches; prose favours dense vectors and facts.
SYMBOLIC_FUSION_WEIGHTS: Dict[str, float] = {"ast": 0.45, "bm25": 0.28, "dense": 0.17, "temporal": 0.10}
PROSE_FUSION_WEIGHTS: Dict[str, float] = {"ast": 0.15, "bm25": 0.20, "dense": 0.45, "temporal": 0.20}


def extract_passage_text(payload: Dict[str, Any]) -> str:
    """Text of a Qdrant point payload: 'text', LlamaIndex '_node_content', 'document', else the JSON payload."""
    if not payload:
        return ""
    if "text" in payload:
        return str(payload["text"])
    if "_node_content" in payload:
        try:
            nc = json.loads(payload["_node_content"])
            if "text" in nc:
                return str(nc["text"])
        except (ValueError, TypeError) as exc:
            logger.debug("Undecodable _node_content payload: %s", exc)
    if "document" in payload:
        return str(payload["document"])
    return json.dumps(payload, default=str)


def redistribute_weights(
    weights: Dict[str, float],
    inactive_keys: Optional[List[str]] = None,
) -> Dict[str, float]:
    """
    Redistributes the weight mass of inactive modalities proportionally
    across all active modalities so that the active weights sum to 1.0.
    """
    if inactive_keys is None:
        inactive_keys = ["dense"]

    inactive_set = set(inactive_keys)
    active_sum = sum(w for k, w in weights.items() if k not in inactive_set)
    if active_sum <= 0:
        return dict(weights)

    total_weight = sum(weights.values())
    new_weights: Dict[str, float] = {}
    for k, w in weights.items():
        if k in inactive_set:
            new_weights[k] = 0.0
        else:
            new_weights[k] = (w / active_sum) * total_weight
    return new_weights


def redistribute_ci_weights(weights: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """Redistributes dense weight across active modalities for CI mode."""
    target_weights = dict(weights) if weights is not None else dict(DEFAULT_FUSION_WEIGHTS)
    return redistribute_weights(target_weights, inactive_keys=["dense"])


def compute_adaptive_weights(
    query: str,
    ci_mode: Optional[bool] = None,
    dense_available: bool = True,
    suppress_ast_signal: bool = False,
) -> Dict[str, float]:
    """RRF weights by query type, with the mass of unavailable signals (dense offline, AST suppressed) redistributed."""
    if ci_mode is None:
        ci_mode = is_ci_mode()

    if is_symbolic_query(query):
        weights = dict(SYMBOLIC_FUSION_WEIGHTS)
    else:
        weights = dict(PROSE_FUSION_WEIGHTS)

    inactive_keys: List[str] = []
    if ci_mode or not dense_available:
        inactive_keys.append("dense")
    if suppress_ast_signal:
        inactive_keys.append("ast")

    if inactive_keys:
        weights = redistribute_weights(weights, inactive_keys=inactive_keys)

    return weights


def _passage_and_meta(item: Any) -> Tuple[str, Dict[str, Any]]:
    """Normalizes a candidate (str, (str, meta), dict or object with .text) into (passage, metadata)."""
    if isinstance(item, str):
        return item, {}
    if isinstance(item, tuple):
        return str(item[0]), dict(item[1]) if len(item) > 1 and isinstance(item[1], dict) else {}
    if isinstance(item, dict):
        meta = dict(item)
        if isinstance(item.get("metadata"), dict):
            meta.update(item["metadata"])
        return str(item.get("text") or item.get("passage") or item.get("document") or item), meta
    meta = getattr(item, "metadata", None)
    return str(getattr(item, "text", None) or getattr(item, "passage", item)), dict(meta) if isinstance(meta, dict) else {}


def compute_length_penalty(passage: str, pivot_chars: int = 500, alpha: float = 0.5) -> float:
    """
    Computes the logarithmic length normalization factor ("Semantic Sponge" defense):
        LengthFactor = 1.0 / (1.0 + alpha * log2(max(1.0, len(passage) / pivot_chars)))
    Prevents overly verbose chunks or reports from crowding out surgical definitions.
    """
    if not passage:
        return 1.0
    char_len = len(passage)
    if char_len <= pivot_chars:
        return 1.0
    ratio = max(1.0, char_len / float(pivot_chars))
    penalty = 1.0 / (1.0 + alpha * math.log2(ratio))
    return max(0.40, min(1.0, penalty))


def has_exact_lexical_identifier_match(passage: str, query: Optional[str]) -> bool:
    """Checks whether the candidate passage contains an exact code identifier from the query."""
    if not query or not passage:
        return False
    idents = [
        tok for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)
        if ("_" in tok or any(c.isupper() for c in tok[1:]))
    ]
    if not idents:
        return False
    return any(ident in passage for ident in idents)


def rank_candidates_rrf(
    signals: List[Tuple[List[Any], float]] | Dict[str, List[Any]],
    candidate_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    weights: Optional[Dict[str, float]] = None,
    query: Optional[str] = None,
) -> List[Tuple[str, float]]:
    """
    Weighted Reciprocal Rank Fusion with a length penalty for long chunks and a boost for
    exact identifier matches:  score(c) = sum_tier w_tier / (K_RRF + rank) * length_penalty * lex_boost
    """
    rrf_scores: Dict[str, float] = {}
    meta_lookup = candidate_metadata or {}

    if isinstance(signals, dict):
        active_weights = dict(weights) if weights is not None else dict(DEFAULT_FUSION_WEIGHTS)
        if is_ci_mode() or not signals.get("dense"):
            active_weights = redistribute_weights(active_weights, inactive_keys=["dense"])
        signal_list: List[Tuple[List[Any], float]] = [
            (items, active_weights.get(tier, 0.0)) for tier, items in signals.items()
        ]
        bm25_items = signals.get("bm25") or []
    else:
        signal_list = signals
        bm25_items = signal_list[1][0] if len(signal_list) > 1 else []

    for items, weight in signal_list:
        for rank, item in enumerate(items):
            passage, item_meta = _passage_and_meta(item)
            meta = {**meta_lookup.get(passage, {}), **item_meta}
            len_mod = compute_length_penalty(passage) if meta.get("apply_length_penalty", len(passage) > 1200) else 1.0
            lex_boost = 1.15 if (query and has_exact_lexical_identifier_match(passage, query)) else 1.0
            rrf_scores[passage] = rrf_scores.get(passage, 0.0) + (weight / (K_RRF + rank + 1)) * len_mod * lex_boost

    # Floor for exact identifier hits in BM25's top 3, so a precise lexical match is not outvoted.
    if query:
        for rank, b_item in enumerate(bm25_items[:3]):
            b_pass, _ = _passage_and_meta(b_item)
            if b_pass in rrf_scores and has_exact_lexical_identifier_match(b_pass, query):
                rrf_scores[b_pass] = max(rrf_scores[b_pass], 0.92 / (K_RRF + rank + 1))

    return sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)


class FederatedRetrievalResult(TypedDict, total=False):
    """Structured envelope for 4-tier federated retrieval."""
    query: str
    symbolic_context: List[Any]
    code_chunks: List[str]
    episodic_history: List[str]
    architectural_overview: Optional[str]
    total_estimated_tokens: int
    markdown: Optional[str]


def apply_knapsack_mmr(
    candidates: List[Any],
    query: str,
    token_budget: int = 400,
    lambda_param: float = 0.7,
    diversity_threshold: float = 0.65,
) -> List[str]:
    """
    Selects a diverse subset of episodic candidates within the token budget
    using Maximal Marginal Relevance (MMR) and knapsack capacity constraints.
    """
    if not candidates or token_budget <= 0:
        return []

    norm_candidates: List[str] = [
        c if isinstance(c, str) else json.dumps(c) for c in candidates if c
    ]
    if not norm_candidates:
        return []

    query_tokens = set(re.findall(r"\w+", query.lower()))

    def token_sim(a_words: set[str], b_words: set[str]) -> float:
        if not a_words or not b_words:
            return 0.0
        intersection = len(a_words & b_words)
        union = len(a_words | b_words)
        return intersection / union if union > 0 else 0.0

    cand_word_sets = [set(re.findall(r"\w+", c.lower())) for c in norm_candidates]

    selected: List[str] = []
    selected_word_sets: List[set[str]] = []
    current_tokens = 0
    remaining_indices = list(range(len(norm_candidates)))

    while remaining_indices:
        best_idx = None
        best_score = -float("inf")

        for idx in remaining_indices:
            cand_text = norm_candidates[idx]
            cand_tokens = count_tokens(cand_text)

            if current_tokens + cand_tokens > token_budget:
                continue

            cand_words = cand_word_sets[idx]
            redundancy = (
                max(token_sim(cand_words, sel_words) for sel_words in selected_word_sets)
                if selected_word_sets
                else 0.0
            )

            if redundancy >= diversity_threshold:
                continue

            query_rel = token_sim(cand_words, query_tokens)
            rank_prior = 1.0 / (1.0 + 0.1 * idx)
            relevance = 0.6 * query_rel + 0.4 * rank_prior
            score = lambda_param * relevance - (1.0 - lambda_param) * redundancy

            if score > best_score:
                best_score = score
                best_idx = idx

        if best_idx is None or best_score <= 0.0:
            break

        cand_text = norm_candidates[best_idx]
        selected.append(cand_text)
        selected_word_sets.append(cand_word_sets[best_idx])
        current_tokens += count_tokens(cand_text)
        remaining_indices.remove(best_idx)

    return selected
