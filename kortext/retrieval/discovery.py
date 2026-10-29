"""
Unified discovery for Qdrant vector collections.
Supports both remote HTTP Qdrant and Zero-Docker native embedded Qdrant (read-only SQLite inspection).
Invariant 3 compliance: Secondary processes NEVER instantiate QdrantClient(path=...).
"""

import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

import httpx

from kortext._home import get_kortext_home

logger = logging.getLogger("kortext.retrieval.discovery")


def discover_vector_collections(qdrant_url: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Discover all vector collections, their point counts, statuses, and vector configurations.

    Args:
        qdrant_url: Optional URL to remote Qdrant instance. If omitted, checks os.getenv("QDRANT_URL").
                   If set and non-empty, queries Qdrant via HTTP.
                   If unset or empty, operates in Zero-Docker / native embedded mode, inspecting
                   ~/.kortext/qdrant/meta.json and collection storage.sqlite directly (read-only).

    Returns:
        List of collection metadata dictionaries.
    """
    effective_url = (qdrant_url or os.getenv("QDRANT_URL", "")).strip()

    if effective_url:
        target_url = effective_url.replace("://localhost", "://127.0.0.1").rstrip("/")
        try:
            with httpx.Client(timeout=4.0) as client:
                resp = client.get(f"{target_url}/collections")
                if resp.status_code != 200:
                    raise RuntimeError(f"Qdrant /collections returned HTTP {resp.status_code}: {resp.text}")
                data = resp.json().get("result", {}).get("collections", [])
                details = []
                for c in data:
                    name = c.get("name") if isinstance(c, dict) else str(c)
                    points_count = 0
                    vectors_count = 0
                    indexed_vectors = 0
                    status_val = "green"
                    vectors_config = None
                    try:
                        info_resp = client.get(f"{target_url}/collections/{name}")
                        if info_resp.status_code == 200:
                            res = info_resp.json().get("result", {})
                            points_count = res.get("points_count", 0)
                            vectors_count = res.get("vectors_count", points_count)
                            indexed_vectors = res.get("indexed_vectors_count", points_count)
                            status_val = res.get("status", "green")
                            vectors_config = res.get("config", {}).get("params", {}).get("vectors")
                    except Exception as e:
                        logger.debug(f"Failed to fetch details for collection '{name}': {e}")

                    entry: Dict[str, Any] = {
                        "name": name,
                        "status": status_val,
                        "points_count": points_count,
                        "vectors_count": vectors_count,
                        "indexed_vectors_count": indexed_vectors,
                    }
                    if vectors_config is not None:
                        entry["vectors_config"] = vectors_config
                    details.append(entry)
                return details
        except Exception as e:
            logger.warning(f"Error querying Qdrant HTTP collections at {target_url}: {e}")
            raise

    # Zero-Docker / native embedded mode
    kortext_home = get_kortext_home()
    qpath = kortext_home / "qdrant"
    meta_file = qpath / "meta.json"
    if not meta_file.is_file():
        return []

    try:
        meta_data = json.loads(meta_file.read_text(encoding="utf-8"))
        collections_map = meta_data.get("collections", {})
        details = []
        for name in sorted(collections_map.keys()):
            col_meta = collections_map.get(name, {})
            pt_count = 0
            found_count = False

            possible_sqlite_paths = [
                qpath / "collection" / name / "storage.sqlite",
                qpath / "collections" / name / "storage.sqlite",
                qpath / "storage.sqlite",
            ]
            for sql_file in possible_sqlite_paths:
                if sql_file.is_file():
                    conn = None
                    try:
                        conn = sqlite3.connect(f"file:{sql_file}?mode=ro", uri=True)
                        cur = conn.cursor()
                        cur.execute("SELECT count(*) FROM points")
                        row = cur.fetchone()
                        if row:
                            pt_count = int(row[0])
                            found_count = True
                    except Exception as e:
                        logger.debug(f"Could not read points from {sql_file}: {e}")
                    finally:
                        if conn:
                            try:
                                conn.close()
                            except Exception:
                                logger.debug("Suppressed non-fatal exception")
                    if found_count:
                        break

            if not found_count and isinstance(col_meta, dict):
                if "points_count" in col_meta and col_meta["points_count"] is not None:
                    pt_count = int(col_meta["points_count"])
                elif (
                    "metadata" in col_meta
                    and isinstance(col_meta["metadata"], dict)
                    and col_meta["metadata"].get("points_count") is not None
                ):
                    pt_count = int(col_meta["metadata"]["points_count"])

            entry = {
                "name": name,
                "status": "embedded",
                "points_count": pt_count,
                "vectors_count": pt_count,
                "indexed_vectors_count": pt_count,
                "engine": "RocksDB (Rust)",
            }
            if isinstance(col_meta, dict) and "vectors" in col_meta and col_meta["vectors"] is not None:
                entry["vectors_config"] = col_meta["vectors"]
            details.append(entry)

        return details
    except Exception as e:
        logger.warning(f"Error inspecting embedded Qdrant collections: {e}")
        raise
