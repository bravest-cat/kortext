"""
Tests for Kortext Grounded Benchmark Suite.
Verifies questions integrity and end-to-end execution of the benchmark runner.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import pytest

from kortext.benchmarks.runner import (
    check_tokenizer_available,
    run_benchmark,
    normalize_file_path,
    extract_files_from_search_result,
    extract_files_from_seek_result,
    extract_files_from_trace_result,
    extract_files_from_rg_output,
    compute_recall_at_5,
    compute_mrr,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = REPO_ROOT / "kortext" / "benchmarks" / "questions.json"


def test_questions_file_exists_and_schema():
    assert QUESTIONS_PATH.is_file(), f"Missing questions file at {QUESTIONS_PATH}"
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    assert len(questions) >= 30, f"Expected at least 30 questions, got {len(questions)}"

    search_count = sum(1 for q in questions if q.get("type") == "search")
    seek_count = sum(1 for q in questions if q.get("type") == "seek")
    trace_count = sum(1 for q in questions if q.get("type") == "trace")

    assert search_count >= 10, f"Expected at least 10 search questions, got {search_count}"
    assert seek_count >= 10, f"Expected at least 10 seek questions, got {seek_count}"
    assert trace_count >= 10, f"Expected at least 10 trace questions, got {trace_count}"


def test_questions_gold_files_and_symbols_exist():
    """Asserts every gold file and gold symbol exists on disk so questions cannot rot silently."""
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))

    for q in questions:
        q_id = q.get("id", "unknown")
        gold_files = q.get("gold_files", [])
        gold_symbols = q.get("gold_symbols", [])
        rg_pat = q.get("rg_pattern", "")

        assert gold_files, f"Question {q_id} has empty gold_files"
        assert gold_symbols, f"Question {q_id} has empty gold_symbols"
        assert rg_pat, f"Question {q_id} has empty rg_pattern"

        all_defined_symbols = set()
        for gf in gold_files:
            file_path = REPO_ROOT / gf
            assert file_path.is_file(), f"Gold file '{gf}' for question {q_id} does not exist"

            tree = ast.parse(file_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    all_defined_symbols.add(node.name)

        for sym in gold_symbols:
            assert sym in all_defined_symbols, (
                f"Gold symbol '{sym}' for question {q_id} was not found in any of {gold_files}"
            )


def test_search_questions_rg_patterns_are_verbatim_question_words():
    """Asserts that search question rg_patterns are alternations of 2 to 4 words from the question."""
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    search_qs = [q for q in questions if q.get("type") == "search"]
    assert len(search_qs) == 10
    for q in search_qs:
        pattern = q.get("rg_pattern", "")
        question = q.get("question", "")
        terms = pattern.split("|")
        assert 2 <= len(terms) <= 4, f"Pattern '{pattern}' in {q['id']} does not have 2-4 terms"
        for term in terms:
            assert term in question, f"Term '{term}' in pattern '{pattern}' does not appear in question '{question}'"


def test_tokenizer_missing_exits(monkeypatch):
    """Refuses to run and exits with code 1 if tiktoken is unavailable."""
    import kortext.benchmarks.runner as runner_module
    monkeypatch.setattr(runner_module, "_TOKENIZER", None)

    with pytest.raises(SystemExit) as exc_info:
        runner_module.check_tokenizer_available()
    assert exc_info.value.code == 1


def test_metric_helpers():
    gold = {"kortext/_qdrant.py", "kortext/daemon.py"}
    retrieved = ["kortext/_qdrant.py", "kortext/retrieval/engine.py"]
    assert compute_recall_at_5(retrieved, gold) == 0.5
    assert compute_mrr(retrieved, gold) == 1.0

    retrieved2 = ["other.py", "kortext/daemon.py"]
    assert compute_mrr(retrieved2, gold) == 0.5

    retrieved3 = ["other.py", "other2.py"]
    assert compute_mrr(retrieved3, gold) == 0.0


@pytest.mark.usefixtures("real_falkordb")
def test_benchmark_runner_end_to_end_on_temp_repo(tmp_path: Path):
    """Runs runner end to end on small temp repo with real FalkorDB and verifies cleanup."""
    pytest.importorskip("tiktoken")
    # Create small mock project
    proj_dir = tmp_path / "mock_project"
    proj_dir.mkdir()

    calc_file = proj_dir / "calculator.py"
    calc_file.write_text(
        "class MiniCalculator:\n"
        "    '''Performs basic math calculations.'''\n"
        "    def add_numbers(self, a: int, b: int) -> int:\n"
        "        return a + b\n",
        encoding="utf-8",
    )

    app_file = proj_dir / "app.py"
    app_file.write_text(
        "from calculator import MiniCalculator\n\n"
        "def run_app():\n"
        "    calc = MiniCalculator()\n"
        "    return calc.add_numbers(1, 2)\n",
        encoding="utf-8",
    )

    mini_questions = [
        {
            "id": "mini_search_01",
            "type": "search",
            "question": "how are two numbers added together",
            "gold_files": ["calculator.py"],
            "gold_symbols": ["MiniCalculator"],
            "rg_pattern": "class MiniCalculator",
        },
        {
            "id": "mini_seek_01",
            "type": "seek",
            "question": "where is MiniCalculator defined",
            "symbol": "MiniCalculator",
            "gold_files": ["calculator.py"],
            "gold_symbols": ["MiniCalculator"],
            "rg_pattern": "class MiniCalculator",
        },
        {
            "id": "mini_trace_01",
            "type": "trace",
            "question": "what calls add_numbers",
            "symbol": "add_numbers",
            "gold_files": ["app.py"],
            "gold_symbols": ["run_app"],
            "rg_pattern": "add_numbers\\(",
        },
    ]

    q_file = tmp_path / "mini_questions.json"
    q_file.write_text(json.dumps(mini_questions), encoding="utf-8")

    out_file = tmp_path / "results.json"
    coll_name = "test_e2e_bench_col"
    graph_name = "test_e2e_bench_sym"

    results = run_benchmark(
        root=proj_dir,
        k=1,
        out_path=out_file,
        questions_path=q_file,
        collection=coll_name,
        symbols_graph=graph_name,
    )

    assert out_file.is_file()
    assert "metadata" in results
    assert "rg_scope_rule" in results["metadata"]
    assert "file_exclusions" in results["metadata"]
    assert "summary" in results
    assert "overall" in results["summary"]
    assert "by_tool" in results["summary"]
    assert len(results["tasks"]) == 3

    # Verify dedicated collections were cleaned up
    from kortext._qdrant import get_sync_qdrant_client
    from kortext.falkordb_client import get_shared_falkordb_client

    qc = get_sync_qdrant_client()
    assert not qc.collection_exists(coll_name)

    fc = get_shared_falkordb_client()
    r = fc.get_redis()
    graphs = [str(g) for g in (r.execute_command("GRAPH.LIST") or [])]
    assert graph_name not in graphs
