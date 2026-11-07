# Kortext Dashboard & CLI Reference Guide

> **Modular ES Modules Architecture, Anti-Hairball Graph Navigation, Interactive Kanban DAG, and Cross-Platform CLI Tools**

---

## 1. The Interactive Kortext Dashboard

The Kortext Dashboard is an operational command center providing real-time visual introspection into memory substrates, code symbol graphs, active Task DAGs, and episodic journals.

Access the dashboard locally at:
```bash
kortext dashboard
# Opens http://127.0.0.1:8004 in your browser
```

```
┌────────────────────────────────────────────────────────────────────────┐
│                        KORTEXT COGNITIVE DASHBOARD                     │
├────────────────────────────────────────────────────────────────────────┤
│  [Overview] [Graph Explorer] [Task Kanban] [Decision Journal]          │
│  [Memory Inbox] [Retrieval] [Health]                                   │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│   Knowledge Graph Canvas HUD             Companion Inspector Panel     │
│   ┌────────────────────────────────┐    ┌───────────────────────────┐  │
│   │ [Architecture] [Zoom] [Freeze] │    │ System Telemetry          │  │
│   │                                │    │ Substrate RAM: 48.2 MB    │  │
│   │   🧩 Subsystems Architecture   │    │ FalkorDB Latency: 0.8 ms  │  │
│   │   Louvain community clusters (count depends on the graph)│    ├───────────────────────────┤  │
│   │   Zero-Hairball Topology       │    │ Relationship Palette      │  │
│   │                                │    │ [x] CALLS  [x] DEFINED_IN │  │
│   └────────────────────────────────┘    └───────────────────────────┘  │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

The interface is organized into seven tabs:
- **Overview:** Substrate and daemon status, re-index spool queue depth, and estimated token savings.
- **Graph Explorer:** Code symbol knowledge graph with subsystem clustering and neighborhood isolation.
- **Task Kanban:** 5-column drag-and-drop Task DAG board.
- **Decision Journal:** Searchable episodic memory records and technical decisions.
- **Memory Inbox:** Review, edit, approve, and supersede episodic facts with CSRF protection.
- **Retrieval:** Multi-tier search sandbox comparing dense, sparse, and graph scores.
- **Health:** Substrate connectivity and hook execution diagnostics.

---

## 2. Modular ES Modules Frontend Architecture

The frontend is constructed using pure modern ECMAScript modules (no heavy Webpack or Vite build steps needed), as ES modules:

```
kortext/dashboard/static/js/
├── main.js                  # Application entry point & backward-compatible window bindings
└── modules/
    ├── state.js             # Centralized reactive application state store
    ├── api.js               # Async fetch client with error envelopes and toasts
    ├── ui.js                # Toast notifications, modals, and WCAG AA contrast helpers
    ├── overview.js          # Overview landing metrics, spool status, and token savings
    ├── health.js            # Substrate health, 5-tier memory grid, and hooks status
    ├── graph.js             # Knowledge Graph explorer & Vis.js network manager
    ├── graph_inspector.js   # Relationship palette, exact link inspector, side drawer
    ├── tasks.js             # Interactive 5-column Drag-and-Drop Task DAG Kanban board
    ├── journal.js           # Decision journal & 8-category outcome taxonomy explorer
    ├── memory.js            # Memory Inbox fact inspection, editing, and superseding
    ├── retrieval.js         # Multi-tier RRF retrieval sandbox & score breakdown
    ├── blast.js             # Blast radius analyzer & hierarchical caller/callee tree
    ├── dream.js             # Cognitive Dream Mode trigger modal
    └── icons.js             # Inlined SVG icon definitions
