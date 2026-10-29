"""
End-to-End FastEmbed Fallback and Substrate Resilience Invariant Tests.

Verifies:
1. End-to-end ingestion fallback:
   When EMBED_BASE_URL points to an unreachable endpoint (e.g. http://127.0.0.1:9999/v1),
   index_single_file() automatically falls back to FastEmbed, successfully chunks and
   vectors the file with 384-dim embeddings, and records status "indexed".
2. End-to-end retrieval query fallback:
   When EMBED_BASE_URL is unreachable, RetrievalEngine._get_query_embedding() returns
   a valid 384-dim float embedding from FastEmbed without error.
3. Substrate resilience invariants:
   - FalkorDBClient UDS socket priority (~/.kortext/run/falkordb.sock) with loopback TCP fallback.
   - Qdrant Sole Custodian invariant: get_qdrant_client() connects via HTTP client by default
     and does not instantiate embedded file locks when URL is configured.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import redis

from kortext._home import get_kortext_home
from kortext._qdrant import (
    get_async_qdrant_client,
    get_qdrant_client,
    get_sync_qdrant_client,
)
from kortext.cache import ChunkEmbeddingCache, IngestCache
from kortext.falkordb_client import (
    DEFAULT_SOCKET,
    FalkorDBClient,
    get_default_socket,
)
from kortext.ingest import index_single_file
from kortext.retrieval.engine import RetrievalEngine


# ---------------------------------------------------------------------------
# 1. Ingestion Fallback Tests
# ---------------------------------------------------------------------------

class TestIngestionFallback:
    """Verifies that index_single_file() gracefully falls back to local FastEmbed."""

    @pytest.mark.anyio
    async def test_index_single_file_falls_back_to_fastembed_when_embed_url_unreachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: configure unreachable embedding service URL
        unreachable_url = "http://127.0.0.1:9999/v1"
        monkeypatch.setenv("EMBED_BASE_URL", unreachable_url)
        monkeypatch.setenv("KORTEXT_EMBED_BASE_URL", unreachable_url)

        source_code = (
            "def calculate_total(prices: list[float], tax_rate: float = 0.05) -> float:\n"
            "    \"\"\"Compute total order amount including applicable taxes.\"\"\"\n"
            "    subtotal = sum(prices)\n"
            "    return subtotal * (1.0 + tax_rate)\n"
        )
        sample_file = tmp_path / "finance.py"
        sample_file.write_text(source_code, encoding="utf-8")

        mock_client = AsyncMock()
        cache = IngestCache(db_path=tmp_path / "cache.db")
        chunk_cache = ChunkEmbeddingCache(db_path=tmp_path / "chunk_cache.db")
        mock_symbol_mgr = MagicMock()

        # Act: index file through end-to-end ingestion pipeline
        result = await index_single_file(
            file_path=sample_file,
            root=tmp_path,
            client=mock_client,
            cache=cache,
            chunk_cache=chunk_cache,
            symbol_mgr=mock_symbol_mgr,
            force=False,
        )

        # Assert: ingestion reports successful indexing
        assert result["status"] == "indexed"
        assert result["chunks"] >= 1
        assert result["changed"] is True
        assert len(result["points"]) >= 1

        # Assert: Qdrant upsert was called with points containing 384-dim FastEmbed vectors
        mock_client.upsert.assert_awaited_once()
        upsert_kwargs = mock_client.upsert.call_args.kwargs
        points = upsert_kwargs["points"]
        assert len(points) >= 1

        point_vector = points[0].vector
        assert "text-dense" in point_vector
        dense_vec = point_vector["text-dense"]

        assert isinstance(dense_vec, list)
        assert len(dense_vec) == 384
        assert all(isinstance(x, float) for x in dense_vec)
        # Vector must contain real embeddings, not all zeros
        assert any(x != 0.0 for x in dense_vec)

    @pytest.mark.anyio
    async def test_index_single_file_forced_reindex_fallback_when_embed_url_unreachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: unreachable endpoint with forced re-index
        monkeypatch.setenv("EMBED_BASE_URL", "http://127.0.0.1:9999/v1")
        sample_file = tmp_path / "greeter.py"
        sample_file.write_text("def greet(name: str) -> str:\n    return f'Hello {name}'\n", encoding="utf-8")

        mock_client = AsyncMock()
        cache = IngestCache(db_path=tmp_path / "cache.db")
        chunk_cache = ChunkEmbeddingCache(db_path=tmp_path / "chunk_cache.db")
        mock_symbol_mgr = MagicMock()

        # Act: index with force=True
        result = await index_single_file(
            file_path=sample_file,
            root=tmp_path,
            client=mock_client,
            cache=cache,
            chunk_cache=chunk_cache,
            symbol_mgr=mock_symbol_mgr,
            force=True,
        )

        # Assert
        assert result["status"] == "indexed"
        assert result["chunks"] >= 1
        mock_client.upsert.assert_awaited_once()
        points = mock_client.upsert.call_args.kwargs["points"]
        assert len(points[0].vector["text-dense"]) == 384


# ---------------------------------------------------------------------------
# 2. Retrieval Query Fallback Tests
# ---------------------------------------------------------------------------

class TestRetrievalQueryFallback:
    """Verifies that RetrievalEngine query embedding falls back cleanly to FastEmbed."""

    @pytest.mark.anyio
    async def test_query_embedding_falls_back_to_fastembed_when_embed_url_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: point to unreachable port
        unreachable_url = "http://127.0.0.1:9999/v1"
        monkeypatch.setenv("EMBED_BASE_URL", unreachable_url)
        monkeypatch.setenv("KORTEXT_EMBED_BASE_URL", unreachable_url)

        engine = RetrievalEngine()
        query_text = "def calculate_total(prices: list[float]) -> float:"

        # Act: retrieve query vector
        query_vector = await engine._get_query_embedding(query_text)

        # Assert: returns valid 384-dimensional dense float vector
        assert isinstance(query_vector, list)
        assert len(query_vector) == 384
        assert all(isinstance(val, float) for val in query_vector)
        assert any(val != 0.0 for val in query_vector)

    @pytest.mark.anyio
    async def test_query_embedding_returns_empty_list_on_blank_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: unreachable endpoint with empty query
        monkeypatch.setenv("EMBED_BASE_URL", "http://127.0.0.1:9999/v1")
        engine = RetrievalEngine()

        # Act & Assert
        assert await engine._get_query_embedding("") == []
        assert await engine._get_query_embedding("   ") == []

    @pytest.mark.anyio
    async def test_query_embedding_falls_back_when_env_url_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: unset all embedding URLs
        monkeypatch.delenv("EMBED_BASE_URL", raising=False)
        monkeypatch.delenv("KORTEXT_EMBED_BASE_URL", raising=False)

        engine = RetrievalEngine()

        # Act
        query_vector = await engine._get_query_embedding("search for architectural patterns")

        # Assert
        assert isinstance(query_vector, list)
        assert len(query_vector) == 384
        assert all(isinstance(x, float) for x in query_vector)


# ---------------------------------------------------------------------------
# 3. FalkorDB Substrate Resilience Invariants
# ---------------------------------------------------------------------------

class TestFalkorDBSubstrateResilience:
    """
    Verifies Substrate Resilience Invariant for FalkorDB:
    Prefers Unix domain socket (~/.kortext/run/falkordb.sock) with loopback TCP fallback.
    """

    def test_default_socket_path_resolves_to_kortext_home_run_socket(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange
        custom_home = tmp_path / ".kortext"
        monkeypatch.setenv("KORTEXT_HOME", str(custom_home))
        monkeypatch.delenv("FALKORDB_SOCKET", raising=False)

        # Act
        resolved_socket = get_default_socket()

        # Assert
        assert resolved_socket == custom_home / "run" / "falkordb.sock"

    def test_uds_socket_priority_when_socket_available(self) -> None:
        # Arrange: mock socket path that exists and is a socket
        dummy_socket = Path("/tmp/mock_kortext_run/falkordb.sock")

        with patch.object(Path, "is_socket", return_value=True), \
             patch("redis.Redis") as mock_redis_cls:

            mock_instance = MagicMock()
            mock_instance.ping.return_value = True
            mock_redis_cls.return_value = mock_instance

            client = FalkorDBClient(
                host="127.0.0.1",
                port=6379,
                socket_path=dummy_socket,
            )

            # Act
            r = client.get_redis()

            # Assert: UDS connection was prioritized and returned
            assert r is mock_instance
            mock_redis_cls.assert_called_once_with(
                unix_socket_path=str(dummy_socket),
                password=None,
                decode_responses=True,
                socket_timeout=5.0,
                socket_connect_timeout=0.5,
                retry=ANY,
                max_connections=32,
            )
            mock_instance.ping.assert_called_once()

    def test_uds_fallback_to_loopback_tcp_when_socket_ping_fails(self) -> None:
        # Arrange: socket exists, but ping throws connection error
        dummy_socket = Path("/tmp/mock_kortext_run/falkordb.sock")

        uds_redis_mock = MagicMock()
        uds_redis_mock.ping.side_effect = redis.ConnectionError("UDS connection refused")

        tcp_redis_mock = MagicMock()
        tcp_redis_mock.ping.return_value = True

        def redis_factory(**kwargs):
            if "unix_socket_path" in kwargs:
                return uds_redis_mock
            return tcp_redis_mock

        with patch.object(Path, "is_socket", return_value=True), \
             patch("redis.Redis", side_effect=redis_factory) as mock_redis_cls:

            client = FalkorDBClient(
                host="127.0.0.1",
                port=6379,
                socket_path=dummy_socket,
            )

            # Act
            r = client.get_redis()

            # Assert: fell back to loopback TCP connection
            assert r is tcp_redis_mock
            assert mock_redis_cls.call_count == 2

            # Check that second attempt targeted loopback TCP
            second_call_kwargs = mock_redis_cls.call_args_list[1].kwargs
            assert second_call_kwargs["host"] == "127.0.0.1"
            assert second_call_kwargs["port"] == 6379
            assert "unix_socket_path" not in second_call_kwargs

    def test_uds_fallback_to_loopback_tcp_when_socket_does_not_exist(self) -> None:
        # Arrange: socket does not exist
        missing_socket = Path("/nonexistent/falkordb.sock")

        with patch.object(Path, "is_socket", return_value=False), \
             patch("redis.Redis") as mock_redis_cls:

            mock_tcp_instance = MagicMock()
            mock_redis_cls.return_value = mock_tcp_instance

            client = FalkorDBClient(
                host="127.0.0.1",
                port=6379,
                socket_path=missing_socket,
            )

            # Act
            r = client.get_redis()

            # Assert: direct TCP connection without UDS attempt
            assert r is mock_tcp_instance
            mock_redis_cls.assert_called_once_with(
                host="127.0.0.1",
                port=6379,
                password=None,
                decode_responses=True,
                socket_timeout=5.0,
                socket_connect_timeout=0.5,
                retry=ANY,
                max_connections=32,
            )


# ---------------------------------------------------------------------------
# 4. Qdrant Sole Custodian Invariants
# ---------------------------------------------------------------------------

class TestQdrantSoleCustodianInvariant:
    """
    Verifies Sole Custodian Invariant for Qdrant:
    get_qdrant_client() connects via HTTP client by default and does not
    instantiate embedded file locks when URL is configured.
    """

    def test_get_qdrant_client_connects_via_http_url_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: ensure clean URL resolution
        monkeypatch.delenv("QDRANT_URL", raising=False)
        monkeypatch.delenv("KORTEXT_QDRANT_URL", raising=False)

        with patch("kortext._qdrant.QdrantClient") as mock_qdrant_cls:
            # Act
            client = get_qdrant_client()

            # Assert: connects over HTTP, no embedded storage lock
            mock_qdrant_cls.assert_called_once_with(
                url="http://localhost:6333",
                timeout=5.0,
            )
            # Verify 'path' argument was never provided
            call_kwargs = mock_qdrant_cls.call_args.kwargs
            assert "path" not in call_kwargs

    def test_get_qdrant_client_uses_configured_env_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: configure remote URL
        remote_url = "http://127.0.0.1:6333"
        monkeypatch.setenv("KORTEXT_QDRANT_URL", remote_url)

        with patch("kortext._qdrant.QdrantClient") as mock_qdrant_cls:
            # Act
            client = get_qdrant_client()

            # Assert
            mock_qdrant_cls.assert_called_once_with(
                url=remote_url,
                timeout=5.0,
            )
            assert "path" not in mock_qdrant_cls.call_args.kwargs

    def test_get_qdrant_client_uses_explicit_url_parameter(self) -> None:
        # Arrange: pass url explicitly
        explicit_url = "http://qdrant-cluster.local:6333"

        with patch("kortext._qdrant.QdrantClient") as mock_qdrant_cls:
            # Act
            client = get_qdrant_client(url=explicit_url, timeout=12.0)

            # Assert
            mock_qdrant_cls.assert_called_once_with(
                url=explicit_url,
                timeout=12.0,
            )
            assert "path" not in mock_qdrant_cls.call_args.kwargs

    def test_get_async_qdrant_client_uses_http_url_and_never_embedded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        configured_url = "http://127.0.0.1:6333"
        monkeypatch.setenv("KORTEXT_QDRANT_URL", configured_url)

        with patch("kortext._qdrant.AsyncQdrantClient") as mock_async_cls:
            # Act
            client = get_async_qdrant_client()

            # Assert
            mock_async_cls.assert_called_once_with(
                url=configured_url,
                timeout=5.0,
            )
            assert "path" not in mock_async_cls.call_args.kwargs

    def test_embedded_mode_strictly_gated_to_explicit_storage_path(
        self, tmp_path: Path
    ) -> None:
        # Arrange: explicit storage path (e.g. test isolation)
        isolated_store = tmp_path / "embedded_vector_store"

        with patch("kortext._qdrant.QdrantClient") as mock_qdrant_cls:
            # Act
            client = get_qdrant_client(storage_path=isolated_store)

            # Assert: only when storage_path is explicitly supplied does embedded client instantiate
            mock_qdrant_cls.assert_called_once_with(path=str(isolated_store))
            assert isolated_store.exists()
