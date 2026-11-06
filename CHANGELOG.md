# Changelog

All notable changes to Kortext are documented in this file.

## [2.1.0] - 2026-11-07

### Added

- CLI command `kortext init [path]`: non-interactive setup, installation, substrate startup, and workspace indexing in a single command.
- CLI command `kortext doctor [--json]`: runs 10 diagnostic checks across Python version, substrate binaries, FalkorDB, Qdrant, daemon health, hooks, embedder cache, index staleness, re-index spool, and agy version.
- CLI command `kortext savings [--days 30] [--project NAME] [--json]`: computes token savings estimates from logged tool events without percentage claims.
- CLI command `kortext affected [--base HEAD] [--root PATH] [--json]`: maps git diffs to affected test files via static AST call graphs.
- FastMCP tool `find_affected_tests`: exposes static test selection directly to coding agents over MCP.
- Memory vault: Markdown journals stored in `.kortext/memory/` or `~/.kortext/journal/<project>/`, with automated 40-line `INDEX.md` summaries, secret redaction, and CLI commands `kortext memory rebuild` and `kortext memory index`.
- AST body hash tracking: episodic nodes and relations record grounded AST symbols; search results mark outdated facts with `[may be out of date: <symbol> changed since this was recorded]` when code symbols change or vanish.
- Dual ripgrep baselines in benchmark suite (`rg_naive` and `rg_informed`) alongside pass@k and pass^k containment metrics.
- Dashboard Overview tab (`/#overview`): landing tab displaying daemon status, FalkorDB and Qdrant connectivity, re-index spool queue diagnostics, project freshness, and token savings.
- Dashboard Memory Inbox tab (`/#memory`): inspection, approval, editing, and superseding of episodic facts with CSRF protection.
- Support for `kortext dream --no-purge-stale` via `argparse.BooleanOptionalAction`.

### Changed

- Split LongMemEval into deterministic 50/50 calibration and held-out sets (25 items each); the 0.42 dense score threshold is tuned on calibration and evaluated on held-out samples.
- Multi-session reasoning in LongMemEval is scored across the top 5 returned facts.
- Dashboard replaced runtime in-browser Tailwind compiler and Lucide bundle with precompiled CSS and inlined SVG icons, saving 851 KB.
- Dashboard editing a memory records a new fact and supersedes the old one, so recall matches the new text.
- Token savings logging appends events in under 0.05 ms with bounded 5 MB file rotation.
- Upgraded FastMCP tools to 13 registered tools, keeping high-frequency tools loaded while lazy-loading administrative tools.

### Removed

- Removed runtime dependency on `scipy`, reducing resolved packages.
- Deleted legacy HippoRAG Personalized PageRank (PPR), TAA-k knee trimmer, Weibull retention decay curves, offline reranker client, and instinct store.
- Removed `hipporag` keywords from project configuration.

### Fixed

- Hooks now match the tool names agy 1.5.x sends (`write_file`, `edit_file`, `read_file`). Before this, re-index on edit and the file safety gates did not fire for those tools in real sessions.
- The Stop pipeline accepts the real payload (`executionNum` 0, `terminationReason` `NO_TOOL_CALL`). Before this, it returned early on every real session, so the outcome reminder and session log never ran. The outcome reminder fires at most once per session.
- Savings events are recorded by the daemon, because the PostToolUse payload does not carry the tool result.
- Eliminated embedded Qdrant lock collisions across multiple editor windows by routing requests through the single-machine FastMCP daemon.
- Resolved Cypher query null maps for undefined caller stubs in `seek_symbol`, preventing FalkorDB panics.
- Corrected pinned substrate binary SHA-256 hashes across all supported platforms.
- Quarantined runtime invariant modifications to project home directories instead of modifying tracked repository files.

### Upgrading Notes

1. Stop the old running daemon before upgrading:
   ```bash
   uv run kortext stop
   ```
   Or terminate running daemon processes manually:
   ```bash
   pkill -f "kortext.*server" || true
   ```
2. Session logs in the memory vault (`.kortext/memory/`) store daily work logs and redacted prompts. If you do not want session logs committed to your git repository, add `.kortext/memory/` to `.gitignore`:
   ```bash
   echo ".kortext/memory/" >> .gitignore
   ```
