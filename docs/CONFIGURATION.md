# Kortext Configuration Reference

This document lists every environment variable read by Kortext. Variables are resolved through `kortext._env.py`, which checks `KORTEXT_<NAME>` first, followed by `<NAME>`.

## Substrate Services

| Variable | Default | Description |
|---|---|---|
| `FALKORDB_SOCKET` | `~/.kortext/run/falkordb.sock` | Path to FalkorDB Unix domain socket. Set to `none` or `disabled` to force TCP. |
| `FALKORDB_HOST` | `127.0.0.1` | Hostname or IP address for FalkorDB TCP connections. |
| `FALKORDB_PORT` | `6379` | TCP port for FalkorDB connections. |
| `FALKORDB_PASSWORD` | empty | Authentication password for FalkorDB if required. |
| `FALKORDB_DATABASE` (or `KORTEXT_DATABASE`, `DATABASE`) | `kortext` | Name of the FalkorDB property graph used for episodic decisions and tasks. |
| `FALKORDB_SYMBOLS_GRAPH` (or `KORTEXT_SYMBOLS_GRAPH`, `SYMBOLS_GRAPH`, `SYMBOLS_GRAPH_NAME`) | `kortext_symbols` | Name of the FalkorDB property graph used for AST symbols, declarations, and call edges. |
| `QDRANT_URL` | `http://localhost:6333` | HTTP endpoint URL for remote Qdrant instances. If omitted or set to empty, embedded mode is used. |
| `QDRANT_MODE` (or `KORTEXT_QDRANT_MODE`) | empty | Qdrant connection mode: `embedded`, `local`, `in-process`, or `remote`. |
| `QDRANT_COLLECTION` (or `KORTEXT_COLLECTION`, `COLLECTION`, `COLLECTION_NAME`) | `kortext_codebase` | Default Qdrant collection name for vector embeddings. |
| `KORTEXT_ALLOW_COLLECTION_RESET` | `0` | If set to `1`, permits wiping and recreating collections during ingestion. |

## Embeddings and Models

| Variable | Default | Description |
|---|---|---|
| `EMBED_BASE_URL` | empty | Base URL for OpenAI-compatible embedding endpoints (e.g., vLLM, TEI, Ollama). If unset, defaults to local FastEmbed ONNX. |
| `EMBED_MODEL_NAME` | `BAAI/bge-small-en-v1.5` (local) or `mlx-community--Qwen3-Embedding-8B-4bit-DWQ` (remote) | Model identifier used for dense vector embeddings. |
| `EMBED_DIMENSION` (or `EMBED_DIMENSIONS`) | `384` (local) or `4096` (remote) | Vector dimension of the embedding model. |
| `EMBED_API_KEY` (or `OPENAI_API_KEY`) | empty | Bearer API key for remote OpenAI-compatible embedding endpoints. |
| `EMBED_BATCH_SIZE` | `8` | Number of text chunks embedded in a single batch during ingestion. |
| `EMBED_CONCURRENCY` | `1` | Number of parallel worker threads submitting embedding requests. |
| `FASTEMBED_CACHE_PATH` | `~/.cache/fastembed` | Local directory for cached FastEmbed ONNX model weights. |

## Storage and Paths

| Variable | Default | Description |
|---|---|---|
| `KORTEXT_HOME` (or `PLUGIN_DATA`) | `~/.kortext` | Base root directory for Kortext runtime data, binaries, sockets, and logs. |
| `JOURNAL_DIR` | `<workspace>/.kortext/memory/` or `~/.kortext/journal/<project>/` | Directory for Markdown daily journals and session logs. |
| `WORKSPACE_ROOTS` | Current working directory | Comma-separated list of root directories for multi-project resolution. |
| `KORTEXT_WORKSPACE_ROOT` | Current working directory | Explicit override for active workspace path. |
| `KORTEXT_CACHE_DB` (or `CACHE_DB`) | `~/.kortext/cache/ingest_cache.db` | SQLite database storing file content hashes and parse metadata. |
| `KORTEXT_PROJECT` (or `PROJECT`, `KORTEXT_PROJECT_ID`) | Directory basename or `default` | Project identifier for scoping tasks, journals, and episodic facts. |

## FastMCP Daemon and Server

| Variable | Default | Description |
|---|---|---|
| `KORTEXT_DAEMON_PORT` | `8767` | Port for the background FastMCP daemon on `127.0.0.1`. |
| `KORTEXT_SERVER_MODE` | empty | Set to `inprocess` to run FastMCP tools inside the client process without background daemon proxying. |
| `KORTEXT_ALLOW_UNVERIFIED_BINARY` | `0` | If set to `1`, bypasses SHA-256 integrity verification during substrate binary downloads. |
| `KORTEXT_FALKORDB_VERSION` | `v4.22.0` | Pinned version string for FalkorDB binary downloads. |
| `KORTEXT_QDRANT_VERSION` | `v1.13.4` | Pinned version string for Qdrant binary downloads. |

