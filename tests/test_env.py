"""
Unit tests for centralized environment variable resolver in kortext._env.
"""

from pathlib import Path
import pytest

from kortext._env import (
    get_env,
    get_int_env,
    get_float_env,
    get_bool_env,
    get_falkordb_socket,
    get_falkordb_host,
    get_falkordb_port,
    get_falkordb_database,
    get_falkordb_symbols_graph,
    get_qdrant_url,
    get_qdrant_collection,
    get_embed_base_url,
    get_embed_model_name,
)
import kortext


def test_kortext_package_exports():
    expected_exports = [
        "get_env",
        "get_int_env",
        "get_float_env",
        "get_bool_env",
        "get_falkordb_socket",
        "get_falkordb_host",
        "get_falkordb_port",
        "get_falkordb_database",
        "get_falkordb_symbols_graph",
        "get_qdrant_url",
        "get_qdrant_collection",
        "get_embed_base_url",
        "get_embed_model_name",
    ]
    for name in expected_exports:
        assert hasattr(kortext, name), f"kortext is missing export {name}"


class TestGetEnv:
    def test_kortext_prefix_takes_precedence_over_raw(self, monkeypatch):
        monkeypatch.setenv("KORTEXT_TEST_VAR", "kortext_val")
        monkeypatch.setenv("TEST_VAR", "raw_val")

        assert get_env("TEST_VAR") == "kortext_val"

    def test_fallback_to_raw_name_when_prefix_absent(self, monkeypatch):
        monkeypatch.delenv("KORTEXT_TEST_VAR", raising=False)
        monkeypatch.setenv("TEST_VAR", "raw_val")

        assert get_env("TEST_VAR") == "raw_val"

    def test_fallback_to_raw_name_when_prefix_empty(self, monkeypatch):
        monkeypatch.setenv("KORTEXT_TEST_VAR", "")
        monkeypatch.setenv("TEST_VAR", "raw_val")

        assert get_env("TEST_VAR") == "raw_val"

    def test_fallback_to_default_when_all_absent_or_empty(self, monkeypatch):
        monkeypatch.delenv("KORTEXT_TEST_VAR", raising=False)
        monkeypatch.delenv("TEST_VAR", raising=False)

        assert get_env("TEST_VAR", "default_val") == "default_val"
        assert get_env("TEST_VAR") is None

        monkeypatch.setenv("TEST_VAR", "")
        assert get_env("TEST_VAR", "default_val") == "default_val"

    def test_direct_lookup_when_name_starts_with_kortext_prefix(self, monkeypatch):
        monkeypatch.setenv("KORTEXT_CUSTOM_VAR", "explicit_kortext")
        assert get_env("KORTEXT_CUSTOM_VAR") == "explicit_kortext"


