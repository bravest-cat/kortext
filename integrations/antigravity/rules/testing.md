---
trigger: glob
description: "Testing standards: 80%+ test coverage, strict TDD workflow (RED-GREEN-REFACTOR), and test structure."
globs: "**/{test_*,*_test,tests}/**/*.py"
---

# Testing Requirements

## Minimum Test Coverage: 80%

All production code must maintain at least 80% line and branch test coverage. Every feature, bug fix, or refactor must be accompanied by appropriate automated tests.

### Required Test Types

1. **Unit Tests** - Individual functions, utilities, domain logic, and data models. Fast, fully isolated, zero network dependencies.
2. **Integration Tests** - Substrate communication (FalkorDB UDS/TCP, Qdrant FastMCP), tool dispatch routes, and database operations.
3. **E2E Tests** - Complete multi-step workflows, Task DAG execution, and autonomous loops.

## Test-Driven Development (TDD) Workflow

TDD is **MANDATORY** for all implementation tasks:

```
    ┌──────────────┐
    │  1. RED      │  Write failing test specifying expected behavior
    └──────┬───────┘
           │ Run test suite (verify test fails with expected signature)
           ▼
    ┌──────────────┐
    │  2. GREEN    │  Implement MINIMAL code necessary to pass
    └──────┬───────┘
           │ Run test suite (verify test passes cleanly)
           ▼
    ┌──────────────┐
    │  3. REFACTOR │  Improve code structure, eliminate duplication, verify typing
    └──────┬───────┘
           │ Run full test suite (ensure 100% passing, 80%+ coverage)
           ▼
    ┌──────────────┐
    │  4. VERIFY   │  Execute Verification Gate before task handoff
    └──────────────┘
```

1. **Write test first (RED):** Define the test describing the desired behavior before touching implementation files. Run the test to confirm it fails for the expected reason.
2. **Minimal implementation (GREEN):** Write only the code required to make the test pass. Avoid speculative features.
3. **Refactor (IMPROVE):** Clean code structure, enforce immutability, ensure strict typing, and eliminate redundancy while keeping tests green.
4. **Verify coverage (80%+):** Run the complete test suite to confirm zero regressions across the codebase.

## Troubleshooting Test Failures

1. **Check test isolation:** Ensure tests do not share state, leak socket descriptors, or leave uncommitted database transactions. Use temporary directories (`tmp_path`) and fixture cleanup.
2. **Verify mock fidelity:** Ensure mocks, stubs, and doubles accurately reflect real substrate interfaces and FastMCP schemas.
3. **Fix implementation, not tests:** When tests fail, diagnose and fix the production code. Never weaken test assertions to mask an underlying defect.

## Test Structure (AAA Pattern)

Structure all test functions using the **Arrange-Act-Assert** pattern:

```python
def test_redistribute_weights_moves_dense_mass_to_active_signals():
    # Arrange
    weights = {"ast": 0.35, "bm25": 0.25, "dense": 0.30, "temporal": 0.10}

    # Act
    result = redistribute_weights(weights, inactive_keys=["dense"])

    # Assert
    assert result["dense"] == 0.0
    assert abs(sum(result.values()) - 1.0) < 1e-9
```

### Test Naming Conventions

Use descriptive, behavior-oriented names that clearly state the scenario and expected outcome:

```python
def test_seek_symbol_returns_declaration_and_signature_when_symbol_exists(): ...
def test_seek_symbol_returns_none_when_symbol_not_indexed(): ...
def test_embedded_qdrant_raises_lock_conflict_on_direct_instantiation(): ...
def test_task_dag_executes_independent_leaf_tasks_in_parallel(): ...
```

## Verification Gate

Before declaring any task complete or submitting code changes:
- Run project test suite:
  ```bash
  uv run pytest -q
  ```
- If modifying plugin configurations, hooks, or agent manifests:
  ```bash
  python3 scripts/test_plugin_manifest.py
  ```
- 100% of tests must pass with zero unhandled errors.