## Ingestion and Chunking

| Variable | Default | Description |
|---|---|---|
| `INGEST_CHUNK_SIZE` | `2048` | Maximum character length per code chunk. |
| `INGEST_CHUNK_OVERLAP` | `128` | Character overlap between consecutive chunks. |
| `INGEST_BATCH_DOC_COUNT` | `50` | Number of documents batched per Qdrant point upsert call. |
| `INGEST_EXCLUDE_PATTERNS` | Standard VCS, vendor, and build artifact exclusions | Comma-separated glob patterns ignored during file tree walking. |
| `MAX_FILE_SIZE_BYTES` | `2097152` (2 MB) | Maximum size of files processed during batch indexing. |
| `KORTEXT_VERBOSE_PREAMBLE` | `0` | If set to `1`, prepends verbose structural headers to code chunks. |

## Retrieval and Ranking

| Variable | Default | Description |
|---|---|---|
| `RETRIEVE_TOP_K` | `30` | Candidate pool size retrieved from Qdrant before fusion. |
| `RETRIEVE_RESULTS` | `5` | Maximum number of hybrid chunks returned to caller. |
| `KORTEXT_EPISODIC_MIN_SCORE` | `0.42` | Dense similarity threshold for episodic memory. Queries scoring below this value return no facts. |
| `KORTEXT_EPISODIC_MIN_LEXICAL` | `0.25` | Lexical match threshold used as fallback when embeddings are unavailable. |
| `CI_MODE` (or `KORTEXT_CI_MODE`) | empty | Disables local workspace heuristics when running under automated CI. |

## Dashboard

| Variable | Default | Description |
|---|---|---|
| `DASHBOARD_HOST` | `127.0.0.1` | Bind address for the FastAPI web dashboard. |
| `DASHBOARD_PORT` | `8004` | Listening port for the FastAPI web dashboard. |

## Watcher and Spool

| Variable | Default | Description |
|---|---|---|
| `KORTEXT_WATCH_DEBOUNCE` | `2.0` | Debounce delay in seconds for batching file change events. |
| `KORTEXT_WATCHER_STATUS` | `~/.kortext/watcher_status.json` | Path to file recording active watcher heartbeat and statistics. |

## Lifecycle Hooks and Guards

| Variable | Default | Description |
|---|---|---|
| `KORTEXT_HOOK_PROFILE` (or `ECC_HOOK_PROFILE`) | `standard` | Active hook interceptor profile: `minimal`, `standard`, `strict`, or `disabled`. |
| `KORTEXT_ALLOW_0_MATCH_GREP` | `0` | If set to `1`, permits ripgrep queries that match zero results instead of denying. |
| `KORTEXT_AUTO_RECORD_MODIFIED_FILES` | `1` | Automatically records modified files in session daily journals upon turn completion. |
| `KORTEXT_BLOCK_PROMPT_SECRETS` | `1` | Blocks prompts containing detected secrets before submission to tools. |
| `KORTEXT_GATEGUARD_ENFORCE` | `0` | Enforces first-touch blast radius checks before modifying high-in-degree files. |
| `KORTEXT_OUTCOME_GUARD_ENFORCE` | `0` | Requires recording a system outcome before completing tasks with significant code changes. |
| `KORTEXT_TEST_GATE_ENFORCE` | `0` | Enforces running test suites before marking tasks complete. |
| `KORTEXT_INJECT_INVARIANTS` | `0` | Injects grounded architectural invariants directly into agent prompts. |
| `KORTEXT_INTEGRITY_MODE` | empty | Enforces cryptographic HMAC validation on inter-agent handoff batons. |
| `KORTEXT_LOAD_MEMORY_INDEX` | `1` | Injects the 40-line `INDEX.md` memory vault summary into session start context. |
| `KORTEXT_CONTEXT_WINDOW_TOKENS` (or `ECC_CONTEXT_WINDOW_TOKENS`) | Model-specific context window | Overrides detected token window size for 25% compaction threshold warnings. |
| `KORTEXT_ACTIVE_AGENT` | empty | Name of the currently active subagent persona for write-confinement policy checks. |
| `KORTEXT_TASK_ID` | empty | Active task identifier for file lease and ownership checks. |

## Task DAG and Review Orchestration

| Variable | Default | Description |
|---|---|---|
| `KORTEXT_ENFORCE_REVIEW_VERDICT` | `0` | If set to `1`, prevents closing tasks without an explicit PASS verdict from a reviewer agent. |
| `KORTEXT_REVIEW_COMPLEXITY_THRESHOLD` | `0` | Task complexity score above which two-stage review is required. |
| `KORTEXT_VERIFY_TEST_PROOF` | `1` (`0` in pytest) | Requires valid test execution proof before completing tasks or promoting outcomes to `tool_confirmed`. |
| `KORTEXT_ENFORCE_WORKTREE_ISOLATION` | `0` | Rejects task claims on files with active overlaps in another session. |
