---
trigger: model_decision
description: "Activate when searching for code symbols, function or class declarations, caller graphs, or refactoring blast radius."
---

# Invariant [Anti-Ripgrep-Mandate]

## Core Rule
Agents MUST NEVER execute `ripgrep`, `rg`, `grep`, `grep_search`, or sequential regex text scans across code files to locate symbol declarations, signatures, type definitions, call sites, or interface dependencies.

Instead, agents MUST use the compiler-grade AST B-Tree index and graph memory:
- **Locate exact symbol declaration & call hierarchy:** Call `seek_symbol(symbol_name="<symbol>")` (<2ms, <150 tokens).
- **Assess multi-hop refactoring impact:** Call `trace_symbol_impact(symbol="<symbol>", direction="upstream"|"downstream")` (<5ms).
- **Semantic associative concept search:** Call `search_knowledge_and_memory(query="<intent>")`.

## The AST Tool Suite

### 1. `seek_symbol(symbol_name="<symbol>")`
- **Purpose:** Direct point lookup against the Tree-sitter AST B-Tree symbol index.
- **Returns:** Exact file path, start and end line numbers, full type signature, docstring, and immediate callers/callees.
- **Latency & Tokens:** <2ms latency, ~120 tokens returned.
- **Use when:** You need to inspect, modify, or understand a known class, function, method, interface, or constant.

### 2. `trace_symbol_impact(symbol="<symbol>", direction="upstream"|"downstream")`
- **Purpose:** Multi-hop graph traversal over the `kortext_symbols` FalkorDB graph.
- **Returns:** Upstream callers (blast radius of breaking changes) or downstream dependencies (what this symbol relies on).
- **Latency & Tokens:** <5ms latency, structured graph paths.
- **Use when:** Refactoring, renaming, modifying signatures, or assessing blast radius before edits.

### 3. `search_knowledge_and_memory(query="<intent>")`
- **Purpose:** Hybrid dense-sparse vector and semantic graph retrieval.
- **Returns:** Contextual code chunks, design rationale, episodic session outcomes, and architecture decisions.
- **Use when:** Exploring unfamiliar concepts, searching for implementations by intent rather than exact name, or retrieving historical architecture decisions.

## Contrast Matrix (WRONG vs CORRECT)

| Intent | ❌ WRONG (Ripgrep / Regex) | ✅ CORRECT (Kortext AST) |
|---|---|---|
| Find function definition | `rg "def parse_manifest"` | `seek_symbol(symbol_name="parse_manifest")` |
| Find class declaration | `rg "class TokenOptimizer"` | `seek_symbol(symbol_name="TokenOptimizer")` |
| Trace call sites & blast radius | `rg "record_system_outcome\("` | `trace_symbol_impact(symbol="record_system_outcome", direction="upstream")` |
| Trace symbol dependencies | Multi-file regex hunting across imports | `trace_symbol_impact(symbol="VectorStore", direction="downstream")` |
| Locate architectural concept | `rg -i "fusion weights"` | `search_knowledge_and_memory(query="RRF fusion weights")` |
| Read surrounding code | Unbounded regex dump into context | `view_file` targeted to line range from `seek_symbol` |

## Context Economics & Token Budgeting Rationale

1. **Token Bloat Prevention:** A single ripgrep invocation across a medium codebase injects 8,000-16,000 tokens of noisy output containing partial matches, string comments, and variable collisions. AST B-Tree lookups return the exact AST node in ~120 tokens (>95% token savings).
2. **Context Integrity & Reasoning Preservation:** Unbounded grep dumps pollute the working memory of the LLM, triggering "lost in the middle" degradation, severe context fragmentation, and hallucinations in subsequent turns.
3. **Deterministic Precision:** Ripgrep cannot distinguish between a symbol declaration, a mock in a test, a comment reference, or a substring in a log message. Tree-sitter AST queries guarantee 100% precision with zero false positives.

## Cold-Index Fallback Handling

If `seek_symbol` returns empty results for a known symbol, verify the index is populated by calling `check_kortext_health()`. If the symbol graph is empty, fall back to targeted `view_file` reads or suggest running `kortext index --root <workspace>` to populate the index. Do NOT run broad `rg` scans as a first resort.

## Permitted Narrow Exceptions

Agents may ONLY use text search in the following narrow circumstances:
- Searching for raw literal non-symbol text where no AST entity exists (e.g., locating an exact hardcoded error message string such as `"Database connection timed out at epoch"` or finding a CSS color hex code `#7C3AED` when the identifier is unknown).
- Even in such cases, scope the search to the specific directory or file rather than searching the entire repository.

