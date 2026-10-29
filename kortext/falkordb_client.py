"""
Canonical FalkorDB Client & Token-Safe Cypher Query Engine for Kortext.

Provides:
1. Thread-safe, connection-pooled Redis connection lifecycle and reconnect logic.
2. Token-safe Cypher parameter interpolation using word boundaries
   and identifier validation (preventing Cypher injection and prefix collisions).
3. Schema index verification for Symbol, File, Document, Tag, and Concept nodes.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from falkordb.helpers import stringify_param_value
except Exception:
    def stringify_param_value(val: Any) -> str:
        if val is None:
            return "null"
        return json.dumps(val)

try:
    import redis
except Exception:
    redis = None  # type: ignore

logger = logging.getLogger(__name__)

from kortext._home import get_kortext_home

_CYPHER_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FalkorDBQueryError(RuntimeError):
    """Typed exception raised when a FalkorDB Cypher query fails and raise_on_error is enabled."""


def _validate_cypher_identifier(key: Any) -> str:
    """Validates a Cypher parameter or map key against strict identifier syntax."""
    if not isinstance(key, str) or not _CYPHER_IDENT_RE.fullmatch(key):
        raise ValueError(
            f"Invalid Cypher identifier/parameter key {key!r}: must match ^[A-Za-z_][A-Za-z0-9_]*$"
        )
    return key


def get_default_socket() -> Path:
    env_sock = os.getenv("FALKORDB_SOCKET", "").strip()
    if env_sock:
        env_path = Path(env_sock).expanduser()
        if env_path.is_socket():
            return env_path
    candidate = get_kortext_home() / "run" / "falkordb.sock"
    if candidate.is_socket() or os.getenv("KORTEXT_HOME") or os.getenv("PLUGIN_DATA"):
        return candidate
    for home_cand in (Path.home(), Path(__file__).resolve().parents[2]):
        fallback = home_cand / ".kortext" / "run" / "falkordb.sock"
        if fallback.is_socket():
            return fallback
    return candidate


DEFAULT_SOCKET = get_default_socket()

FALKORDB_HOST = os.getenv("FALKORDB_HOST", "127.0.0.1")
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
FALKORDB_PASS = os.getenv("FALKORDB_PASSWORD", "")


def build_cypher_params_header(params: Optional[Dict[str, Any]]) -> str:
    """Constructs native FalkorDB parameterized query header (CYPHER `k`=v ...)."""
    if not params:
        return ""
    parts = []
    for k, v in params.items():
        safe_k = _validate_cypher_identifier(k)
        if isinstance(v, dict):
            for map_k in v.keys():
                _validate_cypher_identifier(map_k)
        parts.append(f"`{safe_k}`={stringify_param_value(v)}")
    return "CYPHER " + " ".join(parts) + " "


_format_params_header = build_cypher_params_header


def _format_cypher_val(v: Any) -> str:
    """Recursively formats a Python value into Cypher literal format."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        escaped_v = v.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
        return f"'{escaped_v}'"
    if isinstance(v, dict):
        elements = [f"{_validate_cypher_identifier(k)}: {_format_cypher_val(val)}" for k, val in v.items()]
        return "{" + ", ".join(elements) + "}"
    if isinstance(v, (list, tuple, set)):
        elements = [_format_cypher_val(item) for item in v]
        return "[" + ", ".join(elements) + "]"
    escaped = str(v).replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
    return f"'{escaped}'"


_CYPHER_TOKEN_PATTERN = re.compile(
    r"(?P<quote>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`(?:[^`\\]|\\.)*`)|"
    r"(?P<comment>//[^\r\n]*|/\*[\s\S]*?\*/)|"
    r"\$(?P<param>[a-zA-Z_][a-zA-Z0-9_]*)"
)


def format_cypher_query(cypher: str, params: Optional[Dict[str, Any]] = None) -> str:
    """
    Interpolates parameters into a Cypher query string with token-safe single-pass substitution,
    preventing double-substitution, key collisions, and replacement inside string literals or comments.
    """
    if not params:
        return cypher

    for k in params.keys():
        _validate_cypher_identifier(k)

    def _replace_token(match: re.Match[str]) -> str:
        param_name = match.group("param")
        if param_name is not None and param_name in params:
            return _format_cypher_val(params[param_name])
        return match.group(0)

    return _CYPHER_TOKEN_PATTERN.sub(_replace_token, cypher)


_USE_DEFAULT_SOCKET = object()


