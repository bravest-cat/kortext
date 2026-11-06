"""
Kortext Grounded AST & Hybrid Memory Benchmark Runner.
Evaluates public MCP tools against deterministic AST and dense retrieval benchmarks.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("kortext.benchmarks.runner")

# Enforce real tokenizer requirement
try:
    import tiktoken
    _TOKENIZER = tiktoken.get_encoding("cl100k_base")
except ImportError:
    _TOKENIZER = None


def check_tokenizer_available() -> Any:
    """Verifies that tiktoken is installed, refusing to estimate tokens."""
    if _TOKENIZER is None:
        sys.stderr.write(
            "Error: tiktoken is required for benchmark token measurement.\n"
            "Install it via: pip install 'kortext[bench]'\n"
        )
        sys.exit(1)
    return _TOKENIZER


def count_tokens(text: str) -> int:
    """Counts exact tokens using tiktoken cl100k_base as proxy for Gemini tokenizer."""
    tokenizer = check_tokenizer_available()
    if not text:
        return 0
    return len(tokenizer.encode(text))


def check_real_falkordb() -> None:
    """Refuses to run if a real FalkorDB instance does not answer."""
    try:
        from kortext.falkordb_client import get_shared_falkordb_client
        client = get_shared_falkordb_client()
        r = client.get_redis()
        if not r.ping():
            sys.stderr.write("Error: FalkorDB ping failed.\n")
            sys.exit(1)
        # Verify graph module is loaded by listing graphs
        r.execute_command("GRAPH.LIST")
    except Exception as exc:
        sys.stderr.write(f"Error: FalkorDB is not reachable or GRAPH module missing: {exc}\n")
        sys.exit(1)


def normalize_file_path(p: str, root: Path) -> str:
    """Normalizes any candidate path to a relative posix path from root."""
    if not p:
        return ""
    p_str = p.strip().strip("'\"")
    if p_str.lower() in ("null", "none", "unknown", ""):
        return ""
    try:
        path_obj = Path(p_str)
        if path_obj.is_absolute():
            try:
                return path_obj.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                return path_obj.as_posix()
        resolved = (root / path_obj).resolve()
        try:
            return resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            return path_obj.as_posix()
    except Exception:
        return p_str.replace("\\", "/").lstrip("./")


def extract_files_from_search_result(text: str, root: Path) -> List[str]:
    """Extracts ordered list of unique files returned by search_knowledge_and_memory."""
    files: List[str] = []
    seen: Set[str] = set()

    def _add(f: Any) -> None:
        if not f or not isinstance(f, str):
            return
        norm = normalize_file_path(f, root)
        if norm and norm not in seen:
            seen.add(norm)
            files.append(norm)

    try:
        data = json.loads(text)
    except Exception:
        data = {}

    if isinstance(data, dict):
        for s in data.get("symbolic_context", []):
            if isinstance(s, dict):
                _add(s.get("file_path") or s.get("file"))
                for m in s.get("matches", []):
                    if isinstance(m, dict):
                        _add(m.get("file_path") or m.get("file"))
            elif isinstance(s, str):
                for match in re.finditer(r"(?:#\s*File:|file_path\":\s*\")([^\s,\"\n]+)", s):
                    _add(match.group(1))

        for c in data.get("code_chunks", []):
            if isinstance(c, str):
                match = re.search(r"#\s*File:\s*([^\s\n]+)", c)
                if match:
                    _add(match.group(1))
            elif isinstance(c, dict):
                _add(c.get("file_path") or c.get("file"))

        arch = data.get("architectural_overview")
        if arch and isinstance(arch, str):
            for match in re.finditer(r"(?:#\s*File:|File:\s*|file:\s*)([^\s,\"\n]+)", arch):
                _add(match.group(1))

    for match in re.finditer(r"(?:#\s*File:\s*|[\"']file(?:_path)?[\"']\s*:\s*[\"'])([^\s,\"\n]+)", text):
        _add(match.group(1))

    return files


def extract_files_from_seek_result(text: str, root: Path) -> List[str]:
    """Extracts ordered list of unique files returned by seek_symbol."""
    files: List[str] = []
    seen: Set[str] = set()

    def _add(f: Any) -> None:
        if not f or not isinstance(f, str):
            return
        norm = normalize_file_path(f, root)
        if norm and norm not in seen:
            seen.add(norm)
            files.append(norm)

    try:
        data = json.loads(text)
    except Exception:
        data = {}

    if isinstance(data, dict):
        _add(data.get("file_path") or data.get("file"))
        for m in data.get("matches", []):
            if isinstance(m, dict):
                _add(m.get("file") or m.get("file_path"))
                for cl in m.get("callers", []):
                    if isinstance(cl, dict):
                        _add(cl.get("file") or cl.get("file_path"))
                for ce in m.get("callees", []):
                    if isinstance(ce, dict):
                        _add(ce.get("file") or ce.get("file_path"))

    for match in re.finditer(r"(?:[\"']file(?:_path)?[\"']\s*:\s*[\"'])([^\s,\"\n]+)", text):
        _add(match.group(1))

    return files


def extract_files_from_trace_result(text: str, root: Path) -> List[str]:
    """Extracts ordered list of unique files returned by trace_symbol_impact."""
    files: List[str] = []
    seen: Set[str] = set()

    def _add(f: Any) -> None:
        if not f or not isinstance(f, str):
            return
        norm = normalize_file_path(f, root)
        if norm and norm not in seen:
            seen.add(norm)
            files.append(norm)

    try:
        data = json.loads(text)
    except Exception:
        data = {}

    if isinstance(data, dict):
        for node in data.get("direct_impacts", []):
            if isinstance(node, dict):
                _add(node.get("source_file") or node.get("file_path") or node.get("file"))
        for node in data.get("transitive_impacts", []):
            if isinstance(node, dict):
                _add(node.get("source_file") or node.get("file_path") or node.get("file"))
        for node in data.get("type_hierarchies", []):
            if isinstance(node, dict):
                _add(node.get("file_path") or node.get("source_file") or node.get("file"))
        for d in data.get("definitions", []):
            if isinstance(d, dict):
                _add(d.get("file_path") or d.get("file"))
        for f in data.get("omitted_files", []):
            _add(f)

    for match in re.finditer(r"(?:[\"'](?:source_file|file_path|file)[\"']\s*:\s*[\"'])([^\s,\"\n]+)", text):
        _add(match.group(1))

    return files


def extract_files_from_rg_output(stdout: str, root: Path) -> List[str]:
    """Extracts ordered list of unique files matching in ripgrep output."""
    files: List[str] = []
    seen: Set[str] = set()

    for line in stdout.splitlines():
        line_clean = line.strip()
        if not line_clean or line_clean == "--":
            continue
        parts = re.split(r"[:-]", line, maxsplit=2)
        if len(parts) >= 2 and parts[1].isdigit():
            cand = parts[0].strip()
            norm = normalize_file_path(cand, root)
            if norm and norm not in seen:
                seen.add(norm)
                files.append(norm)

    return files


def compute_recall_at_5(retrieved: Sequence[str], gold_set: Set[str]) -> float:
    """Fraction of gold relevant files retrieved in top 5."""
    if not gold_set:
        return 0.0
    top5 = retrieved[:5]
    hits = set(top5) & gold_set
    return len(hits) / float(len(gold_set))


def compute_mrr(retrieved: Sequence[str], gold_set: Set[str]) -> float:
    """Reciprocal rank of first gold relevant file in retrieved list."""
    for idx, f in enumerate(retrieved, 1):
        if f in gold_set:
            return 1.0 / idx
    return 0.0


def percentile(values: List[float], p: float) -> float:
    """Computes p-th percentile of values using linear interpolation."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    k = (len(sorted_v) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_v[int(k)])
    return float(sorted_v[f] * (c - k) + sorted_v[c] * (k - f))


