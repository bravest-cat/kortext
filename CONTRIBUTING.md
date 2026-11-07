# Contributing to Kortext

This document defines development setup, contribution workflows, testing invariants, and architectural rules for Kortext.

---

## Code of Conduct

All contributors and participants must adhere to our [Code of Conduct](CODE_OF_CONDUCT.md). Please report any violations or concerns to the project maintainers.

---

## Local Development Setup

Kortext requires Python 3.11 or 3.12 and the `uv` packaging tool.

### 1. Environment Bootstrap

```bash
# Clone repository
git clone https://github.com/kortext/kortext.git
cd kortext

# Initialize virtual environment and install dependencies with development extras
uv venv && source .venv/bin/activate
uv pip install -e ".[dev,tier2-languages]"
```

### 2. Substrate Initialization

Kortext manages its vector and graph substrates natively:

```bash
# Provision local FalkorDB and embedded Qdrant daemons
kortext start

# Verify running substrates
kortext status
```

### 3. Environment Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Configure `EMBED_BASE_URL` and `EMBED_MODEL_NAME` to point to your local or remote inference endpoint (such as oMLX, TEI, Ollama, vLLM, or OpenAI).

### 4. Dashboard Stylesheet Compilation

Regenerate the static Tailwind stylesheet after editing templates or dashboard JS modules:

```bash
npx -y tailwindcss@3 -o kortext/dashboard/static/css/tailwind.css --content "./kortext/dashboard/templates/**/*.html,./kortext/dashboard/static/js/**/*.js" --minify
```

---

## Testing & Quality Invariants

All pull requests must satisfy our automated verification gates prior to merge.

### 1. Mandatory Test-Driven Development (TDD) Workflow

Every modification or new feature must strictly follow the TDD lifecycle:

1. **RED**: Write a failing test specifying the desired behavior or reproducing the bug before touching production code. Run the test to verify the expected failure signature.
2. **GREEN**: Implement the minimal amount of code needed to ensure the test passes cleanly.
3. **REFACTOR**: Clean up structure, enforce strict typing and immutability, and eliminate redundancy.
4. **VERIFY**: Run the complete test suite to ensure zero regressions across all modules.

### 2. Test Execution

Run the project test suite via `uv`:

```bash
# Run complete test suite quietly
uv run pytest -q

# Run specific test modules
uv run pytest -q tests/test_code_hygiene.py

# Validate plugin manifests and subagent configurations
python3 scripts/test_plugin_manifest.py
```

### 3. Minimum Test Coverage

Maintain at least **80% line and branch test coverage** across all production modules. Every public function and branching condition must be tested with isolated unit or integration tests.

---

## Core Architectural Invariants

Every code contribution must comply with the following architectural invariants:

### 1. Anti-Ripgrep Invariant (Deterministic AST Navigation)
- Never use brute-force `ripgrep` (`rg`) or broad regex sweeps to locate code symbols.
- Always use deterministic Tree-sitter AST symbol lookups via `seek_symbol` or AST queries.
- Preserves context window tokens and guarantees compiler-grade precision across renames and call graph analyses.

### 2. FastMCP Sole Custodian Model
- The FastMCP daemon (`kortext.server`) is the single, exclusive owner of on-disk vector stores (Qdrant) and key-value state (RocksDB).
- Subagents, background sidecars, and tools must never instantiate `QdrantClient(path=...)` directly in embedded mode or directly access raw database files in `~/.kortext/data/`.
- Embedded backends acquire exclusive file locks (`LOCK_EX`); concurrent access causes database lock crashes and segment corruption. All vector operations must flow through FastMCP server tools.

### 3. Substrate Resilience & Local IPC
- Graph communication uses Unix Domain Sockets at `~/.kortext/run/falkordb.sock` (<1ms IPC latency) with automatic fallback to loopback TCP at `127.0.0.1:6379`.
- Subsystems must degrade gracefully to in-memory fallbacks if substrates are unreachable, emitting clear diagnostics.

### 4. 5-Signal Memory Precedence
- Tier 0 AST symbols provide deterministic ground truth and take absolute precedence over Tier 1 episodic facts, Tier 2 structural maps, Tier 3 vector chunks, and Tier 4 associative walks.

### 5. Immutability & File Organization
- Never mutate incoming function parameters or data structures in place. Return fresh copies.
- Keep source files cohesive and focused (200-400 lines typical, 800 lines soft ceiling).

---

## Git Workflow & Conventions

### 1. Commit Message Format

Commit messages must follow Conventional Commits:

```
<type>: <description>

<optional detailed body explaining context, rationale, and breaking changes>
```

#### Allowed Types:
- `feat`: A new feature, capability, or user-facing enhancement.
- `fix`: A bug fix or defect remediation.
- `refactor`: Code restructuring without behavioral changes.
- `docs`: Documentation updates or comment adjustments.
- `test`: Adding, modifying, or refactoring test suites.
- `chore`: Maintenance tasks, dependency bumps, or tool configurations.
- `perf`: Performance optimizations and memory reductions.
- `ci`: CI/CD pipeline, build scripts, or daemon automation changes.

#### Conventions:
- Write the summary line in the imperative mood (e.g., `feat: implement AST symbol seek`, not `feat: implemented`).
- Keep the summary line under 72 characters.
- Detail the architectural rationale and motivation in the body.

### 2. Branch Naming
- `feat/<description>` for new capabilities
- `fix/<description>` for bug fixes
- `refactor/<description>` for structural improvements
- `docs/<description>` for documentation changes
- `perf/<description>` for performance enhancements

### 3. Pre-Commit Verification Gate

Before staging, committing, or pushing:
1. `uv run pytest -q` passes 100% with zero errors.
2. `python3 scripts/test_plugin_manifest.py` passes schema validation.
3. No hardcoded secrets, machine-specific paths, or direct file locks committed.
4. AST deterministic symbol rules respected.

---

## Pull Request Guidelines

1. **Review Diff**: Inspect `git diff` against `main` to ensure no transient debugging code or unintended changes exist.
2. **Include Evidence**: Document test execution commands and results in the pull request description.
3. **Link Issues**: Reference related issue numbers in the PR description.
4. **Clean History**: Rebase against the target branch and keep commit history logical.

---

## Reporting Issues

- **Bug Reports**: Submit an issue detailing OS version, Python version, steps to reproduce, and logs.
- **Feature Requests**: Outline the proposed capability, architectural fit with the 5-signal memory model, and expected API contract.
