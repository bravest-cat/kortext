<div align="center">
  <img src="assets/logo.svg" alt="Kortext Logo" width="160" height="160" />
  <h1>Kortext v2.0.0</h1>
  <p><strong>5-Tier Dual-Memory, AST Code Intelligence & Cognitive OS for Google Antigravity CLI</strong></p>

  <p>
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-GPL_3.0-blue.svg" alt="GPL-3.0 License" /></a>
    <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.11%20%7C%203.12-blue" alt="Python 3.11+" /></a>
    <a href="https://github.com/jlowin/fastmcp"><img src="https://img.shields.io/badge/FastMCP-2.0-green" alt="FastMCP 2.0" /></a>
    <a href="https://qdrant.tech/"><img src="https://img.shields.io/badge/Qdrant-1.13+-red" alt="Qdrant" /></a>
    <a href="https://falkordb.com/"><img src="https://img.shields.io/badge/FalkorDB-4.22+-orange" alt="FalkorDB" /></a>
  </p>
</div>

---

**Kortext** is a stateful cognitive operating system and persistent dual-memory substrate designed for the **Google Antigravity CLI (`agy`)** across **macOS, Linux, and Windows**. It replaces unbounded ripgrep dumps, full-file reads, context rot, and cross-session amnesia with:

- **Compiler-Grade AST Intelligence:** Deterministic Tree-sitter symbol indexing (`seek_symbol`) and multi-hop knapsack-budgeted call hierarchies (`trace_symbol_impact`) across 11 languages, plus SCIP Protobuf ingestion.
- **Hybrid retrieval:** symbol hits from the AST graph, BM25 and dense vectors from Qdrant, and current episodic facts from FalkorDB, fused with weighted Reciprocal Rank Fusion. Episodic search scores facts with the dense embedder and returns nothing when no fact clears the threshold.
- **Graph-Backed Task DAG & 4-Pattern Orchestration Router:** Atomic single-query task claiming, automatic expired-lease self-healing, Context-Rich Task Briefs (`.kortext/briefs/task_<id>_brief.md`), compaction-proof progress ledgers (`.kortext/ledger.md`), and deterministic 5-pattern routing (`single_agent`, `subagents`, `parallel_specialist`, `builder_validator`, `agent_team`).
- **Dream mode:** pruning of superseded facts, Louvain subsystem detection, and AST body-hash plus MinHash invariant drift auditing.
- **5-Pipeline Lifecycle Hooks & MINJA Defense:** Configurable runtime profiles (`KORTEXT_HOOK_PROFILE=minimal|standard|strict|disabled`) enforcing Anti-Ripgrep auto-seek, GateGuard first-touch blast-radius checks, subagent write confinement, 25%-compaction-ceiling pre-compaction batons, and MINJA causal prompt-injection quarantine.
- **Interactive Anti-Hairball Web Dashboard:** Modular ES modules dashboard on `:8004` with Sole-Custodian-safe federated retrieval, 1-click Subsystems Architecture view, Blast Radius visualizer, and a 5-column Drag-and-Drop Task DAG Kanban board.

Everything runs **100% locally and Zero-Docker native** on macOS and Linux (with native Qdrant + automatic WSL2/Docker FalkorDB detection and privilege-safe `copytree` plugin installation on Windows).

---

## 📚 Complete Documentation Suite

Comprehensive guides are available in the [`docs/`](docs/) directory:

| Guide | Description |
|:---|:---|
| 📖 **[1. System Architecture](docs/ARCHITECTURE.md)** | 5-Tier Memory Hierarchy, FastMCP Sole Custodian Model, UDS/TCP IPC, and multi-project isolation. |
| ⚡ **[2. AST Symbols & Anti-Ripgrep](docs/SYMBOLS_AST_GUIDE.md)** | Tree-sitter AST extraction, point lookup via `seek_symbol`, multi-hop blast radius via `trace_symbol_impact`, and SCIP indexing. |
| 📋 **[3. Task DAG & Subagents](docs/TASK_DAG_AND_AGENTS.md)** | 4-Pattern Execution Router, the 5 specialized subagent personas, artifact-first handoffs, and Two-Stage Review Gates. |
| 🌙 **[4. Hybrid Dream Mode](docs/DREAM_MODE.md)** | Fact pruning, Louvain community partitioning, MEX AST invariant grounding, and skill distillation. |
| 🛡️ **[5. Lifecycle Hooks & Guardrails](docs/LIFECYCLE_HOOKS.md)** | 5-pipeline interceptor runner, `KORTEXT_HOOK_PROFILE`, AntiRipgrepGate, BlastRadiusGuard, and MINJA defense. |
| 📊 **[6. Dashboard & CLI Reference](docs/DASHBOARD_AND_CLI.md)** | Modular ES modules dashboard, anti-hairball graph navigation, interactive Kanban board, and full `kortext` CLI reference. |
| ⚖️ **[7. Trademark Policy](TRADEMARKS.md)** | Brand assets, proper usage guidelines, and fork attribution rules. |

---

## 🚀 Quick Start (macOS, Linux & Windows)

### 1. Installation

Install Kortext using `uv` or `pip`:

```bash
git clone https://github.com/kortext/kortext.git
cd kortext

# Install dependencies with uv (or pip install -e .)
uv sync
```

### 2. Guided Setup Wizard

Run the setup wizard to pull verified native substrate binaries (with SHA-256 & `.sha256` sidecar verification) and initialize `~/.kortext`:

```bash
# Interactive guided setup
uv run kortext setup

# Non-interactive setup for CI or automated onboarding
uv run kortext setup --yes --start
```

### 3. Install the Antigravity Plugin, Skills, Agents & Daemons

```bash
# Installs plugin manifest, 12 canonical skills, 5 subagents, hooks, and OS services
# (macOS LaunchAgents, Linux systemd --user units, or Windows copytree fallback)
uv run kortext install --all
```

Optional on Linux: `integrations/antigravity/daemons/` also holds `kortext-watcher.service` and `kortext-dashboard.service` (user units, not installed automatically). To enable one, fill in the paths and start it:

```bash
mkdir -p ~/.config/systemd/user ~/.kortext/logs
sed -e "s|{{PROJECT_DIR}}|$PWD|g" -e "s|{{PYTHON_BIN}}|$PWD/.venv/bin/python3|g" \
  integrations/antigravity/daemons/kortext-watcher.service > ~/.config/systemd/user/kortext-watcher.service
systemctl --user daemon-reload && systemctl --user enable --now kortext-watcher.service
```

### 4. Start Substrates & Verify Health

```bash
uv run kortext start
uv run kortext status
```

### 5. Launch the Visual Dashboard

```bash
uv run kortext dashboard
# Open http://127.0.0.1:8004 in your browser
```

---

## 🛠️ FastMCP Tools Reference

Kortext exposes 12 standardized FastMCP tools over `stdio`—keeping the 5 high-frequency core tools `alwaysLoad: True` and the 7 administrative/on-demand tools `alwaysLoad: False` to cut prompt schema overhead per turn (not measured):

### Core High-Frequency Tools (`alwaysLoad: True`)
1. **`seek_symbol(symbol_name, detail_level='skeleton')`**: Deterministic AST point lookup returning exact file path, line span, signature, and 1-hop callers/callees in `<2ms` warm / `<15ms` uncached.
2. **`trace_symbol_impact(symbol, direction='upstream'|'downstream', max_depth=3, token_budget=1500)`**: Multi-hop recursive call-graph traversal with progressive Level-of-Detail (Hop 1 signatures, Hop 2 files, Hop 3+ summary counts).
3. **`search_knowledge_and_memory(query, collection=None, token_budget=1500, detail_level='balanced', project=None)`**: Federated 4-tier hybrid retrieval across AST symbols, Qdrant dense+BM25 code chunks, FalkorDB bi-temporal episodic facts, and Louvain subsystem overviews.
4. **`record_system_outcome(episode_name, content, category='implementation', trust_level='speculative', ...)`**: Persists verified architectural decisions and outcomes to FalkorDB and dated Markdown journals (`journal/<category>/`), guarded by MINJA `CausalSanitizer`.
5. **`manage_tasks(action='create'|'claim'|'complete_task'|'get_ready'|'recommend_pattern'|..., ...)`**: Graph-backed Task DAG engine with atomic claiming, expired-lease self-healing, `.kortext/briefs/` generation, and 5-line completion contracts.

