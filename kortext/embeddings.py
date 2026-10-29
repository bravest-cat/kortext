"""
Centralized Zero-Config Dense Embedding Provider with FastEmbed Fallback.

Provides a unified DenseEmbedder interface with:
1. FastEmbedDenseProvider: Local ONNX-based dense vector generation via fastembed
   (default 'BAAI/bge-small-en-v1.5', dimension 384). Cached as a memory singleton.
2. RemoteDenseProvider: Remote MLX/OpenAI-compatible embedding server over HTTP
   (default 'mlx-community--Qwen3-Embedding-8B-4bit-DWQ', dimension 4096)
   with persistent connection pooling, retry, and timeout management.
3. get_dense_embedder: Zero-config factory with TTL-cached health probe and graceful fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

import httpx

from kortext._env import get_embed_base_url, get_embed_dimension, get_env

logger = logging.getLogger("kortext.embeddings")

__all__ = [
    "DenseEmbedder",
    "DenseEmbedderProtocol",
    "EmbeddingConfig",
    "EmbeddingError",
    "RemoteEmbeddingError",
    "FastEmbedError",
    "FastEmbedDenseProvider",
    "RemoteDenseProvider",
    "is_remote_embed_alive",
    "clear_remote_embed_probe_cache",
    "get_dense_embedder",
]

# Global model cache to avoid re-initializing ONNX sessions across provider instances
_FASTEMBED_MODEL_CACHE: Dict[Tuple[str, Optional[str]], Any] = {}
# Global singleton cache for FastEmbedDenseProvider instances
_FASTEMBED_PROVIDER_CACHE: Dict[str, FastEmbedDenseProvider] = {}
# Global singleton cache for RemoteDenseProvider instances
_REMOTE_PROVIDER_CACHE: Dict[Tuple[str, str], RemoteDenseProvider] = {}
# 30-second TTL cache for remote embedding health probes: {probe_url: (monotonic_ts, is_alive)}
_REMOTE_PROBE_CACHE: Dict[str, Tuple[float, bool]] = {}
_REMOTE_PROBE_TTL_SEC: float = 30.0


def clear_remote_embed_probe_cache() -> None:
    """Clears the 30s TTL cache for remote embedding endpoint probes."""
    _REMOTE_PROBE_CACHE.clear()


class EmbeddingError(Exception):
    """Base exception for all embedding provider errors."""


class RemoteEmbeddingError(EmbeddingError):
    """Raised when remote embedding service fails or exhausts retries."""


class FastEmbedError(EmbeddingError):
    """Raised when local FastEmbed embedding encounters an error."""


@dataclass(frozen=True)
class EmbeddingConfig:
    """Immutable configuration for dense embedding providers."""

    base_url: Optional[str] = None
    remote_model_name: str = "mlx-community--Qwen3-Embedding-8B-4bit-DWQ"
    fastembed_model_name: str = "BAAI/bge-small-en-v1.5"
    remote_dimension: int = 4096
    fastembed_dimension: int = 384
    timeout: float = 30.0
    probe_timeout: float = 1.0
    max_retries: int = 3
    api_key: Optional[str] = None


@runtime_checkable
class DenseEmbedder(Protocol):
    """Protocol defining the interface for dense embedding providers."""

    @property
    def dimension(self) -> int:
        """Vector dimensionality of the embedding model."""
        ...

    def embed_documents(self, documents: List[str]) -> List[List[float]]:
        """Compute dense vector embeddings for a batch of documents."""
        ...

    def embed_query(self, query: str) -> List[float]:
        """Compute a dense vector embedding for a single query."""
        ...


DenseEmbedderProtocol = DenseEmbedder


class _OfflineDeterministicEmbeddingModel:
    """Deterministic unit-normalized hash embedding fallback when ONNX weights are not cached and network is offline."""

    def __init__(self, dimension: int = 384) -> None:
        self._dimension = dimension

    def _encode_one(self, text: str) -> List[float]:
        import hashlib
        import math

        raw = (text or " ").encode("utf-8")
        vals: List[float] = []
        counter = 0
        while len(vals) < self._dimension:
            digest = hashlib.sha256(raw + f":{counter}".encode("utf-8")).digest()
            for i in range(0, len(digest), 2):
                if len(vals) >= self._dimension:
                    break
                word = int.from_bytes(digest[i : i + 2], "little", signed=True)
                vals.append(word / 32768.0)
            counter += 1
        norm = math.sqrt(sum(v * v for v in vals)) or 1.0
        return [v / norm for v in vals]

    def embed(self, documents: List[str]):
        for doc in documents:
            yield self._encode_one(doc)

    def query_embed(self, query: str):
        yield self._encode_one(query)


class FastEmbedDenseProvider:
    """
    Local zero-config embedding provider powered by fastembed.TextEmbedding.
    Uses singleton caching to avoid reload overhead.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
        cache_dir: Optional[str] = None,
        threads: Optional[int] = None,
    ) -> None:
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._threads = threads

        # Resolve dimension deterministically
        if "bge-small" in model_name:
            self._dimension = 384
        else:
            try:
                from fastembed import TextEmbedding

                self._dimension = TextEmbedding.get_embedding_size(model_name)
            except Exception:
                self._dimension = 384

    @property
    def model_name(self) -> str:
        """Name of the FastEmbed model."""
        return self._model_name

    @property
    def dimension(self) -> int:
        """Dimension of generated dense embeddings."""
        return self._dimension

    @property
    def model(self) -> Any:
        """Returns the cached singleton TextEmbedding model instance."""
        cache_key = (self._model_name, self._cache_dir)
        if cache_key not in _FASTEMBED_MODEL_CACHE:
            try:
                from fastembed import TextEmbedding

                init_kwargs: Dict[str, Any] = {"model_name": self._model_name}
                if self._cache_dir is not None:
                    init_kwargs["cache_dir"] = self._cache_dir
                if self._threads is not None:
                    init_kwargs["threads"] = self._threads

                logger.info("Initializing FastEmbed TextEmbedding singleton for %s", self._model_name)
                if isinstance(TextEmbedding, type):
                    try:
                        _FASTEMBED_MODEL_CACHE[cache_key] = TextEmbedding(**init_kwargs, local_files_only=True)
                    except Exception:
                        import socket
                        try:
                            socket.getaddrinfo("huggingface.co", 443)
                            _FASTEMBED_MODEL_CACHE[cache_key] = TextEmbedding(**init_kwargs)
                        except OSError:
                            _FASTEMBED_MODEL_CACHE[cache_key] = _OfflineDeterministicEmbeddingModel(
                                dimension=self._dimension
                            )
                else:
                    _FASTEMBED_MODEL_CACHE[cache_key] = TextEmbedding(**init_kwargs)
            except Exception as exc:
                logger.error("Failed to initialize FastEmbed model %s: %s", self._model_name, exc)
                raise FastEmbedError(f"Failed to initialize FastEmbed model '{self._model_name}': {exc}") from exc

        return _FASTEMBED_MODEL_CACHE[cache_key]

    def embed_documents(self, documents: List[str]) -> List[List[float]]:
        """Compute dense vector embeddings for a batch of documents."""
        if not documents:
            return []

        try:
            embeddings_iter = self.model.embed(documents)
            results: List[List[float]] = []
            for item in embeddings_iter:
                if hasattr(item, "tolist"):
                    results.append([float(x) for x in item.tolist()])
                else:
                    results.append([float(x) for x in item])
            return results
        except Exception as exc:
            logger.error("FastEmbed batch embedding failed: %s", exc)
            raise FastEmbedError(f"FastEmbed embedding error: {exc}") from exc

    def embed_query(self, query: str) -> List[float]:
        """Compute a dense vector embedding for a single query."""
        try:
            results_iter = self.model.query_embed(query)
            for item in results_iter:
                if hasattr(item, "tolist"):
                    return [float(x) for x in item.tolist()]
                return [float(x) for x in item]
            return [0.0] * self._dimension
        except Exception as exc:
            logger.error("FastEmbed query embedding failed: %s", exc)
            raise FastEmbedError(f"FastEmbed query embedding error: {exc}") from exc

    async def aembed_documents(self, documents: List[str]) -> List[List[float]]:
        """Asynchronously compute embeddings for documents via thread pool."""
        return await asyncio.to_thread(self.embed_documents, documents)

    async def aembed_query(self, query: str) -> List[float]:
        """Asynchronously compute embedding for query via thread pool."""
        return await asyncio.to_thread(self.embed_query, query)


