"""
Batch & single-file ingestor: reads files from a directory tree and indexes them into Qdrant and FalkorDB.
Unified on direct AsyncQdrantClient.upsert with hybrid dense + BM25 sparse vectors,
Content-Hash Incremental Ingestion, and MINJA CausalSanitizer trust-tier tagging.

Usage:
  kortext-ingest --root /path/to/project [--collection my_collection] [--force]
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import fnmatch
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from dotenv import load_dotenv
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qmodels


class Document:
    """Lightweight zero-dependency Document representation compatible with LlamaIndex Document."""

    def __init__(
        self,
        text: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        id_: Optional[str] = None,
        doc_id: Optional[str] = None,
        node_id: Optional[str] = None,
        **kwargs: Any,
    ):
        import uuid

        self.text = text
        self.metadata: Dict[str, Any] = dict(metadata) if metadata else {}
        self.id_ = id_ or doc_id or node_id or str(uuid.uuid4())
        self.source_node = None
        self.relationships: Dict[Any, Any] = {}
        self.excluded_embed_metadata_keys: List[str] = []
        self.excluded_llm_metadata_keys: List[str] = []

    @property
    def doc_id(self) -> str:
        return self.id_

    @property
    def node_id(self) -> str:
        return self.id_

    @property
    def extra_info(self) -> Dict[str, Any]:
        return self.metadata

    def get_content(self, *args: Any, **kwargs: Any) -> str:
        return self.text


class SimpleDirectoryReader:
    """Lightweight native directory reader replacing heavy LlamaIndex SimpleDirectoryReader."""

    _IS_NATIVE_READER = True

    def __init__(
        self,
        input_dir: Optional[str] = None,
        input_files: Optional[List[str]] = None,
        recursive: bool = True,
        required_exts: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        **kwargs: Any,
    ):
        self.input_dir = input_dir
        self.input_files = input_files
        self.recursive = recursive
        self.required_exts = set(required_exts) if required_exts else None
        self.exclude = list(exclude) if exclude else []

    def load_data(self) -> List[Document]:
        docs: List[Document] = []
        paths: List[Path] = []
        if self.input_files is not None:
            paths = [Path(p) for p in self.input_files]
        elif self.input_dir is not None:
            root = Path(self.input_dir)
            if root.is_dir():
                iterator = root.rglob("*") if self.recursive else root.glob("*")
                for cand in iterator:
                    if not cand.is_file():
                        continue
                    if self.required_exts and cand.suffix.lower() not in self.required_exts:
                        continue
                    rel_str = str(cand.relative_to(root))
                    if any(
                        fnmatch.fnmatch(rel_str, pat)
                        or fnmatch.fnmatch(cand.name, pat)
                        or pat.strip("/") in cand.parts
                        for pat in self.exclude
                    ):
                        continue
                    paths.append(cand)
        for p in paths:
            try:
                resolved = p.resolve()
                text = resolved.read_text(encoding="utf-8", errors="replace")
                docs.append(
                    Document(
                        text=text,
                        metadata={"file_path": str(resolved), "file_name": resolved.name},
                    )
                )
            except Exception:
                continue
        return docs


class VectorStoreIndex:
    """Compatibility shim for legacy tests patching VectorStoreIndex."""

    def __init__(self, *args: Any, **kwargs: Any):
        pass

from kortext._env import (
    get_configured_qdrant_url,
    get_embed_base_url,
    get_embed_model_name,
    get_falkordb_symbols_graph,
    get_qdrant_collection,
    get_qdrant_url,
)
from kortext._home import get_kortext_home
from kortext.cache import ChunkEmbeddingCache, IngestCache
from kortext.chunking.languages import ALL_SUPPORTED_EXTS, SUPPORTED_DOC_EXTS
from kortext.embeddings import get_dense_embedder
from kortext.ingest_collection import setup_collection
from kortext.server_helpers import SENSITIVE_DIR_NAMES
from kortext.trust.sanitizer import CausalSanitizer, SecurityViolation

try:
    from fastembed import SparseTextEmbedding
    TextSparseModel = SparseTextEmbedding
except ImportError:
    SparseTextEmbedding = None  # type: ignore
    TextSparseModel = None      # type: ignore

_SPARSE_MODEL: Optional[Any] = None
_SPARSE_MODEL_INIT_FAILED: bool = False


def get_sparse_model() -> Optional[Any]:
    """Lazily initializes and returns singleton TextSparseModel for BM25 sparse vectors."""
    global _SPARSE_MODEL, _SPARSE_MODEL_INIT_FAILED
    if _SPARSE_MODEL is not None:
        return _SPARSE_MODEL
    if _SPARSE_MODEL_INIT_FAILED or TextSparseModel is None:
        return None
    try:
        try:
            _SPARSE_MODEL = TextSparseModel(model_name="Qdrant/bm25", local_files_only=True)
        except Exception:
            import socket

            try:
                socket.getaddrinfo("huggingface.co", 443)
                _SPARSE_MODEL = TextSparseModel(model_name="Qdrant/bm25")
            except OSError as net_exc:
                _SPARSE_MODEL_INIT_FAILED = True
                logger.warning(f"[Kortext Ingest] Offline mode detected; skipping remote BM25 download: {net_exc}")
                return None
    except Exception as exc:
        _SPARSE_MODEL_INIT_FAILED = True
        logger.warning(f"[Kortext Ingest] Failed to initialize sparse model: {exc}")
    return _SPARSE_MODEL


load_dotenv()

logger = logging.getLogger(__name__)

EMBED_BASE_URL = get_embed_base_url()
QDRANT_URL = get_qdrant_url().replace("://localhost", "://127.0.0.1")
COLLECTION = get_qdrant_collection()
DEFAULT_SYMBOLS_GRAPH = get_falkordb_symbols_graph()

_BASE_EXCLUDES = [
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "*.gguf",
    "*.pyc",
    "*.log",
    "*.lock",
    ".DS_Store",
    "coverage/",
    "target/",
    "*.min.js",
    "vendor",
    "vendors",
    "third_party",
    "extern",
    "deps",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "journal/dreams",
    "journal/reports",
    "*-dream.md",
    *sorted(SENSITIVE_DIR_NAMES),
]
_ENV_EXCLUDES = [
    p.strip()
    for p in os.getenv("INGEST_EXCLUDE_PATTERNS", "").split(",")
    if p.strip()
]
DEFAULT_EXCLUDE: List[str] = list(dict.fromkeys(_BASE_EXCLUDES + _ENV_EXCLUDES))
MAX_FILE_SIZE_BYTES = int(os.getenv("MAX_FILE_SIZE_BYTES", str(2 * 1024 * 1024)))  # 2MB ceiling

SUPPORTED_EXT = ALL_SUPPORTED_EXTS


def _is_sensitive_file_path(p: Path) -> bool:
    """Returns True if path resides inside a sensitive directory or matches credential file patterns."""
    if set(p.parts) & SENSITIVE_DIR_NAMES:
        return True
    name_lower = p.name.lower()
    if name_lower == ".env" or name_lower.startswith(".env."):
        return True
    if name_lower.endswith((".pem", ".key", ".p12", ".pfx")):
        return True
    return False


def _resolve_trust_tier_for_extension(ext: str) -> int:
    """Assigns MINJA provenance trust_tier: 2 for verified code/config, 3 for markdown/prose docs."""
    if ext in SUPPORTED_DOC_EXTS or ext in (".md", ".markdown", ".txt", ".rst"):
        return 3
    return 2


def _sanitize_chunk_text(
    sanitizer: CausalSanitizer,
    file_name: str,
    text: str,
    trust_tier: int,
) -> bool:
    """Returns True if chunk passes MINJA CausalSanitizer, False if blocked/quarantined."""
    try:
        sanitizer.sanitize(
            subject=file_name,
            relation="INGEST_CHUNK",
            fact=text,
            source_tier=trust_tier,
        )
        return True
    except SecurityViolation as sv:
        logger.warning("[Kortext Ingest] Quarantined adversarial chunk in %s: %s", file_name, sv)
        return False


def _build_qdrant_points(
    valid_nodes: List[Any],
    sparse_model: Optional[Any],
    default_file_path: Optional[str] = None,
    default_file_name: Optional[str] = None,
) -> tuple[List[qmodels.PointStruct], Dict[str, List[str]]]:
    """
    Constructs Qdrant PointStruct items with unified flat payload schema,
    dense + sparse BM25 vectors, and MINJA trust_tier tagging.
    """
    sparse_vectors: Dict[str, qmodels.SparseVector] = {}
    try:
        active_sparse_model = sparse_model or get_sparse_model()
        if active_sparse_model is not None and valid_nodes:
            sparse_texts = [n.get_content() for n in valid_nodes]
            raw_sparse = list(active_sparse_model.embed(sparse_texts))
            for n, semb in zip(valid_nodes, raw_sparse):
                if hasattr(semb, "indices") and hasattr(semb, "values"):
                    s_indices = [int(i) for i in semb.indices]
                    s_values = [float(v) for v in semb.values]
                elif isinstance(semb, dict):
                    s_indices = [int(i) for i in semb.get("indices", [])]
                    s_values = [float(v) for v in semb.get("values", [])]
                elif isinstance(semb, (tuple, list)) and len(semb) == 2:
                    s_indices = [int(i) for i in semb[0]]
                    s_values = [float(v) for v in semb[1]]
                else:
                    s_indices, s_values = [], []

                if s_indices and s_values:
                    sparse_vectors[n.node_id] = qmodels.SparseVector(
                        indices=s_indices,
                        values=s_values,
                    )
    except Exception as exc:
        logger.warning(f"[Kortext Ingest] Diagnostic note generating sparse vectors: {exc}")

    sanitizer = CausalSanitizer()
    points: List[qmodels.PointStruct] = []
    file_to_point_ids: Dict[str, List[str]] = {}

    for n in valid_nodes:
        raw_meta = dict(getattr(n, "metadata", {}) or {})
        fp = str(raw_meta.get("file_path") or default_file_path or "")
        resolved_fp = str(Path(fp).resolve()) if fp else ""
        fn = str(raw_meta.get("file_name") or default_file_name or (Path(resolved_fp).name if resolved_fp else ""))
        ext = Path(fn or resolved_fp).suffix.lower()
        trust_tier = int(raw_meta.get("trust_tier") or _resolve_trust_tier_for_extension(ext))
        chunk_text = n.get_content()

        if not _sanitize_chunk_text(sanitizer, fn or resolved_fp or "chunk", chunk_text, trust_tier):
            continue

        # Strip non-serializable internal AST parse objects from payload
        clean_meta = {
            k: v
            for k, v in raw_meta.items()
            if not k.startswith("_")
        }
        clean_meta["trust_tier"] = trust_tier

        vec_payload: Dict[str, Any] = {"text-dense": n.embedding}
        if n.node_id in sparse_vectors:
            vec_payload["text-sparse-new"] = sparse_vectors[n.node_id]

        points.append(
            qmodels.PointStruct(
                id=n.node_id,
                vector=vec_payload,
                payload={
                    "text": chunk_text,
                    "file_path": resolved_fp,
                    "file_name": fn,
                    **clean_meta,
                },
            )
        )
        if resolved_fp:
            file_to_point_ids.setdefault(resolved_fp, []).append(n.node_id)

    return points, file_to_point_ids


def _create_async_qdrant_client(qdrant_url: str = QDRANT_URL) -> AsyncQdrantClient:
    """
    Instantiate a single AsyncQdrantClient without opening a competing sync QdrantLocal lock.
    Prefers configured/active HTTP Qdrant daemon (:6333); falls back to single-handle embedded storage.
    """
    effective_url = get_configured_qdrant_url(probe_local=True)
    if effective_url:
        return AsyncQdrantClient(url=effective_url, timeout=10.0)
    qpath = get_kortext_home() / "qdrant"
    qpath.mkdir(parents=True, exist_ok=True)
    from kortext._qdrant import EmbeddedQdrantLockError, is_embedded_qdrant_locked

    if is_embedded_qdrant_locked(qpath):
        raise EmbeddedQdrantLockError(
            f"Embedded Qdrant storage at {qpath} is locked by another process (Sole Custodian: kortext.server)."
        )
    return AsyncQdrantClient(path=str(qpath))


@asynccontextmanager
async def managed_qdrant_client(
    client: Optional[AsyncQdrantClient] = None,
    qdrant_url: str = QDRANT_URL,
) -> AsyncIterator[AsyncQdrantClient]:
    """Ensures standalone ingestion/watcher calls deterministically release QdrantLocal file locks."""
    owns_client = client is None
    active_client = client if client is not None else _create_async_qdrant_client(qdrant_url)
    try:
        yield active_client
    finally:
        if owns_client:
            try:
                await active_client.close()
            except Exception:
                pass


async def index_single_file(
    file_path: Path,
    collection: str = COLLECTION,
    target_symbols_db: str = DEFAULT_SYMBOLS_GRAPH,
    root: Optional[Path] = None,
    force: bool = False,
    client: Optional[AsyncQdrantClient] = None,
    cache: Optional[IngestCache] = None,
    chunk_cache: Optional[ChunkEmbeddingCache] = None,
    symbol_mgr: Optional[Any] = None,
    sparse_model: Optional[Any] = None,
    skip_ast_if_sha: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Indexes a single file: ensures collection exists, cleans old points from Qdrant,
    extracts AST symbols/markdown into FalkorDB, and chunks & upserts vector points into Qdrant.
    """
    p = Path(file_path).resolve()
    resolved_fp = str(p)

    if not p.exists() or not p.is_file():
        return {
            "status": "error",
            "error": f"File does not exist: {file_path}",
            "file_path": resolved_fp,
            "chunks": 0,
            "symbols": 0,
            "points": [],
            "cached": 0,
            "misses": 0,
            "changed": False,
        }

    if _is_sensitive_file_path(p):
        return {
            "status": "skipped",
            "reason": "sensitive_file",
            "file_path": resolved_fp,
            "chunks": 0,
            "symbols": 0,
            "points": [],
            "cached": 0,
            "misses": 0,
            "changed": False,
        }

    if cache is None:
        cache = IngestCache()
    if chunk_cache is None:
        chunk_cache = ChunkEmbeddingCache()
    if symbol_mgr is None:
        from kortext.symbols import SymbolGraphManager
        symbol_mgr = SymbolGraphManager(graph_name=target_symbols_db)

    # Guardrail: Check file size ceiling
    try:
        st = p.stat()
        if st.st_size > MAX_FILE_SIZE_BYTES:
            logger.warning(
                f"[Kortext Ingest] ⚠️  Skipping large file {p.name} ({st.st_size} bytes > {MAX_FILE_SIZE_BYTES})"
            )
            return {
                "status": "skipped",
                "reason": "file_too_large",
                "file_path": resolved_fp,
                "chunks": 0,
                "symbols": 0,
                "points": [],
                "cached": 0,
                "misses": 0,
                "changed": False,
            }
    except Exception:
        logger.debug("Suppressed non-fatal exception")

    # Guardrail: Binary detection
    try:
        with open(p, "rb") as bf:
            header = bf.read(8192)
            if b"\x00" in header:
                logger.debug(f"[Kortext Ingest] Skipping binary file: {p.name}")
                return {
                    "status": "skipped",
                    "reason": "binary_file",
                    "file_path": resolved_fp,
                    "chunks": 0,
                    "symbols": 0,
                    "points": [],
                    "cached": 0,
                    "misses": 0,
                    "changed": False,
                }
    except Exception:
        logger.debug("Suppressed non-fatal exception")

    # 1. Content-Hash Incremental Ingestion Check
    if force:
        changed = True
        mtime, sha = IngestCache.get_file_stats(p)
        old_points = cache.get_all_cached_files(collection).get(resolved_fp, [])
    else:
        changed, mtime, sha, old_points = cache.is_file_changed(resolved_fp, collection)

    if not changed:
        return {
            "status": "skipped",
            "file_path": resolved_fp,
            "chunks": 0,
            "symbols": 0,
            "points": old_points,
            "cached": 0,
            "misses": 0,
            "changed": False,
        }

    # 2. Read file content
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as exc:
        logger.warning(f"[Kortext Ingest] ⚠️  Cannot read {p.name}: {exc}")
        return {
            "status": "error",
            "error": str(exc),
            "file_path": resolved_fp,
            "chunks": 0,
            "symbols": 0,
            "points": [],
            "cached": 0,
            "misses": 0,
            "changed": True,
        }

    owns_client = client is None
    active_client = client if client is not None else _create_async_qdrant_client(QDRANT_URL)
    try:
        if owns_client:
            try:
                await setup_collection(client=active_client, collection=collection, cache=cache, force=False)
            except Exception as exc:
                logger.debug("setup_collection check in index_single_file skipped: %s", exc)

        # 3. Clean old points from Qdrant before upserting new ones
        if old_points:
            try:
                await active_client.delete(
                    collection_name=collection,
                    points_selector=qmodels.PointIdsList(points=old_points),
                )
            except Exception as exc:
                logger.debug(f"[Kortext Ingest] Old points deletion notice for {resolved_fp}: {exc}")

        # 4. Extract AST symbols or markdown into FalkorDB
        from kortext.chunking.ast_splitter import ASTCodeSplitter, LANG_MAP
        from kortext.symbols import MarkdownSymbolExtractor

        ext = p.suffix.lower()
        symbol_count = 0
        tree = None
        source_bytes = None
        ast_info = None

        if ext in LANG_MAP:
            try:
                splitter = ASTCodeSplitter(language=LANG_MAP[ext])
                source_bytes = content.encode("utf-8")
                tree = splitter.parser.parse(source_bytes) if hasattr(splitter, "parser") else None
                ast_info = splitter.extract_symbols_and_references(
                    content, file_path=resolved_fp, tree=tree, source_bytes=source_bytes
                )
                decls = ast_info.get("declarations", [])
                imports = ast_info.get("imports", [])
                refs = ast_info.get("references", [])
                if not (skip_ast_if_sha and skip_ast_if_sha == sha):
                    symbol_mgr.save_file_ast(
                        file_path=resolved_fp,
                        declarations=decls,
                        imports=imports,
                        references=refs,
                    )
                symbol_count = len(decls)
            except Exception as exc:
                logger.warning(f"[Kortext Ingest] ⚠️  Symbol extraction error on {p.name}: {exc}")
        elif ext in (".md", ".markdown"):
            try:
                md_ext = MarkdownSymbolExtractor(workspace_root=str(root) if root else str(p.parent))
                md_info = md_ext.extract(content, file_path=resolved_fp)
                symbol_mgr.save_markdown_ast(
                    file_path=md_info["file_path"],
                    title=md_info["title"],
                    frontmatter=md_info["frontmatter"],
                    sections=md_info["sections"],
                    declarations=md_info["declarations"],
                    links=md_info["links"],
                    tags=md_info["tags"],
                )
                symbol_count = len(md_info.get("declarations", []))
            except Exception as exc:
                logger.warning(f"[Kortext Ingest] ⚠️  Markdown extraction error on {p.name}: {exc}")

        # 5. AST-aware chunking
        from kortext.chunking import SmartCodeNodeParser

        chunk_size = int(os.getenv("INGEST_CHUNK_SIZE", "2048"))
        chunk_overlap = int(os.getenv("INGEST_CHUNK_OVERLAP", "128"))
        parser = SmartCodeNodeParser(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        trust_tier = _resolve_trust_tier_for_extension(ext)
        doc = Document(
            text=content,
            metadata={
                "file_path": resolved_fp,
                "file_name": p.name,
                "trust_tier": trust_tier,
                "_tree": tree,
                "_source_bytes": source_bytes,
                "_ast_info": ast_info,
            },
        )
        nodes = parser.get_nodes_from_documents([doc])

        if not nodes:
            cache.record_file(resolved_fp, collection, mtime, sha, [])
            return {
                "status": "indexed",
                "file_path": resolved_fp,
                "chunks": 0,
                "symbols": symbol_count,
                "points": [],
                "cached": 0,
                "misses": 0,
                "changed": True,
            }

        # 6. Embeddings with ChunkEmbeddingCache
        embedder = get_dense_embedder()
        embed_model_name = getattr(embedder, "model_name", None) or get_embed_model_name()
        texts = [n.get_content() for n in nodes]
        cached_hits, misses = chunk_cache.get_batch(texts, embed_model_name)
        for idx, vec in cached_hits.items():
            nodes[idx].embedding = vec

        if misses:
            batch_size = int(os.getenv("EMBED_BATCH_SIZE", "8"))
            concurrency = int(os.getenv("EMBED_CONCURRENCY", "1"))
            miss_batches = [misses[i : i + batch_size] for i in range(0, len(misses), batch_size)]
            sem = asyncio.Semaphore(concurrency)

            try:
                async def embed_batch(batch_idx: int, miss_batch):
                    async with sem:
                        miss_texts = [item[1] for item in miss_batch]
                        if hasattr(embedder, "aembed_documents") and callable(embedder.aembed_documents):
                            embeddings = await embedder.aembed_documents(miss_texts)
                        else:
                            embeddings = embedder.embed_documents(miss_texts)

                        new_cache_items = []
                        for (orig_idx, miss_text), emb in zip(miss_batch, embeddings):
                            nodes[orig_idx].embedding = emb
                            new_cache_items.append((miss_text, emb))
                        chunk_cache.put_batch(new_cache_items, embed_model_name)

                await asyncio.gather(*[embed_batch(i, b) for i, b in enumerate(miss_batches)])
            except Exception as exc:
                logger.error(f"[Kortext Ingest] ⚠️  Embedding failed for {p.name}: {exc}")
                return {
                    "status": "error",
                    "error": f"Embedding failed: {exc}",
                    "file_path": resolved_fp,
                    "chunks": 0,
                    "symbols": symbol_count,
                    "points": [],
                    "cached": len(cached_hits),
                    "misses": len(misses),
                    "changed": True,
                }

        # 7. Upsert valid vectors into Qdrant via unified PointStruct pipeline
        valid_nodes = [n for n in nodes if n.embedding]
        points, file_to_points = _build_qdrant_points(
            valid_nodes,
            sparse_model=sparse_model,
            default_file_path=resolved_fp,
            default_file_name=p.name,
        )
        point_ids = file_to_points.get(resolved_fp, [pt.id for pt in points])

        if points:
            try:
                await active_client.upsert(collection_name=collection, points=points)
            except Exception as upsert_exc:
                err_str = str(upsert_exc)
                if "dimension" in err_str.lower() or "expected dim" in err_str.lower():
                    logger.warning(
                        "[Kortext Ingest] Single-file vector upsert skipped due to dimension mismatch (%s); AST symbol graph updated.",
                        upsert_exc,
                    )
                    point_ids = []
                else:
                    raise

        # 8. Record in IngestCache
        cache.record_file(resolved_fp, collection, mtime, sha, point_ids)

        return {
            "status": "indexed",
            "file_path": resolved_fp,
            "chunks": len(points),
            "symbols": symbol_count,
            "points": point_ids,
            "cached": len(cached_hits),
            "misses": len(misses),
            "changed": True,
        }
    finally:
        if owns_client:
            try:
                await active_client.close()
            except Exception:
                pass


async def ingest(
    root: str,
    extra_excludes: Optional[List[str]] = None,
    collection: str = COLLECTION,
    force: bool = False,
    symbols_graph: Optional[str] = None,
    client: Optional[AsyncQdrantClient] = None,
):
    exclude = list(dict.fromkeys(DEFAULT_EXCLUDE + (extra_excludes or [])))
    target_symbols_db = symbols_graph or (
        f"{collection}_symbols" if collection != "kortext_codebase" else DEFAULT_SYMBOLS_GRAPH
    )
    logger.info(f"[Kortext Ingest] Indexing: {root}")
    logger.info(
        f"[Kortext Ingest] Qdrant Collection: {collection} | FalkorDB Symbol Graph: {target_symbols_db} (force={force})"
    )

    embedder = get_dense_embedder()
    target_embed_dim = getattr(embedder, "dimension", 384)
    embed_model_name = getattr(embedder, "model_name", None) or get_embed_model_name()

    async with managed_qdrant_client(client=client, qdrant_url=QDRANT_URL) as client:
        cache = IngestCache()

        try:
            embed_dim = await setup_collection(client=client, collection=collection, cache=cache, force=force)
        except TypeError:
            embed_dim = await setup_collection(client=client, collection=collection, cache=cache)

        skip_vector_upsert = (
            isinstance(embed_dim, int) and isinstance(target_embed_dim, int) and embed_dim != target_embed_dim
        )
        if skip_vector_upsert:
            logger.warning(
                f"[Kortext Ingest] Collection '{collection}' dimension ({embed_dim}) != active embedder dimension ({target_embed_dim}). "
                f"Skipping vector upsert to protect existing index; Tier 0 AST symbol graph will still be updated."
            )

        # Pre-filter unchanged files before SimpleDirectoryReader.load_data() to avoid loading unchanged files into RAM
        prefiltered_files = None
        discovered_files = set()
        root_path = Path(root)
        if SimpleDirectoryReader.__dict__.get("_IS_NATIVE_READER") is True and root_path.is_dir():
            import fnmatch
            candidate_paths = []
            for cand in root_path.rglob("*"):
                if not cand.is_file() or cand.suffix.lower() not in SUPPORTED_EXT:
                    continue
                rel_str = str(cand.relative_to(root_path))
                if any(fnmatch.fnmatch(rel_str, pat) or fnmatch.fnmatch(cand.name, pat) or pat.strip("/") in cand.parts for pat in exclude):
                    continue
                if _is_sensitive_file_path(cand):
                    continue
                try:
                    if cand.stat().st_size > MAX_FILE_SIZE_BYTES:
                        continue
                except OSError:
                    continue
                resolved_cand = str(cand.resolve())
                discovered_files.add(resolved_cand)
                if not force:
                    try:
                        is_chg, _, _, _ = cache.is_file_changed(resolved_cand, collection)
                        if not is_chg:
                            continue
                    except Exception:
                        pass
                candidate_paths.append(resolved_cand)
            prefiltered_files = candidate_paths

        try:
            if prefiltered_files is not None:
                if not discovered_files:
                    logger.warning("[Kortext Ingest] ⚠️  No matching documents found.")
                    return
                if not prefiltered_files:
                    docs = []
                else:
                    reader = SimpleDirectoryReader(input_files=prefiltered_files)
                    docs = reader.load_data()
            else:
                reader = SimpleDirectoryReader(
                    input_dir=root,
                    recursive=True,
                    required_exts=list(SUPPORTED_EXT),
                    exclude=exclude,
                )
                docs = reader.load_data()
        except Exception as e:
            logger.error(f"[Kortext Ingest] Error reading directory: {e}")
            return

        if not docs and not discovered_files:
            logger.warning("[Kortext Ingest] ⚠️  No matching documents found.")
            return

        # 1. Content-Hash Incremental Ingestion Check
        current_files = discovered_files or {
            str(Path(d.metadata["file_path"]).resolve())
            for d in docs
            if isinstance(getattr(d, "metadata", None), dict) and "file_path" in d.metadata
        }

        # Purge deleted files
        purged = cache.purge_missing_files(collection, current_files, root=root)
        if purged:
            logger.info(f"[Kortext Ingest] 🗑️  Purging {len(purged)} deleted file(s) from collection '{collection}'...")
            from kortext.symbols import SymbolGraphManager
            symbol_graph = SymbolGraphManager(graph_name=target_symbols_db)
            for purged_file, old_points in purged:
                if old_points:
                    try:
                        await client.delete(
                            collection_name=collection,
                            points_selector=qmodels.PointIdsList(points=old_points),
                        )
                    except Exception as exc:
                        logger.warning(f"[Kortext Ingest] Warning: Failed to delete points for {purged_file}: {exc}")
                try:
                    symbol_graph.delete_file_ast(purged_file)
                except Exception as exc:
                    logger.warning(f"[Kortext Ingest] Warning: Failed to delete symbol AST for {purged_file}: {exc}")

        docs_to_index = []
        files_to_reindex = {}  # {resolved_fp: (mtime, sha, old_points)}

        for doc in docs:
            fp = doc.metadata.get("file_path")
            if not fp:
                docs_to_index.append(doc)
                continue
            p = Path(fp).resolve()
            resolved_fp = str(p)

            if _is_sensitive_file_path(p):
                logger.debug(f"[Kortext Ingest] Skipping sensitive file: {p.name}")
                continue

            # Guardrail: Check file size ceiling
            try:
                st = p.stat()
                if st.st_size > MAX_FILE_SIZE_BYTES:
                    logger.warning(
                        f"[Kortext Ingest] ⚠️  Skipping large file {p.name} ({st.st_size} bytes > {MAX_FILE_SIZE_BYTES})"
                    )
                    continue
            except Exception:
                logger.debug("Suppressed non-fatal exception")

            # Guardrail: Binary detection
            try:
                with open(p, "rb") as bf:
                    header = bf.read(8192)
                    if b"\x00" in header:
                        logger.debug(f"[Kortext Ingest] Skipping binary file: {p.name}")
                        continue
            except Exception:
                logger.debug("Suppressed non-fatal exception")

            if force:
                changed = True
                mtime, sha = IngestCache.get_file_stats(Path(resolved_fp))
                old_points = cache.get_all_cached_files(collection).get(resolved_fp, [])
            else:
                changed, mtime, sha, old_points = cache.is_file_changed(resolved_fp, collection)

            if changed:
                docs_to_index.append(doc)
                files_to_reindex[resolved_fp] = (mtime, sha, old_points)

        if not docs_to_index:
            logger.info(f"[Kortext Ingest] ⚡ All {len(docs)} files are up-to-date in cache. Nothing to index.")
            return

        logger.info(
            f"[Kortext Ingest] Detected {len(docs_to_index)} modified or new file(s) out of {len(docs)} total files."
        )

        # Delete stale vector points for modified files before upserting new ones
        for resolved_fp, (_, _, old_points) in files_to_reindex.items():
            if old_points:
                try:
                    await client.delete(
                        collection_name=collection,
                        points_selector=qmodels.PointIdsList(points=old_points),
                    )
                except Exception:
                    logger.debug("Suppressed non-fatal exception")

        # 2. Auto-detect and ingest compiler-grade SCIP index if present in workspace root
        scip_candidates = [
            Path(root) / "index.scip",
            Path(root) / ".scip",
            Path(root) / "dump.scip",
        ]
        scip_ingested = False
        for scip_path in scip_candidates:
            if scip_path.is_file():
                try:
                    from kortext.symbols.scip import SCIPImporter
                    scip_imp = SCIPImporter(graph_name=target_symbols_db)
                    scip_res = scip_imp.ingest_index(scip_path, purge_existing=False)
                    logger.info(
                        f"[Kortext Ingest] Auto-ingested compiler-grade SCIP index ({scip_path.name}): "
                        f"{scip_res['symbols_indexed']} symbols, {scip_res['references_indexed']} references, "
                        f"{scip_res['relationships_indexed']} type relationships."
                    )
                    scip_ingested = True
                    break
                except Exception as e:
                    logger.warning(f"[Kortext Ingest] Warning auto-ingesting SCIP index {scip_path}: {e}")

        if not scip_ingested:
            try:
                from kortext.symbols.scip import auto_index_and_ingest, detect_project_scip_indexer
                detector = detect_project_scip_indexer(root)
                if detector.get("binary"):
                    auto_res = auto_index_and_ingest(root, graph_name=target_symbols_db, purge_existing=False)
                    if auto_res.get("status") == "missing_indexer":
                        logger.info(
                            f"[Kortext Ingest] Compiler manifest detected ({detector['manifest']}) for language '{detector['language']}'. "
                            f"Install '{detector['install_hint']}' to upgrade to 100% type-checked semantic graphs. "
                            f"Using Tree-sitter AST fallback."
                        )
                    elif auto_res.get("documents_indexed", 0) > 0:
                        logger.info(
                            f"[Kortext Ingest] Compiler-grade SCIP index generated & ingested: "
                            f"{auto_res['symbols_indexed']} symbols, {auto_res['references_indexed']} references, "
                            f"{auto_res['relationships_indexed']} type relationships."
                        )
            except Exception as e:
                logger.warning(f"[Kortext Ingest] Diagnostic note checking compiler SCIP indexer: {e}")

        # 3. Chunked Streaming Ingestion using unified direct PointStruct upsert pipeline
        from kortext.chunking import SmartCodeNodeParser
        from kortext.chunking.ast_splitter import ASTCodeSplitter, LANG_MAP
        from kortext.symbols import MarkdownSymbolExtractor, SymbolGraphManager

        symbol_graph = SymbolGraphManager(graph_name=target_symbols_db)
        md_extractor = MarkdownSymbolExtractor(workspace_root=root)

        if os.getenv("KORTEXT_VERBOSE_PREAMBLE", "0").strip() in ("1", "true", "yes"):
            try:
                from kortext.chunking.token_utils import prefetch_git_temporal_info_batch

                prefetch_git_temporal_info_batch(root_path)
            except Exception as exc:
                logger.debug("Batch git temporal prefetch skipped: %s", exc)

        chunk_size = int(os.getenv("INGEST_CHUNK_SIZE", "2048"))
        chunk_overlap = int(os.getenv("INGEST_CHUNK_OVERLAP", "128"))
        parser = SmartCodeNodeParser(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

        chunk_cache = ChunkEmbeddingCache()
        batch_size = int(os.getenv("EMBED_BATCH_SIZE", "8"))
        concurrency = int(os.getenv("EMBED_CONCURRENCY", "1"))
        BATCH_DOC_COUNT = int(os.getenv("INGEST_BATCH_DOC_COUNT", "50"))
        sparse_model = get_sparse_model()

        total_indexed_chunks = 0
        total_decls = 0
        total_refs = 0
        total_docs = 0
        total_doc_links = 0
        total_batches = (len(docs_to_index) + BATCH_DOC_COUNT - 1) // BATCH_DOC_COUNT

        ast_splitters_by_ext: Dict[str, Any] = {}
        for batch_idx, batch_start in enumerate(range(0, len(docs_to_index), BATCH_DOC_COUNT)):
            batch_docs = docs_to_index[batch_start : batch_start + BATCH_DOC_COUNT]
            current_batch_num = batch_idx + 1

            # 1. AST symbol / Markdown extraction for this batch of documents
            for doc in batch_docs:
                fp = doc.metadata.get("file_path", "")
                ext = Path(fp).suffix.lower() if fp else ""
                doc.metadata["trust_tier"] = _resolve_trust_tier_for_extension(ext)
                if fp:
                    doc.metadata.setdefault("file_name", Path(fp).name)
                if ext in LANG_MAP:
                    try:
                        splitter = ast_splitters_by_ext.get(ext)
                        if splitter is None:
                            splitter = ASTCodeSplitter(language=LANG_MAP[ext])
                            ast_splitters_by_ext[ext] = splitter
                        source_bytes = doc.text.encode("utf-8")
                        tree = splitter.parser.parse(source_bytes) if hasattr(splitter, "parser") else None
                        res = splitter.extract_symbols_and_references(
                            doc.text, file_path=fp, tree=tree, source_bytes=source_bytes
                        )
                        symbol_graph.save_file_ast(fp, res["declarations"], res["imports"], res["references"])
                        total_decls += len(res["declarations"])
                        total_refs += len(res["references"])
                        doc.metadata["_tree"] = tree
                        doc.metadata["_source_bytes"] = source_bytes
                        doc.metadata["_ast_info"] = res
                    except Exception as e:
                        logger.warning(f"[Kortext Ingest] Warning extracting symbols for {fp}: {e}")
                elif ext in (".md", ".markdown"):
                    try:
                        md_res = md_extractor.extract(doc.text, file_path=fp)
                        symbol_graph.save_markdown_ast(
                            file_path=md_res["file_path"],
                            title=md_res["title"],
                            frontmatter=md_res["frontmatter"],
                            sections=md_res["sections"],
                            declarations=md_res["declarations"],
                            links=md_res["links"],
                            tags=md_res["tags"],
                        )
                        total_docs += 1
                        total_decls += len(md_res["declarations"])
                        total_doc_links += len(md_res["links"])
                    except Exception as e:
                        logger.warning(f"[Kortext Ingest] Warning extracting markdown structure for {fp}: {e}")

            if skip_vector_upsert:
                for doc in batch_docs:
                    fp = doc.metadata.get("file_path")
                    if fp:
                        resolved_fp = str(Path(fp).resolve())
                        if resolved_fp in files_to_reindex:
                            mtime, sha, old_pts = files_to_reindex[resolved_fp]
                            cache.record_file(resolved_fp, collection, mtime, sha, old_pts or [])
                continue

            # 2. Chunk nodes for this batch
            nodes = parser.get_nodes_from_documents(batch_docs, show_progress=False)

            if not nodes:
                for doc in batch_docs:
                    fp = doc.metadata.get("file_path")
                    if fp:
                        resolved_fp = str(Path(fp).resolve())
                        if resolved_fp in files_to_reindex:
                            mtime, sha, _ = files_to_reindex[resolved_fp]
                            cache.record_file(resolved_fp, collection, mtime, sha, [])
                logger.info(
                    f"[Kortext Ingest] Processed batch {current_batch_num}/{total_batches} "
                    f"({len(batch_docs)} files, 0 chunks)"
                )
                continue

            # 3. Lookup ChunkEmbeddingCache and embed misses in batches of EMBED_BATCH_SIZE (8)
            all_texts = [n.get_content() for n in nodes]
            cached_hits, cache_misses = chunk_cache.get_batch(all_texts, embed_model_name)

            for orig_idx, vec in cached_hits.items():
                nodes[orig_idx].embedding = vec

            if cache_misses:
                miss_batches = [cache_misses[i : i + batch_size] for i in range(0, len(cache_misses), batch_size)]
                total_miss_batches = len(miss_batches)
                sem = asyncio.Semaphore(concurrency)

                async def embed_batch(b_idx: int, miss_batch):
                    async with sem:
                        miss_texts = [item[1] for item in miss_batch]
                        try:
                            if hasattr(embedder, "aembed_documents") and callable(embedder.aembed_documents):
                                embeddings = await embedder.aembed_documents(miss_texts)
                            else:
                                embeddings = embedder.embed_documents(miss_texts)

                            new_cache_items = []
                            for (orig_idx, miss_text), emb in zip(miss_batch, embeddings):
                                nodes[orig_idx].embedding = emb
                                new_cache_items.append((miss_text, emb))
                            chunk_cache.put_batch(new_cache_items, embed_model_name)
                        except Exception as exc:
                            logger.error(
                                f"[Kortext Ingest] ⚠️  Batch {b_idx + 1}/{total_miss_batches} "
                                f"failed: {exc}. Skipping batch."
                            )

                await asyncio.gather(*[embed_batch(i, b) for i, b in enumerate(miss_batches)])

            # 4. Write valid nodes for this batch directly into Qdrant via client.upsert
            valid_nodes = [n for n in nodes if n.embedding is not None]
            batch_file_to_nodes: Dict[str, List[str]] = {}
            if valid_nodes:
                points, batch_file_to_nodes = _build_qdrant_points(
                    valid_nodes,
                    sparse_model=sparse_model,
                )
                if points:
                    await client.upsert(collection_name=collection, points=points)
                    total_indexed_chunks += len(points)

            # 5. Record file cache in IngestCache for this batch
            for doc in batch_docs:
                fp = doc.metadata.get("file_path")
                if fp:
                    resolved_fp = str(Path(fp).resolve())
                    if resolved_fp in files_to_reindex:
                        mtime, sha, _ = files_to_reindex[resolved_fp]
                        node_ids = batch_file_to_nodes.get(resolved_fp, [])
                        cache.record_file(resolved_fp, collection, mtime, sha, node_ids)

            logger.info(
                f"[Kortext Ingest] Processed batch {current_batch_num}/{total_batches} "
                f"({len(batch_docs)} files, {len(valid_nodes)} chunks)"
            )

        if total_decls > 0 or total_doc_links > 0 or total_docs > 0:
            msg_parts = []
            if total_decls > 0:
                msg_parts.append(f"{total_decls} symbols/sections")
            if total_refs > 0:
                msg_parts.append(f"{total_refs} code references")
            if total_docs > 0:
                msg_parts.append(f"{total_docs} markdown notes ({total_doc_links} links)")
            logger.info(
                f"[Kortext Ingest] 🕸️  Extracted {', '.join(msg_parts)} into FalkorDB ('{target_symbols_db}')."
            )

        logger.info(
            f"[Kortext Ingest] ✅ Indexed {total_indexed_chunks} chunks from {len(docs_to_index)} changed file(s) "
            f"into Qdrant collection '{collection}'."
        )
        try:
            from kortext.chunking.ast_splitter import clear_ast_cache

            clear_ast_cache()
        except Exception:
            pass


def run_cli_ingest(root: str, force: bool = False, **kwargs: Any) -> None:
    """CLI ingest. When the kortext daemon holds embedded Qdrant, the daemon does the indexing."""
    from kortext._env import is_qdrant_embedded_mode
    from kortext._qdrant import is_embedded_qdrant_locked

    if is_qdrant_embedded_mode() and is_embedded_qdrant_locked():
        from kortext import daemon

        args = {"path": os.path.abspath(root)}
        if kwargs.get("collection"):
            args["collection"] = kwargs["collection"]
        if kwargs.get("symbols_graph"):
            args["symbols_graph"] = kwargs["symbols_graph"]
        if kwargs.get("extra_excludes"):
            args["exclude_patterns"] = kwargs["extra_excludes"]
        if force:
            print("Note: --force is ignored when the daemon indexes; it re-indexes changed files only.")
        result = asyncio.run(daemon.call_tool("index_directory", args))
        print(result.content[0].text if result.content else result)
        return
    asyncio.run(ingest(root=root, force=force, **kwargs))


def main():
    parser = argparse.ArgumentParser(description="Kortext: batch ingest codebase into Qdrant & FalkorDB")
    parser.add_argument("--root", default=".", help="Root directory to ingest")
    parser.add_argument("--collection", default=COLLECTION, help="Target Qdrant collection name")
    parser.add_argument("--symbols-graph", default=None, help="Target FalkorDB symbol graph name")
    parser.add_argument("--exclude", nargs="*", default=[], help="Extra exclude patterns")
    parser.add_argument("--force", action="store_true", help="Force full re-index ignoring cache")
    args = parser.parse_args()

    run_cli_ingest(
        args.root,
        force=args.force,
        extra_excludes=args.exclude,
        collection=args.collection,
        symbols_graph=args.symbols_graph,
    )


if __name__ == "__main__":
    main()