def get_git_sha(repo_dir: Path) -> str:
    """Returns current git commit SHA or 'unknown'."""
    try:
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True)
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"


def get_tool_versions() -> Dict[str, str]:
    """Collects installed versions of benchmarked tools and runtime."""
    versions = {
        "python": sys.version.split()[0],
    }
    try:
        import tiktoken
        versions["tiktoken"] = getattr(tiktoken, "__version__", "unknown")
    except Exception:
        pass
    try:
        import fastmcp
        versions["fastmcp"] = getattr(fastmcp, "__version__", "unknown")
    except Exception:
        pass
    try:
        import qdrant_client
        versions["qdrant_client"] = getattr(qdrant_client, "__version__", "unknown")
    except Exception:
        pass
    try:
        import falkordb
        versions["falkordb"] = getattr(falkordb, "__version__", "unknown")
    except Exception:
        pass
    if shutil.which("rg"):
        try:
            res = subprocess.run(["rg", "--version"], capture_output=True, text=True)
            versions["ripgrep"] = res.stdout.splitlines()[0]
        except Exception:
            pass
    return versions


async def _cleanup_bench_storage(collection: str, symbols_graph: str) -> None:
    """Removes dedicated benchmark collection from Qdrant, file cache, and symbols graph from FalkorDB."""
    try:
        from kortext.cache import IngestCache
        IngestCache().clear_collection(collection)
    except Exception as exc:
        logger.debug("Non-fatal: IngestCache cleanup for '%s': %s", collection, exc)

    try:
        from kortext._qdrant import get_sync_qdrant_client
        qc = get_sync_qdrant_client()
        if qc.collection_exists(collection):
            qc.delete_collection(collection)
    except Exception as exc:
        logger.debug("Non-fatal: Qdrant cleanup for '%s': %s", collection, exc)

    try:
        from kortext.falkordb_client import get_shared_falkordb_client
        fc = get_shared_falkordb_client()
        r = fc.get_redis()
        graphs = r.execute_command("GRAPH.LIST") or []
        if symbols_graph in [str(g) for g in graphs]:
            r.execute_command("GRAPH.DELETE", symbols_graph)
    except Exception as exc:
        logger.debug("Non-fatal: FalkorDB cleanup for '%s': %s", symbols_graph, exc)


