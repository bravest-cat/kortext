"""Kortext: Local RAG, 5-tier dual-memory, AST code intelligence, and task DAG knowledge OS for Antigravity CLI."""

import logging
from pathlib import Path
from typing import Any
from kortext._home import KORTEXT_HOME, get_kortext_home
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
    get_embed_dimension,
)

logger = logging.getLogger(__name__)

_QDRANT_ATTRS = {
    "get_qdrant_client",
    "get_sync_qdrant_client",
    "get_async_qdrant_client",
    "is_qdrant_online",
}

_EMBED_ATTRS = {
    "DenseEmbedder",
    "DenseEmbedderProtocol",
    "FastEmbedDenseProvider",
    "RemoteDenseProvider",
    "get_dense_embedder",
    "is_remote_embed_alive",
}


def __getattr__(name: str) -> Any:
    """Lazy-load heavy Qdrant and embedding submodules on first access (PEP 562)
    so lightweight CLI hooks (kortext.hooks.runner) avoid ~100ms import overhead."""
    if name in _QDRANT_ATTRS:
        try:
            import kortext._qdrant as _qdrant
            return getattr(_qdrant, name)
        except ImportError:
            logger.debug("Failed to import kortext._qdrant for %s", name, exc_info=True)
            return None
    if name in _EMBED_ATTRS:
        try:
            import kortext.embeddings as _embeddings
            return getattr(_embeddings, name)
        except ImportError:
            logger.debug("Failed to import kortext.embeddings for %s", name, exc_info=True)
            return None
    try:
        import importlib
        return importlib.import_module(f"kortext.{name}")
    except ModuleNotFoundError as exc:
        mod_dir = Path(__file__).resolve().parent
        is_real_submodule = (mod_dir / f"{name}.py").is_file() or (mod_dir / name / "__init__.py").is_file()
        logger.debug("ImportError loading kortext.%s", name, exc_info=True)
        if is_real_submodule or (exc.name is not None and exc.name != f"kortext.{name}"):
            raise
    except ImportError:
        logger.debug("ImportError loading kortext.%s", name, exc_info=True)
        raise
    raise AttributeError(f"module 'kortext' has no attribute {name!r}")
