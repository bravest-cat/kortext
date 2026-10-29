---
name: subagent-driven-development
description: Use when orchestrating multi-agent tasks, dispatching parallel subagents, executing implementation plans across a Task DAG, running autonomous loops with de-sloppify passes, or managing session token budgets.
---

# Subagent-Driven Development (SDD) & Multi-Agent Orchestration

Unifies multi-agent Task DAG execution, parallel worker dispatch, author-blind review gates, 5-round fix escalation, autonomous loop patterns (`de-sloppify`), and session token-budget discipline.

## 1. Orchestrator-Worker Architecture & 4-Pattern Routing

1. **Context Guardian Invariant**: When complexity $C \ge 7$ (or $\ge 4$ files across disjoint subsystems), the parent orchestrator never writes sprawling implementation diffs or runs noisy debug loops inline. It decomposes the DAG, dispatches isolated workers, and audits handoffs. For simple edits ($\le 3$ files, $C < 4$), execute inline (`single_agent`) without subagent overhead.
2. **Exclusive File Ownership**: Never run two parallel workers on the same file or on 1-hop AST-coupled files in a shared working tree. `manage_tasks(action="check_overlaps", target_files=[...])` lists in_progress tasks that touch them; sequence those via `depends_on` or put them on separate branches (`recommended_workspace_mode="branch"`). After claiming, a worker locks its files with `manage_tasks(action="claim_files", task_id=..., target_files=[...])`; while it runs with `KORTEXT_TASK_ID` set, the PreToolUse hook denies its edits to files another in_progress task owns.
3. **15-Minute Lease & Heartbeat Defense**: Workers claim the next ready task with `manage_tasks(action="claim", assigned_to="<worker>")` (a 15-minute lease), send heartbeats on long tasks (`action="heartbeat", task_id=...`), and expired leases are reclaimed with `action="reclaim_expired"`.

## 2. The SDD Execution Loop per Task Node

1. **Anchor Base Commit & Claim Ready Task**:
   ```python
   ready = manage_tasks(action="get_ready")
   task = manage_tasks(action="claim", assigned_to="kortext-task-runner")
   manage_tasks(action="claim_files", task_id=task_id, target_files=[...])
   ```
   Record `BASE_COMMIT=$(git rev-parse HEAD)` and provide a self-contained task brief (`target_files`, AST signatures from `seek_symbol`, invariants, and exact `test_command`).
2. **Execute in Leaf Worker (`kortext-task-runner`)**:
   Worker implements via strict RED-GREEN-REFACTOR TDD and completes the node via:
   ```python
   manage_tasks(
       action="complete_task",
       task_id=task_id,
       modified_files=[...],
       test_command="uv run pytest tests/test_target.py -q",
       tests_passed=True,
       description="<150-token summary>",
   )
   ```
   This writes the full verification payload to `~/.kortext/handoffs/<task_id>.json` and returns only a `<150`-token `handoff:<task_id>` pointer to the parent session.
3. **Optional De-Sloppify Pass (Autonomous Loop Cleanup)**:
   After thorough TDD implementation on complex features, run a focused cleanup pass to strip redundant framework-type tests, dead branches, or unnecessary defensive wrappers while keeping 100% of business-logic tests green.
4. **Independent Author-Blind Review (`kortext-code-reviewer`)**:
   Dispatch `kortext-code-reviewer` with **only** the task brief, `git diff $BASE_COMMIT...HEAD`, and fresh `pytest` output (never the worker's self-reported prose).
5. **Persist Outcome on `APPROVE`**:
   ```python
   record_system_outcome(
       episode_name=f"Completed {task_id}: {title}",
       content="<concise verified outcome>",
       category="implementation",
   )
   ```

## 3. The 5-Round Fix Loop & Model Escalation Matrix

When `kortext-code-reviewer` returns `BLOCK` or `WARN`:

| Round | Assigned Agent & Tier | Strategy | Escalation Trigger |
|---|---|---|---|
| **Rounds 1–3** | `kortext-task-runner` (`flash` / Balanced) | Address specific reviewer findings with targeted TDD regression tests | Still `BLOCK` after Round 3 $\rightarrow$ **Escalate to `pro` tier** |
| **Rounds 4–5** | `kortext-task-runner` (`pro` / High-Reasoning) | Ingest full diff history, AST blast radius (`trace_symbol_impact`), and reviewer critique | Still `BLOCK` after Round 5 $\rightarrow$ **Trip Circuit Breaker** |
| **Circuit Breaker** | Orchestrator + Human | `git checkout $BASE_COMMIT`, mark `manage_tasks(action="update_status", task_id=..., status="failed")`, halt loop, and escalate root-cause report to human | Never run Round 6 autonomously |

## 4. Token Budget & Response Depth Discipline

When the user asks to control response depth/token budget (`"short version"`, `"25% depth"`, `"exhaustive"`, `"token budget"`) or when managing context window economics:

| Depth Level | Shortcut Triggers | Target Output Scope |
|---|---|---|
| **[1] 25% Essential** | `"1"`, `"25%"`, `"tldr"`, `"short version"`, `"brief"` | 2–4 sentences max; direct answer and conclusion only |
| **[2] 50% Moderate** | `"2"`, `"50%"`, `"moderate"`, `"balanced"` | 1–3 paragraphs; core answer + key context + 1 concise example |
| **[3] 75% Detailed** | `"3"`, `"75%"`, `"detailed"`, `"thorough"` | Structured tables, trade-offs, alternatives, and implementation steps |
| **[4] 100% Exhaustive** | `"4"`, `"100%"`, `"exhaustive"`, `"full deep dive"` | Complete analysis, edge cases, full code listings, and verification |

When transcripts approach 40,000+ tokens or at major phase transitions (Spec $\rightarrow$ DAG Execution), call `memory_session_handoff` to checkpoint DAG state and continue in a clean session.