async def execute_tool_call(
    client: Any,
    task: Dict[str, Any],
    collection: str,
    symbols_graph: str,
    root_path: Path,
) -> Tuple[str, List[str], float, int]:
    """Executes a single public MCP tool call and returns (output_text, retrieved_files, latency_ms, tokens)."""
    t_type = task["type"]
    t0 = time.perf_counter()

    if t_type == "search":
        res = await client.call_tool(
            "search_knowledge_and_memory",
            {
                "query": task["question"],
                "collection": collection,
            },
        )
        text_out = res.content[0].text if res.content else ""
        retrieved_files = extract_files_from_search_result(text_out, root_path)

    elif t_type == "seek":
        res = await client.call_tool(
            "seek_symbol",
            {
                "symbol_name": task["symbol"],
            },
        )
        text_out = res.content[0].text if res.content else ""
        retrieved_files = extract_files_from_seek_result(text_out, root_path)

    elif t_type == "trace":
        res = await client.call_tool(
            "trace_symbol_impact",
            {
                "symbol": task["symbol"],
                "symbols_graph": symbols_graph,
            },
        )
        text_out = res.content[0].text if res.content else ""
        retrieved_files = extract_files_from_trace_result(text_out, root_path)

    else:
        raise ValueError(f"Unknown task type: {t_type}")

    t1 = time.perf_counter()
    latency_ms = (t1 - t0) * 1000.0
    tokens = count_tokens(text_out)

    return text_out, retrieved_files, latency_ms, tokens