class FalkorDBClient:
    """Canonical FalkorDB Redis client with thread-safe connection pooling and indexing."""

    _is_live_falkor_client = True

    def __init__(
        self,
        host: str = FALKORDB_HOST,
        port: int = FALKORDB_PORT,
        password: Optional[str] = FALKORDB_PASS,
        socket_path: Any = _USE_DEFAULT_SOCKET,
        max_connections: int = 32,
    ):
        self.host = host
        self.port = port
        self.password = password or None
        self.socket_path = get_default_socket() if socket_path is _USE_DEFAULT_SOCKET else socket_path
        self.max_connections = max_connections
        self._r: Optional[redis.Redis] = None
        self._lock = threading.RLock()
        self._verified_indexes: Set[Tuple[str, str, str]] = set()
        self._resolved_graphs: Dict[str, str] = {}

    def get_redis(self) -> redis.Redis:
        """Returns active thread-safe Redis client connection, preferring Unix domain socket."""
        with self._lock:
            if self._r is not None:
                return self._r

            # redis-py 8 retries a refused connect for ~4 s by default; hooks have a 5 s budget.
            from redis.backoff import ExponentialBackoff
            from redis.retry import Retry
            fast_fail = {"socket_connect_timeout": 0.5, "retry": Retry(ExponentialBackoff(cap=0.1, base=0.02), 2)}

            # Attempt 1: Unix Domain Socket (Ultra-low latency IPC)
            if self.socket_path and Path(self.socket_path).is_socket():
                try:
                    uds_kwargs: Dict[str, Any] = {
                        "unix_socket_path": str(self.socket_path),
                        "password": self.password,
                        "decode_responses": True,
                        "socket_timeout": 5.0,
                        **fast_fail,
                    }
                    uds_kwargs["max_connections"] = self.max_connections
                    r = redis.Redis(**uds_kwargs)
                    r.ping()
                    self._r = r
                    return self._r
                except Exception as exc:
                    logger.debug("UDS connection fallback to TCP: %s", exc)

            # Attempt 2: TCP Loopback / Remote Endpoint with connection pool
            tcp_kwargs: Dict[str, Any] = {
                "host": self.host,
                "port": self.port,
                "password": self.password,
                "decode_responses": True,
                "socket_timeout": 5.0,
                **fast_fail,
            }
            tcp_kwargs["max_connections"] = self.max_connections
            self._r = redis.Redis(**tcp_kwargs)
            return self._r

    def _reset_redis(self) -> redis.Redis:
        """Thread-safely resets and reconnects the underlying Redis client."""
        with self._lock:
            self._r = None
            return self.get_redis()

    def resolve_graph_name(self, graph_name: str) -> str:
        """Returns the canonical graph name."""
        return graph_name

    def ensure_indexes(self, graph_name: str, indexes: List[Tuple[str, str]]):
        """
        Ensures B-tree schema indexes on specified labels and properties.
        indexes: list of (node_label, property_name)
        """
        resolved_graph = self.resolve_graph_name(graph_name)
        if redis is None:
            return []
        client = self.get_redis()
        for label, prop in indexes:
            if not _CYPHER_IDENT_RE.match(str(label)) or not _CYPHER_IDENT_RE.match(str(prop)):
                logger.warning("Skipped invalid index identifier: %r.%r", label, prop)
                continue
            key = (resolved_graph, label, prop)
            with self._lock:
                if key in self._verified_indexes:
                    continue
            try:
                client.execute_command("GRAPH.QUERY", resolved_graph, f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception as exc:
                logger.debug("Index creation note on %s(%s.%s): %s", resolved_graph, label, prop, exc)
            with self._lock:
                self._verified_indexes.add(key)

    def query(
        self,
        graph_name: str,
        cypher: str,
        params: Optional[Dict[str, Any]] = None,
        attempts: int = 2,
        read_only: bool = False,
        raise_on_error: bool = False,
    ) -> List[List[Any]]:
        """
        Executes a Cypher query on the specified graph with native parameter binding
        and thread-safe reconnect. Raises FalkorDBQueryError when raise_on_error=True.
        """
        resolved_graph = self.resolve_graph_name(graph_name)
        if params and not cypher.lstrip().startswith("CYPHER "):
            query_str = build_cypher_params_header(params) + cypher
        else:
            query_str = cypher

        if redis is None:
            return []
        client = self.get_redis()
        cmd = "GRAPH.RO_QUERY" if read_only else "GRAPH.QUERY"

        for attempt in range(attempts):
            try:
                res = client.execute_command(cmd, resolved_graph, query_str)
                return res if res and isinstance(res, list) else []
            except (redis.ConnectionError, redis.TimeoutError) as conn_err:
                client = self._reset_redis()
                if attempt == attempts - 1:
                    raise
            except Exception as e:
                logger.error("[FalkorDBClient] Error executing query on '%s': %s", graph_name, e)
                if raise_on_error:
                    raise FalkorDBQueryError(f"FalkorDB query failed on '{graph_name}': {e}") from e
                return []
        return []


_SINGLETON_CLIENT: Optional[FalkorDBClient] = None
_SINGLETON_LOCK = threading.Lock()


def get_shared_falkordb_client(
    host: str = FALKORDB_HOST,
    port: int = FALKORDB_PORT,
    password: Optional[str] = FALKORDB_PASS,
) -> FalkorDBClient:
    """Returns thread-safe singleton FalkorDBClient instance."""
    global _SINGLETON_CLIENT
    if _SINGLETON_CLIENT is None:
        with _SINGLETON_LOCK:
            if _SINGLETON_CLIENT is None:
                _SINGLETON_CLIENT = FalkorDBClient(host=host, port=port, password=password)
    return _SINGLETON_CLIENT
