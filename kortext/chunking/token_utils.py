"""
Token Estimation, Git Temporal Metadata and AST Docstring Utilities for Kortext.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

try:
    from tree_sitter import Node as TSNode
except ImportError:
    TSNode = None

_CODE_SYNTAX_CHARS = frozenset("{}[]();:=<>+-*/%&|^~@#\\")


def _is_cjk_char(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x3040 <= cp <= 0x30FF
        or 0xAC00 <= cp <= 0xD7AF
    )


def count_tokens(text: str) -> int:
    """
    Fast token estimator calibrated for code, CJK, and prose density without network blocking.
    - CJK characters are counted at ~1.5 tokens per character.
    - Syntax-dense code (where >=14% of characters are programming syntax punctuation and not a raw JSON dict)
      is estimated at ~3.4 characters per token.
    - Standard prose is estimated at 4 characters per token.
    """
    if not text:
        return 0
    n = len(text)
    sample = text[:2048]
    s_len = len(sample)
    cjk_sample = sum(1 for ch in sample if _is_cjk_char(ch))
    cjk_ratio = cjk_sample / s_len if s_len else 0.0
    if cjk_ratio >= 0.05:
        est_cjk_chars = int(n * cjk_ratio)
        est_non_cjk_chars = n - est_cjk_chars
        return max(1, int(est_cjk_chars * 1.5) + max(0, est_non_cjk_chars // 4))
    if n >= 16 and not text.lstrip().startswith("{"):
        syntax_count = sum(1 for ch in sample if ch in _CODE_SYNTAX_CHARS)
        if syntax_count / s_len >= 0.14:
            return max(1, int(n / 3.4))
    return max(1, n // 4)


estimate_tokens = count_tokens


_GIT_TEMPORAL_CACHE: Dict[str, tuple[float, Optional[Dict[str, str]]]] = {}


def prefetch_git_temporal_info_batch(root_dir: str | Path) -> int:
    """
    Pre-populates _GIT_TEMPORAL_CACHE for a git repository in a single `git log` pass,
    eliminating per-file subprocess spawn overhead when KORTEXT_VERBOSE_PREAMBLE=1.
    """
    root_path = Path(root_dir).resolve()
    if not root_path.is_dir():
        return 0
    try:
        res = subprocess.run(
            ["git", "log", "-n", "200", "--name-only", "--format=COMMIT|%h|%as|%s"],
            cwd=str(root_path),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return 0
        populated = 0
        current_info: Optional[Dict[str, str]] = None
        seen_files: set[str] = set()
        for raw_line in res.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("COMMIT|"):
                parts = line.split("|", 3)
                if len(parts) == 4:
                    current_info = {
                        "commit_hash": parts[1],
                        "commit_date": parts[2],
                        "commit_msg": parts[3],
                    }
            elif current_info is not None and line not in seen_files:
                seen_files.add(line)
                fp = (root_path / line).resolve()
                if fp.is_file():
                    try:
                        mtime = fp.stat().st_mtime
                        _GIT_TEMPORAL_CACHE[str(fp)] = (mtime, dict(current_info))
                        populated += 1
                    except OSError:
                        continue
        return populated
    except Exception:
        return 0


def get_file_temporal_info(file_path: str) -> Optional[Dict[str, str]]:
    """
    Extracts commit hash, commit date, and commit message for a file.
    If not in a git repository or untracked, falls back to filesystem mtime.
    Cached in-memory per (file_path, mtime) so modified files invalidate stale cache entries.
    """
    if not file_path:
        return None

    p = Path(file_path)
    current_mtime = 0.0
    exists = p.exists()
    if exists:
        try:
            current_mtime = p.stat().st_mtime
        except OSError:
            current_mtime = 0.0

    cached_entry = _GIT_TEMPORAL_CACHE.get(file_path)
    if cached_entry is not None:
        if isinstance(cached_entry, tuple) and len(cached_entry) == 2:
            cached_mtime, cached_val = cached_entry
            if cached_mtime == current_mtime:
                return cached_val
        elif isinstance(cached_entry, dict) and current_mtime == 0.0:
            return cached_entry

    res_dict: Optional[Dict[str, str]] = None

    if exists:
        # 1. Try git log
        try:
            cwd_dir = str(p.parent) if p.parent.exists() else None
            res = subprocess.run(
                ["git", "log", "-1", "--format=%h|%as|%s", "--", p.name],
                cwd=cwd_dir,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                parts = res.stdout.strip().split("|", 2)
                if len(parts) == 3:
                    res_dict = {
                        "commit_hash": parts[0],
                        "commit_date": parts[1],
                        "commit_msg": parts[2],
                    }
        except Exception:
            pass

        # 2. Fallback to filesystem mtime
        if not res_dict:
            try:
                mtime = current_mtime or os.path.getmtime(file_path)
                mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
                res_dict = {
                    "commit_hash": "local",
                    "commit_date": mtime_str,
                    "commit_msg": "Working tree modification",
                }
            except Exception:
                res_dict = None

    if not res_dict:
        res_dict = {
            "commit_hash": "local",
            "commit_date": datetime.now().strftime("%Y-%m-%d"),
            "commit_msg": "Working tree modification",
        }

    _GIT_TEMPORAL_CACHE[file_path] = (current_mtime, res_dict)
    return res_dict


def extract_first_docstring_text(node: TSNode, source_bytes: bytes) -> Optional[str]:
    """
    Extracts docstring / doc-comment summary across Python, Rust, C/C++, Java, Kotlin, Swift, C#, Go.
    Checks:
    1. Internal docstrings / expression strings (Python, JS)
    2. Immediately preceding sibling doc comments (Rust '///', Swift '///', C# '///', C/C++ '/**', Go '//')
    """
    if node is None:
        return None

    # 1. Internal docstrings/comments (Python, JS)
    for child in node.children:
        if child.type == "expression_statement":
            for sc in child.children:
                if sc.type == "string":
                    raw = source_bytes[sc.start_byte:sc.end_byte].decode("utf-8", errors="replace")
                    clean = raw.strip("\"' \t\n")
                    first_line = clean.splitlines()[0].strip() if clean else ""
                    return first_line[:180] if first_line else None
        elif child.type in ("comment", "line_comment", "block_comment"):
            raw = source_bytes[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
            clean = raw.strip("/*#/ \t\n")
            first_line = clean.splitlines()[0].strip() if clean else ""
            return first_line[:180] if first_line else None

    # 2. Preceding sibling comments (Rust '///', Go '//', C/C++ '/**', Swift '///', C# '///')
    prev = node.prev_sibling
    preceding_comments: List[str] = []
    while prev and prev.type in ("comment", "line_comment", "block_comment"):
        raw = source_bytes[prev.start_byte:prev.end_byte].decode("utf-8", errors="replace")
        clean = raw.strip("/*#/ \t\n")
        if clean:
            preceding_comments.append(clean)
        prev = prev.prev_sibling

    if preceding_comments:
        preceding_comments.reverse()
        return " ".join(preceding_comments)[:180]

    return None
