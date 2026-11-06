# Kortext Benchmark Methodology and Results

This document describes the benchmark harness, evaluation methodology, baseline comparisons, LongMemEval evaluation, and full empirical results for Kortext.

## 1. How to Run

To run the benchmark suite against the local repository:

```bash
uv run kortext benchmark --k 3 --out data/benchmark_results.json
```

Requirements:
- Native FalkorDB instance running on `~/.kortext/run/falkordb.sock` or `127.0.0.1:6379`.
- Qdrant running in embedded mode or via HTTP on `127.0.0.1:6333`.
- Active repository indexed into FalkorDB and Qdrant via `kortext index`.

---

## 2. What Is Measured

The suite evaluates 30 tasks across three core MCP tools:
- `search_knowledge_and_memory` (10 tasks): semantic concept, configuration, and architectural queries.
- `seek_symbol` (10 tasks): exact function, class, and method symbol declarations.
- `trace_symbol_impact` (10 tasks): upstream caller hierarchies and downstream dependencies.

For each task and tool, the suite records:
- **Latency:** Execution time in milliseconds (p50 and p95 percentiles).
- **Reply Tokens:** Token counts measured using the `tiktoken` `cl100k_base` tokenizer (serving as a proxy for the Gemini tokenizer).
- **Recall@5:** Proportion of relevant gold files returned within the top 5 results.
- **Mean Reciprocal Rank (MRR):** Reciprocal rank ($1/\text{rank}$) of the first relevant file.
- **Pass@k ($k=3$):** Probability that at least one of $k$ repeated runs retrieves a relevant gold file (`any(r['passed'] for r in runs)`).
- **Pass^k ($k=3$):** Probability that all $k$ repeated runs retrieve a relevant gold file (`all(r['passed'] for r in runs)`).

---

## 3. Methodology and Baselines

### Real Repository and Unprimed Questions
- Benchmarks run against the real Kortext codebase (`3581ccea065a3ba990b1f3e637addd9f0df9da7f`).
- Questions do not disclose explicit file paths or module answers.
- Evaluation runs strictly through FastMCP tool implementations against real database instances.

### Dual Ripgrep Baselines and Biases
To assess retrieval tradeoffs, Kortext is compared against two ripgrep baselines:

1. **`rg_naive`:** Runs ripgrep using search terms derived from question words.
   - **Bias:** Unbounded text sweeps return massive match lists. On behavioural queries, `rg_naive` averages 294,585 tokens per query (and up to 808,828 tokens on `search_01`).
   - **Metric artifact:** Because Pass@k evaluates file containment (`any(f in gold_files for f in retrieved_files)`), a ripgrep dump containing nearly every file in the repository trivially achieves 1.00 Pass@k despite having 0.00 Recall@5 and 0.0365 MRR on semantic queries.
2. **`rg_informed`:** Runs ripgrep using targeted identifiers and patterns known to match the subsystem.
   - **Bias:** Simulates an engineer who already knows the codebase. It avoids massive dumps (averaging 5,857 tokens on search), but represents an unrealistic upper bound for unfamiliar tasks.

---

## 4. Overall Results Summary

Data from `bench-results.json` ($N=30$ tasks, $k=3$ repetitions):

| System | Pass@k | Pass^k | Recall@5 | MRR | Latency p50 | Latency p95 | Tokens Avg | Tokens p50 | Tokens p95 |
|---|---|---|---|---|---|---|---|---|---|
| **kortext** | 0.9000 | 0.9000 | 0.8222 | 0.7292 | 9.27 ms | 225.61 ms | 1,548.7 | 879.0 | 4,655.0 |
| **rg_naive** | 1.0000 | 1.0000 | 0.5167 | 0.3093 | 5.83 ms | 9.95 ms | 99,515.1 | 2,370.0 | 475,535.0 |
| **rg_informed** | 1.0000 | 1.0000 | 0.7389 | 0.4590 | 5.65 ms | 6.96 ms | 3,272.5 | 817.0 | 9,523.0 |

---

## 5. Tool Breakdown Results

### `search_knowledge_and_memory` (10 tasks)

