"""
Kortext Automated Incremental File Watcher.
Monitors project directories using watchdog, debouncing file modification events
and incrementally synchronizing Qdrant vectors and FalkorDB symbol graphs in near real-time.
Leverages ChunkEmbeddingCache to make single-file re-indexing sub-second.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import logging
import os
import signal
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from dotenv import load_dotenv
from qdrant_client import models as qmodels
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from kortext.cache import ChunkEmbeddingCache, IngestCache
from kortext.chunking.ast_splitter import ASTCodeSplitter, LANG_MAP
from kortext.ingest import DEFAULT_EXCLUDE, SUPPORTED_EXT, index_single_file
from kortext.symbols import SymbolGraphManager

load_dotenv()

logger = logging.getLogger("kortext.watcher")

from kortext._env import (
    get_embed_base_url,
    get_embed_model_name,
    get_falkordb_symbols_graph,
    get_qdrant_collection,
    get_qdrant_url,
    is_qdrant_embedded_mode,
)
from kortext._home import KORTEXT_HOME, get_kortext_home
from kortext._qdrant import get_async_qdrant_client, is_embedded_qdrant_locked

COLLECTION = get_qdrant_collection()
DEFAULT_DEBOUNCE = float(os.getenv("KORTEXT_WATCH_DEBOUNCE", "2.0"))
WATCHER_STATUS_FILE = Path(os.getenv("KORTEXT_WATCHER_STATUS", str(KORTEXT_HOME / "watcher_status.json")))

DEFAULT_EXCLUDE_DIRS: List[str] = list(
    dict.fromkeys(
        [
            ".git",
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
            "dist",
            "build",
            ".pytest_cache",
            ".gemini",
            "*.min.js",
            "*.pyc",
            "*.lock",
            "*.map",
        ]
        + [x for x in DEFAULT_EXCLUDE if x]
    )
)
DEFAULT_WATCHER_EXCLUDE = DEFAULT_EXCLUDE_DIRS


def write_status(
    running: bool,
    pid: Optional[int] = None,
    root: Optional[str] = None,
    collection: Optional[str] = None,
    debounce_sec: Optional[float] = None,
    total_processed: int = 0,
    last_event_time: Optional[float] = None,
    last_synced_file: Optional[str] = None,
    pending_count: int = 0,
    burst_mode: bool = False,
    event_rate: float = 0.0,
):
    """Persists watcher lifecycle and telemetry to status file and updates HUD snapshot."""
    try:
        WATCHER_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "running": running,
            "pid": pid or os.getpid(),
            "root": root,
            "collection": collection,
            "debounce_sec": debounce_sec,
            "total_processed": total_processed,
            "last_event_time": last_event_time,
            "last_synced_file": last_synced_file,
            "pending_count": pending_count,
            "burst_mode": burst_mode,
            "event_rate": event_rate,
            "timestamp": time.time(),
        }
        tmp_status = WATCHER_STATUS_FILE.with_suffix(f".tmp.{os.getpid()}")
        tmp_status.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp_status.replace(WATCHER_STATUS_FILE)
    except Exception:
        logger.debug("Suppressed non-fatal exception")

    try:
        from kortext.tasks.tracker import write_hud_state_snapshot

        write_hud_state_snapshot()
    except Exception:
        pass


def get_watcher_status() -> Dict[str, Any]:
    """Reads live watcher status, checking PID vitality."""
    if not WATCHER_STATUS_FILE.exists():
        return {"running": False, "status": "STOPPED", "message": "Watcher daemon not started"}
    try:
        with open(WATCHER_STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        pid = data.get("pid")
        if not data.get("running") or not pid:
            return {"running": False, "status": "STOPPED", **data}
        try:
            os.kill(pid, 0)
            data["alive"] = True
            data["status"] = "RUNNING"
        except OSError:
            data["alive"] = False
            data["running"] = False
            data["status"] = "DEAD_STALE_PID"
        return data
    except Exception as exc:
        return {"running": False, "status": "ERROR", "error": str(exc)}


class KortextIncrementalSync:
    """Performs single-file incremental updates across Qdrant, Chunk Cache, and FalkorDB."""

    def __init__(
        self,
        collection: Optional[str] = None,
        symbols_graph: Optional[str] = None,
        embed_model_name: Optional[str] = None,
        embed_base_url: Optional[str] = None,
        qdrant_url: Optional[str] = None,
    ):
        self.collection = collection or get_qdrant_collection()
        default_sym = get_falkordb_symbols_graph()
        self.symbols_graph = symbols_graph or (
            f"{self.collection}_symbols" if self.collection != "kortext_codebase" else default_sym
        )
        self.embed_model_name = embed_model_name or get_embed_model_name()
        self.embed_base_url = embed_base_url or get_embed_base_url()
        self.qdrant_url = (qdrant_url or get_qdrant_url()).replace("://localhost", "://127.0.0.1")

        self.cache = IngestCache()
        self.chunk_cache = ChunkEmbeddingCache()
        self.symbol_mgr = SymbolGraphManager(graph_name=self.symbols_graph)
        self._embedded_locked = False
        if qdrant_url is None and is_qdrant_embedded_mode(probe_local=True):
            if is_embedded_qdrant_locked():
                self._embedded_locked = True
                self.qdrant_client = None
            else:
                self.qdrant_client = get_async_qdrant_client(url=None)
        else:
            self.qdrant_client = get_async_qdrant_client(url=self.qdrant_url)

    async def _process_file_event(
        self,
        file_path: str | Path,
        force: bool = False,
        skip_ast_if_sha: Optional[str] = None,
    ) -> bool:
        """Incrementally syncs a created or modified file delegating to index_single_file with socket retry."""
        p = Path(file_path).resolve()
        start_t = time.perf_counter()

        if not p.exists():
            return await self.delete_file(str(p))

        if self._embedded_locked and is_embedded_qdrant_locked():
            try:
                ext = p.suffix.lower()
                lang = LANG_MAP.get(ext)
                if lang:
                    source_bytes = p.read_bytes()
                    source_code = source_bytes.decode("utf-8", errors="replace")
                    splitter = ASTCodeSplitter(language=lang)
                    ast_info = splitter.extract_symbols_and_references(
                        source_code,
                        file_path=str(p),
                        source_bytes=source_bytes,
                    )
                    self.symbol_mgr.save_file_ast(
                        str(p),
                        ast_info.get("declarations", []),
                        ast_info.get("imports", []),
                        ast_info.get("references", []),
                    )
            except Exception as ast_exc:
                logger.debug("[Kortext Watcher] Embedded-lock AST sync skipped for '%s': %s", p.name, ast_exc)
            try:
                spool_dir = get_kortext_home() / "spool" / "invalidation"
                spool_dir.mkdir(parents=True, exist_ok=True)
                ev_file = spool_dir / f"event_{time.time_ns()}_{os.getpid()}.json"
                ev_file.write_text(
                    json.dumps(
                        {
                            "event": "file_modified",
                            "path": str(p),
                            "collection": self.collection,
                            "symbols_graph": self.symbols_graph,
                            "timestamp": time.time(),
                        }
                    ),
                    encoding="utf-8",
                )
            except Exception as sp_exc:
                logger.debug("[Kortext Watcher] Failed writing invalidation spool for '%s': %s", p.name, sp_exc)
            return True

        res: Dict[str, Any] = {}
        max_retries = 3

        for attempt in range(1, max_retries + 1):
            try:
                res = await index_single_file(
                    file_path=p,
                    collection=self.collection,
                    target_symbols_db=self.symbols_graph,
                    force=force,
                    client=self.qdrant_client,
                    cache=self.cache,
                    chunk_cache=self.chunk_cache,
                    symbol_mgr=self.symbol_mgr,
                    skip_ast_if_sha=skip_ast_if_sha,
                )
                break
            except Exception as exc:
                if attempt < max_retries:
                    backoff = 0.2 * (2 ** (attempt - 1))
                    logger.warning(
                        "[Kortext Watcher] Transient error syncing '%s' (attempt %d/%d): %s. Reconnecting in %.2fs...",
                        p.name, attempt, max_retries, exc, backoff,
                    )
                    await asyncio.sleep(backoff)
                    self.qdrant_client = get_async_qdrant_client(url=self.qdrant_url)
                    self.symbol_mgr = SymbolGraphManager(graph_name=self.symbols_graph)
                else:
                    logger.error("[Kortext Watcher] Exhausted retries syncing '%s': %s", p.name, exc)
                    return False

        if res.get("status") == "skipped":
            return False

        if res.get("status") == "error":
            logger.warning(f"[Kortext Watcher] ⚠️  Error syncing '{p.name}': {res.get('error')}")
            return False

        duration = time.perf_counter() - start_t
        chunks = res.get("chunks", 0)
        cached = res.get("cached", 0)
        misses = res.get("misses", 0)
        hit_pct = round((cached / (cached + misses)) * 100, 1) if (cached + misses) > 0 else 0.0
        logger.info(
            f"[Kortext Watcher] ⚡ Synced '{p.name}' in {duration:.3f}s "
            f"({chunks} chunks, {cached} cached [{hit_pct}%], {misses} new vectors)"
        )
        return True

    sync_file = _process_file_event

    async def delete_file(self, file_path: str) -> bool:
        """Purges a deleted file from Qdrant, Cache, and FalkorDB with socket recovery."""
        p = Path(file_path).resolve()
        resolved_fp = str(p)

        old_points = self.cache.remove_file(resolved_fp, self.collection)
        if old_points and self.qdrant_client is not None:
            for attempt in range(1, 4):
                try:
                    await self.qdrant_client.delete(
                        collection_name=self.collection,
                        points_selector=qmodels.PointIdsList(points=old_points),
                    )
                    break
                except Exception as exc:
                    if attempt < 3:
                        await asyncio.sleep(0.2)
                        self.qdrant_client = get_async_qdrant_client(url=self.qdrant_url)
                    else:
                        logger.warning("[Kortext Watcher] Failed to delete points for '%s': %s", p.name, exc)

        # Purge File node, Section nodes, ghost CALLS edges, and orphaned Symbols in FalkorDB
        for attempt in range(1, 4):
            try:
                self.symbol_mgr.delete_file_ast(resolved_fp)
                break
            except Exception as exc:
                if attempt < 3:
                    await asyncio.sleep(0.2)
                    self.symbol_mgr = SymbolGraphManager(graph_name=self.symbols_graph)
                else:
                    logger.warning("[Kortext Watcher] Failed to delete symbol AST for '%s': %s", p.name, exc)

        logger.info(f"[Kortext Watcher] 🗑️  Purged deleted file '{p.name}' from vector index and AST symbol graph.")
        return True


IncrementalSync = KortextIncrementalSync
FileWatcher = KortextIncrementalSync


class DebouncedChangeHandler(FileSystemEventHandler):
    """Gathers filesystem events and queues them with a thread-safe adaptive burst debouncer."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        syncer: KortextIncrementalSync,
        debounce_sec: float = 2.0,
        root: str = "",
        collection: str = "",
        burst_threshold: float = 15.0,
        quiescence_sec: float = 2.0,
        exclude_dirs: Optional[List[str] | Set[str]] = None,
    ):
        self.loop = loop
        self.syncer = syncer
        self.debounce_sec = debounce_sec
        self.root = root
        self.collection = collection
        self.burst_threshold = burst_threshold
        self.quiescence_sec = quiescence_sec
        self.exclude_dirs: List[str] = list(exclude_dirs) if exclude_dirs is not None else list(DEFAULT_EXCLUDE_DIRS)

        self.pending_events: Dict[str, float] = {}  # file_path -> last_event_time
        self._synced_ast_shas: Dict[str, str] = {}  # resolved_file_path -> sha256 synced by PostToolUse
        self.deleted_events: Set[str] = set()
        self.total_processed: int = 0
        self.last_event_time: Optional[float] = None
        self.last_synced_file: Optional[str] = None
        self._lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._last_status_write = 0.0

        # Burst debouncing state (>15 files/sec)
        self._recent_event_times: deque = deque()
        self.burst_mode: bool = False
        self.burst_start_time: Optional[float] = None
        self.last_burst_event_time: Optional[float] = None
        self.current_event_rate: float = 0.0

    def _should_ignore(self, path: str | Path) -> bool:
        p = Path(path)
        if p.is_file() and p.suffix.lower() not in SUPPORTED_EXT:
            return True
        name = p.name
        path_str = str(p)
        for ex in self.exclude_dirs:
            clean_ex = ex.strip("*/")
            if clean_ex and clean_ex in p.parts:
                return True
            if fnmatch.fnmatch(name, ex) or (clean_ex and fnmatch.fnmatch(name, f"*{clean_ex}*")):
                return True
            try:
                if fnmatch.fnmatch(path_str, ex) or p.match(ex):
                    return True
            except Exception:
                if fnmatch.fnmatch(path_str, ex):
                    return True
        return False

    def _record_event(self, path: str, is_deleted: bool = False, dest_path: Optional[str] = None):
        """Tracks event rate and manages adaptive burst mode transition under _state_lock."""
        now = time.time()
        with self._state_lock:
            self.last_event_time = now
            self._recent_event_times.append(now)

            # Sliding window of events in last 1.0 second
            cutoff = now - 1.0
            while self._recent_event_times and self._recent_event_times[0] < cutoff:
                self._recent_event_times.popleft()

            self.current_event_rate = float(len(self._recent_event_times))

            # Check burst trigger threshold
            if self.current_event_rate > self.burst_threshold:
                if not self.burst_mode:
                    self.burst_mode = True
                    self.burst_start_time = now
                    logger.warning(
                        f"[Kortext Watcher] High event frequency ({self.current_event_rate:.1f} events/s > {self.burst_threshold}/s). "
                        f"Entering burst mode: pausing incremental graph updates to prevent lock thrashing."
                    )
                self.last_burst_event_time = now

            if is_deleted:
                self.deleted_events.add(path)
                self.pending_events.pop(path, None)
            else:
                self.pending_events[path] = now

            if dest_path:
                self.pending_events[dest_path] = now

    def on_created(self, event: FileSystemEvent):
        if not event.is_directory and not self._should_ignore(event.src_path):
            self._record_event(event.src_path, is_deleted=False)

    def on_modified(self, event: FileSystemEvent):
        if not event.is_directory and not self._should_ignore(event.src_path):
            self._record_event(event.src_path, is_deleted=False)

    def on_deleted(self, event: FileSystemEvent):
        if not event.is_directory and not self._should_ignore(event.src_path):
            self._record_event(event.src_path, is_deleted=True)

    def on_moved(self, event: FileSystemEvent):
        if not event.is_directory:
            src_ignored = self._should_ignore(event.src_path)
            dest_ignored = self._should_ignore(event.dest_path)
            if not src_ignored and not dest_ignored:
                self._record_event(event.src_path, is_deleted=True, dest_path=event.dest_path)
            elif not src_ignored:
                self._record_event(event.src_path, is_deleted=True)
            elif not dest_ignored:
                self._record_event(event.dest_path, is_deleted=False)

    async def _execute_burst_scoped_diff(self):
        """Executes a scoped diff scan across the gathered files after a burst settling period."""
        with self._state_lock:
            deleted_paths = list(self.deleted_events)
            self.deleted_events.clear()
            candidate_paths = list(self.pending_events.keys())
            self.pending_events.clear()

        for path_str in deleted_paths:
            try:
                await self.syncer.delete_file(path_str)
                self.total_processed += 1
                self.last_synced_file = path_str
            except Exception as exc:
                logger.error(f"[Kortext Watcher] Error purging {path_str} during burst diff: {exc}")

        synced_count = 0
        skipped_count = 0
        for path_str in candidate_paths:
            p = Path(path_str)
            if not p.exists():
                try:
                    await self.syncer.delete_file(path_str)
                    self.total_processed += 1
                except Exception:
                    logger.debug("Suppressed non-fatal exception")
                continue

            changed, _, _, _ = self.syncer.cache.is_file_changed(str(p.resolve()), self.collection)
            if changed:
                try:
                    if await self.syncer.sync_file(path_str):
                        self.total_processed += 1
                        self.last_synced_file = path_str
                        synced_count += 1
                except Exception as exc:
                    logger.error(f"[Kortext Watcher] Error syncing {path_str} during burst diff: {exc}")
            else:
                skipped_count += 1

        logger.info(
            f"[Kortext Watcher] Scoped diff scan complete: {synced_count} files synchronized, "
            f"{len(deleted_paths)} purged, {skipped_count} unchanged skipped."
        )

    def _consume_spool_invalidations(self):
        """Drains spooled invalidation events from ~/.kortext/spool/invalidation into pending_events."""
        spool_dir = get_kortext_home() / "spool" / "invalidation"
        if not spool_dir.is_dir():
            return
        now = time.time()
        own_suffix = f"_{os.getpid()}.json"
        for f in sorted(spool_dir.glob("event_*.json")):
            if f.name.endswith(own_suffix):
                continue
            try:
                if json.loads(f.read_text(encoding="utf-8")).get("attempts"):
                    continue  # a failed re-index the daemon is retrying with backoff
            except (OSError, ValueError):
                pass
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                target_path = data.get("path")
                if target_path and not self._should_ignore(target_path):
                    with self._state_lock:
                        self.pending_events[str(target_path)] = now - self.debounce_sec
                        if data.get("ast_synced_sha256"):
                            try:
                                self._synced_ast_shas[str(Path(target_path).resolve())] = str(data["ast_synced_sha256"])
                            except OSError:
                                self._synced_ast_shas[str(target_path)] = str(data["ast_synced_sha256"])
            except Exception as exc:
                logger.debug("Failed reading spool invalidation %s: %s", f.name, exc)
            finally:
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    logger.debug("Suppressed non-fatal exception")

    async def flush_debounced(self):
        """Periodically flushes debounced items under _lock and _state_lock."""
        async with self._lock:
            self._consume_spool_invalidations()
            now = time.time()

            with self._state_lock:
                in_burst = self.burst_mode
                last_burst_t = self.last_burst_event_time or now
                pending_total = len(self.pending_events) + len(self.deleted_events)
                rate = self.current_event_rate

            if in_burst:
                silence_duration = now - last_burst_t
                if silence_duration < self.quiescence_sec:
                    if now - self._last_status_write > 1.0:
                        self._last_status_write = now
                        write_status(
                            running=True,
                            pid=os.getpid(),
                            root=self.root,
                            collection=self.collection,
                            debounce_sec=self.debounce_sec,
                            total_processed=self.total_processed,
                            last_event_time=self.last_event_time,
                            last_synced_file=self.last_synced_file,
                            pending_count=pending_total,
                            burst_mode=True,
                            event_rate=rate,
                        )
                    return

                logger.info(f"[Kortext Watcher] Burst activity settled after {silence_duration:.2f}s quiescence. Executing scoped diff scan...")
                await self._execute_burst_scoped_diff()
                with self._state_lock:
                    self.burst_mode = False
                    self.burst_start_time = None
                    self.last_burst_event_time = None
                return

            with self._state_lock:
                to_process = []
                for path_str, last_t in list(self.pending_events.items()):
                    if now - last_t >= self.debounce_sec:
                        to_process.append(path_str)
                        del self.pending_events[path_str]

                deleted_batch = list(self.deleted_events)
                self.deleted_events.clear()

            # Process deletions
            for path_str in deleted_batch:
                try:
                    await self.syncer.delete_file(path_str)
                    self.total_processed += 1
                    self.last_synced_file = path_str
                except Exception as exc:
                    logger.error(f"[Kortext Watcher] Error purging {path_str}: {exc}")

            # Process modifications / creations
            for path_str in to_process:
                try:
                    with self._state_lock:
                        skip_sha = self._synced_ast_shas.pop(str(Path(path_str).resolve()), None) or self._synced_ast_shas.pop(str(path_str), None)
                    if skip_sha:
                        synced = await self.syncer.sync_file(path_str, skip_ast_if_sha=skip_sha)
                    else:
                        synced = await self.syncer.sync_file(path_str)
                    if synced:
                        self.total_processed += 1
                        self.last_synced_file = path_str
                except Exception as exc:
                    logger.error(f"[Kortext Watcher] Error syncing {path_str}: {exc}")

            with self._state_lock:
                remaining_pending = len(self.pending_events) + len(self.deleted_events)
                rate = self.current_event_rate
                burst_flag = self.burst_mode

            if now - self._last_status_write > 2.0 or to_process or deleted_batch:
                self._last_status_write = now
                write_status(
                    running=True,
                    pid=os.getpid(),
                    root=self.root,
                    collection=self.collection,
                    debounce_sec=self.debounce_sec,
                    total_processed=self.total_processed,
                    last_event_time=self.last_event_time,
                    last_synced_file=self.last_synced_file,
                    pending_count=remaining_pending,
                    burst_mode=burst_flag,
                    event_rate=rate,
                )