class TestTypedGetters:
    def test_get_int_env(self, monkeypatch):
        monkeypatch.setenv("KORTEXT_TEST_INT", "42")
        assert get_int_env("TEST_INT", 10) == 42

        # Fallback to raw
        monkeypatch.delenv("KORTEXT_TEST_INT", raising=False)
        monkeypatch.setenv("TEST_INT", "100")
        assert get_int_env("TEST_INT", 10) == 100

        # Default on missing
        monkeypatch.delenv("TEST_INT", raising=False)
        assert get_int_env("TEST_INT", 10) == 10

        # Default on invalid
        monkeypatch.setenv("TEST_INT", "invalid_int")
        assert get_int_env("TEST_INT", 10) == 10

    def test_get_float_env(self, monkeypatch):
        monkeypatch.setenv("KORTEXT_TEST_FLOAT", "3.1415")
        assert get_float_env("TEST_FLOAT", 1.0) == pytest.approx(3.1415)

        # Fallback to raw
        monkeypatch.delenv("KORTEXT_TEST_FLOAT", raising=False)
        monkeypatch.setenv("TEST_FLOAT", "0.5")
        assert get_float_env("TEST_FLOAT", 1.0) == pytest.approx(0.5)

        # Default on missing
        monkeypatch.delenv("TEST_FLOAT", raising=False)
        assert get_float_env("TEST_FLOAT", 1.0) == pytest.approx(1.0)

        # Default on invalid
        monkeypatch.setenv("TEST_FLOAT", "invalid_float")
        assert get_float_env("TEST_FLOAT", 1.0) == pytest.approx(1.0)

    @pytest.mark.parametrize(
        "raw_val, expected",
        [
            ("1", True),
            ("true", True),
            ("TRUE", True),
            ("True", True),
            ("yes", True),
            ("YES", True),
            ("y", True),
            ("t", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("FALSE", False),
            ("False", False),
            ("no", False),
            ("NO", False),
            ("n", False),
            ("f", False),
            ("off", False),
        ],
    )
    def test_get_bool_env_truthy_falsy(self, monkeypatch, raw_val, expected):
        monkeypatch.setenv("KORTEXT_TEST_BOOL", raw_val)
        assert get_bool_env("TEST_BOOL", not expected) is expected

    def test_get_bool_env_default_on_missing_or_invalid(self, monkeypatch):
        monkeypatch.delenv("KORTEXT_TEST_BOOL", raising=False)
        monkeypatch.delenv("TEST_BOOL", raising=False)

        assert get_bool_env("TEST_BOOL", True) is True
        assert get_bool_env("TEST_BOOL", False) is False

        monkeypatch.setenv("TEST_BOOL", "not_a_boolean")
        assert get_bool_env("TEST_BOOL", True) is True
        assert get_bool_env("TEST_BOOL", False) is False


class TestSpecializedGetters:
    def test_get_falkordb_socket_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KORTEXT_FALKORDB_SOCKET", raising=False)
        monkeypatch.delenv("FALKORDB_SOCKET", raising=False)

        fake_home = tmp_path / "kortext_home"
        monkeypatch.setenv("KORTEXT_HOME", str(fake_home))

        expected = fake_home / "run" / "falkordb.sock"
        assert get_falkordb_socket() == expected

    def test_get_falkordb_socket_override(self, monkeypatch, tmp_path):
        custom_sock = tmp_path / "custom.sock"
        monkeypatch.setenv("FALKORDB_SOCKET", str(custom_sock))
        assert get_falkordb_socket() == custom_sock

        monkeypatch.setenv("KORTEXT_FALKORDB_SOCKET", str(tmp_path / "kortext.sock"))
        assert get_falkordb_socket() == tmp_path / "kortext.sock"

    def test_get_falkordb_socket_none_when_disabled(self, monkeypatch):
        monkeypatch.setenv("FALKORDB_SOCKET", "none")
        assert get_falkordb_socket() is None

        monkeypatch.setenv("FALKORDB_SOCKET", "disabled")
        assert get_falkordb_socket() is None

    def test_get_falkordb_host_default_and_override(self, monkeypatch):
        monkeypatch.delenv("KORTEXT_FALKORDB_HOST", raising=False)
        monkeypatch.delenv("FALKORDB_HOST", raising=False)
        assert get_falkordb_host() == "127.0.0.1"

        monkeypatch.setenv("FALKORDB_HOST", "192.168.1.10")
        assert get_falkordb_host() == "192.168.1.10"

        monkeypatch.setenv("KORTEXT_FALKORDB_HOST", "10.0.0.1")
        assert get_falkordb_host() == "10.0.0.1"

    def test_get_falkordb_port_default_and_override(self, monkeypatch):
        monkeypatch.delenv("KORTEXT_FALKORDB_PORT", raising=False)
        monkeypatch.delenv("FALKORDB_PORT", raising=False)
        assert get_falkordb_port() == 6379

        monkeypatch.setenv("FALKORDB_PORT", "6380")
        assert get_falkordb_port() == 6380

        monkeypatch.setenv("KORTEXT_FALKORDB_PORT", "6381")
        assert get_falkordb_port() == 6381

    def test_get_falkordb_database_default_and_override(self, monkeypatch):
        monkeypatch.delenv("KORTEXT_FALKORDB_DATABASE", raising=False)
        monkeypatch.delenv("FALKORDB_DATABASE", raising=False)
        monkeypatch.delenv("KORTEXT_DATABASE", raising=False)
        monkeypatch.delenv("DATABASE", raising=False)

        assert get_falkordb_database() == "kortext"

        monkeypatch.setenv("DATABASE", "raw_db")
        assert get_falkordb_database() == "raw_db"

        monkeypatch.setenv("FALKORDB_DATABASE", "falkor_db")
        assert get_falkordb_database() == "falkor_db"

        monkeypatch.setenv("KORTEXT_DATABASE", "custom_kortext_db")
        assert get_falkordb_database() == "custom_kortext_db"

    def test_get_falkordb_symbols_graph_default_and_override(self, monkeypatch):
        monkeypatch.delenv("KORTEXT_FALKORDB_SYMBOLS_GRAPH", raising=False)
        monkeypatch.delenv("FALKORDB_SYMBOLS_GRAPH", raising=False)
        monkeypatch.delenv("KORTEXT_SYMBOLS_GRAPH", raising=False)
        monkeypatch.delenv("SYMBOLS_GRAPH", raising=False)

        assert get_falkordb_symbols_graph() == "kortext_symbols"

        monkeypatch.setenv("SYMBOLS_GRAPH", "raw_symbols")
        assert get_falkordb_symbols_graph() == "raw_symbols"

        monkeypatch.setenv("FALKORDB_SYMBOLS_GRAPH", "falkor_symbols")
        assert get_falkordb_symbols_graph() == "falkor_symbols"

        monkeypatch.setenv("KORTEXT_SYMBOLS_GRAPH", "custom_kortext_symbols")
        assert get_falkordb_symbols_graph() == "custom_kortext_symbols"

    def test_get_qdrant_url_default_and_override(self, monkeypatch):
        monkeypatch.delenv("KORTEXT_QDRANT_URL", raising=False)
        monkeypatch.delenv("QDRANT_URL", raising=False)
        assert get_qdrant_url() == "http://localhost:6333"

        monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:6334")
        assert get_qdrant_url() == "http://127.0.0.1:6334"

        monkeypatch.setenv("KORTEXT_QDRANT_URL", "http://127.0.0.1:6335")
        assert get_qdrant_url() == "http://127.0.0.1:6335"

    def test_get_qdrant_collection_default_and_override(self, monkeypatch):
        monkeypatch.delenv("KORTEXT_QDRANT_COLLECTION", raising=False)
        monkeypatch.delenv("QDRANT_COLLECTION", raising=False)
        monkeypatch.delenv("KORTEXT_COLLECTION", raising=False)
        monkeypatch.delenv("COLLECTION", raising=False)

        assert get_qdrant_collection() == "kortext_codebase"

        monkeypatch.setenv("COLLECTION", "raw_col")
        assert get_qdrant_collection() == "raw_col"

        monkeypatch.setenv("QDRANT_COLLECTION", "qdrant_col")
        assert get_qdrant_collection() == "qdrant_col"

        monkeypatch.setenv("KORTEXT_QDRANT_COLLECTION", "custom_kortext_codebase")
        assert get_qdrant_collection() == "custom_kortext_codebase"

    def test_get_embed_base_url_default_and_override(self, monkeypatch):
        monkeypatch.delenv("KORTEXT_EMBED_BASE_URL", raising=False)
        monkeypatch.delenv("EMBED_BASE_URL", raising=False)
        assert get_embed_base_url() is None

        monkeypatch.setenv("EMBED_BASE_URL", "http://localhost:9000/v1")
        assert get_embed_base_url() == "http://localhost:9000/v1"

        monkeypatch.setenv("KORTEXT_EMBED_BASE_URL", "http://localhost:9001/v1")
        assert get_embed_base_url() == "http://localhost:9001/v1"

    def test_get_embed_model_name_default_and_override(self, monkeypatch):
        monkeypatch.delenv("KORTEXT_EMBED_MODEL_NAME", raising=False)
        monkeypatch.delenv("EMBED_MODEL_NAME", raising=False)
        monkeypatch.delenv("EMBED_BASE_URL", raising=False)
        assert get_embed_model_name() == "BAAI/bge-small-en-v1.5"

        monkeypatch.setenv("EMBED_BASE_URL", "http://localhost:8000/v1")
        assert get_embed_model_name() == "mlx-community--Qwen3-Embedding-8B-4bit-DWQ"

        monkeypatch.setenv("EMBED_MODEL_NAME", "custom-embed")
        assert get_embed_model_name() == "custom-embed"

        monkeypatch.setenv("KORTEXT_EMBED_MODEL_NAME", "custom-kortext-embed")
        assert get_embed_model_name() == "custom-kortext-embed"
