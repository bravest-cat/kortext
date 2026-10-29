"""
Unit tests verifying incremental re-indexing behavior in kortext/ingest.py.
Only modified or new files should have their AST symbols and markdown structure processed.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from kortext.ingest import ingest


@pytest.fixture(autouse=True)
def mock_sparse_embedding_model(monkeypatch):
    """Mocks get_sparse_model across incremental ingest tests for deterministic offline execution."""
    mock_model = MagicMock()

    def fake_embed(texts):
        results = []
        for _ in texts:
            emb = MagicMock()
            emb.indices = [101, 202]
            emb.values = [1.5, 0.8]
            results.append(emb)
        return results

    mock_model.embed.side_effect = fake_embed
    monkeypatch.setattr("kortext.ingest.get_sparse_model", lambda: mock_model)
    return mock_model


@pytest.mark.anyio
async def test_ingest_incremental_only_indexes_changed_files_into_falkordb(tmp_path):
    unchanged_path = str(tmp_path / "unchanged.py")
    changed_path = str(tmp_path / "changed.py")

    doc_unchanged = MagicMock()
    doc_unchanged.metadata = {"file_path": unchanged_path}
    doc_unchanged.text = "def foo(): pass"

    doc_changed = MagicMock()
    doc_changed.metadata = {"file_path": changed_path}
    doc_changed.text = "def bar(): pass"

    mock_docs = [doc_unchanged, doc_changed]

    mock_reader = MagicMock()
    mock_reader.load_data.return_value = mock_docs

    mock_client = AsyncMock()
    mock_collections_obj = MagicMock()
    mock_collection_info = MagicMock()
    mock_collection_info.name = "test_collection"
    mock_collections_obj.collections = [mock_collection_info]
    mock_client.get_collections.return_value = mock_collections_obj

    col_details = MagicMock()
    col_details.config.params.sparse_vectors = {"text-sparse-new": MagicMock()}
    mock_client.get_collection.return_value = col_details

    mock_cache = MagicMock()
    mock_cache.purge_missing_files.return_value = []

    def fake_is_file_changed(resolved_fp, col):
        if "unchanged.py" in resolved_fp:
            return False, 100, "sha1", []
        return True, 200, "sha2", []

    mock_cache.is_file_changed.side_effect = fake_is_file_changed

    mock_symbol_graph = MagicMock()
    mock_ast_splitter = MagicMock()
    mock_ast_splitter.extract_symbols_and_references.return_value = {
        "declarations": [{"name": "bar", "kind": "function", "line": 1}],
        "imports": [],
        "references": [],
    }

    mock_parser = MagicMock()
    mock_parser.get_nodes_from_documents.return_value = []

    with patch("kortext.ingest.SimpleDirectoryReader", return_value=mock_reader), \
         patch("kortext.ingest.AsyncQdrantClient", return_value=mock_client), \
         patch("kortext.ingest.IngestCache", return_value=mock_cache), \
         patch("kortext.symbols.SymbolGraphManager", return_value=mock_symbol_graph), \
         patch("kortext.chunking.ast_splitter.ASTCodeSplitter", return_value=mock_ast_splitter), \
         patch("kortext.chunking.SmartCodeNodeParser", return_value=mock_parser):

        await ingest(root=str(tmp_path), collection="test_collection", force=False)

        # Assert: Symbol extraction should only have been called for changed.py, NOT unchanged.py
        call_fps = [
            call.kwargs.get("file_path") if "file_path" in call.kwargs else call.args[1]
            for call in mock_ast_splitter.extract_symbols_and_references.call_args_list
        ]
        assert changed_path in call_fps
        assert unchanged_path not in call_fps
        assert len(call_fps) == 1


@pytest.mark.anyio
async def test_index_single_file_indexes_python_ast_and_qdrant(tmp_path):
    from kortext.ingest import index_single_file

    test_file = tmp_path / "sample.py"
    test_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    mock_client = AsyncMock()
    mock_cache = MagicMock()
    mock_cache.is_file_changed.return_value = (True, 12345, "sha_sample", [])
    mock_chunk_cache = MagicMock()
    mock_chunk_cache.get_batch.return_value = ({0: [0.1] * 4096}, [])
    from kortext.chunking.node_parser import TextNode

    mock_node = TextNode(text="def hello():\n    return 'world'\n", id_="node_1")
    mock_parser = MagicMock()
    mock_parser.get_nodes_from_documents.return_value = [mock_node]

    mock_symbol_graph = MagicMock()
    mock_ast_splitter = MagicMock()
    mock_ast_splitter.extract_symbols_and_references.return_value = {
        "declarations": [{"name": "hello", "kind": "function", "line": 1}],
        "imports": [],
        "references": [],
    }

    with patch("kortext.chunking.ast_splitter.ASTCodeSplitter", return_value=mock_ast_splitter), \
         patch("kortext.chunking.SmartCodeNodeParser", return_value=mock_parser):
        res = await index_single_file(
            file_path=test_file,
            collection="test_col",
            target_symbols_db="test_sym",
            force=False,
            client=mock_client,
            cache=mock_cache,
            chunk_cache=mock_chunk_cache,
            symbol_mgr=mock_symbol_graph,
        )

    assert res["status"] == "indexed"
    assert res["file_path"] == str(test_file.resolve())
    assert res["symbols"] == 1
    assert res["chunks"] == 1
    assert mock_symbol_graph.save_file_ast.called
    assert mock_client.upsert.called
    upsert_points = mock_client.upsert.call_args.kwargs["points"]
    assert len(upsert_points) == 1
    assert "text-dense" in upsert_points[0].vector
    assert "text-sparse-new" in upsert_points[0].vector
    assert mock_cache.record_file.called


@pytest.mark.anyio
async def test_index_single_file_skips_unchanged_file(tmp_path):
    from kortext.ingest import index_single_file

    test_file = tmp_path / "sample.py"
    test_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    mock_client = AsyncMock()
    mock_cache = MagicMock()
    mock_cache.is_file_changed.return_value = (False, 12345, "sha_sample", ["point_1"])
    mock_symbol_graph = MagicMock()

    res = await index_single_file(
        file_path=test_file,
        collection="test_col",
        target_symbols_db="test_sym",
        force=False,
        client=mock_client,
        cache=mock_cache,
        symbol_mgr=mock_symbol_graph,
    )

    assert res["status"] == "skipped"
    assert res["chunks"] == 0
    assert not mock_client.upsert.called
    assert not mock_symbol_graph.save_file_ast.called


@pytest.mark.anyio
async def test_watcher_sync_file_delegates_to_index_single_file(tmp_path):
    from kortext.watcher import KortextIncrementalSync

    test_file = tmp_path / "test.py"
    test_file.write_text("x = 1\n", encoding="utf-8")

    syncer = KortextIncrementalSync(collection="test_col")

    with patch("kortext.watcher.index_single_file", new_callable=AsyncMock) as mock_index:
        mock_index.return_value = {
            "status": "indexed",
            "file_path": str(test_file.resolve()),
            "chunks": 1,
            "cached": 1,
            "misses": 0,
        }

        result = await syncer.sync_file(str(test_file))
        assert result is True
        assert mock_index.called
        assert hasattr(syncer, "_process_file_event")
        assert syncer._process_file_event == syncer.sync_file


@pytest.mark.anyio
async def test_index_single_file_indexes_markdown_structure(tmp_path):
    from kortext.ingest import index_single_file
    from kortext.chunking.node_parser import TextNode

    test_file = tmp_path / "notes.md"
    test_file.write_text("# Title\n\nSome notes with [link](other.md)\n", encoding="utf-8")

    mock_client = AsyncMock()
    mock_cache = MagicMock()
    mock_cache.is_file_changed.return_value = (True, 12345, "sha_md", [])
    mock_chunk_cache = MagicMock()
    mock_chunk_cache.get_batch.return_value = ({0: [0.2] * 4096}, [])

    mock_node = TextNode(text="# Title\nSome notes", id_="node_md_1")
    mock_parser = MagicMock()
    mock_parser.get_nodes_from_documents.return_value = [mock_node]

    mock_symbol_graph = MagicMock()
    mock_md_extractor = MagicMock()
    mock_md_extractor.extract.return_value = {
        "file_path": str(test_file.resolve()),
        "title": "Title",
        "frontmatter": {},
        "sections": ["Title"],
        "declarations": [{"name": "Title", "kind": "section", "line": 1}],
        "links": [{"target": "other.md"}],
        "tags": [],
    }

    with patch("kortext.symbols.MarkdownSymbolExtractor", return_value=mock_md_extractor), \
         patch("kortext.chunking.SmartCodeNodeParser", return_value=mock_parser):
        res = await index_single_file(
            file_path=test_file,
            collection="test_col",
            target_symbols_db="test_sym",
            force=False,
            client=mock_client,
            cache=mock_cache,
            chunk_cache=mock_chunk_cache,
            symbol_mgr=mock_symbol_graph,
        )

    assert res["status"] == "indexed"
    assert res["symbols"] == 1
    assert mock_symbol_graph.save_markdown_ast.called
    assert mock_client.upsert.called


@pytest.mark.anyio
async def test_watcher_module_process_file_event(tmp_path):
    from kortext.watcher import KortextIncrementalSync

    test_file = tmp_path / "test.py"
    test_file.write_text("y = 2\n", encoding="utf-8")

    with patch("kortext.watcher.index_single_file", new_callable=AsyncMock) as mock_index:
        mock_index.return_value = {
            "status": "indexed",
            "file_path": str(test_file.resolve()),
            "chunks": 1,
            "cached": 0,
            "misses": 1,
        }

        syncer = KortextIncrementalSync(collection="test_col")
        res = await syncer._process_file_event(test_file)
        assert res is True
        assert mock_index.called


def test_watcher_should_ignore_glob_and_directory_exclusions():
    from kortext.watcher import DebouncedChangeHandler, KortextIncrementalSync

    syncer = MagicMock(spec=KortextIncrementalSync)
    loop = MagicMock()
    handler = DebouncedChangeHandler(loop=loop, syncer=syncer)

    ignored_paths = [
        "app.min.js",
        "sub/dir/bundle.min.js",
        "foo.pyc",
        "nested/cache.pyc",
        "poetry.lock",
        "Pipfile.lock",
        "sub/bundle.js.map",
        ".git/config",
        "node_modules/pkg/index.js",
        "__pycache__/foo.cpython-312.pyc",
        ".venv/lib/python.py",
        "venv/bin/activate",
        "dist/app.js",
        "build/main.js",
        ".pytest_cache/cache.json",
        ".gemini/context.json",
    ]
    for path in ignored_paths:
        assert handler._should_ignore(path) is True, f"Expected {path} to be ignored"
        assert handler._should_ignore(Path(path)) is True, f"Expected Path({path}) to be ignored"

    not_ignored = [
        "src/app.js",
        "kortext/watcher.py",
        "tests/test_something.py",
        "components/Button.tsx",
    ]
    for path in not_ignored:
        assert handler._should_ignore(path) is False, f"Expected {path} NOT to be ignored"
        assert handler._should_ignore(Path(path)) is False, f"Expected Path({path}) NOT to be ignored"


def test_watcher_custom_exclude_dirs():
    from kortext.watcher import DebouncedChangeHandler, KortextIncrementalSync

    syncer = MagicMock(spec=KortextIncrementalSync)
    loop = MagicMock()
    handler = DebouncedChangeHandler(
        loop=loop,
        syncer=syncer,
        exclude_dirs=["custom_dir", "*.custom"],
    )

    assert handler._should_ignore("custom_dir/file.py") is True
    assert handler._should_ignore("file.custom") is True
    assert handler._should_ignore("src/file.py") is False


@pytest.mark.anyio
async def test_index_single_file_reuses_parsed_ast_without_redundant_parses(tmp_path):
    from kortext.ingest import index_single_file
    from kortext.chunking.ast_splitter import ASTCodeSplitter, clear_ast_cache

    clear_ast_cache()
    test_file = tmp_path / "single_parse.py"
    test_file.write_text("def compute(x, y):\n    return x + y\n", encoding="utf-8")

    mock_client = AsyncMock()
    mock_cache = MagicMock()
    mock_cache.is_file_changed.return_value = (True, 12345, "sha_parse", [])
    mock_chunk_cache = MagicMock()
    mock_chunk_cache.get_batch.return_value = ({0: [0.1] * 4096}, [])
    mock_symbol_graph = MagicMock()

    real_init = ASTCodeSplitter.__init__
    parse_counts = []

    class TrackedParser:
        def __init__(self, parser):
            self._parser = parser
            self.count = 0
            parse_counts.append(self)

        def parse(self, *args, **kwargs):
            self.count += 1
            return self._parser.parse(*args, **kwargs)

    def tracked_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        self.parser = TrackedParser(self.parser)

    with patch.object(ASTCodeSplitter, "__init__", tracked_init):
        res = await index_single_file(
            file_path=test_file,
            collection="test_col",
            target_symbols_db="test_sym",
            force=False,
            client=mock_client,
            cache=mock_cache,
            chunk_cache=mock_chunk_cache,
            symbol_mgr=mock_symbol_graph,
        )

    assert res["status"] == "indexed"
    assert res["symbols"] >= 1
    assert res["chunks"] >= 1
    total_parses = sum(tp.count for tp in parse_counts)
    # Total Tree-sitter parse calls across symbol extraction AND chunking must be exactly 1!
    assert total_parses == 1


def test_cache_wal_and_busy_timeout_pragmas(tmp_path):
    from kortext.cache import IngestCache, ChunkEmbeddingCache
    import sqlite3

    db_path = tmp_path / "test_cache.db"
    cache = IngestCache(db_path=db_path)

    # Verify journal mode is wal after _init_db
    with cache._get_connection() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        sync_mode = conn.execute("PRAGMA synchronous;").fetchone()[0]

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 30000
    assert sync_mode == 1

    chunk_db_path = tmp_path / "test_chunk_cache.db"
    chunk_cache = ChunkEmbeddingCache(db_path=chunk_db_path)

    with chunk_cache._get_connection() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        sync_mode = conn.execute("PRAGMA synchronous;").fetchone()[0]

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 30000
    assert sync_mode == 1

    # Verify _get_connection does not re-issue journal_mode=WAL
    recorded = []
    real_connect = sqlite3.connect

    class ProxyConn:
        def __init__(self, real):
            self._real = real
        def __getattr__(self, name):
            return getattr(self._real, name)
        def execute(self, sql, *args):
            recorded.append(sql)
            return self._real.execute(sql, *args)

    with patch("kortext.cache.sqlite3.connect", side_effect=lambda *a, **kw: ProxyConn(real_connect(*a, **kw))):
        conn = cache._get_connection()
        assert not any("journal_mode=WAL" in sql for sql in recorded)
        assert any("busy_timeout=30000" in sql for sql in recorded)
        assert any("synchronous=NORMAL" in sql for sql in recorded)
        conn.close()


def test_cache_concurrent_writes(tmp_path):
    from kortext.cache import IngestCache
    import concurrent.futures

    db_path = tmp_path / "concurrent_cache.db"
    cache = IngestCache(db_path=db_path)

    def write_worker(i):
        cache.record_file(
            file_path=f"/fake/path_{i}.py",
            collection="test_col",
            mtime=100.0 + i,
            sha256=f"hash_{i}",
            point_ids=[f"id_{i}"],
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(write_worker, i) for i in range(20)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    cached_files = cache.get_all_cached_files("test_col")
    assert len(cached_files) == 20


@pytest.mark.anyio
async def test_ingest_workspace_purges_deleted_files_from_falkordb_symbol_graph(tmp_path):
    mock_reader = MagicMock()

    mock_client = AsyncMock()
    mock_collections_obj = MagicMock()
    mock_collection_info = MagicMock()
    mock_collection_info.name = "test_collection"
    mock_collections_obj.collections = [mock_collection_info]
    mock_client.get_collections.return_value = mock_collections_obj

    col_details = MagicMock()
    col_details.config.params.sparse_vectors = {"text-sparse-new": MagicMock()}
    mock_client.get_collection.return_value = col_details

    mock_cache = MagicMock()
    deleted_file = "/workspace/deleted.py"
    mock_cache.purge_missing_files.return_value = [(deleted_file, ["pt_1", "pt_2"])]

    mock_symbol_graph = MagicMock()

    doc = MagicMock()
    doc.metadata = {"file_path": str(tmp_path / "existing.py")}
    doc.text = "x = 1"
    mock_reader.load_data.return_value = [doc]
    mock_cache.is_file_changed.return_value = (False, 100, "sha", [])

    with patch("kortext.ingest.SimpleDirectoryReader", return_value=mock_reader), \
         patch("kortext.ingest.AsyncQdrantClient", return_value=mock_client), \
         patch("kortext.ingest.IngestCache", return_value=mock_cache), \
         patch("kortext.symbols.SymbolGraphManager", return_value=mock_symbol_graph):

        await ingest(root=str(tmp_path), collection="test_collection", force=False)

        mock_symbol_graph.delete_file_ast.assert_called_once_with(deleted_file)


@pytest.mark.anyio
async def test_watcher_delete_file_calls_delete_file_ast(tmp_path):
    from kortext.watcher import KortextIncrementalSync

    syncer = KortextIncrementalSync(collection="test_collection")
    syncer.qdrant_client = AsyncMock()
    syncer.cache = MagicMock()
    syncer.cache.remove_file.return_value = ["point_1"]
    syncer.symbol_mgr = MagicMock()

    target_file = str(tmp_path / "deleted.py")
    res = await syncer.delete_file(target_file)
    assert res is True

    syncer.symbol_mgr.delete_file_ast.assert_called_once_with(str(Path(target_file).resolve()))


@pytest.mark.anyio
async def test_index_single_file_skips_large_files(tmp_path):
    from kortext.ingest import index_single_file

    large_file = tmp_path / "huge.txt"
    large_file.write_text("a" * 500, encoding="utf-8")

    with patch("kortext.ingest.MAX_FILE_SIZE_BYTES", 200):
        res = await index_single_file(large_file)
        assert res["status"] == "skipped"
        assert res.get("reason") == "file_too_large"
        assert res["chunks"] == 0
        assert res["changed"] is False


@pytest.mark.anyio
async def test_index_single_file_skips_binary_files(tmp_path):
    from kortext.ingest import index_single_file

    bin_file = tmp_path / "binary.dat"
    bin_file.write_bytes(b"ELF\x00\x01\x02\x03\x04")

    res = await index_single_file(bin_file)
    assert res["status"] == "skipped"
    assert res.get("reason") == "binary_file"
    assert res["chunks"] == 0
    assert res["changed"] is False


@pytest.mark.anyio
async def test_index_single_file_prevents_cache_poisoning_on_embedding_failure(tmp_path):
    import httpx
    from kortext.ingest import index_single_file
    from kortext.chunking.node_parser import TextNode

    test_file = tmp_path / "code.py"
    test_file.write_text("def test(): pass\n", encoding="utf-8")

    mock_client = AsyncMock()
    mock_cache = MagicMock()
    mock_cache.is_file_changed.return_value = (True, 100.0, "sha_test", [])

    mock_chunk_cache = MagicMock()
    mock_chunk_cache.get_batch.return_value = ({}, [(0, "def test(): pass\n")])

    mock_node = TextNode(text="def test(): pass\n", id_="node_1")
    mock_parser = MagicMock()
    mock_parser.get_nodes_from_documents.return_value = [mock_node]

    mock_symbol_mgr = MagicMock()

    with patch("kortext.chunking.SmartCodeNodeParser", return_value=mock_parser), \
         patch("kortext.embeddings.is_remote_embed_alive", return_value=True), \
         patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("Connection refused")):
        res = await index_single_file(
            test_file,
            client=mock_client,
            cache=mock_cache,
            chunk_cache=mock_chunk_cache,
            symbol_mgr=mock_symbol_mgr,
        )

        assert res["status"] == "error"
        assert "Embedding failed" in res["error"]
        mock_cache.record_file.assert_not_called()


@pytest.mark.anyio
async def test_index_workspace_skips_large_and_binary_files(tmp_path):
    large_file = tmp_path / "large.py"
    large_file.write_text("a" * 500, encoding="utf-8")

    binary_file = tmp_path / "bin.py"
    binary_file.write_bytes(b"hello\x00world")

    normal_file = tmp_path / "normal.py"
    normal_file.write_text("print('hello')", encoding="utf-8")

    doc_large = MagicMock()
    doc_large.metadata = {"file_path": str(large_file)}
    doc_large.text = "a" * 500

    doc_bin = MagicMock()
    doc_bin.metadata = {"file_path": str(binary_file)}
    doc_bin.text = "hello\x00world"

    doc_norm = MagicMock()
    doc_norm.metadata = {"file_path": str(normal_file)}
    doc_norm.text = "print('hello')"

    mock_reader = MagicMock()
    mock_reader.load_data.return_value = [doc_large, doc_bin, doc_norm]

    mock_client = AsyncMock()
    mock_collections_obj = MagicMock()
    mock_collection_info = MagicMock()
    mock_collection_info.name = "test_collection"
    mock_collections_obj.collections = [mock_collection_info]
    mock_client.get_collections.return_value = mock_collections_obj

    col_details = MagicMock()
    col_details.config.params.sparse_vectors = {"text-sparse-new": MagicMock()}
    mock_client.get_collection.return_value = col_details

    mock_cache = MagicMock()
    mock_cache.purge_missing_files.return_value = []
    mock_cache.is_file_changed.return_value = (True, 100, "sha", [])

    mock_symbol_graph = MagicMock()
    mock_ast_splitter = MagicMock()
    mock_ast_splitter.extract_symbols_and_references.return_value = {
        "declarations": [],
        "imports": [],
        "references": [],
    }

    mock_parser = MagicMock()
    mock_parser.get_nodes_from_documents.return_value = []

    with patch("kortext.ingest.SimpleDirectoryReader", return_value=mock_reader), \
         patch("kortext.ingest.AsyncQdrantClient", return_value=mock_client), \
         patch("kortext.ingest.IngestCache", return_value=mock_cache), \
         patch("kortext.symbols.SymbolGraphManager", return_value=mock_symbol_graph), \
         patch("kortext.chunking.ast_splitter.ASTCodeSplitter", return_value=mock_ast_splitter), \
         patch("kortext.chunking.SmartCodeNodeParser", return_value=mock_parser), \
         patch("kortext.ingest.MAX_FILE_SIZE_BYTES", 200):

        await ingest(root=str(tmp_path), collection="test_collection", force=False)

        call_fps = [
            call.kwargs.get("file_path") if "file_path" in call.kwargs else call.args[1]
            for call in mock_ast_splitter.extract_symbols_and_references.call_args_list
        ]
        assert str(normal_file.resolve()) in call_fps
        assert str(large_file.resolve()) not in call_fps
        assert str(binary_file.resolve()) not in call_fps


@pytest.mark.anyio
async def test_index_single_file_batches_embeddings_and_retries_transient_failure(tmp_path):
    import os
    import httpx
    from kortext.ingest import index_single_file
    from kortext.chunking.node_parser import TextNode

    test_file = tmp_path / "batch_test.py"
    test_file.write_text("print('test')\n", encoding="utf-8")

    mock_client = AsyncMock()
    mock_cache = MagicMock()
    mock_cache.is_file_changed.return_value = (True, 100.0, "sha_batch", [])

    misses = [(i, f"chunk_{i}") for i in range(5)]
    nodes = [TextNode(text=f"chunk_{i}", id_=f"node_{i}") for i in range(5)]

    mock_chunk_cache = MagicMock()
    mock_chunk_cache.get_batch.return_value = ({}, misses)

    mock_parser = MagicMock()
    mock_parser.get_nodes_from_documents.return_value = nodes

    mock_symbol_mgr = MagicMock()
    mock_ast_splitter = MagicMock()
    mock_ast_splitter.extract_symbols_and_references.return_value = {
        "declarations": [], "imports": [], "references": []
    }

    call_counts = {"attempts": 0}

    async def fake_post(url, **kwargs):
        call_counts["attempts"] += 1
        if call_counts["attempts"] == 1:
            raise httpx.RequestError("Temporary network hiccup")
        input_texts = kwargs.get("json", {}).get("input", [])
        return httpx.Response(
            status_code=200,
            json={"data": [{"embedding": [0.5] * 8} for _ in input_texts]},
            request=httpx.Request("POST", url),
        )

    with patch.dict(os.environ, {"EMBED_BATCH_SIZE": "2", "EMBED_CONCURRENCY": "2"}), \
         patch("kortext.chunking.ast_splitter.ASTCodeSplitter", return_value=mock_ast_splitter), \
         patch("kortext.chunking.SmartCodeNodeParser", return_value=mock_parser), \
         patch("kortext.embeddings.is_remote_embed_alive", return_value=True), \
         patch("httpx.AsyncClient.post", side_effect=fake_post):

        res = await index_single_file(
            test_file,
            client=mock_client,
            cache=mock_cache,
            chunk_cache=mock_chunk_cache,
            symbol_mgr=mock_symbol_mgr,
        )

        assert res["status"] == "indexed"
        assert res["chunks"] == 5
        assert call_counts["attempts"] == 4
        for n in nodes:
            assert n.embedding == [0.5] * 8
        assert mock_chunk_cache.put_batch.called
        assert mock_cache.record_file.called


@pytest.mark.anyio
async def test_index_single_file_passes_ast_info_to_document_metadata(tmp_path):
    from kortext.ingest import index_single_file
    from kortext.chunking.node_parser import TextNode

    test_file = tmp_path / "ast_meta_test.py"
    test_file.write_text("def sample(): pass\n", encoding="utf-8")

    mock_client = AsyncMock()
    mock_cache = MagicMock()
    mock_cache.is_file_changed.return_value = (True, 100.0, "sha_ast", [])

    mock_chunk_cache = MagicMock()
    mock_chunk_cache.get_batch.return_value = ({0: [0.1] * 8}, [])

    mock_node = TextNode(text="def sample(): pass\n", id_="n_1")
    captured_docs = []
    mock_parser = MagicMock()
    def fake_get_nodes(docs, **kwargs):
        captured_docs.extend(docs)
        return [mock_node]
    mock_parser.get_nodes_from_documents.side_effect = fake_get_nodes

    mock_symbol_mgr = MagicMock()

    with patch("kortext.chunking.SmartCodeNodeParser", return_value=mock_parser):
        res = await index_single_file(
            test_file,
            client=mock_client,
            cache=mock_cache,
            chunk_cache=mock_chunk_cache,
            symbol_mgr=mock_symbol_mgr,
        )

        assert res["status"] == "indexed"
        assert len(captured_docs) == 1
        doc = captured_docs[0]
        assert "_tree" in doc.metadata
        assert doc.metadata["_tree"] is not None
        assert "_source_bytes" in doc.metadata
        assert doc.metadata["_source_bytes"] == b"def sample(): pass\n"
        assert "_ast_info" in doc.metadata
        assert any(d.get("name") == "sample" for d in doc.metadata["_ast_info"].get("declarations", []))


@pytest.mark.anyio
async def test_index_workspace_streams_in_bounded_batches(tmp_path):
    from kortext.chunking.node_parser import TextNode
    from kortext.ingest import Document
    import os

    docs = [
        Document(text=f"def fn_{i}(): pass", metadata={"file_path": str(tmp_path / f"file_{i}.py")})
        for i in range(5)
    ]

    mock_reader = MagicMock()
    mock_reader.load_data.return_value = docs

    mock_client = AsyncMock()
    mock_collections_obj = MagicMock()
    mock_collection_info = MagicMock()
    mock_collection_info.name = "test_col"
    mock_collections_obj.collections = [mock_collection_info]
    mock_client.get_collections.return_value = mock_collections_obj

    col_details = MagicMock()
    col_details.config.params.sparse_vectors = {"text-sparse-new": MagicMock()}
    mock_client.get_collection.return_value = col_details

    mock_cache = MagicMock()
    mock_cache.purge_missing_files.return_value = []
    mock_cache.is_file_changed.return_value = (True, 100, "sha", [])

    mock_chunk_cache = MagicMock()
    mock_chunk_cache.get_batch.side_effect = lambda texts, model: ({i: [0.1] * 8 for i in range(len(texts))}, [])

    mock_symbol_graph = MagicMock()
    mock_ast_splitter = MagicMock()
    mock_ast_splitter.extract_symbols_and_references.return_value = {
        "declarations": [], "imports": [], "references": []
    }

    batch_calls = []
    mock_parser = MagicMock()
    def fake_get_nodes(batch_docs, **kwargs):
        batch_calls.append(len(batch_docs))
        return [TextNode(text=d.text, id_=f"node_{d.metadata['file_path']}") for d in batch_docs]
    mock_parser.get_nodes_from_documents.side_effect = fake_get_nodes

    mock_vector_index = MagicMock()

    with patch.dict(os.environ, {"INGEST_BATCH_DOC_COUNT": "2"}), \
         patch("kortext.ingest.SimpleDirectoryReader", return_value=mock_reader), \
         patch("kortext.ingest.AsyncQdrantClient", return_value=mock_client), \
         patch("kortext.ingest.IngestCache", return_value=mock_cache), \
         patch("kortext.ingest.ChunkEmbeddingCache", return_value=mock_chunk_cache), \
         patch("kortext.symbols.SymbolGraphManager", return_value=mock_symbol_graph), \
         patch("kortext.chunking.ast_splitter.ASTCodeSplitter", return_value=mock_ast_splitter), \
         patch("kortext.chunking.SmartCodeNodeParser", return_value=mock_parser), \
         patch("kortext.ingest.VectorStoreIndex", mock_vector_index):

        await ingest(root=str(tmp_path), collection="test_col", force=False)

    # BATCH_DOC_COUNT is 2, 5 documents -> batches should be [2, 2, 1]
    assert batch_calls == [2, 2, 1]
    assert mock_parser.get_nodes_from_documents.call_count == 3
    assert mock_client.upsert.call_count == 3
    assert mock_cache.record_file.call_count == 5


@pytest.mark.anyio
async def test_index_workspace_forwards_ast_metadata_to_parser(tmp_path):
    from kortext.chunking.node_parser import TextNode
    from kortext.ingest import Document

    test_file = tmp_path / "mod.py"
    test_file.write_text("def hello(): pass\n", encoding="utf-8")
    doc = Document(text="def hello(): pass\n", metadata={"file_path": str(test_file.resolve())})

    mock_reader = MagicMock()
    mock_reader.load_data.return_value = [doc]

    mock_client = AsyncMock()
    mock_collections_obj = MagicMock()
    mock_collection_info = MagicMock()
    mock_collection_info.name = "test_col"
    mock_collections_obj.collections = [mock_collection_info]
    mock_client.get_collections.return_value = mock_collections_obj

    col_details = MagicMock()
    col_details.config.params.sparse_vectors = {"text-sparse-new": MagicMock()}
    mock_client.get_collection.return_value = col_details

    mock_cache = MagicMock()
    mock_cache.purge_missing_files.return_value = []
    mock_cache.is_file_changed.return_value = (True, 100, "sha", [])

    mock_chunk_cache = MagicMock()
    mock_chunk_cache.get_batch.return_value = ({0: [0.1] * 8}, [])

    mock_symbol_graph = MagicMock()

    captured_batch_docs = []
    mock_parser = MagicMock()
    def fake_get_nodes(batch_docs, **kwargs):
        captured_batch_docs.extend(batch_docs)
        return [TextNode(text="def hello(): pass\n", id_="n_mod_1")]
    mock_parser.get_nodes_from_documents.side_effect = fake_get_nodes

    mock_vector_index = MagicMock()

    with patch("kortext.ingest.SimpleDirectoryReader", return_value=mock_reader), \
         patch("kortext.ingest.AsyncQdrantClient", return_value=mock_client), \
         patch("kortext.ingest.IngestCache", return_value=mock_cache), \
         patch("kortext.ingest.ChunkEmbeddingCache", return_value=mock_chunk_cache), \
         patch("kortext.symbols.SymbolGraphManager", return_value=mock_symbol_graph), \
         patch("kortext.chunking.SmartCodeNodeParser", return_value=mock_parser), \
         patch("kortext.ingest.VectorStoreIndex", mock_vector_index):

        await ingest(root=str(tmp_path), collection="test_col", force=False)

    assert len(captured_batch_docs) == 1
    passed_doc = captured_batch_docs[0]
    assert "_tree" in passed_doc.metadata
    assert passed_doc.metadata["_tree"] is not None
    assert "_source_bytes" in passed_doc.metadata
    assert passed_doc.metadata["_source_bytes"] == b"def hello(): pass\n"
    assert "_ast_info" in passed_doc.metadata
    assert any(d.get("name") == "hello" for d in passed_doc.metadata["_ast_info"].get("declarations", []))


@pytest.mark.anyio
async def test_index_single_file_handles_sparse_model_failure_gracefully(tmp_path, monkeypatch):
    """Verifies index_single_file still indexes dense vector when sparse model fails or is offline."""
    from kortext.ingest import index_single_file
    from kortext.chunking.node_parser import TextNode

    test_file = tmp_path / "offline_sparse.py"
    test_file.write_text("def offline_func(): pass\n", encoding="utf-8")

    mock_client = AsyncMock()
    mock_cache = MagicMock()
    mock_cache.is_file_changed.return_value = (True, 12345, "sha_offline", [])
    mock_chunk_cache = MagicMock()
    mock_chunk_cache.get_batch.return_value = ({0: [0.1] * 4096}, [])

    mock_node = TextNode(text="def offline_func(): pass\n", id_="node_off_1")
    mock_parser = MagicMock()
    mock_parser.get_nodes_from_documents.return_value = [mock_node]

    mock_symbol_graph = MagicMock()
    mock_ast_splitter = MagicMock()
    mock_ast_splitter.extract_symbols_and_references.return_value = {
        "declarations": [{"name": "offline_func", "kind": "function", "line": 1}],
        "imports": [],
        "references": [],
    }

    # Simulate get_sparse_model failing/offline
    def failing_sparse_model():
        raise RuntimeError("FastEmbed HuggingFace offline connection error")

    monkeypatch.setattr("kortext.ingest.get_sparse_model", failing_sparse_model)

    with patch("kortext.chunking.ast_splitter.ASTCodeSplitter", return_value=mock_ast_splitter), \
         patch("kortext.chunking.SmartCodeNodeParser", return_value=mock_parser):
        res = await index_single_file(
            file_path=test_file,
            collection="test_col",
            target_symbols_db="test_sym",
            force=False,
            client=mock_client,
            cache=mock_cache,
            chunk_cache=mock_chunk_cache,
            symbol_mgr=mock_symbol_graph,
        )

    assert res["status"] == "indexed"
    assert mock_client.upsert.called
    upsert_points = mock_client.upsert.call_args.kwargs["points"]
    assert len(upsert_points) == 1
    # Dense vector must still be present
    assert "text-dense" in upsert_points[0].vector
    # Sparse vector should be omitted gracefully without crashing
    assert "text-sparse-new" not in upsert_points[0].vector


@pytest.mark.anyio
async def test_index_single_file_embeds_misses_via_dense_embedder(tmp_path):
    """Verifies index_single_file uses get_dense_embedder() to embed misses and stores them with embedder.model_name."""
    from kortext.ingest import index_single_file
    from kortext.chunking.node_parser import TextNode

    test_file = tmp_path / "misses_embed.py"
    test_file.write_text("def miss_func(): return 42\n", encoding="utf-8")

    mock_client = AsyncMock()
    mock_cache = MagicMock()
    mock_cache.is_file_changed.return_value = (True, 12345, "sha_miss", [])
    mock_chunk_cache = MagicMock()
    # Simulate cache miss: cached_hits = {}, misses = [(0, "def miss_func(): return 42\n")]
    mock_chunk_cache.get_batch.return_value = ({}, [(0, "def miss_func(): return 42\n")])

    mock_node = TextNode(text="def miss_func(): return 42\n", id_="node_miss_1")
    mock_parser = MagicMock()
    mock_parser.get_nodes_from_documents.return_value = [mock_node]

    mock_symbol_graph = MagicMock()
    mock_ast_splitter = MagicMock()
    mock_ast_splitter.extract_symbols_and_references.return_value = {
        "declarations": [{"name": "miss_func", "kind": "function", "line": 1}],
        "imports": [],
        "references": [],
    }

    mock_embedder = MagicMock()
    mock_embedder.model_name = "test-model-fastembed"
    mock_embedder.dimension = 384
    mock_embedder.aembed_documents = AsyncMock(return_value=[[0.42] * 384])

    with patch("kortext.ingest.get_dense_embedder", return_value=mock_embedder) as mock_get_embedder, \
         patch("kortext.chunking.ast_splitter.ASTCodeSplitter", return_value=mock_ast_splitter), \
         patch("kortext.chunking.SmartCodeNodeParser", return_value=mock_parser):
        res = await index_single_file(
            file_path=test_file,
            collection="test_col",
            target_symbols_db="test_sym",
            force=False,
            client=mock_client,
            cache=mock_cache,
            chunk_cache=mock_chunk_cache,
            symbol_mgr=mock_symbol_graph,
        )

    assert res["status"] == "indexed"
    assert res["misses"] == 1
    mock_get_embedder.assert_called_once()
    mock_embedder.aembed_documents.assert_awaited_once_with(["def miss_func(): return 42\n"])
    # Verify cached in chunk_cache with embedder.model_name
    mock_chunk_cache.put_batch.assert_called_once()
    put_items, put_model = mock_chunk_cache.put_batch.call_args[0]
    assert put_model == "test-model-fastembed"
    assert len(put_items) == 1
    assert put_items[0] == ("def miss_func(): return 42\n", [0.42] * 384)
    # Verify node embedding assigned and upserted
    assert mock_node.embedding == [0.42] * 384
    assert mock_client.upsert.called


@pytest.mark.anyio
async def test_setup_collection_defaults_to_dense_embedder_dimension():
    """Verifies setup_collection uses embedder.dimension when creating collection and handles recreation."""
    from kortext.ingest import setup_collection
    import os

    mock_client = AsyncMock()
    mock_collections_obj = MagicMock()
    mock_collections_obj.collections = []
    mock_client.get_collections.return_value = mock_collections_obj

    mock_embedder = MagicMock()
    mock_embedder.dimension = 384

    # 1. New collection creation: defaults to embedder.dimension (384) when EMBED_DIMENSION is unset
    with patch("kortext.ingest.get_dense_embedder", return_value=mock_embedder), \
         patch.dict(os.environ, {}, clear=False):
        os.environ.pop("EMBED_DIMENSION", None)
        dim = await setup_collection(client=mock_client, collection="new_fastembed_col")
        assert dim == 384
        mock_client.create_collection.assert_awaited_once()
        create_kwargs = mock_client.create_collection.call_args.kwargs
        assert create_kwargs["collection_name"] == "new_fastembed_col"
        assert create_kwargs["vectors_config"]["text-dense"].size == 384

    # 2. Existing collection with different vector dimension triggers recreation
    mock_client.create_collection.reset_mock()
    mock_existing_col = MagicMock()
    mock_existing_col.name = "existing_col"
    mock_collections_obj.collections = [mock_existing_col]

    col_info = MagicMock()
    col_info.config.params.sparse_vectors = {"text-sparse-new": MagicMock()}
    dense_param = MagicMock()
    dense_param.size = 4096  # Old dimension was 4096, now target is 384
    col_info.config.params.vectors = {"text-dense": dense_param}
    mock_client.get_collection.return_value = col_info

    mock_cache = MagicMock()

    with patch("kortext.ingest.get_dense_embedder", return_value=mock_embedder), \
         patch.dict(os.environ, {}, clear=False):
        os.environ.pop("EMBED_DIMENSION", None)
        dim = await setup_collection(client=mock_client, collection="existing_col", cache=mock_cache)
        assert dim == 384
        # Should delete existing collection and clear cache
        mock_client.delete_collection.assert_awaited_once_with("existing_col")
        mock_cache.clear_collection.assert_called_once_with("existing_col")
        # Should recreate collection with new dimension 384
        mock_client.create_collection.assert_awaited_once()
        assert mock_client.create_collection.call_args.kwargs["vectors_config"]["text-dense"].size == 384



