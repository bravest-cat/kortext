"""
Affected tests detection using git diff and AST symbol call graph.

Maps changed line ranges to symbols in FalkorDB, walks upstream callers to find
affected test files and functions, and generates ready-to-run test runner commands.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from kortext.cache import CACHE_DB_PATH, IngestCache
from kortext.falkordb_client import (
    FALKORDB_HOST,
    FALKORDB_PORT,
    get_shared_falkordb_client,
)

logger = logging.getLogger("kortext.affected")

CODE_EXTENSIONS = {
    ".py", ".go", ".ts", ".tsx", ".js", ".jsx",
    ".java", ".c", ".cpp", ".cc", ".cxx", ".h",
    ".hpp", ".cs", ".rs", ".swift", ".kt",
}

TEST_FILE_PATTERNS = (
    r"^test_.*\.py$",
    r".*_test\.py$",
    r".*_test\.go$",
    r".*\.test\.(?:ts|tsx|js|jsx)$",
    r".*\.spec\.(?:ts|tsx|js|jsx)$",
    r".*Test\.java$",
    r".*Tests\.java$",
    r".*TestCase\.java$",
)


def is_test_file(file_path: str) -> bool:
    """Checks whether a file path points to a test file."""
    p = Path(file_path)
    name = p.name
    for pat in TEST_FILE_PATTERNS:
        if re.match(pat, name, re.IGNORECASE):
            return True
    parts = [part.lower() for part in p.parts]
    if ("tests" in parts or "test" in parts) and p.suffix.lower() in CODE_EXTENSIONS:
        return True
    return False


def is_code_file(file_path: str) -> bool:
    """Checks whether a file is a recognized source code file."""
    return Path(file_path).suffix.lower() in CODE_EXTENSIONS


def parse_git_diff_ranges(diff_text: str) -> Dict[str, List[Tuple[int, int]]]:
    """
    Parses unified git diff output into a dict of {relative_file_path: [(start_line, end_line), ...]}.
    """
    file_ranges: Dict[str, List[Tuple[int, int]]] = {}
    current_file: Optional[str] = None

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                b_path = parts[3]
                if b_path.startswith("b/"):
                    current_file = b_path[2:]
                else:
                    current_file = b_path
                file_ranges.setdefault(current_file, [])
        elif line.startswith("+++ "):
            path_part = line[4:].strip()
            if path_part.startswith("b/"):
                current_file = path_part[2:]
            elif path_part != "/dev/null":
                current_file = path_part
            if current_file:
                file_ranges.setdefault(current_file, [])
        elif line.startswith("@@ ") and current_file:
            # Parse @@ -a,b +c,d @@
            m = re.search(r"@@\s+-[0-9]+(?:,[0-9]+)?\s+\+([0-9]+)(?:,([0-9]+))?\s+@@", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                end = start + max(1, count) - 1
                file_ranges[current_file].append((start, end))

    return file_ranges


def find_affected_tests(
    workspace_root: Optional[str] = None,
    base: str = "HEAD",
    graph_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Finds tests affected by git changes against base branch/commit using static AST call graph.
    Limit: static call graph only, so dynamic dispatch and fixtures can be missed.
    """
    root = Path(workspace_root or os.getcwd()).resolve()
    target_graph = graph_name or os.getenv("KORTEXT_SYMBOLS_GRAPH", "kortext_symbols")

    # 1. Run git diff against base
    cmd = ["git", "diff", "-U0", base]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
        diff_text = proc.stdout
    except Exception as exc:
        return {
            "complete": False,
            "reason": f"git diff command failed: {exc}",
            "affected_tests": [],
            "test_files": [],
            "command": "uv run pytest -q",
            "base": base,
            "changed_files": [],
            "changed_symbols": [],
        }

    changed_files_map = parse_git_diff_ranges(diff_text)
    changed_files = list(changed_files_map.keys())

    # Untracked files when base == HEAD
    if base.upper() == "HEAD":
        try:
            stat_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            for s_line in stat_proc.stdout.splitlines():
                if s_line.startswith("?? "):
                    untracked_f = s_line[3:].strip()
                    if untracked_f not in changed_files_map:
                        changed_files_map[untracked_f] = [(1, 999999)]
                        changed_files.append(untracked_f)
        except Exception:
            pass

    if not changed_files:
        return {
            "complete": True,
            "reason": None,
            "affected_tests": [],
            "test_files": [],
            "command": "",
            "base": base,
            "changed_files": [],
            "changed_symbols": [],
        }

    # 2. Substrate availability probe
    falkor_client = None
    try:
        falkor_client = get_shared_falkordb_client(
            host=os.getenv("FALKORDB_HOST", "127.0.0.1"),
            port=int(os.getenv("FALKORDB_PORT", "6379")),
        )
        falkor_client.get_redis().ping()
    except Exception as exc:
        return {
            "complete": False,
            "reason": f"FalkorDB symbols graph unavailable: {exc}; run full test suite.",
            "affected_tests": [],
            "test_files": [],
            "command": "uv run pytest -q",
            "base": base,
            "changed_files": changed_files,
            "changed_symbols": [],
        }

    incomplete_reasons: List[str] = []
    complete = True

    # 3. Check config/non-code files, graph presence, staleness
    ingest_cache = IngestCache(CACHE_DB_PATH)
    all_cached_files: Dict[str, Dict[str, float]] = {}
    try:
        with ingest_cache._get_connection() as conn:
            rows = conn.execute("SELECT file_path, mtime, updated_at FROM file_cache").fetchall()
            for r in rows:
                norm_fp = str(r["file_path"]).replace("\\", "/")
                all_cached_files[norm_fp] = {"mtime": float(r["mtime"]), "updated_at": float(r["updated_at"])}
    except Exception:
        pass

    for rel_path in changed_files:
        abs_path = (root / rel_path).resolve()
        if not is_code_file(rel_path):
            complete = False
            incomplete_reasons.append(f"Non-code or configuration file modified: {rel_path}")
            continue

        # Check presence in symbol graph
        norm_abs = str(abs_path).replace("\\", "/")
        norm_rel = rel_path.replace("\\", "/")
        check_q = (
            "MATCH (f:File) "
            "WHERE f.path = $abs_p OR f.path ENDS WITH $rel_p "
            "RETURN f.path LIMIT 1"
        )
        file_res = falkor_client.query(target_graph, check_q, {"abs_p": norm_abs, "rel_p": norm_rel})
        found_in_graph = bool(file_res and len(file_res) > 1 and file_res[1])

        if not found_in_graph:
            complete = False
            incomplete_reasons.append(f"Changed file not in symbol graph: {rel_path}")
            continue

        # Check if file was modified after indexing
        if abs_path.is_file():
            try:
                mtime = abs_path.stat().st_mtime
                cached_entry = all_cached_files.get(norm_abs) or all_cached_files.get(str(abs_path))
                if cached_entry:
                    cached_mtime = cached_entry.get("mtime", 0.0)
                    if mtime > cached_mtime + 0.01:
                        complete = False
                        incomplete_reasons.append(f"Symbol graph is older than modified file: {rel_path}")
            except OSError:
                pass

    # 4. Map changed line ranges to symbols
    changed_symbols: Set[str] = set()
    affected_tests_map: Dict[str, Set[str]] = {}

    for rel_path, ranges in changed_files_map.items():
        abs_path = (root / rel_path).resolve()
        norm_abs = str(abs_path).replace("\\", "/")
        norm_rel = rel_path.replace("\\", "/")

        if not is_code_file(rel_path):
            continue

        if is_test_file(rel_path):
            # Test file itself was modified
            affected_tests_map.setdefault(rel_path, set())
            for start_l, end_l in ranges:
                q_sym = (
                    "MATCH (s:Symbol)-[r:DEFINED_IN]->(f:File) "
                    "WHERE (f.path = $abs_p OR f.path ENDS WITH $rel_p) "
                    "  AND r.line <= $end_line AND r.end_line >= $start_line "
                    "RETURN DISTINCT s.name"
                )
                sym_res = falkor_client.query(
                    target_graph,
                    q_sym,
                    {"abs_p": norm_abs, "rel_p": norm_rel, "start_line": start_l, "end_line": end_l},
                )
                if sym_res and len(sym_res) > 1 and sym_res[1]:
                    for row in sym_res[1]:
                        if row and row[0]:
                            s_name = str(row[0])
                            if s_name.startswith("test_") or "test" in s_name.lower():
                                affected_tests_map[rel_path].add(s_name)
            continue

        # Code file changed: find declared symbols overlapping changed ranges
        found_syms_for_file = False
        for start_l, end_l in ranges:
            q_sym = (
                "MATCH (s:Symbol)-[r:DEFINED_IN]->(f:File) "
                "WHERE (f.path = $abs_p OR f.path ENDS WITH $rel_p) "
                "  AND r.line <= $end_line AND r.end_line >= $start_line "
                "RETURN DISTINCT s.name, s.qualified_name"
            )
            sym_res = falkor_client.query(
                target_graph,
                q_sym,
                {"abs_p": norm_abs, "rel_p": norm_rel, "start_line": start_l, "end_line": end_l},
            )
            if sym_res and len(sym_res) > 1 and sym_res[1]:
                for row in sym_res[1]:
                    if row and row[0]:
                        changed_symbols.add(str(row[0]))
                        found_syms_for_file = True

        # Fallback: if lines did not match specific symbol (e.g. imports or constants), take all file symbols
        if not found_syms_for_file:
            q_all = (
                "MATCH (s:Symbol)-[:DEFINED_IN]->(f:File) "
                "WHERE f.path = $abs_p OR f.path ENDS WITH $rel_p "
                "RETURN DISTINCT s.name"
            )
            all_res = falkor_client.query(target_graph, q_all, {"abs_p": norm_abs, "rel_p": norm_rel})
            if all_res and len(all_res) > 1 and all_res[1]:
                for row in all_res[1]:
                    if row and row[0]:
                        changed_symbols.add(str(row[0]))
                        found_syms_for_file = True

        # A file in the graph with no symbols means the graph is incomplete for it.
        # Callers cannot know which tests cover this file, so mark incomplete.
        if not found_syms_for_file:
            complete = False
            incomplete_reasons.append(f"Changed file has no symbols in graph: {rel_path}")

    # 5. Walk callers upstream through the call graph to find test functions
    for sym_name in changed_symbols:
        # Upstream callers up to 4 hops
        q_callers = (
            "MATCH (s:Symbol {name: $name}) "
            "MATCH (caller:Symbol)-[:CALLS*1..4]->(s) "
            "MATCH (caller)-[:DEFINED_IN]->(tf:File) "
            "RETURN DISTINCT caller.name, tf.path"
        )
        call_res = falkor_client.query(target_graph, q_callers, {"name": sym_name})
        if call_res and len(call_res) > 1 and call_res[1]:
            for row in call_res[1]:
                if len(row) >= 2:
                    c_name = str(row[0])
                    tf_path = str(row[1])
                    if is_test_file(tf_path):
                        # Normalize path relative to workspace root
                        try:
                            rel_tf = str(Path(tf_path).relative_to(root))
                        except ValueError:
                            rel_tf = tf_path
                        affected_tests_map.setdefault(rel_tf, set()).add(c_name)

        # Direct file references / imports from test files
        q_refs = (
            "MATCH (s:Symbol {name: $name}) "
            "MATCH (tf:File)-[:REFERENCES|IMPORTS]->(s) "
            "RETURN DISTINCT tf.path"
        )
        ref_res = falkor_client.query(target_graph, q_refs, {"name": sym_name})
        if ref_res and len(ref_res) > 1 and ref_res[1]:
            for row in ref_res[1]:
                if row and row[0]:
                    tf_path = str(row[0])
                    if is_test_file(tf_path):
                        try:
                            rel_tf = str(Path(tf_path).relative_to(root))
                        except ValueError:
                            rel_tf = tf_path
                        affected_tests_map.setdefault(rel_tf, set())

    # 6. Format results and build runner command
    affected_tests_list = []
    test_files_list = sorted(affected_tests_map.keys())

    all_python = True
    python_test_functions: List[str] = []

    for tf in test_files_list:
        funcs = sorted(affected_tests_map[tf])
        affected_tests_list.append({
            "file": tf,
            "functions": funcs,
        })
        if not tf.endswith(".py"):
            all_python = False
        else:
            python_test_functions.extend(funcs)

    command = ""
    if complete and test_files_list:
        if all_python:
            files_arg = " ".join(test_files_list)
            # Filter functions to valid test identifiers
            test_funcs = [fn for fn in python_test_functions if fn.startswith("test_") or "test" in fn.lower()]
            if test_funcs and len(test_funcs) <= 15:
                filter_expr = " or ".join(test_funcs)
                command = f'uv run pytest {files_arg} -k "{filter_expr}" -q'
            else:
                command = f"uv run pytest {files_arg} -q"
        elif any(f.endswith(".go") for f in test_files_list):
            command = "go test ./..."
        elif any(f.endswith((".ts", ".js")) for f in test_files_list):
            command = f"npm test -- {' '.join(test_files_list)}"
        else:
            command = "uv run pytest -q"
    else:
        command = "uv run pytest -q"

    final_reason = "; ".join(incomplete_reasons) if incomplete_reasons else None

    return {
        "complete": complete,
        "reason": final_reason,
        "affected_tests": affected_tests_list,
        "test_files": test_files_list,
        "command": command,
        "base": base,
        "changed_files": changed_files,
        "changed_symbols": sorted(changed_symbols),
    }
