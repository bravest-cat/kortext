<div align="center">
  <img src="assets/logo.svg" alt="Kortext Logo" width="160" height="160" />
  <h1>Kortext</h1>
</div>

Kortext provides local memory, symbol navigation, and code intelligence for the Antigravity CLI (agy). It parses source files into an AST symbol graph stored in FalkorDB, indexes text and code chunks into Qdrant vectors, tracks decisions in an episodic graph, and logs session history in Markdown files. During coding sessions, kortext supplies targeted symbol definitions, callers, callees, and project context directly to agents through FastMCP tools and lifecycle hooks, avoiding full-file dumps and repetitive searches.

## Requirements

- Python 3.11 to 3.13
- Antigravity CLI (agy)

## Installation

```bash
git clone https://github.com/bravest-cat/kortext
cd kortext
uv sync --extra tier2-languages
uv run kortext init
```

The first index of a repository this size can take several minutes.

`kortext init` runs non-interactive setup, installs plugin files to `~/.gemini/config/plugins/kortext`, starts native substrate services, and indexes the current directory.

## What You Get

- Symbol lookup: `seek_symbol` locates definitions, line spans, signatures, and direct callers and callees via Tree-sitter AST queries in FalkorDB.
- Impact tracing: `trace_symbol_impact` traverses upstream callers and downstream dependencies across the codebase with progressive depth limits.
- Search: `search_knowledge_and_memory` runs federated hybrid retrieval across AST symbols, Qdrant dense and BM25 vectors, episodic facts, and architecture outlines across 4 knowledge tiers.
- Memory: `record_system_outcome` and `delete_or_supersede_fact` record and supersede decisions in FalkorDB, while the memory vault maintains Markdown journals and session logs.
- Tasks: `manage_tasks` coordinates graph-backed task claiming, dependency resolution, and 5-pattern execution routing (`single_agent`, `subagents`, `parallel_specialist`, `builder_validator`, `agent_team`).
- Dashboard: web interface on port 8004 providing Overview metrics, AST symbol visualizers, a task Kanban board, and a Memory Inbox.
- Hooks: lifecycle interceptors that check tool invocations, enforce first-touch blast radius checks, block unindexed scans, fence unverified memories, and record token telemetry.

## Measured Results

The benchmark suite (`kortext benchmark --k 3`) compares kortext against two ripgrep baselines across 30 tasks on this repository.

| Tool / System | Pass@k | Pass^k | Recall@5 | MRR | Latency p50 | Latency p95 | Tokens Avg |
|---|---|---|---|---|---|---|---|
| **Overall** | | | | | | | |
| kortext | 0.90 | 0.90 | 0.82 | 0.73 | 9.27 ms | 225.61 ms | 1,549 |
| rg_naive | 1.00 | 1.00 | 0.52 | 0.31 | 5.83 ms | 9.95 ms | 99,515 |
| rg_informed | 1.00 | 1.00 | 0.74 | 0.46 | 5.65 ms | 6.96 ms | 3,272 |
| **seek_symbol** | | | | | | | |
| kortext | 1.00 | 1.00 | 1.00 | 1.00 | 6.25 ms | 9.82 ms | 583 |
| rg_naive | 1.00 | 1.00 | 0.90 | 0.60 | 5.68 ms | 5.85 ms | 541 |
| rg_informed | 1.00 | 1.00 | 0.90 | 0.60 | 5.52 ms | 6.62 ms | 541 |
| **trace_symbol_impact** | | | | | | | |
| kortext | 1.00 | 1.00 | 0.77 | 0.67 | 8.10 ms | 219.42 ms | 3,039 |
| rg_naive | 1.00 | 1.00 | 0.65 | 0.29 | 5.77 ms | 8.91 ms | 3,420 |
| rg_informed | 1.00 | 1.00 | 0.62 | 0.30 | 5.69 ms | 6.80 ms | 3,420 |
| **search_knowledge_and_memory** | | | | | | | |
| kortext | 0.70 | 0.70 | 0.70 | 0.52 | 23.26 ms | 227.93 ms | 1,024 |
| rg_naive | 1.00 | 1.00 | 0.00 | 0.04 | 7.33 ms | 10.96 ms | 294,585 |
| rg_informed | 1.00 | 1.00 | 0.70 | 0.48 | 5.99 ms | 7.06 ms | 5,857 |

Ripgrep reaches a higher pass rate (1.00) than kortext (0.90) because pass@k is a file containment check: any returned file matching the gold set counts as a pass, and an unbounded ripgrep dump contains almost everything. The naive baseline token count (averaging 294,585 tokens on behavioural queries) is unrealistically high because an agent would exhaust context limits. The informed baseline knows identifiers in advance and averages 5,857 tokens. Token savings on behavioural search therefore form a range between 5.7x against informed search and 287x against naive search.

