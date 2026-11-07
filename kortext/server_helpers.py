"""Helper utilities for Kortext FastMCP server formatting, MINJA retrieval filtering, and path validation."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Union
import copy
import json

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


PROTECTED_REPLY_KEYS = frozenset({"status", "truncated", "dropped_count", "injectSteps"})


def enforce_reply_size_limit(
    data: Union[str, Dict[str, Any]],
    item_keys: Optional[Sequence[str]] = None,
    max_bytes: int = 10000,
) -> str:
    """
    Enforces the Antigravity 10 KB on-demand MCP tool reply size limit.
    When JSON serialized payload exceeds max_bytes, drops lowest-ranked items
    from the longest serialized lists without cutting JSON mid-string, and sets:
        "truncated": True
        "dropped_count": <int>
    """
    if isinstance(data, str):
        payload = json.loads(data)
    else:
        payload = copy.deepcopy(data)

    if not isinstance(payload, dict):
        return json.dumps(payload, separators=(",", ":"))

    current_json = json.dumps(payload, separators=(",", ":"))
    if len(current_json.encode("utf-8")) <= max_bytes:
        return current_json

    keys_to_search = list(item_keys) if item_keys else [
        "code_chunks",
        "episodic_history",
        "matches",
        "items",
        "results",
        "symbols",
        "symbolic_context",
    ]

    dropped_count = 0
    payload["truncated"] = True
    payload["dropped_count"] = 0

    while True:
        current_json = json.dumps(payload, separators=(",", ":"))
        if len(current_json.encode("utf-8")) <= max_bytes:
            return current_json

        # 1. Pop from the longest serialized candidate list
        candidate_keys = [
            k for k in keys_to_search
            if isinstance(payload.get(k), list) and len(payload[k]) > 0
        ]
        if candidate_keys:
            best_k = max(
                candidate_keys,
                key=lambda k: len(json.dumps(payload[k], separators=(",", ":")).encode("utf-8")),
            )
            payload[best_k].pop()
            dropped_count += 1
            payload["dropped_count"] = dropped_count
            continue

        # 2. Fallback: pop from the longest serialized list among any remaining lists in payload
        other_list_keys = [
            k for k, v in payload.items()
            if k not in PROTECTED_REPLY_KEYS and isinstance(v, list) and len(v) > 0
        ]
        if other_list_keys:
            best_k = max(
                other_list_keys,
                key=lambda k: len(json.dumps(payload[k], separators=(",", ":")).encode("utf-8")),
            )
            payload[best_k].pop()
            dropped_count += 1
            payload["dropped_count"] = dropped_count
            continue

        # 3. Clear optional heavy string fields
        cleared_heavy = False
        for heavy_field in ("markdown", "architectural_overview"):
            if heavy_field in payload and payload[heavy_field]:
                payload[heavy_field] = None
                dropped_count += 1
                payload["dropped_count"] = dropped_count
                cleared_heavy = True
                break
        if cleared_heavy:
            continue

        # 4. Truncate or drop remaining non-protected fields (handles single huge items)
        removable_keys = [
            k for k in payload
            if k not in PROTECTED_REPLY_KEYS and payload[k] is not None
        ]
        if removable_keys:
            largest_k = max(
                removable_keys,
                key=lambda k: len(json.dumps(payload[k], separators=(",", ":")).encode("utf-8")),
            )
            val = payload[largest_k]
            if isinstance(val, str) and len(val) > 10:
                current_len = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
                excess = current_len - max_bytes
                trim = max(excess + 16, len(val) // 2, 1)
                payload[largest_k] = val[:-trim] if len(val) > trim else ""
            else:
                del payload[largest_k]
            dropped_count += 1
            payload["dropped_count"] = dropped_count
            continue

        # 5. Guaranteed termination without removing required fields (e.g. status)
        if isinstance(payload.get("status"), str) and len(payload["status"]) > 10:
            current_len = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            excess = current_len - max_bytes
            trim = max(excess + 16, len(payload["status"]) // 2, 1)
            payload["status"] = payload["status"][:-trim] if len(payload["status"]) > trim else payload["status"][:8]
            dropped_count += 1
            payload["dropped_count"] = dropped_count
            continue

        break

    return json.dumps(payload, separators=(",", ":"))


def extract_root_conversation_id(ctx: Optional[Any]) -> Optional[str]:
    """Extracts antigravity.google/root_conversation_id from FastMCP Context or RequestContext metadata."""
    if ctx is None:
        return None
    try:
        req_ctx = getattr(ctx, "request_context", None)
        meta = getattr(req_ctx, "meta", None)
        if isinstance(meta, dict):
            val = meta.get("antigravity.google/root_conversation_id")
            if not val and isinstance(meta.get("meta"), dict):
                val = meta["meta"].get("antigravity.google/root_conversation_id")
            if val:
                return str(val).strip()
        elif meta is not None:
            val = getattr(meta, "root_conversation_id", None)
            if not val and hasattr(meta, "model_extra"):
                extra = getattr(meta, "model_extra") or {}
                val = extra.get("antigravity.google/root_conversation_id")
            if not val and hasattr(meta, "get"):
                val = meta.get("antigravity.google/root_conversation_id")
            if val:
                return str(val).strip()
        direct_meta = getattr(ctx, "meta", None)
        if isinstance(direct_meta, dict):
            val = direct_meta.get("antigravity.google/root_conversation_id")
            if val:
                return str(val).strip()
    except Exception:
        pass
    return None

