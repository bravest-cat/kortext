"""
Query classification, workspace resolution, and symbol graph mapping for Kortext retrieval.

Provides deterministic query intent detection (symbolic vs prose),
safe workspace root discovery for Qdrant collections, and symbol graph mapping.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Dict, Optional

from kortext._env import get_env, get_falkordb_symbols_graph
from kortext._home import get_kortext_home

logger = logging.getLogger("kortext.retrieval.query_parser")

# Programming keywords & constructs indicating code intent
_PROGRAMMING_KEYWORDS_PATTERN = re.compile(
    r"\b("
    r"def|class|function|fn|func|method|"
    r"async|await|return|returns|yield|"
    r"import|from|export|"
    r"var|let|const|"
    r"lambda|"
    r"raise|throw|except|catch|finally|try|"
    r"interface|struct|enum|type|impl|trait|"
    r"public|private|protected|static|"
    r"self|cls"
    r")\b",
    re.IGNORECASE,
)

# CamelCase / PascalCase pattern (e.g. RetrievalEngine, getUserData, HippoRAGPPR)
_CAMEL_CASE_PATTERN = re.compile(
    r"\b[a-z]+[A-Z0-9][a-zA-Z0-9]*\b|\b[A-Z]+[a-z]+[A-Z][a-zA-Z0-9]*\b"
)

# Dotted symbol / module / code path (e.g. engine.py, os.path, kortext.retrieval)
_DOTTED_SYMBOL_PATTERN = re.compile(r"(?<=[a-zA-Z_])\.(?=[a-zA-Z0-9_])")

# Parentheses indicating function calls (e.g. foo(), bar(x), compute())
_CALL_PARENS_PATTERN = re.compile(r"\b[a-zA-Z_]\w*\s*\([^)]*\)")

# Backticks wrapping code or operators
_CODE_SYNTAX_PATTERN = re.compile(r"`[^`]+`|::|->|=>|==|!=|\+=|-=|\*=")

_COLLECTION_WORKSPACE_CACHE: Dict[str, str] = {}


def is_ci_mode() -> bool:
    """Checks whether CI mode is enabled in the environment."""
    val = get_env("CI_MODE") or get_env("KORTEXT_CI_MODE")
    if not val:
        return False
    return val.strip().lower() in ("1", "true", "yes", "on")


def is_symbolic_query(query: str) -> bool:
    """
    Detects if a query is symbolic / code-oriented versus natural language prose / conceptual.

    Returns True if the query contains:
      - snake_case identifiers (containing '_')
      - camelCase or PascalCase identifiers
      - Dotted symbol / module paths (e.g. engine.py, os.getenv)
      - Parentheses indicating function calls (e.g. foo(), compute(x, y))
      - Programming keywords (e.g. def, class, return, import)
      - Backticks or code operators (e.g. `symbol`, ->, ::)
    """
    if not query or not query.strip():
        return False

    q = query.strip()
    if "_" in q:
        return True
    if _CODE_SYNTAX_PATTERN.search(q):
        return True
    if _PROGRAMMING_KEYWORDS_PATTERN.search(q):
        return True
    if _CAMEL_CASE_PATTERN.search(q):
        return True
    if _DOTTED_SYMBOL_PATTERN.search(q):
        return True
    if _CALL_PARENS_PATTERN.search(q):
        return True
    return False


def is_file_in_workspace(file_path: Optional[str], ws_root: str) -> bool:
    """Validates whether a given file path belongs within a specified workspace root."""
    if not file_path:
        return False
    fp_str = str(file_path)
    ws_str = str(ws_root)
    try:
        p_file = Path(fp_str)
        p_ws = Path(ws_str).resolve()
        if p_file.is_absolute():
            resolved = p_file.resolve()
            return resolved == p_ws or p_ws in resolved.parents
        if (p_ws / p_file).exists():
            return True
        resolved = p_file.resolve()
        return resolved == p_ws or p_ws in resolved.parents
    except Exception as exc:
        logger.debug(f"Workspace path resolution check error for '{file_path}': {exc}")
        return False


def _find_git_or_top_parent(path: Path) -> Optional[str]:
    """Finds the root repository directory (.git) or topmost project directory below home."""
    try:
        resolved = path.resolve()
        for parent in [resolved.parent] + list(resolved.parents):
            if (parent / ".git").is_dir():
                return str(parent)
        for parent in reversed(list(resolved.parents)):
            if parent not in (Path.home(), Path("/")):
                return str(parent)
    except Exception as exc:
        logger.debug(f"Error determining git/top parent for '{path}': {exc}")
    return None


async def _resolve_from_qdrant_network(collection_name: str, qdrant_url: str) -> Optional[str]:
    """Attempts to discover workspace root by scrolling Qdrant points over network client."""
    client = None
    try:
        from qdrant_client import AsyncQdrantClient

        client = AsyncQdrantClient(url=qdrant_url.strip())
        pts, _ = await client.scroll(collection_name=collection_name, limit=5, with_payload=True)
        if pts:
            for pt in pts:
                payload = getattr(pt, "payload", {}) or {}
                fp = payload.get("file_path") or payload.get("metadata", {}).get("file_path")
                if fp:
                    parent_root = _find_git_or_top_parent(Path(fp))
                    if parent_root:
                        return parent_root
    except Exception as exc:
        logger.debug(f"Network Qdrant scroll failed for collection '{collection_name}': {exc}")
    finally:
        if client:
            try:
                await client.close()
            except Exception as close_exc:
                logger.debug(f"Client close error: {close_exc}")
    return None


def _resolve_from_embedded_storage(collection_name: str) -> Optional[str]:
    """Inspects embedded Qdrant meta.json or storage.sqlite metadata on disk."""
    try:
        kortext_home = get_kortext_home()
        qdir = kortext_home / "qdrant"

        meta_file = qdir / "meta.json"
        if meta_file.is_file():
            meta_data = json.loads(meta_file.read_text(encoding="utf-8"))
            collections_map = meta_data.get("collections", {})
            if collection_name in collections_map:
                col_meta = collections_map.get(collection_name, {})
                if isinstance(col_meta, dict):
                    ws = (col_meta.get("metadata") or {}).get("workspace_root") or (
                        col_meta.get("metadata") or {}
                    ).get("workspace")
                    if ws and Path(ws).is_dir():
                        return str(Path(ws).resolve())

        col_dir = qdir / "collection" / collection_name
        if not col_dir.is_dir():
            col_dir = qdir / "collections" / collection_name

        if col_dir.is_dir():
            col_meta_file = col_dir / "meta.json"
            if col_meta_file.is_file():
                try:
                    col_data = json.loads(col_meta_file.read_text(encoding="utf-8"))
                    ws = (col_data.get("metadata") or {}).get("workspace_root") or (
                        col_data.get("metadata") or {}
                    ).get("workspace")
                    if ws and Path(ws).is_dir():
                        return str(Path(ws).resolve())
                except Exception as read_exc:
                    logger.debug(f"Failed parsing collection meta.json: {read_exc}")

        sql_file = col_dir / "storage.sqlite"
        if sql_file.is_file():
            try:
                conn = sqlite3.connect(f"file:{sql_file}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute("SELECT point FROM points LIMIT 5")
                rows = cur.fetchall()
                conn.close()
                for (blob,) in rows:
                    if not blob or not isinstance(blob, (bytes, bytearray)):
                        continue
                    try:
                        pt_data = json.loads(blob.decode("utf-8"))
                        payload = pt_data.get("payload") if isinstance(pt_data, dict) else None
                        if payload and isinstance(payload, dict):
                            fp = payload.get("file_path") or payload.get("metadata", {}).get(
                                "file_path"
                            )
                            if fp:
                                root = _find_git_or_top_parent(Path(fp))
                                if root:
                                    return root
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        logger.debug("Suppressed non-fatal exception")

                    path_matches = re.findall(
                        rb"(/[a-zA-Z0-9_\-\.]+/[a-zA-Z0-9_\-\.]+/[a-zA-Z0-9_\-\./]+)", blob
                    )
                    for raw_p in path_matches:
                        try:
                            cand = raw_p.decode("utf-8", errors="ignore")
                            root = _find_git_or_top_parent(Path(cand))
                            if root:
                                return root
                        except Exception:
                            continue
            except Exception as sql_exc:
                logger.debug(f"Embedded sqlite points check error: {sql_exc}")
    except Exception as exc:
        logger.debug(f"Could not resolve workspace root via meta.json for '{collection_name}': {exc}")
    return None


async def resolve_workspace_root_for_collection(collection_name: Optional[str]) -> Optional[str]:
    """
    Dynamically discovers the workspace root associated with a Qdrant collection
    by inspecting payload file paths, registered workspace roots, or common project directories.
    """
    default_ws = get_env("WORKSPACE_ROOTS", str(Path.cwd())) or str(Path.cwd())
    if not collection_name or collection_name.lower() in ("all", "default"):
        return default_ws

    if collection_name in _COLLECTION_WORKSPACE_CACHE:
        return _COLLECTION_WORKSPACE_CACHE[collection_name]

    col_lower = collection_name.lower()

    # 1. Dynamic check for Kortext root
    if col_lower in ("kortext", "kortext_codebase"):
        kortext_root = Path(__file__).resolve().parent.parent.parent
        if (kortext_root / "kortext").is_dir():
            _COLLECTION_WORKSPACE_CACHE[collection_name] = str(kortext_root)
            return str(kortext_root)

    # 2. Dynamic check for registered workspace roots
    env_roots = [
        Path(p.strip()) for p in (get_env("WORKSPACE_ROOTS") or "").split(",") if p.strip()
    ]
    norm_col = col_lower.replace("_", "-")
    search_dirs = env_roots + [Path.home() / norm_col, Path.home() / col_lower, Path.cwd()]
    for cand in search_dirs:
        cand_norm = cand.name.lower().replace("_", "-")
        if cand.is_dir() and (cand_norm in norm_col or norm_col in cand_norm):
            resolved = str(cand.resolve())
            _COLLECTION_WORKSPACE_CACHE[collection_name] = resolved
            return resolved

    # 3. Dynamic lookup via network Qdrant or embedded storage
    qdrant_url = get_env("QDRANT_URL")
    if qdrant_url and qdrant_url.strip() and not qdrant_url.startswith("file:"):
        ws = await _resolve_from_qdrant_network(collection_name, qdrant_url)
        if ws:
            _COLLECTION_WORKSPACE_CACHE[collection_name] = ws
            return ws

    ws_embed = _resolve_from_embedded_storage(collection_name)
    if ws_embed:
        _COLLECTION_WORKSPACE_CACHE[collection_name] = ws_embed
        return ws_embed

    _COLLECTION_WORKSPACE_CACHE[collection_name] = default_ws
    return default_ws


def resolve_symbol_graph_for_collection(collection_name: Optional[str]) -> str:
    """
    Maps a collection name to its corresponding FalkorDB symbol/knowledge graph name.
    e.g. 'Antigravity_Docs' -> 'antigravity_docs', 'kortext_codebase' -> 'kortext_symbols'.
    """
    default_sym = get_falkordb_symbols_graph()
    if not collection_name:
        return default_sym
    col_lower = collection_name.lower()
    if "antigravity" in col_lower:
        return "antigravity_docs"
    if "symbol" in col_lower:
        return collection_name
    if col_lower in ("kortext", "kortext_codebase", "default"):
        return default_sym
    return f"{col_lower}_symbols"
