# Kortext Troubleshooting Guide

This guide covers diagnostic procedures and recovery steps for common failure modes in Kortext.

## 1. Initial Diagnostic: `kortext doctor`

Always start troubleshooting by running the diagnostic tool:

```bash
uv run kortext doctor
```

For programmatic inspection or scripts, use the JSON format:

```bash
uv run kortext doctor --json
```

`kortext doctor` performs 10 health checks in under 5 seconds:
1. Python version (verifies 3.11+)
2. Substrate binaries present and SHA-256 verified
3. FalkorDB answering over Unix socket or TCP
4. Qdrant reachable via HTTP or locked by the active daemon
5. FastMCP daemon health and token file permissions
6. Plugin installation and lifecycle hooks enabled
7. FastEmbed model cached locally
8. Current workspace index freshness and staleness
9. Failed re-index events in `spool/failed/`
10. Antigravity CLI (`agy`) binary availability

Each failing check outputs `[fail]` or `[warn]` alongside a specific remediation command.

---

## 2. Common Failure Modes and Solutions

### FalkorDB Offline or Not Answering

**Symptoms:**
- `kortext doctor` reports `[fail] FalkorDB is offline`.
- AST symbol lookup or episodic queries fail or fall back to emulator mode.

**Root Causes:**
- Native FalkorDB process stopped or crashed.
- Unix domain socket file `~/.kortext/run/falkordb.sock` is missing or has incorrect permissions.
- Stale PID file preventing startup.

**Resolution:**
1. Start substrate services:
   ```bash
   uv run kortext start
   ```
2. Check FalkorDB logs for error messages:
   ```bash
   cat ~/.kortext/logs/falkordb.log
   ```
3. If the process is hung or socket is orphaned:
   ```bash
   uv run kortext stop
   rm -f ~/.kortext/run/falkordb.sock
   uv run kortext start
   ```
4. Verify connectivity:
   ```bash
   uv run kortext status
   ```

---

### Embedded Qdrant Database Lock Collisions

**Symptoms:**
- Errors containing `sqlite3.OperationalError: database is locked`.
- Multiple editor windows failing to query vector collections.

**Root Causes:**
- Embedded Qdrant (`~/.kortext/qdrant/`) uses SQLite and RocksDB metadata stores that require exclusive file locks (`LOCK_EX`).
- Multiple processes attempting to open `QdrantClient(path=...)` directly will collide.

**Resolution:**
1. Use the FastMCP Sole Custodian daemon. Start the server daemon:
   ```bash
   uv run kortext server
   ```
   All editor windows and client tools connect to the daemon over HTTP on `127.0.0.1:8767` rather than opening raw storage files directly.
2. If an orphaned process holds the file lock:
   ```bash
   pkill -f "qdrant" || true
   pkill -f "kortext.*server" || true
   rm -f ~/.kortext/qdrant/.lock
   uv run kortext start
   ```
3. Alternatively, run Qdrant in server mode and configure `QDRANT_URL=http://127.0.0.1:6333`.

---

### Daemon Bearer Token File Permissions

**Symptoms:**
- `kortext doctor` reports `[fail] Daemon token file permissions too open (... instead of 0600)`.
- FastMCP proxy fails to authenticate with the background daemon.

**Root Causes:**
- The daemon writes its authentication token to `~/.kortext/run/daemon.token`.
- On POSIX platforms, file permissions must restrict read and write access strictly to the current user (`0600`). If permissions permit group or world access, the doctor check fails.

**Resolution:**
Restrict permissions to the current owner:
```bash
chmod 600 ~/.kortext/run/daemon.token
```

---

### Failed Re-index Events in `spool/failed/`

**Symptoms:**
- `kortext doctor` reports `[warn] N failed re-index event(s) in spool/failed/`.
- File modifications in watched directories are not reflected in symbol or vector indices.

**Root Causes:**
- File watcher spools change events to `~/.kortext/spool/invalidation/`.
- The daemon retries failed ingest events up to 5 times with exponential backoff (10s, 20s, 40s, 80s).
- After 5 consecutive failures (e.g. unparseable syntax, locked files, or out-of-memory errors), events move to `~/.kortext/spool/failed/`.

**Resolution:**
1. Inspect the failed event files to identify the failing source files:
   ```bash
   cat ~/.kortext/spool/failed/*.json
   ```
2. Resolve the underlying file issue (fix syntax errors or update exclusions).
3. Re-queue the failed events for indexing:
   ```bash
   mv ~/.kortext/spool/failed/* ~/.kortext/spool/invalidation/
   ```
4. Check that the queue processes cleanly:
   ```bash
   uv run kortext doctor
   ```

---

### Plugin Lifecycle Hooks Disabled or Missing

**Symptoms:**
- `kortext doctor` reports `[fail] Plugin hooks manifest (hooks.json) not found` or `Plugin hooks disabled`.
- AntiRipgrepGate, GateGuard, or token tracking does not activate in the Antigravity CLI.

**Root Causes:**
- Plugin was not installed to `~/.gemini/config/plugins/kortext/`.
- Individual hooks in `hooks.json` have `"enabled": false`.

**Resolution:**
1. Reinstall the plugin and default hook configurations:
   ```bash
   uv run kortext install
   ```
2. Confirm that `~/.gemini/config/plugins/kortext/hooks.json` exists and that required pipelines (`PreToolUse`, `PostToolUse`, `PreInvoke`, `SessionStart`, `Stop`) are enabled.

---

### Fallback to Emulator Mode

**Symptoms:**
- Cypher queries return fixed mock symbols or fail on multi-hop traversals.
- Graph analysis reports synthetic node identifiers.

**Root Causes:**
- When real FalkorDB is unreachable on Unix socket `~/.kortext/run/falkordb.sock` and TCP `127.0.0.1:6379`, test suites and offline CLI helpers fall back to the regex-based `FalkorDBEmulator`.
- The emulator provides static mock symbols for testing but cannot execute live Cypher queries, graph community detection, or recursive impact traversals.

**Resolution:**
1. Check if FalkorDB is running:
   ```bash
   uv run kortext doctor
   ```
2. Start the real FalkorDB instance:
   ```bash
   uv run kortext start
   ```
3. Re-index the workspace into the live graph:
   ```bash
   uv run kortext index
   ```
