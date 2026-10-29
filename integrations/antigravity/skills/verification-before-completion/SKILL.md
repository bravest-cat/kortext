---
name: verification-before-completion
description: Use before claiming any task, bugfix, refactor, or review is complete, before marking a Task DAG node completed in manage_tasks, or before emitting a completion handoff.
---

# Verification Before Completion Protocol

Enforces an ironclad pre-completion gate: every completion claim must be backed by fresh command execution in the current turn (`exit_code == 0`), line-by-line acceptance criteria verification, `IntegrityModeAuditor` compliance, and dual graph-journal persistence.

## 1. The Iron Verification Law

> **NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE IN THE CURRENT TURN.**
> If a test ran before your last edit—even a "one-line comment or typo fix"—that test run is **STALE AND INVALID**. Never say *"should pass"*, *"looks good"*, or *"tests passed earlier"*. Run the command now and read the exit code and summary string.

## 2. The 4-Phase Pre-Completion Gate

| Phase | Action | Mandatory Verification Check |
|---|---|---|
| **1. Discover Authoritative Gates** | Identify targeted unit tests, full regression suite, manifest checks, and linters | Python: `uv run pytest -q`, `python3 scripts/test_plugin_manifest.py`, `uv run ruff check .`<br>Node/TS: `pnpm test`, `npx tsc --noEmit` |
| **2. Execute Fresh in Current Turn** | Run commands via `run_command` after the final file edit | Inspect `exit_code == 0`, zero unhandled tracebacks in `stderr`, and capture exact summary string (e.g., `"669 passed in 8.42s"`). |
| **3. Line-by-Line Acceptance & Integrity Audit** | Map every prompt/spec requirement to verified `file:line` | Verify zero `unittest.mock` or test-cheating in production files (`IntegrityModeAuditor`), frozen models, and zero leftover `[DEBUG-xxxx]` probes (`git diff \| grep DEBUG`). |
| **4. Dual Persistence & Artifact Handoff** | Persist outcome to FalkorDB/Qdrant and write `<150`-token handoff | Call `record_system_outcome(category="implementation", ...)` and `manage_tasks(action="complete_task", test_command=..., tests_passed=True)`. |

## 3. Dual Persistence & Artifact-First Handoff Pattern

1. **Complete DAG Task (with Hard Gate Verification)**:
   ```python
   manage_tasks(
       action="complete_task",
       task_id="task_xxxx",
       modified_files=["kortext/module.py", "tests/test_module.py"],
       test_command="uv run pytest tests/test_module.py -q",
       tests_passed=True,
       description="Implemented X; verified 18 passed in 0.42s.",
   )
   ```
2. **Persist Significant Outcomes to Episodic Memory**:
   ```python
   record_system_outcome(
       episode_name="Implemented <feature_or_fix>",
       content="<Concise summary of root cause/implementation and fresh test evidence>",
       category="implementation",
   )
   ```

## 4. Rationalization Defense Table

| Common Excuse | Why It Fails | Mandated Protocol Action |
|---|---|---|
| *"I only fixed a docstring/import after running `pytest`, no need to re-run."* | Import edits and syntax touch-ups routinely break module loading or ruff checks. | Re-run `uv run pytest -q` after the final edit—no exceptions. |
| *"The targeted unit test passed, so I don't need to run the full suite."* | Local changes frequently break upstream callers in other test modules. | Run `trace_symbol_impact` callers' tests and the full suite (`uv run pytest -q`). |
| *"The test failed with an unrelated environment error, so my code is done."* | Unverified code is not done; hidden regressions masquerade as "environment errors". | Diagnose why the test failed (`systematic-debugging`) or report the task as `blocked`—never `completed`. |

## 5. Post-Session Retrospective & Glossary Sharpening (`/retro`)

At the end of a milestone or complex session:
1. **Record project lessons**: Persist non-trivial debugging breakthroughs or project conventions via `record_system_outcome(category="decision", trust_level="tool_confirmed")`.
2. **Sharpen Ubiquitous Language (`GLOSSARY.md`)**: Append concise 1-row definitions for any newly introduced domain concepts to `GLOSSARY.md` so future sessions share the exact terminology.
