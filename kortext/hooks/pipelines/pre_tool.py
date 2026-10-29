"""
Pre-tool interceptor pipeline and security/invariant guards for Antigravity hooks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any, List, Optional, Sequence, Tuple

from kortext.hooks.context import HookContext
from kortext.hooks.types import InterceptorResult
from kortext.trust.sanitizer import SECRET_PATTERNS

logger = logging.getLogger("kortext.hooks.pipelines.pre_tool")

CORE_ARCHITECTURAL_FILES = frozenset({
    "server.py",
    "graph.py",
    "health.py",
    "falkordb_client.py",
    "_qdrant.py",
    "dream.py",
    "invariants.py",
    "tracker.py",
    "sanitizer.py",
    "engine.py",
    "driver.py",
})

import unicodedata

_CMD_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00ad]+")

DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\s+[/~]", re.IGNORECASE),
    re.compile(r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r\s+[/~]", re.IGNORECASE),
    re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=.*of=/dev/", re.IGNORECASE),
    re.compile(r"\bformat\s+c:", re.IGNORECASE),
    re.compile(r">\s*~?/?\.kortext/data/", re.IGNORECASE),
    re.compile(r"\bcp\s+.*~?/?\.kortext/data/", re.IGNORECASE),
    re.compile(r"\bmv\s+.*~?/?\.kortext/data/", re.IGNORECASE),
    re.compile(r"\bshutil\.rmtree\s*\(\s*['\"]?(?:/|~|\$HOME|.*(?:\.kortext|kortext/data))", re.IGNORECASE),
    re.compile(r"\bfind\s+(?:/|~|\$HOME)\b.*-delete\b", re.IGNORECASE),
    re.compile(r"\bchmod\s+-R\s+777\s+/(?:\s|$)", re.IGNORECASE),
    re.compile(r"\bdrop\s+database\b", re.IGNORECASE),
    re.compile(r"\b(?:FLUSHALL|FLUSHDB)\b", re.IGNORECASE),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
    re.compile(r"\bgit\s+checkout\s+--\s+\.(?:\s|$)", re.IGNORECASE),
    re.compile(r"\bgit\s+clean\s+-[a-zA-Z]*f[a-zA-Z]*d\b", re.IGNORECASE),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"),
    re.compile(r"\b(?:bash|sh|zsh)\s+-c\s+['\"].*\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b", re.IGNORECASE),
    re.compile(r"\$\([^)]*\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b", re.IGNORECASE),
    re.compile(r"\bpython3?\s+-c\s+['\"].*\b(?:shutil\.rmtree|os\.remove|os\.unlink|os\.system|Path\([^)]*\)\.unlink)\b.*(?:\.kortext|kortext/data|rm\s+-rf)", re.IGNORECASE),
]


class FastMCPLockGuard:
    """
    Guards against embedded Qdrant direct instantiation.
    Enforces the Sole Custodian model to prevent SQLite/RocksDB exclusive file lock collisions.
    """

    def check(self, context: HookContext) -> InterceptorResult:
        if context.tool_name != "run_command":
            return InterceptorResult.allow()

        cmd = context.command_line
        if not cmd:
            return InterceptorResult.allow()

        if re.search(r"qdrant_client.*path\s*=", cmd, re.IGNORECASE) or re.search(
            r"QdrantClient\s*\([^)]*path\s*=", cmd, re.IGNORECASE
        ):
            reason = (
                "[FastMCPLockGuard] Execution blocked: Direct embedded QdrantClient instantiation with 'path=' "
                "is strictly prohibited. Violates Sole Custodian invariant and causes exclusive lock contention "
                "(sqlite3.OperationalError: database is locked). Use FastMCP server tools instead."
            )
            return InterceptorResult.deny(reason=reason)

        return InterceptorResult.allow()


def _extract_symbol_from_pattern(pattern: str) -> Optional[str]:
    """Extracts a likely symbol identifier from a grep/rg search pattern."""
    if not pattern:
        return None
    cleaned = pattern.strip().strip("'\"")
    # Strip leading definition keywords
    m_def = re.match(
        r"^(?:async\s+)?(?:def|class|fn|function|interface|struct|type)\s+([A-Za-z_][A-Za-z0-9_]*)",
        cleaned,
    )
    if m_def:
        return m_def.group(1)

    # Strip regex boundary markers
    cleaned = re.sub(r"\\b|\^|\$", "", cleaned).strip()
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]{2,}$", cleaned):
        return cleaned

    try:
        from kortext.symbols.seek import extract_symbol_candidates

        cands = extract_symbol_candidates(cleaned)
        if cands:
            return cands[0]
    except Exception as exc:
        logger.debug("Suppressed in pre_tool:%d: %s", 124, exc)
    return None


def _is_symbol_graph_empty() -> bool:
    """Returns True ONLY if a live FalkorDB symbol graph has 0 indexed Symbol nodes (unindexed workspace)."""
    try:
        from kortext._env import get_falkordb_symbols_graph
        from kortext.falkordb_client import get_shared_falkordb_client
        from kortext.hooks.pipelines.pre_invoke import _is_raw_falkor_reachable, _check_unix_socket
        from kortext._home import get_kortext_home

        if not (_check_unix_socket(get_kortext_home() / "run" / "falkordb.sock", 0.02) or _is_raw_falkor_reachable()):
            return False
        client = get_shared_falkordb_client()
        res = client.query(get_falkordb_symbols_graph(), "MATCH (s:Symbol) RETURN count(s) LIMIT 1", read_only=True)
        if res and len(res) > 1 and res[1] and res[1][0]:
            return int(res[1][0][0] or 0) == 0
    except Exception as exc:
        logger.debug("Suppressed in pre_tool:%d: %s", 143, exc)
    return False


def _auto_seek_symbol_hint(pattern: str) -> str:
    """Runs SymbolSeekEngine.seek_symbol on the extracted symbol and formats a compact hint."""
    sym = _extract_symbol_from_pattern(pattern)
    if not sym:
        return ""
    try:
        from kortext._home import get_kortext_home
        from kortext.hooks.pipelines.pre_invoke import _check_unix_socket, _is_raw_falkor_reachable
        from kortext.symbols.seek import SymbolSeekEngine

        if not (_check_unix_socket(get_kortext_home() / "run" / "falkordb.sock", 0.02) or _is_raw_falkor_reachable()):
            return ""

        engine = SymbolSeekEngine()
        res = engine.seek_symbol(sym, detail_level="interface")
        if res and "not_found" not in str(res) and '"matches": []' not in str(res):
            snippet = str(res).strip()
            if len(snippet) > 900:
                snippet = snippet[:897] + "..."
            return f"\n\n⚡ [Auto-Seek Result for `{sym}`]:\n{snippet}"
    except Exception as exc:
        logger.debug("Auto-seek inside AntiRipgrepGate skipped: %s", exc)
    return ""


_SHELL_WRAPPERS = {"sudo", "env", "command", "time", "nohup", "nice"}


def _split_unquoted_shell_stages(cmd: str) -> List[List[str]]:
    """Splits a shell command into subcommands, and each subcommand into pipeline stages, respecting quotes."""
    subcommands: List[List[str]] = []
    curr_pipe: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    escaped = False
    i = 0
    n = len(cmd)

    while i < n:
        ch = cmd[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            buf.append(ch)
            i += 1
            continue
        if quote:
            if ch == quote:
                quote = None
            buf.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stage = "".join(buf).strip()
            if stage:
                curr_pipe.append(stage)
            if curr_pipe:
                subcommands.append(curr_pipe)
                curr_pipe = []
            buf = []
            i += 1
            continue
        if cmd[i : i + 2] in ("&&", "||"):
            stage = "".join(buf).strip()
            if stage:
                curr_pipe.append(stage)
            if curr_pipe:
                subcommands.append(curr_pipe)
                curr_pipe = []
            buf = []
            i += 2
            continue
        if ch == "|":
            stage = "".join(buf).strip()
            if stage:
                curr_pipe.append(stage)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        curr_pipe.append(tail)
    if curr_pipe:
        subcommands.append(curr_pipe)
    return subcommands


def _parse_standalone_grep_or_rg(cmd: str) -> Optional[Tuple[str, str, List[str], List[str]]]:
    """
    Parses a shell command line into logical subcommands (split on &&, ||, ;) and pipeline stages (|),
    respecting quoted strings so regex alternations (`rg "foo|bar"`) are never split as shell pipes.
    Only inspects the FIRST stage of each pipeline (standalone commands that scan files/directories),
    allowing piped filter stages such as `ps aux | grep python` or `pip list | grep torch`.
    Returns (executable, pattern, target_paths, flags) if a standalone rg/grep is found, else None.
    """
    if not cmd or not cmd.strip():
        return None

    # Unwrap subshell command substitutions $(...) and bash/sh -c "..." wrappers
    unwrapped_cmd = cmd.strip()
    subshell_m = re.search(r"\$\(([^)]+)\)", unwrapped_cmd)
    if subshell_m:
        inner_res = _parse_standalone_grep_or_rg(subshell_m.group(1))
        if inner_res is not None:
            return inner_res

    flags_with_arg = {"-e", "--regexp", "-f", "--file", "-g", "--glob", "-t", "--type", "-A", "-B", "-C", "-m", "--max-count"}

    for stages in _split_unquoted_shell_stages(unwrapped_cmd):
        if not stages:
            continue
        first_stage = stages[0].strip()
        if not first_stage:
            continue

        try:
            tokens = shlex.split(first_stage)
        except ValueError:
            tokens = first_stage.split()

        while tokens and ("=" in tokens[0] and not tokens[0].startswith("-")):
            tokens.pop(0)
        while tokens and Path(tokens[0]).name.lower() in _SHELL_WRAPPERS:
            wrapper = Path(tokens.pop(0)).name.lower()
            if wrapper == "env":
                while tokens and ("=" in tokens[0] or tokens[0].startswith("-")):
                    tokens.pop(0)

        if len(tokens) >= 3 and Path(tokens[0]).name.lower() in ("sh", "bash", "zsh", "dash", "ksh") and tokens[1] == "-c":
            inner_res = _parse_standalone_grep_or_rg(tokens[2])
            if inner_res is not None:
                return inner_res

        if not tokens:
            continue

        exe = Path(tokens[0]).name.lower()
        # Allow git log --grep, pytest, curl, etc.
        if exe not in ("rg", "ripgrep", "grep", "egrep", "fgrep"):
            continue

        flags: List[str] = []
        positional: List[str] = []
        explicit_patterns: List[str] = []
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--":
                positional.extend(tokens[i + 1 :])
                break
            if tok.startswith("-"):
                flags.append(tok)
                if tok in flags_with_arg and i + 1 < len(tokens):
                    val = tokens[i + 1]
                    if tok in ("-e", "--regexp"):
                        explicit_patterns.append(val)
                    i += 2
                    continue
                if tok.startswith("--regexp=") or tok.startswith("-e="):
                    explicit_patterns.append(tok.split("=", 1)[1])
            else:
                positional.append(tok)
            i += 1

        if explicit_patterns:
            pattern = explicit_patterns[0]
            targets = positional
        elif positional:
            pattern = positional[0]
            targets = positional[1:]
        else:
            pattern = ""
            targets = []

        return exe, pattern, targets, flags

    return None


class AntiRipgrepGate:
    """
    Enforces the Anti-Ripgrep Mandate across both run_command (rg/grep) and native grep_search.
    Allows piped filter commands (e.g. `ps aux | grep`, `git log --grep`, single-file grep),
    intercepts broad rg/grep scans over source directories, and auto-executes `seek_symbol`
    in the rejection reason so the agent receives the symbol location immediately.
    """

    SYMBOL_INDICATORS = ("def ", "class ", "fn ", "function ", "interface ", "struct ", "type ")

    def check(self, context: HookContext) -> InterceptorResult:
        # 1. Intercept built-in grep_search when searching for symbol declarations or bare symbol identifiers across directories
        if context.tool_name == "grep_search":
            args = context.tool_args
            raw_query = str(args.get("Query") or args.get("query") or "").strip()
            query_str = raw_query.lower()
            search_path = str(args.get("SearchPath") or args.get("search_path") or "").strip()
            if not query_str:
                return InterceptorResult.allow()
            if search_path and os.path.isfile(search_path):
                return InterceptorResult.allow()
            has_indicator = any(query_str.startswith(ind) or f" {ind}" in query_str for ind in self.SYMBOL_INDICATORS)
            is_bare_identifier = bool(
                re.match(r"^[A-Za-z_][A-Za-z0-9_]{3,}$", raw_query)
                and (
                    "_" in raw_query
                    or (any(c.isupper() for c in raw_query) and any(c.islower() for c in raw_query))
                )
            )
            if has_indicator or is_bare_identifier:
                hint = _auto_seek_symbol_hint(raw_query)
                if not has_indicator and not hint:
                    return InterceptorResult.allow()
                if not hint and os.getenv("KORTEXT_ALLOW_0_MATCH_GREP", "0") == "1":
                    return InterceptorResult.allow()
                sym = _extract_symbol_from_pattern(raw_query) or "<symbol>"
                reason = (
                    f"🛑 [AntiRipgrepGate] Symbol definition search via `grep_search` is prohibited. "
                    f"Use `seek_symbol(symbol_name='{sym}')` for deterministic AST point lookup (<2ms, <150 tokens) "
                    f"or `trace_symbol_impact` to preserve context tokens.{hint}"
                )
                return InterceptorResult.deny(reason=reason)
            return InterceptorResult.allow()

        if context.tool_name != "run_command":
            return InterceptorResult.allow()

        cmd = context.command_line
        if not cmd:
            return InterceptorResult.allow()

        parsed = _parse_standalone_grep_or_rg(cmd)
        if parsed is None:
            return InterceptorResult.allow()

        exe, pattern, targets, flags = parsed

        # Allow single-file non-recursive grep/rg or explicit log/journal targets
        is_recursive_flag = any(
            f in ("-r", "-R", "--recursive") or (f.startswith("-") and not f.startswith("--") and ("r" in f[1:] or "R" in f[1:]))
            for f in flags
        )
        if targets and not is_recursive_flag:
            all_single_or_log = True
            for t in targets:
                clean_t = t.strip("'\"")
                if os.path.isfile(clean_t) or clean_t.endswith((".log", ".txt", ".jsonl")) or "journal" in Path(clean_t).parts:
                    continue
                all_single_or_log = False
                break
            if all_single_or_log:
                return InterceptorResult.allow()

        lower_pattern = pattern.lower()
        clean_pat = pattern.strip().strip("'\"")
        is_symbol_search = any(ind in lower_pattern for ind in self.SYMBOL_INDICATORS)
        is_fixed_string = any(f in ("-F", "--fixed-strings") for f in flags) or exe == "fgrep"
        has_regex_meta = bool(re.search(r"[|?*+\[\]()^$\\]", clean_pat))
        is_route_or_path = clean_pat.startswith("/") and "/" in clean_pat[1:]
        # Allow non-symbol literal strings (error messages, log phrases, route paths)
        if not is_symbol_search and (
            is_route_or_path
            or (is_fixed_string and " " in clean_pat)
            or (" " in clean_pat and not has_regex_meta and len(clean_pat.split()) >= 2)
        ):
            return InterceptorResult.allow()

        is_broad_rg = exe in ("rg", "ripgrep") or is_recursive_flag

        if is_symbol_search or is_broad_rg:
            hint = _auto_seek_symbol_hint(pattern)
            if not hint and not is_symbol_search and (os.getenv("KORTEXT_ALLOW_0_MATCH_GREP", "0") == "1" or _is_symbol_graph_empty()):
                return InterceptorResult.allow()
            sym = _extract_symbol_from_pattern(pattern) or "<symbol>"
            reason = (
                f"🛑 [AntiRipgrepGate] Broad symbol search via rg/grep is prohibited. "
                f"Use `seek_symbol(symbol_name='{sym}')` for deterministic AST point lookup (<2ms, <150 tokens) "
                f"or `trace_symbol_impact` to preserve context tokens and prevent context degradation.{hint}"
            )
            return InterceptorResult.deny(reason=reason)

        return InterceptorResult.allow()


class CommandSafetyGuard:
    """
    Intercepts dangerous, destructive, or unauthorized direct substrate writes.
    """

    _SUBSHELL_WRAPPER_RE = re.compile(r"\b(?:sh|bash|zsh|dash|ksh|python3?|node|ruby|perl)\s+-[ce]\b", re.IGNORECASE)
    _QUOTED_LITERAL_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")

    @classmethod
    def _strip_safe_quoted_literals(cls, cmd_str: str) -> str:
        """
        Strips single-quoted literals and double-quoted literals that do not contain
        command substitutions (`$(...)` or backticks), unless the command invokes a shell/interpreter `-c` wrapper.
        """
        if cls._SUBSHELL_WRAPPER_RE.search(cmd_str):
            return cmd_str

        def _replacer(match: re.Match) -> str:
            lit = match.group(0)
            if lit.startswith('"') and ("$(" in lit or "`" in lit):
                return lit
            return '""'

        return cls._QUOTED_LITERAL_RE.sub(_replacer, cmd_str)

    def check(self, context: HookContext) -> InterceptorResult:
        if context.tool_name != "run_command":
            return InterceptorResult.allow()

        cmd = context.command_line
        if not cmd:
            return InterceptorResult.allow()

        normalized_cmd = _CMD_ZERO_WIDTH_RE.sub("", unicodedata.normalize("NFKC", cmd))
        unquoted_cmd = self._strip_safe_quoted_literals(cmd)
        unquoted_norm = self._strip_safe_quoted_literals(normalized_cmd)
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(unquoted_cmd) or pattern.search(unquoted_norm):
                matched = pattern.pattern
                reason = (
                    f"🛑 [CommandSafetyGuard] Forbidden destructive pattern detected: '{matched}'. "
                    "Command execution has been blocked."
                )
                return InterceptorResult.deny(reason=reason)

        return InterceptorResult.allow()


class SecretLeakGuard:
    """
    Scans command lines and file modifications for high-entropy secrets and API tokens.
    """

    def check(self, context: HookContext) -> InterceptorResult:
        if context.tool_name == "run_command":
            cmd = context.command_line
            if cmd:
                for pattern in SECRET_PATTERNS:
                    if pattern.search(cmd):
                        return InterceptorResult.deny(
                            reason="🛑 [SecretLeakGuard] Potential secret/credential leak detected in command line. "
                            "Load secrets from environment variables instead."
                        )

        if context.tool_name in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
            args = context.tool_args
            for field_name in ("CodeContent", "ReplacementContent", "ReplacementChunks", "code", "content", "Content"):
                content = str(args.get(field_name, ""))
                if content:
                    for pattern in SECRET_PATTERNS:
                        if pattern.search(content):
                            return InterceptorResult.deny(
                                reason=f"🛑 [SecretLeakGuard] Potential secret/API key leak detected in '{field_name}'. "
                                "Never commit credentials or tokens into source code."
                            )

        return InterceptorResult.allow()


class BlastRadiusGuard:
    """
    Audits modifications to core architectural files or high-in-degree files in FalkorDB (>= 5 callers).
    Implements ECC GateGuard capabilities:
      1. Prior-AST-Search Credit: credits prior `trace_symbol_impact` or `seek_symbol` calls in the current turn.
      2. Comment/Whitespace Trivial Pass: skips comment/whitespace-only replacements.
      3. Queues blast-radius caller advisories into `pending_advisory.json` for PostInvocation injection,
         and supports strict first-touch fact-forcing denial when `KORTEXT_GATEGUARD_ENFORCE=1` (or `enforce_first_touch=True`).
    """

    def __init__(self, enforce_first_touch: Optional[bool] = None):
        self.enforce_first_touch = (
            enforce_first_touch
            if enforce_first_touch is not None
            else (os.getenv("KORTEXT_GATEGUARD_ENFORCE", "0").strip() == "1")
        )

    @staticmethod
    def _strip_comments_and_ws(text: str) -> str:
        lines = []
        for raw_ln in text.splitlines():
            stripped = raw_ln.strip()
            if not stripped or stripped.startswith(("#", "//")):
                continue
            lines.append("".join(stripped.split()))
        return "\n".join(lines)

    @classmethod
    def _is_trivial_comment_or_whitespace_edit(cls, context: HookContext) -> bool:
        if context.tool_name != "replace_file_content":
            return False
        args = context.tool_args
        old_txt = str(args.get("TargetContent") or "")
        new_txt = str(args.get("ReplacementContent") or "")
        if not old_txt or not new_txt:
            return False
        return cls._strip_comments_and_ws(old_txt) == cls._strip_comments_and_ws(new_txt)

    @staticmethod
    def _is_high_impact_file(target_file: str, file_name: str) -> bool:
        if file_name in CORE_ARCHITECTURAL_FILES:
            return True
        try:
            from kortext._env import get_falkordb_symbols_graph
            from kortext.falkordb_client import get_shared_falkordb_client

            client = get_shared_falkordb_client()
            res = client.query(
                get_falkordb_symbols_graph(),
                "MATCH (caller:Symbol)-[:CALLS|REFERENCES]->(s:Symbol) "
                "WHERE s.file_path ENDS WITH $fname AND caller.file_path <> s.file_path "
                "RETURN count(DISTINCT caller.file_path)",
                {"fname": file_name},
            )
            if res and len(res) > 1 and res[1] and res[1][0]:
                return int(res[1][0][0] or 0) >= 5
        except Exception as exc:
            logger.debug("Suppressed in pre_tool:%d: %s", 578, exc)
        return False

    @staticmethod
    def _has_prior_ast_search_credit(steps: Sequence[Any], file_name: str) -> bool:
        stem = Path(file_name).stem.lower()
        for step in steps:
            for tc in step.get("tool_calls", ()):
                if not isinstance(tc, (dict, MappingProxyType)):
                    continue
                name = str(tc.get("name") or tc.get("tool_name") or "")
                if "trace_symbol_impact" in name or "seek_symbol" in name or "boost_prompt_context" in name:
                    args = tc.get("args") or {}
                    if not isinstance(args, (dict, MappingProxyType)) or not args:
                        return True
                    args_str = str(dict(args)).lower()
                    if stem in args_str or file_name.lower() in args_str or "trace_symbol_impact" in name:
                        return True
        return False

    @staticmethod
    def _check_and_mark_first_touch(session_id: str, target_file: str) -> bool:
        """Returns True if target_file was already gated once in this session; otherwise marks it and returns False."""
        from kortext._home import get_kortext_home

        run_dir = get_kortext_home() / "run"
        state_file = run_dir / "gateguard_checked.json"
        key = f"{session_id or 'default'}:{Path(target_file).resolve()}"
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            data = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
            if not isinstance(data, dict):
                data = {}
            checked = list(data.get("checked") or [])
            if key in checked:
                return True
            checked.append(key)
            data["checked"] = checked[-200:]
            state_file.write_text(json.dumps(data), encoding="utf-8")
            return False
        except Exception:
            return True

    def check(self, context: HookContext) -> InterceptorResult:
        if context.tool_name not in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
            return InterceptorResult.allow()

        target_file = context.target_file
        if not target_file:
            return InterceptorResult.allow()

        if self._is_trivial_comment_or_whitespace_edit(context):
            return InterceptorResult.allow()

        file_name = Path(target_file).name
        if not self._is_high_impact_file(target_file, file_name):
            return InterceptorResult.allow()

        steps = context.read_transcript_steps()
        if self._has_prior_ast_search_credit(steps, file_name):
            return InterceptorResult.allow()

        caller_detail = ""
        try:
            from kortext._env import get_falkordb_symbols_graph
            from kortext.falkordb_client import get_shared_falkordb_client

            c_res = get_shared_falkordb_client().query(
                get_falkordb_symbols_graph(),
                "MATCH (caller:Symbol)-[:CALLS|REFERENCES]->(s:Symbol) "
                "WHERE s.file_path ENDS WITH $fname AND caller.file_path <> s.file_path "
                "RETURN DISTINCT caller.name, caller.file_path LIMIT 4",
                {"fname": file_name},
            )
            if c_res and len(c_res) > 1 and c_res[1]:
                c_items = [f"{r[0]} ({Path(str(r[1] or '')).name})" for r in c_res[1] if r and r[0]]
                if c_items:
                    caller_detail = f" Upstream callers: {', '.join(c_items)}."
        except Exception as exc:
            logger.debug("Suppressed in pre_tool:%d: %s", 657, exc)

        inv_detail = ""
        try:
            from kortext.invariants import grounded_read_path

            g_file = grounded_read_path()

            if g_file.is_file():
                g_items = json.loads(g_file.read_text(encoding="utf-8"))
                if isinstance(g_items, list):
                    matched_invs = [
                        f"[{g.get('domain', 'INV')}] {g.get('rule', '')}"
                        for g in g_items
                        if isinstance(g, dict)
                        and not g.get("quarantined")
                        and Path(str(g.get("file_path", ""))).name == file_name
                    ]
                    if matched_invs:
                        inv_detail = f" Grounded invariants: {'; '.join(matched_invs[:2])}."
        except Exception as exc:
            logger.debug("Suppressed in pre_tool:%d: %s", 678, exc)

        msg = (
            f"[BlastRadiusGuard] ℹ️ Modifying core architectural file '{file_name}' without prior "
            f"trace_symbol_impact or seek_symbol analysis.{caller_detail}{inv_detail} Run `trace_symbol_impact` to verify upstream callers."
        )
        print(msg, file=sys.stderr)

        try:
            from kortext.hooks.pipelines.post_tool import queue_pending_advisory

            conv_id = getattr(context, "conversation_id", "") or ""
            sess_id = context.session_id or ""
            queue_pending_advisory(msg, session_id=sess_id, conversation_id=conv_id)
        except Exception as exc:
            logger.debug("Suppressed in pre_tool:%d: %s", 693, exc)

        if self.enforce_first_touch:
            conv_id = getattr(context, "conversation_id", "") or context.session_id or ""
            if not self._check_and_mark_first_touch(conv_id, target_file):
                return InterceptorResult.deny(
                    reason=(
                        f"🛑 [BlastRadiusGuard / GateGuard First-Touch Gate] Modifying high-blast-radius file "
                        f"`{file_name}` without prior AST investigation in this turn.{caller_detail} "
                        f"Before editing `{file_name}`, call `seek_symbol` or `trace_symbol_impact` to inspect its "
                        "upstream callers and active invariants, or retry the edit after verifying caller contracts."
                    )
                )

        return InterceptorResult.allow(
            reason="Advisory: Core file modification without prior trace_symbol_impact.",
            data=MappingProxyType({"advisory": msg, "target_file": target_file}),
        )


class SensitivePathReadGuard:
    """
    Blocks unauthorized reads of sensitive credential directories (~/.ssh, ~/.aws, ~/.gnupg,
    ~/.kube, ~/.docker, ~/.netrc) and secret .env files (excluding .env.example / .env.template),
    and enforces FM6 read-only confinement when active agent is kortext-researcher.
    """

    _SENSITIVE_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker", ".pypirc", ".netrc"})
    _SAFE_ENV_SUFFIXES = (".env.example", ".env.sample", ".env.template", ".env.test", ".env.defaults")

    def check(self, context: HookContext) -> InterceptorResult:
        if context.tool_name in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
            agent_role = str(
                context.raw_payload.get("agent_name")
                or context.raw_payload.get("subagent_type")
                or os.getenv("KORTEXT_ACTIVE_AGENT", "")
            ).strip().lower()
            if agent_role == "kortext-researcher":
                target = context.target_file or ""
                norm_target = target.replace("\\", "/")
                if "/handoffs/" not in norm_target and "/.kortext/" not in norm_target:
                    return InterceptorResult.deny(
                        reason=(
                            f"🛑 [SensitivePathReadGuard / FM6 Read-Only Confinement] Agent `kortext-researcher` "
                            f"is read-only and may only write handoff artifacts under `~/.kortext/handoffs/` "
                            f"(attempted write to `{target}`)."
                        )
                    )
        if context.tool_name == "view_file":
            target = context.target_file
            if not target:
                return InterceptorResult.allow()
            p = Path(target).expanduser()
            parts = set(p.parts)
            hit_dirs = parts & self._SENSITIVE_DIRS
            if hit_dirs:
                return InterceptorResult.deny(
                    reason=(
                        f"🛑 [SensitivePathReadGuard] Reading sensitive credential path `{target}` "
                        f"({', '.join(sorted(hit_dirs))}) via `view_file` is blocked."
                    )
                )
            name_lower = p.name.lower()
            if (name_lower == ".env" or name_lower.startswith(".env.")) and not name_lower.endswith(self._SAFE_ENV_SUFFIXES):
                return InterceptorResult.deny(
                    reason=(
                        f"🛑 [SensitivePathReadGuard] Reading secret environment file `{p.name}` directly "
                        "is blocked to prevent credential leakage into context. Inspect `.env.example` instead."
                    )
                )
        return InterceptorResult.allow()


class FileOwnershipGuard:
    """Denies a task worker's edit to a file that another in_progress task has claimed."""

    def check(self, context: HookContext) -> InterceptorResult:
        own = os.getenv("KORTEXT_TASK_ID", "").strip()
        if not own or not context.target_file:
            return InterceptorResult.allow()
        if context.tool_name not in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
            return InterceptorResult.allow()
        from kortext.tasks.orchestration_mixin import _as_list, _same_path
        from kortext.tasks.tracker import TaskDAGTracker

        tracker = TaskDAGTracker()
        rows = tracker.client.query(
            tracker.graph,
            "MATCH (t:Task) WHERE t.status = 'in_progress' AND t.id <> $own RETURN t.id, coalesce(t.target_files, [])",
            {"own": own},
        )
        for tid, files in (rows[1] if rows and len(rows) > 1 else []) or []:
            if any(_same_path(context.target_file, f) for f in _as_list(files)):
                return InterceptorResult.deny(
                    reason=f"{context.target_file} is owned by in_progress task '{tid}'. "
                    f"Coordinate through the orchestrator or wait for '{tid}' to complete."
                )
        return InterceptorResult.allow()


class PreToolPipeline:
    """Dispatches PreToolUse interceptors sequentially."""

    def __init__(self, guards: Optional[Sequence[Any]] = None):
        self.guards = tuple(guards) if guards is not None else (
            FastMCPLockGuard(),
            AntiRipgrepGate(),
            CommandSafetyGuard(),
            SecretLeakGuard(),
            SensitivePathReadGuard(),
            BlastRadiusGuard(),
            FileOwnershipGuard(),
        )

    def execute(self, context: HookContext) -> InterceptorResult:
        for guard in self.guards:
            try:
                res = guard.check(context)
                if res.decision == "deny":
                    return res
            except Exception as e:
                logger.debug("Guard %s failed with exception: %s", type(guard).__name__, e)
                continue

        return InterceptorResult.allow()
