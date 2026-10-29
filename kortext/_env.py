"""
Centralized Environment Variable Resolver for Kortext.

Provides deterministic resolution with Kortext -> Raw fallback ordering,
type-safe parsing, and specialized getters for substrate services.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Optional, Tuple

from kortext._home import get_kortext_home

__all__ = [
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
    "get_configured_qdrant_url",
    "clear_qdrant_probe_cache",
    "is_qdrant_embedded_mode",
    "get_qdrant_collection",
    "get_embed_base_url",
    "get_embed_model_name",
    "get_embed_dimension",
]

_QDRANT_PROBE_CACHE: Optional[Tuple[float, Optional[str]]] = None
_QDRANT_PROBE_TTL_SEC: float = 5.0


def clear_qdrant_probe_cache() -> None:
    """Clears cached local Qdrant TCP probe result."""
    global _QDRANT_PROBE_CACHE
    _QDRANT_PROBE_CACHE = None


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """
    Resolve environment variable with Kortext -> Raw fallback.

    Resolution order:
    a) If `name` already starts with 'KORTEXT_', check directly.
    b) Otherwise, check `KORTEXT_{name}` first.
    c) If not found or empty, fall back to `{name}`.
    d) Return `default` if none found.
    """
    if name.startswith("KORTEXT_"):
        val = os.getenv(name)
        if val is not None and val != "":
            return val
        return default

    kortext_val = os.getenv(f"KORTEXT_{name}")
    if kortext_val is not None and kortext_val != "":
        return kortext_val

    raw_val = os.getenv(name)
    if raw_val is not None and raw_val != "":
        return raw_val

    return default


def get_int_env(name: str, default: int) -> int:
    """Resolve an integer environment variable with fallback to default."""
    val = get_env(name)
    if val is None:
        return default
    try:
        return int(val.strip())
    except (ValueError, TypeError):
        return default


def get_float_env(name: str, default: float) -> float:
    """Resolve a float environment variable with fallback to default."""
    val = get_env(name)
    if val is None:
        return default
    try:
        return float(val.strip())
    except (ValueError, TypeError):
        return default


def get_bool_env(name: str, default: bool) -> bool:
    """
    Resolve a boolean environment variable (case-insensitive true/false, 1/0, yes/no).
    Returns default if missing or unrecognized.
    """
    val = get_env(name)
    if val is None:
        return default
    cleaned = val.strip().lower()
    if cleaned in ("1", "true", "yes", "t", "y", "on"):
        return True
    if cleaned in ("0", "false", "no", "f", "n", "off"):
        return False
    return default


def get_falkordb_socket() -> Optional[Path]:
    """
    Resolve FalkorDB Unix domain socket path.
    Uses get_kortext_home() / 'run' / 'falkordb.sock' as default.
    """
    val = get_env("FALKORDB_SOCKET")
    if val:
        cleaned = val.strip()
        if cleaned.lower() in ("none", "disabled", "false", "0"):
            return None
        return Path(os.path.expanduser(cleaned))

    return get_kortext_home() / "run" / "falkordb.sock"


def get_falkordb_host() -> str:
    """Resolve FalkorDB host address (default '127.0.0.1')."""
    return get_env("FALKORDB_HOST", "127.0.0.1") or "127.0.0.1"


def get_falkordb_port() -> int:
    """Resolve FalkorDB TCP port (default 6379)."""
    return get_int_env("FALKORDB_PORT", 6379)


def get_falkordb_database() -> str:
    """Resolve FalkorDB database name (default 'kortext')."""
    val = (
        get_env("KORTEXT_DATABASE")
        or get_env("FALKORDB_DATABASE")
        or get_env("DATABASE")
    )
    if val:
        return val
    return "kortext"


def get_falkordb_symbols_graph() -> str:
    """Resolve FalkorDB symbols graph name (default 'kortext_symbols')."""
    val = (
        get_env("KORTEXT_SYMBOLS_GRAPH")
        or get_env("FALKORDB_SYMBOLS_GRAPH")
        or get_env("SYMBOLS_GRAPH")
        or get_env("SYMBOLS_GRAPH_NAME")
    )
    if val:
        return val
    return "kortext_symbols"


def get_qdrant_url() -> str:
    """Resolve Qdrant HTTP URL (default 'http://localhost:6333')."""
    return get_env("QDRANT_URL", "http://localhost:6333") or "http://localhost:6333"


def get_configured_qdrant_url(probe_local: bool = True, timeout: float = 0.05) -> Optional[str]:
    """
    Returns an HTTP Qdrant URL only when explicitly configured via KORTEXT_QDRANT_MODE/QDRANT_URL
    or when a local Qdrant daemon is actively listening on 127.0.0.1:6333 (cached with TTL).
    Returns None in Zero-Docker Embedded Qdrant mode (~/.kortext/qdrant).
    """
    global _QDRANT_PROBE_CACHE

    mode = (get_env("KORTEXT_QDRANT_MODE") or get_env("QDRANT_MODE") or "").strip().lower()
    if mode in ("embedded", "local", "in-process", "none", "disabled"):
        return None

    explicit = get_env("QDRANT_URL")
    if explicit:
        cleaned = explicit.strip()
        if cleaned.lower() in ("none", "embedded", "local", "disabled", "false", "0") or cleaned.startswith("file:"):
            return None
        return cleaned.replace("://localhost", "://127.0.0.1")

    if mode == "remote":
        return "http://127.0.0.1:6333"

    if probe_local:
        use_cache = not bool(os.getenv("PYTEST_CURRENT_TEST"))
        now = time.monotonic()
        if use_cache and _QDRANT_PROBE_CACHE is not None:
            cached_ts, cached_val = _QDRANT_PROBE_CACHE
            if now - cached_ts < _QDRANT_PROBE_TTL_SEC:
                return cached_val
        try:
            with socket.create_connection(("127.0.0.1", 6333), timeout=timeout):
                res: Optional[str] = "http://127.0.0.1:6333"
        except Exception:
            res = None
        if use_cache:
            _QDRANT_PROBE_CACHE = (now, res)
        return res
    return None


def is_qdrant_embedded_mode(probe_local: bool = True) -> bool:
    """Returns True when Qdrant should operate in local embedded mode (~/.kortext/qdrant)."""
    return get_configured_qdrant_url(probe_local=probe_local) is None


def get_qdrant_collection() -> str:
    """Resolve Qdrant collection name (default 'kortext_codebase')."""
    val = (
        get_env("QDRANT_COLLECTION")
        or get_env("COLLECTION")
        or get_env("COLLECTION_NAME")
    )
    if val:
        return val
    return "kortext_codebase"


def get_embed_base_url() -> Optional[str]:
    """
    Resolve embedding server base URL.
    Returns None by default to enable zero-config local FastEmbed ONNX,
    unless EMBED_BASE_URL is explicitly set to a valid URL.
    """
    val = get_env("EMBED_BASE_URL")
    if not val:
        return None
    cleaned = val.strip()
    if cleaned.lower() in ("none", "disabled", "false", "0", "fastembed", "local"):
        return None
    return cleaned


def get_embed_model_name() -> str:
    """
    Resolve embedding model name.
    If EMBED_BASE_URL is configured, defaults to remote model ('mlx-community--Qwen3-Embedding-8B-4bit-DWQ').
    Otherwise defaults to local FastEmbed model ('BAAI/bge-small-en-v1.5').
    """
    val = get_env("EMBED_MODEL_NAME")
    if val:
        return val
    if get_embed_base_url() is not None:
        return "mlx-community--Qwen3-Embedding-8B-4bit-DWQ"
    return "BAAI/bge-small-en-v1.5"


def get_embed_dimension() -> int:
    """
    Resolve embedding dimension.
    If EMBED_DIMENSION is set, returns that.
    If EMBED_BASE_URL is configured, defaults to 4096.
    Otherwise defaults to 384 for local FastEmbed.
    """
    dim = get_int_env("EMBED_DIMENSION", 0)
    if dim > 0:
        return dim
    if get_embed_base_url() is not None:
        return 4096
    return 384

