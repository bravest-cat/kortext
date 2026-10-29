"""
Hybrid retrieval: four signals fused with weighted Reciprocal Rank Fusion (k=60).
  1. AST symbol seek (FalkorDB symbol graph)
  2. BM25 sparse vectors (FastEmbed, Qdrant)
  3. Dense vectors (FastEmbed or an OpenAI-compatible endpoint, Qdrant)
  4. Current episodic facts (FalkorDB, valid-time filtered)
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastembed import SparseTextEmbedding
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qmodels

from kortext._env import (
    get_configured_qdrant_url,
    get_embed_base_url,
    get_embed_dimension,
    get_embed_model_name,
    get_env,
    get_falkordb_database,
    get_falkordb_symbols_graph,
    get_int_env,
    get_qdrant_collection,
    get_qdrant_url,
)
from kortext._home import get_kortext_home
from kortext.embeddings import get_dense_embedder
from kortext.falkordb_client import get_shared_falkordb_client
from kortext.retrieval.federated import (
    execute_federated_query,
    fetch_tier3_overview,
    partition_budgets,
    resolve_project_scope,
)
from kortext.retrieval.fusion import (
    DEFAULT_FUSION_WEIGHTS,
    K_RRF,
    PROSE_FUSION_WEIGHTS,
    SYMBOLIC_FUSION_WEIGHTS,
    W_AST,
    W_BM25,
    W_DENSE,
    W_TEMPORAL,
    FederatedRetrievalResult,
    apply_knapsack_mmr,
    compute_adaptive_weights,
    extract_passage_text,
    rank_candidates_rrf,
    redistribute_ci_weights,
    redistribute_weights,
)
from kortext.retrieval.query_parser import (
    _CALL_PARENS_PATTERN,
    _CAMEL_CASE_PATTERN,
    _CODE_SYNTAX_PATTERN,
    _COLLECTION_WORKSPACE_CACHE,
    _DOTTED_SYMBOL_PATTERN,
    _PROGRAMMING_KEYWORDS_PATTERN,
    is_ci_mode,
    is_file_in_workspace,
    is_symbolic_query,
    resolve_symbol_graph_for_collection,
    resolve_workspace_root_for_collection,
)
from kortext.symbols.seek import extract_symbol_candidates

logger = logging.getLogger("kortext.retrieval")

# Singleton Sparse FastEmbed BM25 Model
_SPARSE_MODEL: Optional[SparseTextEmbedding] = None
_SPARSE_MODEL_INIT_FAILED: bool = False


def get_sparse_model() -> Optional[SparseTextEmbedding]:
    """Lazily initializes and returns the singleton FastEmbed BM25 sparse model."""
    global _SPARSE_MODEL, _SPARSE_MODEL_INIT_FAILED
    if _SPARSE_MODEL is not None:
        return _SPARSE_MODEL
    if _SPARSE_MODEL_INIT_FAILED:
        return None
    try:
        try:
            _SPARSE_MODEL = SparseTextEmbedding(model_name="Qdrant/bm25", local_files_only=True)
        except Exception:
            import socket
            try:
                socket.getaddrinfo("huggingface.co", 443)
                _SPARSE_MODEL = SparseTextEmbedding(model_name="Qdrant/bm25")
            except OSError as net_exc:
                _SPARSE_MODEL_INIT_FAILED = True
                logger.warning(f"Offline mode detected; skipping remote BM25 download: {net_exc}")
                return None
    except Exception as exc:
        _SPARSE_MODEL_INIT_FAILED = True
        logger.warning(f"Failed to initialize sparse model: {exc}")
        return None
    return _SPARSE_MODEL


# Backward-compatible environment and directory constants
EMBED_BASE_URL: str = (get_embed_base_url() or "http://127.0.0.1:8000/v1").replace("://localhost", "://127.0.0.1")
_INITIAL_QDRANT_URL: str = get_qdrant_url()
QDRANT_URL: str = _INITIAL_QDRANT_URL
KORTEXT_HOME = get_kortext_home()
DEFAULT_QDRANT_PATH = KORTEXT_HOME / "qdrant"

DEFAULT_COLLECTION: str = get_qdrant_collection()
DEFAULT_DATABASE: str = get_falkordb_database()
DEFAULT_SYMBOLS_GRAPH: str = get_falkordb_symbols_graph()

TOP_K_RETRIEVE: int = get_int_env("RETRIEVE_TOP_K", 30)
TOP_K_RESULTS: int = get_int_env("RETRIEVE_RESULTS", 5)

# Aliases for backward compatibility
_is_file_in_workspace = is_file_in_workspace


class RetrievalEngine:
    """Hybrid RRF retrieval over Qdrant (dense, BM25) and FalkorDB (symbols, episodic facts)."""

    def __init__(self):
        self._client: AsyncQdrantClient | None = None
        self._falkordb = get_shared_falkordb_client()
        self._candidate_metadata: Dict[str, Dict[str, Any]] = {}
        self._meta_lock = asyncio.Lock()
        self._inflight_embeds: Dict[str, asyncio.Task] = {}
        self._symbol_seek = None
        self._episodic = None

    @property
    def symbol_seek(self):
        if self._symbol_seek is None:
            from kortext.symbols.seek import SymbolSeekEngine

            self._symbol_seek = SymbolSeekEngine()
        return self._symbol_seek

    @symbol_seek.setter
    def symbol_seek(self, val):
        self._symbol_seek = val

    @property
    def episodic(self):
        if self._episodic is None:
            from kortext.episodic import AgentNativeEpisodicDriver

            self._episodic = AgentNativeEpisodicDriver()
        return self._episodic

    @episodic.setter
    def episodic(self, val):
        self._episodic = val

    async def initialize(self):
        # Resolve unified Qdrant mode (avoiding split-brain between Embedded and HTTP :6333)
        if QDRANT_URL != _INITIAL_QDRANT_URL:
            # Module-level QDRANT_URL was explicitly overridden (e.g. by a test)
            effective_url = None if (not QDRANT_URL or QDRANT_URL.startswith("file:")) else QDRANT_URL
        else:
            effective_url = get_configured_qdrant_url(probe_local=True)

        if effective_url:
            self._client = AsyncQdrantClient(url=effective_url)
        else:
            qdrant_dir = get_kortext_home() / "qdrant"
            qdrant_dir.mkdir(parents=True, exist_ok=True)
            self._client = AsyncQdrantClient(path=str(qdrant_dir))

        await self._ensure_collection(DEFAULT_COLLECTION)

    async def _ensure_collection(self, collection_name: str):
        if get_env("EMBED_DIMENSION"):
            try:
                embed_dim = int(get_env("EMBED_DIMENSION"))
            except (ValueError, TypeError):
                embed_dim = get_embed_dimension()
        else:
            try:
                embed_dim = get_dense_embedder().dimension
            except Exception:
                embed_dim = get_embed_dimension()
        try:
            if not await self._client.collection_exists(collection_name):
                await self._client.create_collection(
                    collection_name=collection_name,
                    vectors_config={
                        "text-dense": qmodels.VectorParams(
                            size=embed_dim, distance=qmodels.Distance.COSINE
                        )
                    },
                    sparse_vectors_config={"text-sparse-new": qmodels.SparseVectorParams()},
                )
        except Exception as exc:
            logger.warning(f"Could not verify/create Qdrant collection '{collection_name}': {exc}")

    async def _compute_query_embedding(self, prompt: str) -> List[float]:
        try:
            embedder = get_dense_embedder()
            if hasattr(embedder, "aembed_query") and callable(embedder.aembed_query):
                return await embedder.aembed_query(prompt)
            return embedder.embed_query(prompt)
        except Exception as exc:
            logger.warning(f"Failed to generate query embedding: {exc}")
            return []

    async def _get_query_embedding(self, prompt: str) -> List[float]:
        if not prompt or not prompt.strip():
            return []
        key = prompt.strip()
        existing_task = self._inflight_embeds.get(key)
        if existing_task is not None and not existing_task.done():
            try:
                return await asyncio.shield(existing_task)
            except Exception:
                pass
        task = asyncio.create_task(self._compute_query_embedding(prompt))
        self._inflight_embeds[key] = task
        try:
            return await task
        finally:
            if self._inflight_embeds.get(key) is task:
                self._inflight_embeds.pop(key, None)

    def _get_sparse_embedding(self, prompt: str) -> Optional[Tuple[List[int], List[float]]]:
        if not prompt or not prompt.strip():
            return None
        try:
            model = get_sparse_model()
            if model is None:
                return None
            embeddings = list(model.query_embed(prompt))
            if not embeddings:
                return None
            emb = embeddings[0]
            indices = [int(i) for i in emb.indices]
            values = [float(v) for v in emb.values]
            if not indices or not values:
                return None
            return indices, values
        except Exception as exc:
            logger.warning(f"Failed to generate sparse embedding: {exc}")
            return None

    async def _get_collection_dense_dim(self, col_name: str) -> Optional[int]:
        """Returns the configured 'text-dense' vector dimension for col_name if inspectable."""
        if not self._client or not hasattr(self._client, "get_collection"):
            return None
        cache_attr = getattr(self, "_col_dim_cache", None)
        if cache_attr is None:
            cache_attr = {}
            self._col_dim_cache = cache_attr
        if col_name in cache_attr:
            return cache_attr[col_name]
        try:
            info = await self._client.get_collection(col_name)
            vectors = getattr(getattr(getattr(info, "config", None), "params", None), "vectors", None)
            if isinstance(vectors, dict) and "text-dense" in vectors:
                sz = getattr(vectors["text-dense"], "size", None)
                if isinstance(sz, int) and sz > 0:
                    cache_attr[col_name] = sz
                    return sz
        except Exception:
            pass
        return None

    async def _retrieve_qdrant_points(
        self,
        col_name: str,
        dense_vec: List[float],
        limit: int = TOP_K_RETRIEVE,
        candidate_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[str]:
        if not self._client or not dense_vec:
            return []
        col_dim = await self._get_collection_dense_dim(col_name)
        if isinstance(col_dim, int) and col_dim != len(dense_vec):
            # Vectors from different models are not comparable; padding or truncating them gives noise.
            logger.error(
                "Collection '%s' holds %d-d vectors but the active embedder (%s) makes %d-d ones; dense search is off "
                "for it. Re-index the collection, or restore the EMBED_BASE_URL/EMBED_MODEL_NAME it was built with.",
                col_name, col_dim, get_embed_model_name(), len(dense_vec),
            )
            return []
        target_meta = candidate_metadata if candidate_metadata is not None else self._candidate_metadata
        try:
            response = await self._client.query_points(
                collection_name=col_name,
                query=dense_vec,
                using="text-dense",
                limit=limit,
                with_payload=True,
            )
            passages = []
            for p in response.points:
                payload = getattr(p, "payload", None) or {}
                text = extract_passage_text(payload)
                if text:
                    passages.append(text)
                    if isinstance(payload, dict):
                        ts = (
                            payload.get("timestamp")
                            or payload.get("created_at")
                            or payload.get("valid_from")
                            or payload.get("commit_date")
                        )
                        created_at = payload.get("created_at") or payload.get("commit_date")
                        valid_from = payload.get("valid_from") or payload.get("commit_date")
                        imp = payload.get("importance")
                        if ts is not None or imp is not None or payload.get("chunk_type") is not None:
                            meta_entry = {
                                "timestamp": ts,
                                "created_at": created_at,
                                "valid_from": valid_from,
                                "importance": imp if imp is not None else 5,
                                "tier": "dense",
                                "chunk_type": payload.get("chunk_type"),
                            }
                            target_meta[text] = meta_entry
            return passages
        except Exception as exc:
            logger.warning(f"Qdrant query_points failed on '{col_name}': {exc}")
            return []

    async def _retrieve_qdrant_sparse_points(
        self,
        col_name: str,
        sparse_indices: List[int],
        sparse_values: List[float],
        limit: int = TOP_K_RETRIEVE,
        candidate_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[str]:
        if not self._client or not sparse_indices or not sparse_values:
            return []
        target_meta = candidate_metadata if candidate_metadata is not None else self._candidate_metadata
        try:
            response = await self._client.query_points(
                collection_name=col_name,
                query=qmodels.SparseVector(indices=sparse_indices, values=sparse_values),
                using="text-sparse-new",
                limit=limit,
                with_payload=True,
            )
            passages = []
            for p in response.points:
                payload = getattr(p, "payload", None) or {}
                text = extract_passage_text(payload)
                if text:
                    passages.append(text)
                    if isinstance(payload, dict):
                        ts = (
                            payload.get("timestamp")
                            or payload.get("created_at")
                            or payload.get("valid_from")
                            or payload.get("commit_date")
                        )
                        created_at = payload.get("created_at") or payload.get("commit_date")
                        valid_from = payload.get("valid_from") or payload.get("commit_date")
                        imp = payload.get("importance")
                        if ts is not None or imp is not None or payload.get("chunk_type") is not None:
                            meta_entry = {
                                "timestamp": ts,
                                "created_at": created_at,
                                "valid_from": valid_from,
                                "importance": imp if imp is not None else 5,
                                "tier": "sparse",
                                "chunk_type": payload.get("chunk_type"),
                            }
                            target_meta[text] = meta_entry
            return passages
        except Exception as exc:
            logger.warning(f"Qdrant sparse query_points failed on '{col_name}': {exc}")
            return []

    async def _retrieve_qdrant_hybrid_rrf(
        self,
        col_name: str,
        dense_vec: List[float],
        sparse_indices: List[int],
        sparse_values: List[float],
        limit: int = TOP_K_RETRIEVE,
        candidate_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[str]:
        """
        Single-roundtrip Qdrant Hybrid Query using native Prefetch + FusionQuery(fusion=Fusion.RRF)
        across both 'text-dense' and 'text-sparse-new' vectors.
        """
        if not self._client:
            return []
        col_dim = await self._get_collection_dense_dim(col_name)
        dense_ok = bool(
            dense_vec
            and not (
                isinstance(col_dim, int)
                and len(dense_vec) in (384, 768, 1024, 1536, 4096)
                and col_dim != len(dense_vec)
            )
        )
        sparse_ok = bool(sparse_indices and sparse_values)
        if not dense_ok and not sparse_ok:
            return []
        if not dense_ok:
            return await self._retrieve_qdrant_sparse_points(
                col_name, sparse_indices, sparse_values, limit=limit, candidate_metadata=candidate_metadata
            )
        if not sparse_ok:
            return await self._retrieve_qdrant_points(
                col_name, dense_vec, limit=limit, candidate_metadata=candidate_metadata
            )

        target_meta = candidate_metadata if candidate_metadata is not None else self._candidate_metadata
        try:
            prefetches = [
                qmodels.Prefetch(
                    query=dense_vec,
                    using="text-dense",
                    limit=limit,
                ),
                qmodels.Prefetch(
                    query=qmodels.SparseVector(indices=sparse_indices, values=sparse_values),
                    using="text-sparse-new",
                    limit=limit,
                ),
            ]
            response = await self._client.query_points(
                collection_name=col_name,
                prefetch=prefetches,
                query=qmodels.FusionQuery(fusion=qmodels.Fusion.RRF),
                limit=limit,
                with_payload=True,
            )
            passages: List[str] = []
            for p in response.points:
                payload = getattr(p, "payload", None) or {}
                text = extract_passage_text(payload)
                if text:
                    passages.append(text)
                    if isinstance(payload, dict):
                        ts = (
                            payload.get("timestamp")
                            or payload.get("created_at")
                            or payload.get("valid_from")
                            or payload.get("commit_date")
                        )
                        created_at = payload.get("created_at") or payload.get("commit_date")
                        valid_from = payload.get("valid_from") or payload.get("commit_date")
                        imp = payload.get("importance")
                        if ts is not None or imp is not None or payload.get("chunk_type") is not None:
                            target_meta[text] = {
                                "timestamp": ts,
                                "created_at": created_at,
                                "valid_from": valid_from,
                                "importance": imp if imp is not None else 5,
                                "tier": "hybrid_rrf",
                                "chunk_type": payload.get("chunk_type"),
                            }
            return passages
        except Exception as exc:
            logger.debug(f"Single-roundtrip hybrid RRF fallback on '{col_name}': {exc}")
            return await self._retrieve_qdrant_points(
                col_name, dense_vec, limit=limit, candidate_metadata=candidate_metadata
            )

    extract_symbol_candidates = staticmethod(extract_symbol_candidates)

    def _extract_symbol_candidates(self, query: str) -> List[str]:
        return extract_symbol_candidates(query)

    def _fetch_ast_matches(self, symbols: List[str]) -> List[str]:
        if not symbols:
            return []
        matches = []
        if getattr(self._falkordb, "_is_live_falkor_client", False) is True:
            cypher_batch = """
            UNWIND $names AS sym_name
            MATCH (s:Symbol {name: sym_name})
            OPTIONAL MATCH (s)-[d:DEFINED_IN]->(f:File)
            RETURN s.name, s.kind, f.path, d.line, d.end_line, d.signature, d.docstring
            LIMIT 15
            """
            res = self._falkordb.query(DEFAULT_SYMBOLS_GRAPH, cypher_batch, {"names": symbols[:5]})
            if res and len(res) > 1 and res[1]:
                for row in res[1]:
                    name, kind, path, line, end_line, sig, doc = row
                    passage = (
                        f"### AST Symbol: `{name}` ({kind or 'symbol'})\n"
                        f"- File: `{path or 'unknown'}` (Lines {line or 1}-{end_line or line or 1})\n"
                        f"- Signature: `{sig or name}`\n"
                    )
                    if doc:
                        passage += f"- Docstring: {doc}\n"
                    matches.append(passage)
            return matches

        for sym in symbols[:5]:
            cypher = """
            MATCH (s:Symbol)
            WHERE s.name = $name
            OPTIONAL MATCH (s)-[d:DEFINED_IN]->(f:File)
            RETURN s.name, s.kind, f.path, d.line, d.end_line, d.signature, d.docstring
            LIMIT 3
            """
            res = self._falkordb.query(DEFAULT_SYMBOLS_GRAPH, cypher, {"name": sym})
            if res and len(res) > 1 and res[1]:
                for row in res[1]:
                    name, kind, path, line, end_line, sig, doc = row
                    passage = (
                        f"### AST Symbol: `{name}` ({kind or 'symbol'})\n"
                        f"- File: `{path or 'unknown'}` (Lines {line or 1}-{end_line or line or 1})\n"
                        f"- Signature: `{sig or name}`\n"
                    )
                    if doc:
                        passage += f"- Docstring: {doc}\n"
                    matches.append(passage)
        return matches

    def _fetch_temporal_facts(
        self,
        prompt: str,
        project: Optional[str] = None,
        candidate_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[str]:
        target_meta = candidate_metadata if candidate_metadata is not None else self._candidate_metadata
        try:
            import re
            from datetime import datetime, timezone

            now_iso = datetime.now(timezone.utc).isoformat()
            q_tokens = [w.lower() for w in re.split(r"\W+", prompt or "") if len(w) > 2]

            target_project = project.strip() if isinstance(project, str) else None
            filter_project = bool(target_project and target_project.lower() != "all")
            proj_clause = "AND coalesce(r.project, 'kortext') = $project " if filter_project else ""

            if q_tokens:
                token_clause = (
                    "AND ANY(tok IN $tokens WHERE toLower(coalesce(r.fact, '')) CONTAINS tok "
                    "OR toLower(e.name) CONTAINS tok OR toLower(t.name) CONTAINS tok "
                    "OR toLower(coalesce(r.relation, '')) CONTAINS tok) "
                )
            else:
                token_clause = ""

            cypher = f"""
            MATCH (e:Entity)-[r:RELATES_TO]->(t:Entity)
            WHERE coalesce(r.superseded, false) = false
              AND coalesce(r.dormant, false) = false
              AND coalesce(r.trust_score, 0.8) >= 0.5
              AND coalesce(r.trust_level, 'verified') <> 'untrusted'
              AND (r.valid_to IS NULL OR r.valid_to >= $now)
              {proj_clause}{token_clause}RETURN e.name, r.relation, t.name, r.fact, r.valid_from, r.importance
            ORDER BY r.valid_from DESC
            LIMIT 5
            """
            params: Dict[str, Any] = {"now": now_iso}
            if filter_project:
                params["project"] = target_project
            if q_tokens:
                params["tokens"] = q_tokens

            res = self._falkordb.query(DEFAULT_DATABASE, cypher, params)
            if res and len(res) > 1 and res[1]:
                passages = []
                for row in res[1]:
                    passage = f"[Temporal Fact] `{row[0]}` {row[1]} `{row[2]}`: {row[3]}"
                    passages.append(passage)
                    valid_from = row[4] if len(row) > 4 else None
                    importance = row[5] if len(row) > 5 else 5
                    target_meta[passage] = {
                        "valid_from": valid_from,
                        "importance": importance if importance is not None else 5,
                        "tier": "temporal",
                    }
                return passages
        except Exception as exc:
            logger.debug(f"Temporal facts retrieval error: {exc}")
        return []

    def get_adaptive_weights(
        self,
        query: str,
        dense_available: bool = True,
        ci_mode: Optional[bool] = None,
        suppress_ast_signal: bool = False,
    ) -> Dict[str, float]:
        return compute_adaptive_weights(
            query=query,
            ci_mode=ci_mode,
            dense_available=dense_available,
            suppress_ast_signal=suppress_ast_signal,
        )

    def _rank_candidates(
        self,
        signals: List[Tuple[List[Any], float]] | Dict[str, List[Any]],
        candidate_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
        weights: Optional[Dict[str, float]] = None,
        query: Optional[str] = None,
    ) -> List[Tuple[str, float]]:
        if candidate_metadata is not None:
            meta_lookup = dict(candidate_metadata)
        else:
            meta_lookup = dict(self._candidate_metadata)
        return rank_candidates_rrf(
            signals=signals,
            candidate_metadata=meta_lookup,
            weights=weights,
            query=query,
        )

    async def retrieve(
        self,
        prompt: str,
        collection: Optional[str | List[str]] = None,
        **kwargs: Any,
    ) -> List[str]:
        return await self.query(prompt=prompt, collection=collection, **kwargs)

    async def query(
        self,
        prompt: str,
        collection: Optional[str | List[str]] = None,
        weights: Optional[Dict[str, float]] = None,
        top_k: int = TOP_K_RESULTS,
        limit: Optional[int] = None,
        suppress_ast_signal: bool = False,
        suppress_temporal_signal: bool = False,
        query_vec: Optional[List[float]] = None,
        candidate_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> List[str]:
        if limit is not None:
            top_k = limit

        # Use a request-local dictionary to avoid concurrent race conditions across overlapping query() calls
        local_metadata: Dict[str, Dict[str, Any]] = (
            candidate_metadata if candidate_metadata is not None else {}
        )

        if self._client is None and not getattr(self, "_skip_embedded_qdrant_init", False):
            try:
                await self.initialize()
            except Exception as init_exc:
                logger.warning("Qdrant client initialization skipped (falling back to graph/AST signals): %s", init_exc)

        target_cols = self._resolve_target_collections(collection)

        loop = asyncio.get_running_loop()
        candidates = self._extract_symbol_candidates(prompt)

        # Parallel signal execution (reusing pre-computed query_vec when supplied by federated_query)
        if query_vec is not None:
            dense_vec_task = asyncio.create_task(asyncio.sleep(0, result=query_vec))
        else:
            dense_vec_task = asyncio.create_task(self._get_query_embedding(prompt))

        sparse_vec_task = loop.run_in_executor(None, self._get_sparse_embedding, prompt)
        ast_task = (
            None
            if suppress_ast_signal
            else loop.run_in_executor(None, self._fetch_ast_matches, candidates)
        )
        temporal_task = (
            None
            if suppress_temporal_signal
            else loop.run_in_executor(None, self._fetch_temporal_facts, prompt, None, local_metadata)
        )

        gather_tasks = [dense_vec_task, sparse_vec_task]
        if ast_task is not None:
            gather_tasks.append(ast_task)
        if temporal_task is not None:
            gather_tasks.append(temporal_task)

        gathered_res = await asyncio.gather(*gather_tasks)
        dense_vec = gathered_res[0]
        sparse_res = gathered_res[1]

        idx = 2
        if ast_task is not None:
            ast_passages = gathered_res[idx]
            idx += 1
        else:
            ast_passages = []

        if temporal_task is not None:
            temporal_passages = gathered_res[idx]
        else:
            temporal_passages = []

        def _is_ast_passage(text: Any) -> bool:
            s = str(text).strip()
            if s.startswith("### AST Symbol:") or "### ast symbol:" in s.lower():
                return True
            meta = local_metadata.get(s) or local_metadata.get(str(text)) or {}
            return meta.get("chunk_type") == "ast_skeleton"

        if suppress_ast_signal:
            temporal_passages = [p for p in temporal_passages if not _is_ast_passage(p)]

        sparse_indices, sparse_values = sparse_res if sparse_res else ([], [])

        inactive_keys: List[str] = []
        if suppress_ast_signal:
            inactive_keys.append("ast")
        if suppress_temporal_signal:
            inactive_keys.append("temporal")

        if weights is not None:
            active_weights = dict(weights)
            if inactive_keys:
                active_weights = redistribute_weights(active_weights, inactive_keys=inactive_keys)
        else:
            dense_available = bool(dense_vec) and not is_ci_mode()
            active_weights = compute_adaptive_weights(
                prompt,
                ci_mode=is_ci_mode(),
                dense_available=dense_available,
                suppress_ast_signal=suppress_ast_signal,
            )
            if suppress_temporal_signal:
                active_weights = redistribute_weights(active_weights, inactive_keys=["temporal"])

        w_dense = active_weights.get("dense", W_DENSE)
        w_bm25 = active_weights.get("bm25", W_BM25)

        # Multi-collection parallel retrieval writing into request-local metadata
        async def _call_dense(col: str) -> List[str]:
            try:
                return await self._retrieve_qdrant_points(
                    col, dense_vec, candidate_metadata=local_metadata
                )
            except TypeError:
                return await self._retrieve_qdrant_points(col, dense_vec)

        async def _call_sparse(col: str) -> List[str]:
            try:
                return await self._retrieve_qdrant_sparse_points(
                    col, sparse_indices, sparse_values, candidate_metadata=local_metadata
                )
            except TypeError:
                return await self._retrieve_qdrant_sparse_points(
                    col, sparse_indices, sparse_values
                )

        dense_batches, sparse_batches = await asyncio.gather(
            asyncio.gather(*[_call_dense(col) for col in target_cols]),
            asyncio.gather(*[_call_sparse(col) for col in target_cols]),
        )

        # Snapshot latest request metadata for backward-compatible inspection
        self._candidate_metadata = dict(local_metadata)

        if suppress_ast_signal:
            dense_batches = [[p for p in b if not _is_ast_passage(p)] for b in dense_batches]
            sparse_batches = [[p for p in b if not _is_ast_passage(p)] for b in sparse_batches]

        dense_passages, sparse_passages = self._merge_multi_collection_candidates(
            target_cols, dense_batches, sparse_batches, w_dense, w_bm25
        )

        all_signals = [
            (ast_passages, active_weights.get("ast", W_AST)),
            (sparse_passages, active_weights.get("bm25", W_BM25)),
            (dense_passages, active_weights.get("dense", W_DENSE)),
            (temporal_passages, active_weights.get("temporal", W_TEMPORAL)),
        ]

        ranked_candidates = self._rank_candidates(
            all_signals,
            candidate_metadata=local_metadata,
            query=prompt,
        )
        if not ranked_candidates:
            return []

        if suppress_ast_signal:
            ranked_candidates = [(p, sc) for p, sc in ranked_candidates if not _is_ast_passage(p)]
            if not ranked_candidates:
                return []

        passages = [p for p, _ in ranked_candidates[:top_k]]
        if suppress_ast_signal:
            passages = [p for p in passages if not _is_ast_passage(p)]
        return passages

    def _resolve_target_collections(self, collection: Optional[str | List[str]]) -> List[str]:
        if isinstance(collection, str) and collection.strip().lower() == "all":
            try:
                from kortext.retrieval.discovery import discover_vector_collections

                discovered = discover_vector_collections()
                names = [
                    str(c.get("name")).strip()
                    for c in discovered
                    if isinstance(c, dict) and c.get("name")
                ]
                if names:
                    return names
            except Exception as exc:
                logger.debug(f"Collection discovery fallback for 'all': {exc}")
            return [DEFAULT_COLLECTION]
        if isinstance(collection, str) and "," in collection:
            return [c.strip() for c in collection.split(",") if c.strip()]
        if isinstance(collection, list):
            return [c.strip() for c in collection if c.strip()]
        if collection:
            return [collection]
        return [DEFAULT_COLLECTION]

    def _merge_multi_collection_candidates(
        self,
        target_cols: List[str],
        dense_batches: List[List[str]],
        sparse_batches: List[List[str]],
        w_dense: float,
        w_bm25: float,
    ) -> Tuple[List[str], List[str]]:
        merged_scores: Dict[str, float] = {}
        for i in range(len(target_cols)):
            d_batch = dense_batches[i] if i < len(dense_batches) else []
            s_batch = sparse_batches[i] if i < len(sparse_batches) else []

            d_ranks = {p: r for r, p in enumerate(d_batch)}
            s_ranks = {p: r for r, p in enumerate(s_batch)}

            col_candidates = list(dict.fromkeys(d_batch + s_batch))
            if not col_candidates:
                continue

            local_scores: Dict[str, float] = {}
            for p in col_candidates:
                score_val = 0.0
                if p in d_ranks:
                    score_val += w_dense / (K_RRF + d_ranks[p] + 1)
                if p in s_ranks:
                    score_val += w_bm25 / (K_RRF + s_ranks[p] + 1)
                local_scores[p] = score_val

            max_local = max(local_scores.values()) if local_scores else 0.0
            for p, sc in local_scores.items():
                norm_sc = (sc / max_local) if max_local > 0.0 else 0.0
                merged_scores[p] = max(merged_scores.get(p, 0.0), norm_sc)

        sorted_cands = sorted(merged_scores.keys(), key=lambda p: merged_scores[p], reverse=True)
        all_dense = {p for b in dense_batches for p in b}
        all_sparse = {p for b in sparse_batches for p in b}
        return [p for p in sorted_cands if p in all_dense], [p for p in sorted_cands if p in all_sparse]

    async def _fetch_tier3_overview(
        self,
        workspace_root: Optional[str],
        query: str,
        budget: int,
        detail_level: str = "balanced",
        collection: Optional[str] = None,
        query_vec: Optional[List[float]] = None,
    ) -> Optional[str]:
        return await fetch_tier3_overview(
            client=self._client,
            falkordb=self._falkordb,
            query_embedding_func=self._get_query_embedding,
            workspace_root=workspace_root,
            query=query,
            budget=budget,
            detail_level=detail_level,
            collection=collection,
            query_vec=query_vec,
        )

    async def federated_query(
        self,
        query: str,
        token_budget: int = 1500,
        collection: Optional[str] = None,
        workspace_root: Optional[str] = None,
        detail_level: str = "balanced",
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await execute_federated_query(
            engine=self,
            query=query,
            token_budget=token_budget,
            collection=collection,
            workspace_root=workspace_root,
            detail_level=detail_level,
            project=project,
        )

    async def close(self):
        if self._client:
            await self._client.close()
            self._client = None


_DEFAULT_RETRIEVAL_ENGINE: Optional[RetrievalEngine] = None


async def federated_query(
    query: str,
    token_budget: int = 1500,
    collection: Optional[str] = None,
    workspace_root: Optional[str] = None,
    detail_level: str = "balanced",
    project: Optional[str] = None,
    engine: Optional[RetrievalEngine] = None,
) -> Dict[str, Any]:
    """Module-level convenience wrapper for federated multi-tier retrieval."""
    global _DEFAULT_RETRIEVAL_ENGINE
    if engine is None:
        if _DEFAULT_RETRIEVAL_ENGINE is None:
            _DEFAULT_RETRIEVAL_ENGINE = RetrievalEngine()
        engine = _DEFAULT_RETRIEVAL_ENGINE
    return await engine.federated_query(
        query=query,
        token_budget=token_budget,
        collection=collection,
        workspace_root=workspace_root,
        detail_level=detail_level,
        project=project,
    )


__all__ = [
    "RetrievalEngine",
    "federated_query",
    "get_sparse_model",
    "extract_passage_text",
    "is_ci_mode",
    "is_symbolic_query",
    "is_file_in_workspace",
    "resolve_workspace_root_for_collection",
    "resolve_symbol_graph_for_collection",
    "_COLLECTION_WORKSPACE_CACHE",
    "redistribute_weights",
    "redistribute_ci_weights",
    "compute_adaptive_weights",
    "rank_candidates_rrf",
    "FederatedRetrievalResult",
    "apply_knapsack_mmr",
    "W_AST",
    "W_BM25",
    "W_DENSE",
    "W_TEMPORAL",
    "K_RRF",
    "DEFAULT_FUSION_WEIGHTS",
    "SYMBOLIC_FUSION_WEIGHTS",
    "PROSE_FUSION_WEIGHTS",
    "EMBED_BASE_URL",
    "QDRANT_URL",
    "DEFAULT_COLLECTION",
    "DEFAULT_DATABASE",
    "DEFAULT_SYMBOLS_GRAPH",
    "TOP_K_RETRIEVE",
    "TOP_K_RESULTS",
]