async def start_watcher(
    watch_root: str,
    collection: str = COLLECTION,
    debounce_sec: float = DEFAULT_DEBOUNCE,
    exclude_dirs: Optional[List[str] | Set[str]] = None,
):
    """Starts the background directory observer and debounced flush loop."""
    root_path = Path(watch_root).resolve()
    logger.info(f"[Kortext Watcher] 👁️  Starting file watcher on: {root_path}")
    logger.info(f"[Kortext Watcher] Collection: {collection} | Debounce window: {debounce_sec}s")

    syncer = KortextIncrementalSync(collection=collection)
    loop = asyncio.get_running_loop()
    handler = DebouncedChangeHandler(
        loop=loop,
        syncer=syncer,
        debounce_sec=debounce_sec,
        root=str(root_path),
        collection=collection,
        exclude_dirs=exclude_dirs,
    )

    write_status(
        running=True,
        pid=os.getpid(),
        root=str(root_path),
        collection=collection,
        debounce_sec=debounce_sec,
        total_processed=0,
        pending_count=0,
    )

    observer = Observer()
    observer.schedule(handler, str(root_path), recursive=True)
    observer.start()

    stop_event = asyncio.Event()

    def _on_stop():
        logger.info("[Kortext Watcher] Stopping watcher...")
        observer.stop()
        stop_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _on_stop)
        loop.add_signal_handler(signal.SIGTERM, _on_stop)
    except (NotImplementedError, RuntimeError):
        logger.debug("Suppressed non-fatal exception")

    try:
        while not stop_event.is_set():
            try:
                await handler.flush_debounced()
            except Exception as flush_err:
                logger.error(f"[Kortext Watcher] Error during debounced flush: {flush_err}")
            await asyncio.sleep(0.5)
    finally:
        observer.stop()
        observer.join()
        write_status(
            running=False,
            pid=os.getpid(),
            root=str(root_path),
            collection=collection,
            debounce_sec=debounce_sec,
            total_processed=handler.total_processed,
            last_event_time=handler.last_event_time,
            last_synced_file=handler.last_synced_file,
            pending_count=0,
        )
        logger.info("[Kortext Watcher] Watcher stopped cleanly.")


def run_watcher(
    watch_root: str = ".",
    collection: Optional[str] = None,
    debounce_sec: float = DEFAULT_DEBOUNCE,
    exclude_dirs: Optional[List[str] | Set[str]] = None,
):
    """Synchronous CLI entrypoint wrapper around start_watcher."""
    coll = collection or COLLECTION
    return asyncio.run(
        start_watcher(
            watch_root=watch_root,
            collection=coll,
            debounce_sec=debounce_sec,
            exclude_dirs=exclude_dirs,
        )
    )


def main():
    parser = argparse.ArgumentParser(description="Kortext Incremental Real-Time File Watcher")
    parser.add_argument("--root", default=".", help="Directory to monitor (default: current directory)")
    parser.add_argument("--collection", default=COLLECTION, help="Qdrant collection name")
    parser.add_argument("--debounce", type=float, default=DEFAULT_DEBOUNCE, help="Debounce seconds (default: 2.0)")
    args = parser.parse_args()

    run_watcher(watch_root=args.root, collection=args.collection, debounce_sec=args.debounce)


if __name__ == "__main__":
    main()
