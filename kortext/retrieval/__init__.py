"""Retrieval package: hybrid RRF over Qdrant and FalkorDB."""
from typing import Any

__all__ = [
    "RetrievalEngine",
    "discover_vector_collections",
    "federated_query",
    "FederatedRetrievalResult",
    "apply_knapsack_mmr",
    "resolve_workspace_root_for_collection",
    "resolve_symbol_graph_for_collection",
]

_ENGINE_ATTRS = {
    "RetrievalEngine",
    "federated_query",
    "FederatedRetrievalResult",
    "apply_knapsack_mmr",
    "resolve_workspace_root_for_collection",
    "resolve_symbol_graph_for_collection",
}


def __getattr__(name: str) -> Any:
    if name == "discover_vector_collections":
        from .discovery import discover_vector_collections
        return discover_vector_collections
    if name in _ENGINE_ATTRS:
        from . import engine as _engine
        return getattr(_engine, name)
    raise AttributeError(f"module 'kortext.retrieval' has no attribute {name!r}")

