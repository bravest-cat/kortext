"""Helper utilities for Kortext FastMCP server formatting, MINJA retrieval filtering, and path validation."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

SENSITIVE_DIR_NAMES = frozenset({
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".npm",
    ".pypirc",
    ".netrc",
    ".gemini",
    ".kortext",
})

FORBIDDEN_SYSTEM_PREFIXES = (
    "/etc",
    "/private/etc",
    "/System",
    "/var/root",
    "/proc",
    "/sys",
    "/dev",
)


def _is_trusted_code_definition_chunk(raw_chunk: str) -> bool:
    """
    Returns True when a code chunk is an AST-indexed source module (e.g., sanitizer.py or pre_tool.py)
    that legitimately defines regex security patterns rather than an untrusted prose injection.
    """
    s = str(raw_chunk).strip()
    if not s.startswith("# File:"):
        return False
    first_line = s.split("\n", 1)[0]
    fp = first_line.replace("# File:", "").strip()
    ext = Path(fp).suffix.lower()
    if ext not in (".py", ".rs", ".ts", ".js", ".go"):
        return False
    # Only exempt this installation's own security pattern table modules, resolved, not by substring.
    pkg = Path(__file__).resolve().parent
    trusted = {pkg / "trust" / "sanitizer.py", pkg / "hooks" / "pipelines" / "pre_tool.py"}
    p = Path(fp)
    try:
        return (p if p.is_absolute() else pkg.parent / p).resolve() in trusted
    except (OSError, ValueError):
        return False


def _filter_untrusted_injection_chunks(res: Dict[str, Any]) -> Dict[str, Any]:
    """
    Filters out untrusted (Tier 3) chunks or episodic facts containing imperative
    prompt-injection directives (after NFKC/zero-width normalization) before returning
    search_knowledge_and_memory results, while exempting Kortext's own sanitizer source code.
    """
    from kortext.trust.sanitizer import (
        COMPILED_PATTERNS,
        TIER3_NORMATIVE_PATTERNS,
        normalize_for_security_scan,
    )

    def _is_poisoned(raw_item: Any, is_code: bool = False) -> bool:
        raw_str = str(raw_item)
        if is_code and _is_trusted_code_definition_chunk(raw_str):
            return False
        norm_text = normalize_for_security_scan(raw_str)
        if not norm_text:
            return False
        return any(p.search(norm_text) for p in COMPILED_PATTERNS) or any(
            p.search(norm_text) for p in TIER3_NORMATIVE_PATTERNS
        )

    out = dict(res)
    code_chunks = out.get("code_chunks")
    if isinstance(code_chunks, list):
        out["code_chunks"] = [ch for ch in code_chunks if not _is_poisoned(ch, is_code=True)]

    episodic_history = out.get("episodic_history")
    if isinstance(episodic_history, list):
        out["episodic_history"] = [fact for fact in episodic_history if not _is_poisoned(fact, is_code=False)]

    return out


def _format_federated_markdown(res: Dict[str, Any], query: str, token_budget: int, max_bytes: int = 9500) -> str:
    """Formats a federated retrieval dictionary into a concise Markdown view bounded under CLI 1.4.7's 10KB inline limit."""
    verdict = res.get("confidence_verdict", "strong")
    guidance = res.get("confidence_guidance", "")
    md_lines = [
        f"## Federated Memory Retrieval: {query}",
        f"**Estimated Tokens:** {res.get('total_estimated_tokens', 0)} / {token_budget} | **Confidence:** `{verdict}`",
    ]
    if guidance:
        md_lines.append(f"> {guidance}\n")
    else:
        md_lines.append("")

    sym_ctx = res.get("symbolic_context", [])
    md_lines.append("### Tier 0: AST Symbol Declarations")
    if sym_ctx:
        for item in sym_ctx:
            if isinstance(item, dict):
                s_name = item.get("symbol") or item.get("name", "Unknown")
                kind = item.get("kind", "symbol")
                f_path = item.get("file") or item.get("file_path", "")
                sig = item.get("signature", "")
                lines = item.get("lines", [])
                line_str = f":{lines[0]}" if lines else ""
                md_lines.append(f"- **`{s_name}`** ({kind}) at `{f_path}{line_str}`")
                if sig:
                    md_lines.append(f"  - Signature: `{sig}`")
            else:
                md_lines.append(f"- {item}")
    else:
        md_lines.append("_No candidate symbol declarations located._")
    md_lines.append("")

    code_chunks = res.get("code_chunks", [])
    md_lines.append("### Tier 1: Hybrid Codebase Chunks")
    if code_chunks:
        for i, chunk in enumerate(code_chunks, 1):
            md_lines.append(f"#### Reference {i}\n```\n{chunk}\n```")
    else:
        md_lines.append("_No relevant codebase chunks retrieved._")
    md_lines.append("")

    episodic_history = res.get("episodic_history", [])
    md_lines.append("### Tier 2: Episodic History & Decisions")
    if episodic_history:
        for fact in episodic_history:
            md_lines.append(f"- {fact}")
    else:
        md_lines.append("_No historical episodic facts found._")
    md_lines.append("")

    arch_overview = res.get("architectural_overview")
    if arch_overview:
        md_lines.append("### Tier 3: System Architecture Overview")
        md_lines.append(arch_overview)
        md_lines.append("")

    formatted = "\n".join(md_lines)
    encoded = formatted.encode("utf-8")
    if len(encoded) > max_bytes:
        truncated = encoded[: max_bytes - 64].decode("utf-8", errors="ignore").rstrip()
        return truncated + "\n\n_...[Truncated to <10KB inline MCP ceiling]_"
    return formatted


def _validate_index_path(raw_path: str) -> tuple[bool, str, Optional[Path]]:
    """Validates directory path for indexing and blocks home root, sensitive, or system directories."""
    try:
        resolved = Path(raw_path).expanduser().resolve()
    except Exception as exc:
        return False, f"❌ Invalid path '{raw_path}': {exc}", None

    if not resolved.exists():
        return False, f"❌ Path does not exist: {raw_path}", None
    if not resolved.is_dir():
        return False, f"❌ Path is not a directory: {raw_path}", None

    resolved_str = str(resolved)
    if resolved_str == "/" or resolved == Path.home().resolve():
        return False, f"❌ Refusing to index filesystem or user home root '{resolved_str}' directly.", None

    for prefix in FORBIDDEN_SYSTEM_PREFIXES:
        if resolved_str == prefix or resolved_str.startswith(prefix + "/"):
            return False, f"❌ Refusing to index protected system directory: {resolved_str}", None

    if set(resolved.parts) & SENSITIVE_DIR_NAMES:
        return False, f"❌ Refusing to index sensitive credential directory: {resolved_str}", None

    return True, "", resolved
