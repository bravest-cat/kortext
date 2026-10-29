---
name: kortext-rag
description: Use when querying Kortext 5-tier memory, onboarding to an unfamiliar codebase, researching existing libraries/tools before coding (search-first), re-indexing workspaces, or diagnosing Qdrant/FalkorDB/oMLX health.
---

# Kortext 5-Tier Memory, Search-First & Codebase Onboarding

Governs contextual retrieval across the 5-signal memory substrate, search-before-coding (`search-first`), systematic codebase onboarding, and substrate diagnostics.

## 1. What is indexed where

| Store | Holds | Tool | Best for |
|---|---|---|---|
| FalkorDB `kortext_symbols` | Tree-sitter symbols, files, CALLS/IMPORTS edges | `seek_symbol`, `trace_symbol_impact` | Exact definitions, line spans, callers and callees |
| FalkorDB `kortext` | Episodic facts and decisions, valid-time tracked | `search_knowledge_and_memory` | Prior decisions, bug root causes, task outcomes |
| Qdrant (dense + BM25) | Code and doc chunks | `search_knowledge_and_memory` | Concepts, docstrings, error messages |

`search_knowledge_and_memory` fuses symbol hits, BM25, dense vectors and current facts with weighted RRF.

## 2. Iterative Retrieval & Context Packing

1. **Triage Signal First**: Known identifier $\rightarrow$ `seek_symbol`. Concept, prior decision, or unknown symbol $\rightarrow$ `search_knowledge_and_memory(query="...", collection="kortext_codebase")`.
2. **Reformulate if Sparse**: Replace vague phrasing (*"how memory works"*) with domain anchors (*"FalkorDB bi-temporal valid_until supersession"*). Scope with `collection="kortext"` for episodic facts or `collection="kortext_codebase"` for source chunks.
3. **Respect MMR & Knapsack Ceilings**: Keep single retrieval payloads $\le 1,500$ tokens ($\lambda = 0.7$ MMR diversity). Read only bounded line ranges via `view_file(StartLine, EndLine)`.

## 3. Search-First Protocol (Research Before Coding)

Before writing any new utility, helper, parser, or integration, execute this 4-step check to avoid reinventing existing solutions:

| Step | Channel | Verification Check | Decision Rule |
|---|---|---|---|
| **1. Local AST & Memory** | Repo symbols & past decisions | `seek_symbol(symbol_name="...")` & `search_knowledge_and_memory(query="...")` | **Reuse** existing internal module or helper directly |
| **2. Active MCP Tools** | Connected MCP server schemas | Inspect registered tool declarations | **Adopt** native MCP tool instead of custom scripts |
| **3. Ecosystem Packages** | `pyproject.toml` / `package.json` / PyPI / npm | Check installed deps (`httpx`, `pydantic`, `tenacity`, `ruff`) | **Adopt** (exact match), **Extend** (thin wrapper), or **Compose** |
| **4. Net-New Build** | Custom implementation | Only when Steps 1–3 confirm no viable solution exists | **Build** minimal, immutable, tested implementation |

For deep multi-package evaluations, dispatch `kortext-researcher` to return an Adopt/Extend/Compose/Build comparison matrix via an artifact-first handoff (`~/.kortext/handoffs/research_<topic>.json`).

## 4. 4-Phase Codebase Onboarding Workflow

When onboarding to a repository or generating/updating `AGENTS.md` / `CLAUDE.md`:

1. **Phase 1 — Reconnaissance (Zero Ripgrep)**: Inspect manifests (`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`), locate entry points via `seek_symbol(symbol_name="main" | "create_app" | "server")`, and check top-2 directory levels (`list_dir`) and test configs (`pytest.ini`, `vitest.config.*`).
2. **Phase 2 — Architecture & Data Flow Mapping**: Query episodic memory (`search_knowledge_and_memory(query="architecture overview and core subsystems")`) and trace one request end-to-end from ingress router/tool through validation and domain logic to storage using `trace_symbol_impact(..., direction="downstream")`.
3. **Phase 3 — Convention Detection**: Identify naming patterns, immutability/typing conventions, error handling (`Result` types vs custom exceptions), and test layout.
4. **Phase 4 — Artifact Generation**: Emit a scannable `<100`-line Onboarding Guide (Tech Stack table, Key Entry Points, Request Lifecycle, Common Commands, "Where to Look" table) or update `AGENTS.md` without overwriting existing project invariants.

## 5. Substrate Diagnostics & Indexing Runbook

Run `check_kortext_health()` whenever retrieval returns empty or times out:

| Substrate | Healthy State | Failure Symptom | Immediate Recovery & Degraded Fallback |
|---|---|---|---|
| **FalkorDB** | `~/.kortext/run/falkordb.sock` or `127.0.0.1:6379` | `ConnectionRefusedError` | Automatic TCP fallback (`127.0.0.1:6379`); restart via `brew services restart falkordb`. |
| **Qdrant** | HTTP 200 `/healthz` (`127.0.0.1:6333`) | `Storage locked` / `502` | Enforce Sole Custodian (`kortext.server` only). Fall back to Tier 0 `seek_symbol` while Qdrant recovers. |
| **Embeddings** | Local FastEmbed or oMLX `:8000/v1/models` | `Connection timeout` | Automatic fallback to Tier 0 AST + BM25 sparse search until embedding provider recovers. |

- **Re-index workspace**: `index_directory(path="/abs/path/to/repo", collection="kortext_codebase", auto_dream=True)`
- **Supersede stale fact**: `delete_or_supersede_fact(fact_id="...", reason="...", superseded_by="...")`