On LongMemEval, kortext evaluates against 25 held-out items (with 25 calibration items used to select the 0.42 dense score threshold). Held-out results before vs after dense scoring:
- Information extraction: 0.83 to 1.00
- Knowledge updates: 0.60 to 0.80
- Temporal reasoning: 0.60 to 0.60
- Abstention: 0.00 to 1.00 (evaluated on 4 held-out abstention items)
- Multi-session top-1: 0.20 to 0.20
- Multi-session top-5: 0.20 to 0.20

Multi-session reasoning stays at 0.20 because 4 of the 5 held-out multi-session items have synthesized gold answers rather than verbatim text in the source facts.

For test selection, `kortext affected` inspected 5 recent commits on this repository:
- Selected 19 to 51 of 55 total test files per commit.
- Returned `complete: false` on all 5 commits because non-code files (documentation, lockfiles, or configuration) were modified.

The retrieval engine serves 4 knowledge tiers:
- Tier 0: AST Symbol Declarations
- Tier 1: Hybrid Codebase Chunks
- Tier 2: Episodic History and Decisions
- Tier 3: System Architecture Overview

The dashboard status monitors 5 runtime subsystems (`tier_0` AST symbols, `tier_1` Qdrant vectors, `tier_2` episodic graph, `tier_3` federated retrieval, `tier_4` dream consolidation). Task execution selects among 5 routing patterns (`single_agent`, `subagents`, `parallel_specialist`, `builder_validator`, `agent_team`).

## Commands

- `kortext init [path]`: non-interactive initialization running setup, install, start, and index. Accepts `--no-plugin` and `--skip-binaries`.
- `kortext doctor [--json]`: runs 10 diagnostic checks across Python version, substrate binaries, FalkorDB, Qdrant, daemon health, hooks, embedder cache, index staleness, failed re-index events, and agy version.
- `kortext savings [--days 30] [--project NAME] [--json]`: calculates token savings estimates from logged tool events without percentage claims.
- `kortext affected [--base HEAD] [--root PATH] [--json]`: parses git diffs and queries upstream call graphs to identify affected test files.
- `kortext benchmark [--k 3] [--out PATH]`: executes the AST and vector benchmark suite against the repository.
- `kortext memory rebuild`: rebuilds FalkorDB episodic memory from Markdown journals.
- `kortext dashboard`: starts the local web dashboard on port 8004.

## Where Memory Lives

- Markdown journals: stored in `<workspace>/.kortext/memory/` when working in a repository, or in `~/.kortext/journal/<project>/` as fallback. Configured with `JOURNAL_DIR`. Daily journals record technical decisions and session logs.
- FalkorDB graph data: stores the episodic decision graph in `kortext` and AST symbols with call edges in `kortext_symbols`. Data directory defaults to `~/.kortext/data/falkordb/`.
- Qdrant vectors: vector collections stored in `~/.kortext/qdrant/` and managed exclusively by the FastMCP daemon.
- Handoff batons: stored as JSON files in `~/.kortext/handoffs/` for cross-session and cross-agent context handoffs.

## Documentation

- [System Architecture](docs/ARCHITECTURE.md): memory tiers, FastMCP sole custodian model, daemon IPC, and multi-project scoping.
- [Configuration Reference](docs/CONFIGURATION.md): full table of environment variables and defaults.
- [Troubleshooting Guide](docs/TROUBLESHOOTING.md): diagnostic workflows, recovery steps, and common failure modes.
- [Benchmark Methodology and Results](docs/BENCHMARKS.md): evaluation protocol, baseline models, metric definitions, and full data tables.
- [AST Symbols and Anti-Ripgrep](docs/SYMBOLS_AST_GUIDE.md): Tree-sitter symbol resolution, blast radius analysis, and SCIP indexing.
- [Task DAG and Agents](docs/TASK_DAG_AND_AGENTS.md): task state machine, execution patterns, subagent roles, and handoff batons.
- [Dream Mode](docs/DREAM_MODE.md): offline consolidation, fact pruning, Louvain subsystem detection, and invariant drift auditing.
- [Lifecycle Hooks](docs/LIFECYCLE_HOOKS.md): 5-pipeline interceptor runner, hook profiles, and MINJA causal defense.
- [Dashboard and CLI Reference](docs/DASHBOARD_AND_CLI.md): web dashboard architecture and CLI command reference.
- [Changelog](CHANGELOG.md): release history and upgrade instructions.
