"""
Centralized Qdrant Client Factory and Custody Management for Kortext.

Enforces Sole Custodian Invariant:
- Production runtimes and subagents pool singleton HTTP/gRPC or Embedded Qdrant clients.
- Supports deterministic close/aclose lifecycle management via FastMCP lifespan.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx
from qdrant_client import AsyncQdrantClient, QdrantClient

from kortext._env import get_env, get_qdrant_url, is_qdrant_embedded_mode
from kortext._home import get_kortext_home

logger = logging.getLogger("kortext.qdrant")

__all__ = [
    "EmbeddedQdrantLockError",
    "is_embedded_qdrant_locked",
    "get_qdrant_client",
    "get_sync_qdrant_client",
    "get_async_qdrant_client",
    "close_qdrant_clients",
    "aclose_qdrant_clients",
    "is_qdrant_online",
]

_SYNC_CLIENT_POOL: Dict[Tuple[Any, Optional[str], Optional[str], float], QdrantClient] = {}
_ASYNC_CLIENT_POOL: Dict[Tuple[Any, Optional[str], Optional[str], float], AsyncQdrantClient] = {}
_POOL_LOCK = threading.RLock()


class EmbeddedQdrantLockError(RuntimeError):
    """Raised when another OS process holds the exclusive Embedded Qdrant portalocker lock."""


def is_embedded_qdrant_locked(storage_dir: Optional[Path | str] = None) -> bool:
    """Cross-platform non-blocking probe checking whether ~/.kortext/qdrant/.lock is held by another OS process."""
    qdir = Path(storage_dir) if storage_dir is not None else (get_kortext_home() / "qdrant")
    lock_file = qdir / ".lock"
    if not lock_file.is_file():
        return False
    # If this process already holds the client in _SYNC_CLIENT_POOL or _ASYNC_CLIENT_POOL, it's our own lock
    path_str = str(qdir)
    for (_, _, pooled_path, _) in list(_SYNC_CLIENT_POOL.keys()) + list(_ASYNC_CLIENT_POOL.keys()):
        if pooled_path == path_str:
            return False

    # 1. Cross-platform portalocker check (bundled with qdrant-client)
    try:
        import portalocker

        with open(lock_file, "r+b") as lf:
            try:
                portalocker.lock(lf, portalocker.LOCK_EX | portalocker.LOCK_NB)
                portalocker.unlock(lf)
                return False
            except portalocker.exceptions.BaseLockException:
                return True
    except ImportError:
        pass
    except (BlockingIOError, PermissionError, OSError):
        return True
    except Exception:
        pass

    # 2. Windows msvcrt fallback
    if sys.platform == "win32" or os.name == "nt":
        try:
            import msvcrt

            fd = os.open(str(lock_file), os.O_RDWR)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                return False
            except (BlockingIOError, PermissionError, OSError):
                return True
            finally:
                os.close(fd)
        except Exception:
            return False

    # 3. POSIX fcntl fallback
    try:
        import fcntl

        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except (BlockingIOError, PermissionError, OSError):
            return True
        finally:
            os.close(fd)
    except Exception:
        return False


def _should_use_embedded_default() -> bool:
    """Returns True when environment explicitly configures embedded/local Qdrant mode."""
    mode = (get_env("KORTEXT_QDRANT_MODE") or get_env("QDRANT_MODE") or "").strip().lower()
    if mode in ("embedded", "local", "in-process"):
        return True
    raw_url = (get_env("QDRANT_URL") or "").strip().lower()
    return raw_url in ("embedded", "local", "none") or raw_url.startswith("file:")


def get_qdrant_client(
    url: Optional[str] = None,
    storage_path: Optional[Path | str] = None,
    timeout: float = 5.0,
) -> QdrantClient:
    """
    Centralize Qdrant client lifecycle and enforce Sole Custodian Invariant with singleton pooling.

    Resolution:
    1. If `url` is provided, connect via pooled QdrantClient(url=url, timeout=timeout).
    2. If `storage_path` is explicitly provided (or embedded mode is explicitly configured),
       instantiate/reuse isolated Embedded Qdrant.
    3. Otherwise, use get_qdrant_url() from kortext._env and connect via pooled HTTP client.
    """
    with _POOL_LOCK:
        if url:
            key = (QdrantClient, url, None, float(timeout))
            if key not in _SYNC_CLIENT_POOL:
                logger.info("Connecting to remote Qdrant at %s", url)
                _SYNC_CLIENT_POOL[key] = QdrantClient(url=url, timeout=timeout)
            return _SYNC_CLIENT_POOL[key]

        if storage_path is not None or _should_use_embedded_default():
            p = Path(storage_path) if storage_path is not None else (get_kortext_home() / "qdrant")
            p.mkdir(parents=True, exist_ok=True)
            path_str = str(p)
            key = (QdrantClient, None, path_str, float(timeout))
            if key not in _SYNC_CLIENT_POOL:
                if is_embedded_qdrant_locked(p):
                    raise EmbeddedQdrantLockError(
                        f"Embedded Qdrant storage at {p} is locked by another process (Sole Custodian: kortext.server)."
                    )
                logger.info("Initializing isolated Embedded Qdrant at %s", p)
                _SYNC_CLIENT_POOL[key] = QdrantClient(path=path_str)
            return _SYNC_CLIENT_POOL[key]

        resolved_url = get_qdrant_url()
        key = (QdrantClient, resolved_url, None, float(timeout))
        if key not in _SYNC_CLIENT_POOL:
            logger.info("Connecting to Qdrant at %s", resolved_url)
            _SYNC_CLIENT_POOL[key] = QdrantClient(url=resolved_url, timeout=timeout)
        return _SYNC_CLIENT_POOL[key]


get_sync_qdrant_client = get_qdrant_client


def get_async_qdrant_client(
    url: Optional[str] = None,
    storage_path: Optional[Path | str] = None,
    timeout: float = 5.0,
) -> AsyncQdrantClient:
    """Instantiate or reuse a pooled AsyncQdrantClient."""
    with _POOL_LOCK:
        if url:
            key = (AsyncQdrantClient, url, None, float(timeout))
            if key not in _ASYNC_CLIENT_POOL:
                logger.info("Connecting async Qdrant client at %s", url)
                _ASYNC_CLIENT_POOL[key] = AsyncQdrantClient(url=url, timeout=timeout)
            return _ASYNC_CLIENT_POOL[key]

        if storage_path is not None or _should_use_embedded_default():
            p = Path(storage_path) if storage_path is not None else (get_kortext_home() / "qdrant")
            p.mkdir(parents=True, exist_ok=True)
            path_str = str(p)
            key = (AsyncQdrantClient, None, path_str, float(timeout))
            if key not in _ASYNC_CLIENT_POOL:
                if is_embedded_qdrant_locked(p):
                    raise EmbeddedQdrantLockError(
                        f"Embedded Qdrant storage at {p} is locked by another process (Sole Custodian: kortext.server)."
                    )
                logger.info("Initializing async Embedded Qdrant at %s", p)
                _ASYNC_CLIENT_POOL[key] = AsyncQdrantClient(path=path_str)
            return _ASYNC_CLIENT_POOL[key]

        target_url = get_qdrant_url()
        key = (AsyncQdrantClient, target_url, None, float(timeout))
        if key not in _ASYNC_CLIENT_POOL:
            logger.info("Connecting async Qdrant client at %s", target_url)
            _ASYNC_CLIENT_POOL[key] = AsyncQdrantClient(url=target_url, timeout=timeout)
        return _ASYNC_CLIENT_POOL[key]


def close_qdrant_clients() -> None:
    """Synchronously closes all pooled QdrantClient instances."""
    with _POOL_LOCK:
        clients = list(_SYNC_CLIENT_POOL.values())
        _SYNC_CLIENT_POOL.clear()
    for client in clients:
        try:
            client.close()
        except Exception as exc:
            logger.debug("QdrantClient close ignored: %s", exc)


async def aclose_qdrant_clients() -> None:
    """Asynchronously closes all pooled AsyncQdrantClient and sync QdrantClient instances."""
    with _POOL_LOCK:
        async_clients = list(_ASYNC_CLIENT_POOL.values())
        _ASYNC_CLIENT_POOL.clear()
    for aclient in async_clients:
        try:
            await aclient.close()
        except Exception as exc:
            logger.debug("AsyncQdrantClient close ignored: %s", exc)
    close_qdrant_clients()


def is_qdrant_online(
    url: Optional[str] = None,
    timeout: float = 1.0,
) -> bool:
    """
    Quick boolean health check for Qdrant service using HTTP, socket probe, or embedded check.
    """
    if url is None and is_qdrant_embedded_mode(probe_local=True):
        qdir = get_kortext_home() / "qdrant"
        if (qdir / "meta.json").is_file() or qdir.is_dir():
            return True

    target_url = (url or get_qdrant_url()).rstrip("/")
    if not target_url.startswith(("http://", "https://")):
        target_url = f"http://{target_url}"

    probe_url = target_url.replace("://localhost", "://127.0.0.1")

    # 1. HTTP Probe
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{probe_url}/healthz")
            if resp.status_code < 500:
                return True
            resp = client.get(f"{probe_url}/collections")
            return resp.status_code < 500
    except Exception:
        logger.debug("Suppressed non-fatal exception")

    # 2. TCP Socket Probe fallback
    try:
        parsed = urlparse(target_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 6333
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False