### On-Demand & Administrative Tools (`alwaysLoad: False`)
6. **`boost_prompt_context(prompt, workspace_root=None, token_budget=600)`**: Pre-execution prompt enhancer (`/kortext-boost`) resolving `@file` mentions, `<file>:<line>` `/codesearch` & `/diff` comments, AST skeletons, grounded invariants, and recommended execution patterns.
7. **`memory_session_handoff(action='create'|'read'|'list'|'complete', ...)`**: Creates and resumes structured JSON handoff batons (`~/.kortext/handoffs/`) across sessions and milestone rotations.
8. **`trigger_dream_mode(project='kortext', purge_stale=False, ...)`**: Runs fact pruning, Louvain subsystem clustering, invariant drift auditing, and skill distillation.
9. **`index_directory(path, collection=None, ...)`**: Incremental content-hash and batch `git log` codebase ingestion into Qdrant and FalkorDB.
10. **`delete_or_supersede_fact(fact_id, reason, ...)`**: Soft-supersedes outdated facts by canonical UUID or session-scoped integer alias (`[F1]`, `F1`).
11. **`list_vector_collections()`**: Lists active Qdrant collections and point counts across embedded or HTTP modes.
12. **`check_kortext_health()`**: Performs deep readiness and corruption probes across FalkorDB, Qdrant, FastEmbed/oMLX, and grounded invariants.

---

## 🧩 Canonical Skills, Subagents & Diagnostic Scripts

- **5 Specialized Subagents (`integrations/antigravity/agents/`)**:
  `kortext-architect`, `kortext-researcher` (read-only write-confined), `kortext-task-runner`, `kortext-code-reviewer` (Two-Stage Spec & Quality Gate), and `kortext-security-auditor` (MINJA & OWASP Challenger).
- **12 Canonical Skills (`integrations/antigravity/skills/`)**:
  `kortext-boost`, `anti-ripgrep-seek`, `kortext-rag`, `to-spec`, `to-tickets`, `subagent-driven-development`, `tdd-workflow`, `systematic-debugging`, `code-review`, `security-review`, `expand-contract-refactor`, and `verification-before-completion`.
- **Bundled Architecture & Telemetry Diagnostics**:
  - **Ousterhout Deep-Module Survey**: `python3 integrations/antigravity/skills/expand-contract-refactor/scripts/survey_deep_modules.py`
  - **Session & Hook Telemetry Diagnostics**: `python3 integrations/antigravity/skills/systematic-debugging/scripts/diagnose_session.py`
  - **Pre-Completion Diff Auditor**: `python3 integrations/antigravity/skills/code-review/scripts/verify_diff.py`

---

## 🧪 Verification & Test Suite

```bash
# Validate all Antigravity plugin manifests, 12 skills, 5 subagents, hooks, and rules
python3 scripts/test_plugin_manifest.py

# Run end-to-end Antigravity lifecycle hook contract tests
python3 scripts/test_antigravity_hooks.py

# Execute the pytest test suite
uv run pytest -q
```

---

## 📄 License

Kortext is licensed under the **GNU General Public License v3.0 (GPL-3.0)**. See the [LICENSE](LICENSE) file for the complete license text and [TRADEMARKS.md](TRADEMARKS.md) for branding guidelines.