| System | Pass@k | Pass^k | Recall@5 | MRR | Latency p50 | Latency p95 | Tokens Avg | Tokens p50 |
|---|---|---|---|---|---|---|---|---|
| **kortext** | 0.7000 | 0.7000 | 0.7000 | 0.5167 | 23.26 ms | 227.93 ms | 1,024.3 | 933.0 |
| **rg_naive** | 1.0000 | 1.0000 | 0.0000 | 0.0365 | 7.33 ms | 10.96 ms | 294,584.5 | 239,958.5 |
| **rg_informed** | 1.0000 | 1.0000 | 0.7000 | 0.4761 | 5.99 ms | 7.06 ms | 5,856.7 | 2,330.5 |

### `seek_symbol` (10 tasks)

| System | Pass@k | Pass^k | Recall@5 | MRR | Latency p50 | Latency p95 | Tokens Avg | Tokens p50 |
|---|---|---|---|---|---|---|---|---|
| **kortext** | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 6.25 ms | 9.82 ms | 583.1 | 558.5 |
| **rg_naive** | 1.0000 | 1.0000 | 0.9000 | 0.5976 | 5.68 ms | 5.85 ms | 541.1 | 386.0 |
| **rg_informed** | 1.0000 | 1.0000 | 0.9000 | 0.5976 | 5.52 ms | 6.62 ms | 541.1 | 386.0 |

### `trace_symbol_impact` (10 tasks)

| System | Pass@k | Pass^k | Recall@5 | MRR | Latency p50 | Latency p95 | Tokens Avg | Tokens p50 |
|---|---|---|---|---|---|---|---|---|
| **kortext** | 1.0000 | 1.0000 | 0.7667 | 0.6708 | 8.10 ms | 219.42 ms | 3,038.8 | 3,405.5 |
| **rg_naive** | 1.0000 | 1.0000 | 0.6500 | 0.2937 | 5.77 ms | 8.91 ms | 3,419.6 | 2,216.0 |
| **rg_informed** | 1.0000 | 1.0000 | 0.6167 | 0.3032 | 5.69 ms | 6.80 ms | 3,419.6 | 2,216.0 |

---

## 6. Full Task-by-Task Benchmark Data

Every individual task result from `bench-results.json`:

| Task ID | Type | Target / Query | Kortext Pass | Kortext R@5 | Kortext MRR | Kortext Tokens | RG Naive Tokens | RG Informed Tokens |
|---|---|---|---|---|---|---|---|---|
| `search_01` | search | Multi-window daemon lock management | True | 1.00 | 0.50 | 849 | 808,828 | 3,818 |
| `search_02` | search | Substrate startup and binary downloads | True | 1.00 | 1.00 | 844 | 456,536 | 441 |
| `search_03` | search | Token savings estimation logic | False | 0.00 | 0.00 | 777 | 39,137 | 663 |
| `search_04` | search | FastMCP tool registration and schema | True | 1.00 | 0.33 | 957 | 475,535 | 7,373 |
| `search_05` | search | AST chunking and node parsing | True | 1.00 | 1.00 | 964 | 214,113 | 3,690 |
| `search_06` | search | Task DAG state machine transitions | True | 1.00 | 1.00 | 909 | 152,158 | 5,522 |
| `search_07` | search | Invariant drift auditing and hashing | False | 0.00 | 0.00 | 1,516 | 265,804 | 225 |
| `search_08` | search | Lifecycle hook interceptor runners | True | 1.00 | 0.33 | 1,470 | 406,873 | 35,528 |
| `search_09` | search | Causal sanitization and MINJA quarantine | False | 0.00 | 0.00 | 1,153 | 120,426 | 971 |
| `search_10` | search | Dashboard routes and status API | True | 1.00 | 1.00 | 804 | 6,435 | 336 |
| `seek_01` | seek | `SymbolGraphManager` | True | 1.00 | 1.00 | 667 | 375 | 375 |
| `seek_02` | seek | `EmbeddedQdrantEngine` | True | 1.00 | 1.00 | 559 | 2,192 | 2,192 |
| `seek_03` | seek | `AgentNativeEpisodicDriver` | True | 1.00 | 1.00 | 558 | 221 | 221 |
| `seek_04` | seek | `TaskDAGTracker` | True | 1.00 | 1.00 | 675 | 608 | 608 |
| `seek_05` | seek | `DreamEngine` | True | 1.00 | 1.00 | 526 | 375 | 375 |
| `seek_06` | seek | `CausalSanitizer` | True | 1.00 | 1.00 | 714 | 397 | 397 |
| `seek_07` | seek | `FastMCPLockGuard` | True | 1.00 | 1.00 | 436 | 210 | 210 |
| `seek_08` | seek | `IngestCache` | True | 1.00 | 1.00 | 620 | 433 | 433 |
| `seek_09` | seek | `SCIPImporter` | True | 1.00 | 1.00 | 543 | 412 | 412 |
| `seek_10` | seek | `AntiRipgrepGate` | True | 1.00 | 1.00 | 533 | 188 | 188 |
| `trace_01` | trace | `seek_symbol` | True | 0.33 | 0.25 | 2,646 | 5,056 | 5,056 |
| `trace_02` | trace | `trace_symbol_impact` | True | 1.00 | 1.00 | 2,358 | 1,884 | 1,884 |
| `trace_03` | trace | `search_knowledge_and_memory` | True | 1.00 | 0.50 | 1,124 | 611 | 611 |
| `trace_04` | trace | `manage_tasks` | True | 1.00 | 0.50 | 4,478 | 3,201 | 3,201 |
| `trace_05` | trace | `record_system_outcome` | True | 1.00 | 0.33 | 845 | 2,548 | 2,548 |
| `trace_06` | trace | `trigger_dream_mode` | True | 1.00 | 1.00 | 4,376 | 412 | 412 |
| `trace_07` | trace | `index_directory` | True | 1.00 | 1.00 | 4,165 | 1,275 | 1,275 |
| `trace_08` | trace | `check_kortext_health` | True | 1.00 | 1.00 | 975 | 1,056 | 1,056 |
| `trace_09` | trace | `memory_session_handoff` | True | 0.33 | 1.00 | 4,766 | 9,523 | 9,523 |
| `trace_10` | trace | `find_affected_tests` | True | 0.00 | 0.12 | 4,655 | 8,630 | 8,630 |