def execute_rg_baseline(
    task: Dict[str, Any],
    root_path: Path,
    extra_exclude_globs: Optional[List[str]] = None,
) -> Tuple[str, List[str], float, int]:
    """Executes baseline rg -n -C 5 <rg_pattern> over root_path scoped to indexed file set."""
    rg_bin = shutil.which("rg") or "rg"
    pattern = task["rg_pattern"]

    cmd = [
        rg_bin,
        "-n",
        "-C",
        "5",
        "--glob",
        "!data/**",
        "--glob",
        "!data/*",
        "--glob",
        "!kortext/benchmarks/questions.json",
    ]
    if extra_exclude_globs:
        for g in extra_exclude_globs:
            cmd.extend(["--glob", f"!{g}"])
    cmd.extend(["-e", pattern])

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root_path),
            capture_output=True,
            text=True,
            timeout=30.0,
        )
        stdout = proc.stdout
    except Exception:
        stdout = ""
    t1 = time.perf_counter()

    latency_ms = (t1 - t0) * 1000.0
    tokens = count_tokens(stdout)
    retrieved_files = extract_files_from_rg_output(stdout, root_path)

    return stdout, retrieved_files, latency_ms, tokens


async def run_benchmark_async(
    root: Optional[Path | str] = None,
    k: int = 3,
    out_path: Optional[Path | str] = None,
    questions_path: Optional[Path | str] = None,
    collection: str = "kortext_bench",
    symbols_graph: str = "kortext_bench_symbols",
) -> Dict[str, Any]:
    """Async core running indexing, benchmarks, metrics, and persistence."""
    check_tokenizer_available()
    check_real_falkordb()

    # FastEmbed cache default if not set
    if "FASTEMBED_CACHE_PATH" not in os.environ:
        cand_cache = Path.home() / ".cache" / "fastembed"
        if cand_cache.is_dir():
            os.environ["FASTEMBED_CACHE_PATH"] = str(cand_cache)

    repo_root = Path(__file__).resolve().parent.parent.parent
    root_path = Path(root).resolve() if root is not None else repo_root

    out_file = Path(out_path).resolve() if out_path is not None else (repo_root / "data" / "benchmark_results.json")
    out_file.parent.mkdir(parents=True, exist_ok=True)

    q_file = Path(questions_path).resolve() if questions_path is not None else (Path(__file__).resolve().parent / "questions.json")
    if not q_file.is_file():
        sys.stderr.write(f"Error: Questions file not found at {q_file}\n")
        sys.exit(1)

    questions: List[Dict[str, Any]] = json.loads(q_file.read_text(encoding="utf-8"))
    print(f"Loaded {len(questions)} benchmark questions from {q_file.name}")
    print(f"Target root directory: {root_path}")
    print(f"Repetitions per task (k): {k}")

    # Prepare storage
    await _cleanup_bench_storage(collection, symbols_graph)

    old_col = os.environ.get("KORTEXT_COLLECTION")
    old_sym = os.environ.get("KORTEXT_SYMBOLS_GRAPH")
    os.environ["KORTEXT_COLLECTION"] = collection
    os.environ["KORTEXT_SYMBOLS_GRAPH"] = symbols_graph

    from fastmcp import Client
    from kortext.server import mcp, _ACTIVE_INDEX_TASKS, _COLLECTION_INDEX_LOCKS

    # Exclude data/ and benchmark questions from indexing and rg baseline
    extra_excludes = ["data", "data/*", "data/**", "kortext/benchmarks/questions.json"]
    extra_rg_globs: List[str] = []
    try:
        rel_q = q_file.relative_to(root_path).as_posix()
        if rel_q not in extra_excludes:
            extra_excludes.append(rel_q)
        if rel_q != "kortext/benchmarks/questions.json":
            extra_rg_globs.append(rel_q)
    except ValueError:
        pass

    task_results: List[Dict[str, Any]] = []

    try:
        async with Client(mcp) as client:
            print(f"Indexing directory '{root_path}' into collection '{collection}' and graph '{symbols_graph}'...")
            idx_start = time.perf_counter()
            await client.call_tool(
                "index_directory",
                {
                    "path": str(root_path),
                    "collection": collection,
                    "symbols_graph": symbols_graph,
                    "exclude_patterns": extra_excludes,
                },
            )

            # Wait for background indexing tasks to finish
            while _ACTIVE_INDEX_TASKS:
                await asyncio.gather(*list(_ACTIVE_INDEX_TASKS), return_exceptions=True)

            lock = _COLLECTION_INDEX_LOCKS.get(collection)
            if lock:
                async with lock:
                    pass

            idx_duration = time.perf_counter() - idx_start
            print(f"Indexing completed in {idx_duration:.2f}s")

            # Verify FalkorDB symbol graph was populated and non-empty
            from kortext.falkordb_client import get_shared_falkordb_client
            fc = get_shared_falkordb_client()
            r = fc.get_redis()
            graph_query_res = r.execute_command("GRAPH.QUERY", symbols_graph, "MATCH (n) RETURN count(n)")
            node_count = (
                graph_query_res[1][0][0]
                if (graph_query_res and len(graph_query_res) > 1 and graph_query_res[1] and graph_query_res[1][0])
                else 0
            )
            assert node_count > 0, f"Benchmark symbols graph '{symbols_graph}' is empty ({node_count} nodes) after indexing"

            # Execute tasks
            for idx, task in enumerate(questions, 1):
                t_id = task["id"]
                t_type = task["type"]
                gold_files = [normalize_file_path(g, root_path) for g in task.get("gold_files", [])]
                gold_set = set(gold_files)

                kortext_runs: List[Dict[str, Any]] = []
                rg_runs: List[Dict[str, Any]] = []

                for rep in range(k):
                    # 1. MCP Tool Run
                    t_out, t_files, t_lat, t_tok = await execute_tool_call(
                        client=client,
                        task=task,
                        collection=collection,
                        symbols_graph=symbols_graph,
                        root_path=root_path,
                    )
                    t_recall = compute_recall_at_5(t_files, gold_set)
                    t_mrr = compute_mrr(t_files, gold_set)
                    t_pass = any(f in gold_set for f in t_files)

                    kortext_runs.append({
                        "rep": rep + 1,
                        "latency_ms": t_lat,
                        "reply_tokens": t_tok,
                        "retrieved_files": t_files,
                        "recall_at_5": t_recall,
                        "mrr": t_mrr,
                        "passed": t_pass,
                    })

                    # 2. Ripgrep Baseline Run
                    rg_out, rg_files, rg_lat, rg_tok = execute_rg_baseline(
                        task=task,
                        root_path=root_path,
                        extra_exclude_globs=extra_rg_globs,
                    )
                    rg_recall = compute_recall_at_5(rg_files, gold_set)
                    rg_mrr = compute_mrr(rg_files, gold_set)
                    rg_pass = any(f in gold_set for f in rg_files)

                    rg_runs.append({
                        "rep": rep + 1,
                        "latency_ms": rg_lat,
                        "reply_tokens": rg_tok,
                        "retrieved_files": rg_files,
                        "recall_at_5": rg_recall,
                        "mrr": rg_mrr,
                        "passed": rg_pass,
                    })

                k_pass_at_k = any(r["passed"] for r in kortext_runs)
                k_pass_pow_k = all(r["passed"] for r in kortext_runs)
                k_mean_recall = sum(r["recall_at_5"] for r in kortext_runs) / float(k)
                k_mean_mrr = sum(r["mrr"] for r in kortext_runs) / float(k)

                rg_pass_at_k = any(r["passed"] for r in rg_runs)
                rg_pass_pow_k = all(r["passed"] for r in rg_runs)
                rg_mean_recall = sum(r["recall_at_5"] for r in rg_runs) / float(k)
                rg_mean_mrr = sum(r["mrr"] for r in rg_runs) / float(k)

                print(
                    f"[{idx:02d}/{len(questions):02d}] {t_id:<10} ({t_type:<6}) | "
                    f"kortext: R@5={k_mean_recall:.2f} MRR={k_mean_mrr:.2f} pass@k={int(k_pass_at_k)} | "
                    f"rg: R@5={rg_mean_recall:.2f} MRR={rg_mean_mrr:.2f} pass@k={int(rg_pass_at_k)}"
                )

                task_results.append({
                    "id": t_id,
                    "type": t_type,
                    "question": task.get("question"),
                    "symbol": task.get("symbol"),
                    "gold_files": gold_files,
                    "gold_symbols": task.get("gold_symbols", []),
                    "rg_pattern": task.get("rg_pattern"),
                    "kortext": {
                        "pass_at_k": k_pass_at_k,
                        "pass_pow_k": k_pass_pow_k,
                        "mean_recall_at_5": k_mean_recall,
                        "mean_mrr": k_mean_mrr,
                        "runs": kortext_runs,
                    },
                    "ripgrep_baseline": {
                        "pass_at_k": rg_pass_at_k,
                        "pass_pow_k": rg_pass_pow_k,
                        "mean_recall_at_5": rg_mean_recall,
                        "mean_mrr": rg_mean_mrr,
                        "runs": rg_runs,
                    },
                })

    finally:
        # Restore environment variables
        if old_col is not None:
            os.environ["KORTEXT_COLLECTION"] = old_col
        else:
            os.environ.pop("KORTEXT_COLLECTION", None)

        if old_sym is not None:
            os.environ["KORTEXT_SYMBOLS_GRAPH"] = old_sym
        else:
            os.environ.pop("KORTEXT_SYMBOLS_GRAPH", None)

        print("Cleaning up dedicated benchmark collections and graph...")
        await _cleanup_bench_storage(collection, symbols_graph)

    # Compute aggregate metrics per tool and overall
    def _aggregate(subset: List[Dict[str, Any]], system_key: str) -> Dict[str, Any]:
        if not subset:
            return {}
        n = len(subset)
        pass_at_k_cnt = sum(1 for t in subset if t[system_key]["pass_at_k"])
        pass_pow_k_cnt = sum(1 for t in subset if t[system_key]["pass_pow_k"])

        all_runs = [r for t in subset for r in t[system_key]["runs"]]
        recalls = [r["recall_at_5"] for r in all_runs]
        mrrs = [r["mrr"] for r in all_runs]
        latencies = [r["latency_ms"] for r in all_runs]
        tokens = [r["reply_tokens"] for r in all_runs]

        return {
            "task_count": n,
            "pass_at_k": round(pass_at_k_cnt / float(n), 4),
            "pass_pow_k": round(pass_pow_k_cnt / float(n), 4),
            "recall_at_5": round(sum(recalls) / float(len(recalls)), 4) if recalls else 0.0,
            "mrr": round(sum(mrrs) / float(len(mrrs)), 4) if mrrs else 0.0,
            "latency_p50_ms": round(percentile(latencies, 50.0), 2),
            "latency_p95_ms": round(percentile(latencies, 95.0), 2),
            "reply_tokens_avg": round(sum(tokens) / float(len(tokens)), 1) if tokens else 0.0,
            "reply_tokens_p50": round(percentile(tokens, 50.0), 1),
            "reply_tokens_p95": round(percentile(tokens, 95.0), 1),
        }

    tool_types = ["search", "seek", "trace"]
    tool_names = {
        "search": "search_knowledge_and_memory",
        "seek": "seek_symbol",
        "trace": "trace_symbol_impact",
    }

    by_tool: Dict[str, Any] = {}
    for tt in tool_types:
        sub = [t for t in task_results if t["type"] == tt]
        name = tool_names.get(tt, tt)
        by_tool[name] = {
            "kortext": _aggregate(sub, "kortext"),
            "ripgrep_baseline": _aggregate(sub, "ripgrep_baseline"),
        }

    overall = {
        "kortext": _aggregate(task_results, "kortext"),
        "ripgrep_baseline": _aggregate(task_results, "ripgrep_baseline"),
    }

    payload = {
        "metadata": {
            "git_sha": get_git_sha(repo_root),
            "date": datetime.now(timezone.utc).isoformat(),
            "k": k,
            "root": str(root_path),
            "tool_versions": get_tool_versions(),
            "tokenizer": "tiktoken cl100k_base (proxy for Gemini tokenizer)",
            "tokenizer_note": "cl100k_base is used as a proxy for Gemini tokenizer",
            "rg_scope_rule": "excludes data/ and kortext/benchmarks/questions.json; includes tests/ matching index_directory behavior",
            "file_exclusions": ["data/**", "kortext/benchmarks/questions.json"],
        },
        "summary": {
            "overall": overall,
            "by_tool": by_tool,
        },
        "tasks": task_results,
    }

    out_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nBenchmark results saved to {out_file}")

    _print_summary_table(payload["summary"])

    return payload