```

The Content-Security-Policy still allows `unsafe-inline` for scripts, because about 60 inline handlers remain in `index.html`.

---

## 3. Anti-Hairball Knowledge Graph Navigation

Rendering thousands of symbols and files in canvas force-directed graphs typically collapses into an unusable "hairball". Kortext eliminates hairballs through 5 layered defenses:

1. **Subsystems Architecture View (1-Click):**
   Click the **`Architecture`** button on the canvas HUD to instantly isolate the **67 Louvain Subsystems** and member files. Eliminates leaf symbol clutter and provides an immediate, clean architectural mental model.
2. **Dynamic Density Pruning:**
   For large graphs (>250 nodes), the density slider automatically defaults to **`Deg ≥ 3`** or **`Deg ≥ 4`**, filtering out thousands of single-connection leaf nodes.
3. **Component Scope Filtering:**
   Filter visible elements dynamically via the Scope selector:
   - `🌐 All Components`
   - `🧩 Architecture (Subsystems & Files - ~180 nodes)`
   - `📦 Subsystems Only (<70 nodes)`
   - `⚡ Symbols Only`
   - `📄 Documents & Guides`
4. **Macro-Module Clustering:**
   Groups symbols into expandable module cluster boxes with generous spring spacing (`springLength: 380`, `gravitationalConstant: -55000`).
5. **Ego-Network Isolation (1-Hop / 2-Hop):**
   Click any node and select **Isolate Neighborhood** to dim the rest of the universe and focus strictly on that node's immediate callers and callees.
6. **Hard-Freeze Physics:**
   Barnes-Hut physics stabilizes, then physics is switched off, guaranteeing 60 FPS scrolling and zero CPU/GPU battery drain.

---

## 4. Interactive Task DAG Kanban Board

Manage agent tasks visually with the 5-column drag-and-drop board:

```
┌──────────────┬──────────────┬──────────────┬──────────────┬──────────────┐
│  READY (3)   │IN PROGRESS(1)│ PENDING (2)  │COMPLETED(14) │  FAILED (0)  │
├──────────────┼──────────────┼──────────────┼──────────────┼──────────────┤
│ [task_041]   │ [task_039]   │ [task_042]   │ [task_038]   │              │
│ FastMCP Tool │ Cypher Query │ Frontend UI  │ AST Orphan   │              │
│ C: 3 | Runner│ C: 2 | Pro   │ Blocked: 041 │ Verified: YES│              │
│ [Advance ->] │ [Advance ->] │ [Advance ->] │ [Outcome ->] │              │
└──────────────┴──────────────┴──────────────┴──────────────┴──────────────┘
```

- **Drag-and-Drop:** Drag cards across columns to trigger state transitions via `manage_tasks(action='update_status')`.
- **1-Click Status Advance:** Quickly transition tasks from Ready $\to$ In Progress $\to$ Completed.
- **Dual View Toggle:** Switch seamlessly between the Kanban Board and the Vis.js DAG Network Topology.
- **Project Filter:** Filter the board by active project (`kortext`, `app_alpha`, or `all`).

---

## 5. Complete CLI Reference

The `kortext` CLI provides cross-platform management across macOS, Linux, and Windows:

```bash
# Display help
kortext --help
```

### `kortext setup` (Guided Setup Wizard)
Runs the interactive onboarding wizard:
```bash
# Interactive guided wizard
kortext setup

# Non-interactive automated CI setup
kortext setup --yes
```
- Validates Python 3.11+ and local ports (6379, 6333).
- Downloads native FalkorDB binary / verifies Docker availability.
- Initializes data directories (`~/.kortext/data`, `~/.kortext/run`).
- Writes production `.env` configuration.

### `kortext start` / `kortext stop`
Supervises local substrate daemons:
```bash
# Start FalkorDB, Qdrant, and background services
kortext start

# Stop all background substrates
kortext stop
```

### `kortext status`
Audits system health and substrate connectivity:
```bash
kortext status
```

### `kortext index`
Parses and indexes a codebase using Tree-sitter:
```bash
kortext index --root /path/to/project
```

### `kortext dream`
Executes an offline memory consolidation cycle:
```bash
kortext dream --project kortext
```

### `kortext dashboard`
Starts the local dashboard server:
```bash
DASHBOARD_PORT=8004 kortext dashboard
```

### `kortext init`
Initializes setup, substrate binaries, daemon processes, and workspace indexing in a single command.

### `kortext doctor`
Runs diagnostic checks across Python version, substrate binaries, daemons, hooks, and index health.

### `kortext savings`
Reports estimated token savings computed from logged MCP tool events.

### `kortext affected`
Maps git diffs to affected test files via static AST call graphs.