---

## 7. LongMemEval Split and Evaluation

LongMemEval benchmarks are partitioned deterministically into a 50/50 split across 50 items:
- **Calibration set (25 items):** Used exclusively to calibrate the dense similarity threshold. The 0.42 threshold was chosen based on calibration outcomes.
- **Held-out set (25 items):** Gated production tests evaluate strictly against these held-out items.

### Calibration Set Results ($N=25$)

| Ability | Before Dense Scoring | After Dense Scoring (0.42 Threshold) |
|---|---|---|
| Information Extraction | 0.67 | 0.67 |
| Knowledge Updates | 0.20 | 0.20 |
| Temporal Reasoning | 1.00 | 1.00 |
| Multi-Session Reasoning (top-1) | 0.00 | 0.00 |
| Multi-Session Reasoning (top-5) | 0.20 | 0.20 |
| Abstention | 0.00 | 1.00 |

### Held-Out Set Results ($N=25$)

| Ability | Before Dense Scoring | After Dense Scoring (0.42 Threshold) |
|---|---|---|
| Information Extraction | 0.83 | 1.00 |
| Knowledge Updates | 0.60 | 0.80 |
| Temporal Reasoning | 0.60 | 0.60 |
| Multi-Session Reasoning (top-1) | 0.20 | 0.20 |
| Multi-Session Reasoning (top-5) | 0.20 | 0.20 |
| Abstention (4 items) | 0.00 | 1.00 |

### Multi-Session Reasoning Analysis
Multi-session reasoning containment remains at 0.20 across both calibration and held-out sets. Inspection of the 5 held-out multi-session evaluation items shows that 4 items require synthesized answers not found verbatim in any individual source context fact:
1. "She has celiac disease"
2. "She is allergic to wool"
3. "Mochi has chronic kidney disease"
4. "Dual 4K DisplayPort outputs"

Because evaluation computes exact string containment against retrieved facts, reranking and retrieval diversification cannot elevate containment above 0.20 without generative synthesis.

---

## 8. Limitations and Empirical Boundaries

1. **Containment Metrics:** Pass@k evaluates presence of any gold file in the reply. This gives high scores to ripgrep despite returning thousands of irrelevant lines.
2. **Static AST Test Selection:** `kortext affected` inspected 5 recent commits on this repository:
   - Selected between 19 and 51 of 55 total test files.
   - Returned `complete: false` on all 5 commits because commits modified non-code assets (e.g., `uv.lock`, `plugin.json`, `README.md`, or test data). Static AST analysis cannot trace impacts through non-code files, requiring full test suite execution.
3. **Semantic Query Ambiguity:** Tasks `search_03`, `search_07`, and `search_09` failed because pure dense embedding vectors without lexical grounding missed specific identifier matches.