def _print_summary_table(summary: Dict[str, Any]) -> None:
    """Prints a clear plain English comparison table of Kortext vs Ripgrep baseline."""
    print("\n" + "=" * 90)
    print(f"{'Tool':<32} {'System':<10} {'Pass@k':<8} {'Pass^k':<8} {'R@5':<8} {'MRR':<8} {'Lat p50':<10} {'Tokens':<8}")
    print("-" * 90)

    by_tool = summary.get("by_tool", {})
    for tool_name, data in by_tool.items():
        k_d = data.get("kortext", {})
        rg_d = data.get("ripgrep_baseline", {})
        print(
            f"{tool_name:<32} {'kortext':<10} "
            f"{k_d.get('pass_at_k', 0.0):<8.2f} {k_d.get('pass_pow_k', 0.0):<8.2f} "
            f"{k_d.get('recall_at_5', 0.0):<8.2f} {k_d.get('mrr', 0.0):<8.2f} "
            f"{k_d.get('latency_p50_ms', 0.0):<10.1f} {k_d.get('reply_tokens_avg', 0.0):<8.0f}"
        )
        print(
            f"{'':<32} {'ripgrep':<10} "
            f"{rg_d.get('pass_at_k', 0.0):<8.2f} {rg_d.get('pass_pow_k', 0.0):<8.2f} "
            f"{rg_d.get('recall_at_5', 0.0):<8.2f} {rg_d.get('mrr', 0.0):<8.2f} "
            f"{rg_d.get('latency_p50_ms', 0.0):<10.1f} {rg_d.get('reply_tokens_avg', 0.0):<8.0f}"
        )
        print("-" * 90)

    ov = summary.get("overall", {})
    k_ov = ov.get("kortext", {})
    rg_ov = ov.get("ripgrep_baseline", {})
    print(
        f"{'OVERALL':<32} {'kortext':<10} "
        f"{k_ov.get('pass_at_k', 0.0):<8.2f} {k_ov.get('pass_pow_k', 0.0):<8.2f} "
        f"{k_ov.get('recall_at_5', 0.0):<8.2f} {k_ov.get('mrr', 0.0):<8.2f} "
        f"{k_ov.get('latency_p50_ms', 0.0):<10.1f} {k_ov.get('reply_tokens_avg', 0.0):<8.0f}"
    )
    print(
        f"{'':<32} {'ripgrep':<10} "
        f"{rg_ov.get('pass_at_k', 0.0):<8.2f} {rg_ov.get('pass_pow_k', 0.0):<8.2f} "
        f"{rg_ov.get('recall_at_5', 0.0):<8.2f} {rg_ov.get('mrr', 0.0):<8.2f} "
        f"{rg_ov.get('latency_p50_ms', 0.0):<10.1f} {rg_ov.get('reply_tokens_avg', 0.0):<8.0f}"
    )
    print("=" * 90 + "\n")


def run_benchmark(
    root: Optional[Path | str] = None,
    k: int = 3,
    out_path: Optional[Path | str] = None,
    questions_path: Optional[Path | str] = None,
    collection: str = "kortext_bench",
    symbols_graph: str = "kortext_bench_symbols",
) -> Dict[str, Any]:
    """Synchronous entry point for kortext benchmark CLI."""
    return asyncio.run(
        run_benchmark_async(
            root=root,
            k=k,
            out_path=out_path,
            questions_path=questions_path,
            collection=collection,
            symbols_graph=symbols_graph,
        )
    )
