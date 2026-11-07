---
trigger: model_decision
description: "Activate when interacting with Qdrant, FalkorDB, Unix domain sockets, or debugging substrate connectivity."
---

# Invariant [Substrate-Resilience-Mandate]

## Core Rule
All Kortext operations MUST communicate with memory substrates exclusively through standardized IPC sockets and daemon custodians. Direct filesystem access to raw database files or concurrent embedded client instantiations are strictly prohibited.

## Substrate Protocols

```
┌────────────────────────────────────────────────────────┐
│               Agents & Lifecycle Hooks                 │
└──────────────┬──────────────────────────┬──────────────┘
               │                          │
        FastMCP Protocol           IPC Socket Protocol
               │                          │
               ▼                          ▼
 ┌──────────────────────────┐    ┌───────────────────┐
 │ FastMCP Server Daemon    │    │ FalkorDB Daemon   │
 │ (Sole Custodian)         │    │ (Graph Substrate) │
 └─────────────┬────────────┘    └─────────┬─────────┘
        ┌──────┴──────┐                    │
        ▼             ▼                    ▼
   [Qdrant]       [RocksDB]      [UDS / Loopback TCP]
  (Vectors)      (Key-Value)    (~/.kortext/run/*.sock)
```

### 1. FalkorDB Communication Protocol
- **Primary Transport:** Unix Domain Socket at `~/.kortext/run/falkordb.sock` for ultra-low latency (<1ms IPC).
- **Fallback Transport:** Loopback TCP socket at `127.0.0.1:6379`.
- **Prohibition:** Agents, tools, and hooks MUST NEVER inspect, open, or write directly to raw database files in `~/.kortext/data/falkordb/`.

### 2. FastMCP Sole Custodian Model
- **Sole Custodianship:** The FastMCP daemon (`kortext.server`) is the single, exclusive owner of on-disk vector stores (Qdrant) and key-value state (RocksDB).
- **Tool Mediation:** All vector queries, upserts, payload filtering, and memory indexing MUST flow through FastMCP server tools (`search_knowledge_and_memory`, `record_system_outcome`, `list_vector_collections`). When calling `record_system_outcome`, agents should pass `workspace_root`, the absolute path of the current workspace, so memory lands in the project.
- **Direct Client Prohibition:** Subagents, background sidecars, and lifecycle hooks MUST NEVER instantiate `QdrantClient(path=...)` directly in embedded mode.
- **Lock Contention Rationale:** Embedded Qdrant uses SQLite and RocksDB metadata backends that acquire exclusive advisory file locks (`LOCK_EX`). If an external process or subagent attempts to instantiate `QdrantClient(path=...)` while the server daemon is running, the operating system raises `sqlite3.OperationalError: database is locked`, crashes the execution worker, and risks corrupting vector index segments.

### 3. Health Verification & Graceful Degradation
- **Health Checks:** Validate substrate status via `check_kortext_health()` before executing bulk memory operations.
- **Offline Substrate Handling:**
  - If FalkorDB or oMLX are offline, log a clear diagnostic warning indicating the offline port/socket.
  - Advise running `kortext start` or checking the LaunchAgent daemon status.
  - Subsystems must degrade gracefully to in-memory fallback without throwing unhandled exceptions or halting agent execution.