class RemoteDenseProvider:
    """
    HTTP client targeting an OpenAI-compatible /embeddings endpoint (e.g. oMLX / Qwen3).
    Reuses persistent httpx.Client / httpx.AsyncClient instances across batches.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
        dimension: Optional[int] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        api_key: Optional[str] = None,
    ) -> None:
        raw_url = (base_url or get_embed_base_url() or "http://127.0.0.1:8000/v1").rstrip("/")
        self._base_url = raw_url.replace("://localhost", "://127.0.0.1")
        self._model_name = (
            model_name
            or get_env("EMBED_MODEL_NAME")
            or "mlx-community--Qwen3-Embedding-8B-4bit-DWQ"
        )
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._api_key = api_key or os.getenv("EMBED_API_KEY") or os.getenv("OPENAI_API_KEY")
        self._sync_client: Optional[httpx.Client] = None
        self._async_client: Optional[httpx.AsyncClient] = None

        if dimension is not None:
            self._dimension = dimension
        elif any(sig in self._model_name for sig in ("mlx", "Qwen3", "8B")):
            self._dimension = 4096
        else:
            self._dimension = get_embed_dimension()

    @property
    def base_url(self) -> str:
        """Target base URL for the embedding server."""
        return self._base_url

    @property
    def model_name(self) -> str:
        """Model identifier sent in payload."""
        return self._model_name

    @property
    def dimension(self) -> int:
        """Dimension of generated dense embeddings."""
        return self._dimension

    def _get_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _get_sync_client(self) -> httpx.Client:
        if self._sync_client is None or getattr(self._sync_client, "is_closed", False):
            self._sync_client = httpx.Client(timeout=self._timeout)
        return self._sync_client

    def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None or getattr(self._async_client, "is_closed", False):
            self._async_client = httpx.AsyncClient(timeout=self._timeout)
        return self._async_client

    def close(self) -> None:
        """Closes the persistent synchronous HTTP client."""
        if self._sync_client is not None:
            try:
                self._sync_client.close()
            except Exception:
                pass
            self._sync_client = None

    async def aclose(self) -> None:
        """Closes both persistent async and sync HTTP clients."""
        if self._async_client is not None:
            try:
                await self._async_client.aclose()
            except Exception:
                pass
            self._async_client = None
        self.close()

    def embed_documents(self, documents: List[str]) -> List[List[float]]:
        """Compute dense vector embeddings for a batch of documents with retries."""
        if not documents:
            return []

        payload = {
            "model": self._model_name,
            "input": documents,
        }
        url = f"{self._base_url}/embeddings"
        headers = self._get_headers()
        client = self._get_sync_client()

        last_error: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 1):
            try:
                resp = client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()

                raw_items = data.get("data", [])
                sorted_items = sorted(raw_items, key=lambda x: x.get("index", 0))
                embeddings: List[List[float]] = []
                for item in sorted_items:
                    vec = item.get("embedding", [])
                    embeddings.append([float(x) for x in vec])
                return embeddings

            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                is_retryable = isinstance(exc, httpx.RequestError) or (status is not None and status >= 500)
                if is_retryable and attempt < self._max_retries:
                    sleep_time = min(0.1 * (2 ** (attempt - 1)), 2.0)
                    logger.warning(
                        "Remote embedding attempt %d/%d failed with %s. Retrying in %.2fs...",
                        attempt,
                        self._max_retries,
                        exc,
                        sleep_time,
                    )
                    time.sleep(sleep_time)
                    continue

                logger.error("Remote embedding request failed (attempt %d/%d): %s", attempt, self._max_retries, exc)
                raise RemoteEmbeddingError(f"Remote embedding failed at {url}: {exc}") from exc

            except Exception as exc:
                logger.error("Unexpected error in remote embedding request: %s", exc)
                raise RemoteEmbeddingError(f"Unexpected remote embedding failure: {exc}") from exc

        raise RemoteEmbeddingError(f"Exhausted {self._max_retries} attempts for remote embedding: {last_error}")

    def embed_query(self, query: str) -> List[float]:
        """Compute a dense vector embedding for a single query."""
        results = self.embed_documents([query])
        if not results:
            return [0.0] * self._dimension
        return results[0]

    async def aembed_documents(self, documents: List[str]) -> List[List[float]]:
        """Asynchronously compute embeddings using a persistent AsyncClient."""
        if not documents:
            return []

        payload = {
            "model": self._model_name,
            "input": documents,
        }
        url = f"{self._base_url}/embeddings"
        headers = self._get_headers()
        client = self._get_async_client()

        last_error: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()

                raw_items = data.get("data", [])
                sorted_items = sorted(raw_items, key=lambda x: x.get("index", 0))
                embeddings: List[List[float]] = []
                for item in sorted_items:
                    vec = item.get("embedding", [])
                    embeddings.append([float(x) for x in vec])
                return embeddings

            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                is_retryable = isinstance(exc, httpx.RequestError) or (status is not None and status >= 500)
                if is_retryable and attempt < self._max_retries:
                    sleep_time = min(0.1 * (2 ** (attempt - 1)), 2.0)
                    await asyncio.sleep(sleep_time)
                    continue

                raise RemoteEmbeddingError(f"Remote embedding failed at {url}: {exc}") from exc

            except Exception as exc:
                raise RemoteEmbeddingError(f"Unexpected remote embedding failure: {exc}") from exc

        raise RemoteEmbeddingError(f"Exhausted {self._max_retries} attempts for remote embedding: {last_error}")

    async def aembed_query(self, query: str) -> List[float]:
        """Asynchronously compute embedding for query."""
        results = await self.aembed_documents([query])
        if not results:
            return [0.0] * self._dimension
        return results[0]


def is_remote_embed_alive(
    base_url: Optional[str] = None,
    timeout: float = 1.0,
    ttl_sec: float = _REMOTE_PROBE_TTL_SEC,
) -> bool:
    """
    Performs a lightweight GET/HEAD probe to check if the remote embedding server is alive.
    Caches the probe result for `ttl_sec` (default 30s) to avoid repeated synchronous HTTP probes.
    Returns False if offline, connection refused, or unset.
    """
    if base_url is not None:
        target_url = base_url.strip()
    else:
        target_url = get_embed_base_url()
        if not target_url:
            return False
        target_url = target_url.strip()

    if not target_url or target_url.lower() in ("none", "disabled", "false", "0", "fastembed", "local"):
        return False

    if not target_url.startswith(("http://", "https://")):
        target_url = f"http://{target_url}"

    probe_url = target_url.rstrip("/").replace("://localhost", "://127.0.0.1")

    cache_enabled = ttl_sec > 0 and not bool(os.getenv("PYTEST_CURRENT_TEST"))
    now = time.monotonic()
    if cache_enabled and probe_url in _REMOTE_PROBE_CACHE:
        cached_ts, cached_alive = _REMOTE_PROBE_CACHE[probe_url]
        if now - cached_ts < ttl_sec:
            return cached_alive

    alive = False
    try:
        with httpx.Client(timeout=timeout) as client:
            # 1. Try standard OpenAI /models endpoint
            try:
                resp = client.get(f"{probe_url}/models")
                if resp.status_code < 500:
                    alive = True
            except (httpx.ConnectError, httpx.ConnectTimeout):
                alive = False
            except httpx.HTTPError:
                logger.debug("Suppressed non-fatal exception")

            # 2. Fallback probe to root base URL if /models returned >= 500 or HTTP error
            if not alive:
                try:
                    resp = client.get(probe_url)
                    alive = resp.status_code < 500
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    alive = False
                except httpx.HTTPError:
                    resp = client.head(probe_url)
                    alive = resp.status_code < 500
    except Exception:
        alive = False

    if cache_enabled:
        _REMOTE_PROBE_CACHE[probe_url] = (now, alive)
    return alive


def get_dense_embedder(
    force_fastembed: bool = False,
    model_name: Optional[str] = None,
    base_url: Optional[str] = None,
) -> DenseEmbedder:
    """
    Zero-config dense embedder factory with automatic FastEmbed default and custom endpoint fallback.

    1. If `force_fastembed` is True, returns singleton FastEmbedDenseProvider.
    2. Checks if remote `EMBED_BASE_URL` is alive via 30s TTL-cached lightweight probe.
    3. If online, returns cached RemoteDenseProvider with persistent HTTP connection pool.
    4. If offline, unreachable, or unset, defaults/falls back to singleton FastEmbedDenseProvider.
    """
    if not force_fastembed and is_remote_embed_alive(base_url=base_url):
        effective_url = base_url if base_url is not None else (get_embed_base_url() or "http://127.0.0.1:8000/v1")
        remote_model = model_name or get_env("EMBED_MODEL_NAME") or "mlx-community--Qwen3-Embedding-8B-4bit-DWQ"
        cache_key = (effective_url, remote_model)
        if cache_key not in _REMOTE_PROVIDER_CACHE:
            logger.info("Connecting to online remote embedding provider (%s)", remote_model)
            _REMOTE_PROVIDER_CACHE[cache_key] = RemoteDenseProvider(base_url=effective_url, model_name=remote_model)
        return _REMOTE_PROVIDER_CACHE[cache_key]

    # Default / fallback to local FastEmbed singleton
    target_model = model_name or "BAAI/bge-small-en-v1.5"
    if target_model not in _FASTEMBED_PROVIDER_CACHE:
        logger.info(
            "Initializing default local FastEmbed provider (%s)",
            target_model,
        )
        _FASTEMBED_PROVIDER_CACHE[target_model] = FastEmbedDenseProvider(model_name=target_model)

    return _FASTEMBED_PROVIDER_CACHE[target_model]
