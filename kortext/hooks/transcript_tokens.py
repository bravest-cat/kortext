"""
Hybrid Transcript & Statusline Context/Token Awareness Engine for Kortext.

Bridges the gap between Antigravity's Statusline payload (`context_window` on statusline stdin)
and Lifecycle Hooks (`transcriptPath` on hook stdin), accounting for the ~25% (~250k tokens)
auto-compaction ceiling on 1M-token context window models (Gemini 3.1 Pro, Gemini 3.8 Flash, Claude 1M).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import time
from types import MappingProxyType
from typing import Any, Dict, Optional, Sequence, Tuple

from kortext._home import get_kortext_home

logger = logging.getLogger("kortext.hooks.transcript_tokens")

STANDARD_WINDOW_TOKENS = 200_000
LARGE_WINDOW_TOKENS = 1_000_000
LARGE_WINDOW_COMPACT_CEILING = 250_000  # ~25% of 1M window where auto-compaction triggers
STANDARD_WINDOW_COMPACT_CEILING = 160_000  # ~80% of 200k window
BASELINE_SYSTEM_PROMPT_TOKENS = 18_000  # Baseline system prompt + rules + MCP schemas


@dataclass(frozen=True)
class SessionTokenState:
    """Resolved token and context-window state for the active session turn."""

    estimated_tokens: int
    context_window_size: int
    used_percentage: float
    compaction_ceiling_tokens: int
    compaction_budget_pct: float
    exceeds_200k: bool
    source: str
    is_large_window: bool
    stage1_advisory_reached: bool
    stage2_precompact_reached: bool

    @property
    def is_1m_window(self) -> bool:
        return self.is_large_window

    @property
    def stage1_warn_reached(self) -> bool:
        return self.stage1_advisory_reached

    @property
    def effective_compaction_pct(self) -> float:
        return self.compaction_budget_pct

    def to_dict(self) -> Dict[str, Any]:
        return {
            "estimated_tokens": self.estimated_tokens,
            "context_window_size": self.context_window_size,
            "used_percentage": self.used_percentage,
            "compaction_ceiling_tokens": self.compaction_ceiling_tokens,
            "compaction_budget_pct": self.compaction_budget_pct,
            "effective_compaction_pct": self.compaction_budget_pct,
            "exceeds_200k": self.exceeds_200k,
            "source": self.source,
            "is_large_window": self.is_large_window,
            "stage1_advisory_reached": self.stage1_advisory_reached,
            "stage2_precompact_reached": self.stage2_precompact_reached,
        }


def infer_context_window_size(
    model_name: str = "",
    reported_window_size: int = 0,
    observed_tokens: int = 0,
) -> int:
    """Determines whether the active model uses a 1M window or a 200k window."""
    env_override = os.getenv("KORTEXT_CONTEXT_WINDOW_TOKENS") or os.getenv("ECC_CONTEXT_WINDOW_TOKENS")
    if env_override:
        try:
            parsed = int(env_override)
            if parsed > 0:
                return parsed
        except ValueError as exc:
            logger.debug("Suppressed in transcript_tokens:%d: %s", 86, exc)

    if reported_window_size and reported_window_size > 0:
        return int(reported_window_size)

    m_low = (model_name or "").lower()
    if any(
        marker in m_low
        for marker in ("gemini", "[1m]", "1m", "claude-opus-5", "claude-fable-5", "claude-mythos-5")
    ):
        return LARGE_WINDOW_TOKENS

    if observed_tokens > STANDARD_WINDOW_TOKENS:
        return LARGE_WINDOW_TOKENS

    return STANDARD_WINDOW_TOKENS


def extract_latest_usage_from_steps(steps: Sequence[Any]) -> Tuple[int, str]:
    """
    Scans transcript steps backwards for the most recent turn usage record.
    Returns `(context_tokens, model_name)` where `context_tokens` is
    `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`
    (the true prompt context size of that turn, never summed across turns).
    """
    for step in reversed(steps):
        if not isinstance(step, (dict, MappingProxyType)):
            continue

        model_str = str(step.get("model") or step.get("modelName") or "")
        msg = step.get("message")
        if isinstance(msg, (dict, MappingProxyType)) and not model_str:
            model_str = str(msg.get("model") or "")

        usage = (
            step.get("usage")
            or step.get("usageMetadata")
            or (msg.get("usage") if isinstance(msg, (dict, MappingProxyType)) else None)
        )
        if isinstance(usage, (dict, MappingProxyType)):
            inp = int(usage.get("input_tokens") or usage.get("promptTokenCount") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or usage.get("cachedContentTokenCount") or 0)
            cache_create = int(usage.get("cache_creation_input_tokens") or 0)
            prompt_total = inp + cache_read + cache_create
            if prompt_total > 0:
                return prompt_total, model_str
            tot = int(usage.get("total_tokens") or usage.get("totalTokenCount") or 0)
            if tot > 0:
                return tot, model_str

        tok_cnt = step.get("token_count") or step.get("tokenCount")
        if tok_cnt:
            try:
                val = int(tok_cnt)
                if val > 0:
                    return val, model_str
            except (ValueError, TypeError) as exc:
                logger.debug("Suppressed in transcript_tokens:%d: %s", 143, exc)

    return 0, ""


def estimate_tokens_from_transcript_files(
    transcript_path: Any,
    steps: Optional[Sequence[Any]] = None,
) -> int:
    if isinstance(transcript_path, (list, tuple)):
        transcript_path = str(transcript_path[0]) if transcript_path else ""
    else:
        transcript_path = str(transcript_path or "")
    steps = steps or ()
    """
    Estimates active context tokens when explicit usage blocks are absent.
    Scans for the latest compaction boundary marker if present, or uses the full
    untruncated file size of `transcript_full.jsonl` (or `transcript.jsonl`)
    so transcripts >256KB are never artificially capped at ~64k tokens.
    """
    # Check if steps contain a compaction marker; if so, sum chars only after the last compaction marker + baseline
    if steps:
        compaction_idx = -1
        for i, s in enumerate(steps):
            if isinstance(s, (dict, MappingProxyType)):
                if (
                    s.get("type") in ("compaction", "summary", "context_compaction")
                    or s.get("compacted") is True
                    or "# Resuming from a compaction" in str(s.get("content") or "")
                ):
                    compaction_idx = i
        if compaction_idx >= 0:
            post_compact_chars = sum(len(str(s)) for s in steps[compaction_idx:])
            return max(1, (post_compact_chars // 4) + BASELINE_SYSTEM_PROMPT_TOKENS)

    if transcript_path and os.path.isfile(transcript_path):
        try:
            t_path = Path(transcript_path)
            full_path = t_path.parent / "transcript_full.jsonl"
            target_file = full_path if full_path.is_file() else t_path
            byte_size = target_file.stat().st_size
            if byte_size <= 262_144 and steps:
                step_chars = sum(len(str(s)) for s in steps)
                return max(1, step_chars // 4)
            if byte_size > 0:
                try:
                    raw_content = target_file.read_text(encoding="utf-8", errors="ignore")
                    marker = "# Resuming from a compaction"
                    if marker in raw_content:
                        raw_content = raw_content[raw_content.rfind(marker):]
                        return max(1, (len(raw_content) // 4) + BASELINE_SYSTEM_PROMPT_TOKENS)
                except Exception as exc:
                    logger.debug("Suppressed in transcript_tokens:%d: %s", 195, exc)
                return max(1, (byte_size // 4) + BASELINE_SYSTEM_PROMPT_TOKENS)
        except Exception as exc:
            logger.debug("Transcript file size token estimation fallback: %s", exc)

    if not steps:
        return 0
    total_chars = 0
    for step in steps:
        try:
            total_chars += len(str(step))
        except Exception:
            total_chars += 200
    return max(1, total_chars // 4)


def resolve_session_token_state(
    steps: Sequence[Any] = (),
    raw_payload: Any = None,
    transcript_path: str = "",
    model_name: str = "",
    max_hud_age_sec: float = 300.0,
    kortext_home: Optional[Path] = None,
    session_id: str = "",
) -> SessionTokenState:
    """
    Resolves the current session's token usage, window size, and 25%-compaction-aware status.
    With a session id (given, or the payload's conversationId), only that session's statusline
    figures are used, so one agy window never acts on another window's context usage.
    """
    payload_dict = raw_payload if isinstance(raw_payload, (dict, MappingProxyType)) else {}
    session_id = session_id or str(payload_dict.get("conversationId") or payload_dict.get("sessionId") or "")
    cw = payload_dict.get("context_window")
    cw_dict = cw if isinstance(cw, (dict, MappingProxyType)) else {}

    exceeds_200k = bool(payload_dict.get("exceeds_200k_tokens", False))
    reported_tokens = 0
    reported_window = 0
    reported_pct = 0.0
    source = "transcript_estimate"

    # 1. Check direct payload context_window
    if cw_dict:
        try:
            reported_tokens = int(cw_dict.get("total_input_tokens") or 0)
        except (ValueError, TypeError):
            reported_tokens = 0
        try:
            reported_window = int(cw_dict.get("context_window_size") or 0)
        except (ValueError, TypeError):
            reported_window = 0
        try:
            reported_pct = float(cw_dict.get("used_percentage") or 0.0)
        except (ValueError, TypeError):
            reported_pct = 0.0
        if reported_tokens > 0 or reported_pct > 0.0:
            source = "payload_context_window"

    # 2. Check ~/.kortext/run/hud_state.json (persisted by statusline)
    if reported_tokens <= 0 and reported_pct <= 0.0:
        try:
            base_home = kortext_home or get_kortext_home()
            hud_file = base_home / "run" / "hud_state.json"
            if hud_file.is_file():
                hud_data = json.loads(hud_file.read_text(encoding="utf-8"))
                sessions = hud_data.get("sessions") if isinstance(hud_data, dict) else None
                if session_id and isinstance(sessions, dict):
                    # Per-session figures exist: use only this session's, never another window's.
                    hud_data = sessions.get(session_id)
                if isinstance(hud_data, dict):
                    updated_at = float(hud_data.get("updated_at") or 0.0)
                    if updated_at <= 0.0 or (time.time() - updated_at) <= max_hud_age_sec:
                        h_tokens = int(hud_data.get("total_input_tokens") or 0)
                        h_window = int(hud_data.get("context_window_size") or 0)
                        h_pct = float(hud_data.get("used_percentage") or 0.0)
                        if hud_data.get("exceeds_200k_tokens"):
                            exceeds_200k = True
                        if h_tokens > 0 or h_pct > 0.0:
                            reported_tokens = h_tokens
                            if h_window > 0:
                                reported_window = h_window
                            reported_pct = h_pct
                            source = "hud_state"
        except Exception as exc:
            logger.debug("HUD state token read skipped: %s", exc)

    # 3. Check latest assistant usage block in transcript.jsonl
    usage_tokens, usage_model = extract_latest_usage_from_steps(steps)
    effective_model = model_name or usage_model or str(payload_dict.get("modelName") or "")

    # 4. Compute fallback file-based estimate
    est_file_tokens = estimate_tokens_from_transcript_files(transcript_path, steps)

    # Choose best token count
    if reported_tokens > 0:
        final_tokens = max(reported_tokens, usage_tokens)
    elif usage_tokens > 0:
        final_tokens = usage_tokens
        source = "transcript_usage"
    else:
        final_tokens = est_file_tokens

    window_size = infer_context_window_size(
        model_name=effective_model,
        reported_window_size=reported_window,
        observed_tokens=final_tokens,
    )
    is_large = window_size >= 500_000

    if reported_pct > 0.0 and final_tokens < int((reported_pct / 100.0) * window_size):
        base_win = reported_window if reported_window > 0 else STANDARD_WINDOW_TOKENS
        final_tokens = max(final_tokens, int((reported_pct / 100.0) * base_win))

    if exceeds_200k and final_tokens < 200_000:
        final_tokens = 200_000

    if reported_pct > 0.0 and reported_window <= 0 and not is_large:
        used_pct = round(min(100.0, reported_pct), 1)
    elif window_size > 0 and final_tokens > 0:
        used_pct = round(min(100.0, (final_tokens / float(window_size)) * 100.0), 1)
        if reported_pct > used_pct:
            used_pct = round(min(100.0, reported_pct), 1)
    else:
        used_pct = round(min(100.0, reported_pct), 1)

    compact_ceiling = LARGE_WINDOW_COMPACT_CEILING if is_large else STANDARD_WINDOW_COMPACT_CEILING
    compaction_budget_pct = round(min(100.0, (final_tokens / float(compact_ceiling)) * 100.0), 1) if compact_ceiling > 0 else used_pct

    if is_large:
        stage1 = final_tokens >= 150_000 or used_pct >= 15.0 or exceeds_200k
        stage2 = final_tokens >= 200_000 or used_pct >= 20.0 or exceeds_200k
    else:
        stage1 = final_tokens >= 120_000 or used_pct >= 60.0 or exceeds_200k
        stage2 = final_tokens >= 150_000 or used_pct >= 75.0 or exceeds_200k

    return SessionTokenState(
        estimated_tokens=final_tokens,
        context_window_size=window_size,
        used_percentage=used_pct,
        compaction_ceiling_tokens=compact_ceiling,
        compaction_budget_pct=compaction_budget_pct,
        exceeds_200k=exceeds_200k or final_tokens >= 200_000,
        source=source,
        is_large_window=is_large,
        stage1_advisory_reached=stage1,
        stage2_precompact_reached=stage2,
    )


def check_and_update_compaction_watermark(
    session_id: str = "",
    step_count: int = 0,
    estimated_tokens: int = 0,
    used_percentage: float = 0.0,
    *,
    conversation_id: Optional[str] = None,
    current_tokens: Optional[int] = None,
    kortext_home: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Tracks `(step_count, estimated_tokens, used_percentage)` per session in
    `~/.kortext/run/session_watermarks.json` and detects if context compaction
    occurred since the previous turn (e.g., step_count dropped or tokens dropped by >35%).
    """
    target_id = (conversation_id or session_id or "").strip()
    toks = int(current_tokens if current_tokens is not None else estimated_tokens)
    if not target_id:
        return {"compacted": False}

    base_home = kortext_home or get_kortext_home()
    wm_file = base_home / "run" / "session_watermarks.json"
    try:
        wm_file.parent.mkdir(parents=True, exist_ok=True)
        data: Dict[str, Any] = {}
        if wm_file.is_file():
            try:
                loaded = json.loads(wm_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}

        prev = data.get(target_id)
        compacted = False
        prev_steps = 0
        prev_tokens = 0

        if isinstance(prev, dict):
            prev_steps = int(prev.get("step_count") or 0)
            prev_tokens = int(prev.get("estimated_tokens") or 0)
            if prev_steps > 5 and 0 < step_count < prev_steps:
                compacted = True
            elif prev_tokens >= 100_000 and 0 < toks < int(prev_tokens * 0.65):
                compacted = True

        data[target_id] = {
            "step_count": int(step_count),
            "estimated_tokens": toks,
            "used_percentage": float(used_percentage),
            "updated_at": time.time(),
        }
        if len(data) > 50:
            sorted_keys = sorted(
                data.keys(),
                key=lambda k: float(data[k].get("updated_at", 0.0)) if isinstance(data[k], dict) else 0.0,
            )
            for old_k in sorted_keys[: len(data) - 50]:
                data.pop(old_k, None)

        tmp_file = wm_file.with_suffix(f".tmp.{os.getpid()}")
        tmp_file.write_text(json.dumps(data), encoding="utf-8")
        tmp_file.replace(wm_file)

        return {
            "compacted": compacted,
            "previous_steps": prev_steps,
            "previous_tokens": prev_tokens,
        }
    except Exception as exc:
        logger.debug("Watermark tracking skipped: %s", exc)
        return {"compacted": False}


def load_post_compaction_recovery_brief(
    session_id: str = "",
    project_id: str = "kortext",
    *,
    conversation_id: Optional[str] = None,
    project: Optional[str] = None,
    workspace_dir: Optional[str] = None,
    kortext_home: Optional[Path] = None,
) -> Optional[str]:
    """
    Loads the most recent pre-compaction session checkpoint baton for `session_id`
    from `~/.kortext/handoffs/` and the 10-line header of `.kortext/ledger.md`
    so the agent immediately recovers modified files, recent AST symbols, and active Task DAG state after compaction.
    """
    target_id = (conversation_id or session_id or "").strip()
    target_proj = project or project_id or "kortext"
    if not target_id and not workspace_dir:
        return None
    safe_id = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in (target_id or "compacted"))
    base_home = kortext_home or get_kortext_home()
    baton_file = base_home / "handoffs" / f"session_{safe_id}.json"

    parts: list[str] = []
    if baton_file.is_file():
        try:
            data = json.loads(baton_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                mod_files = data.get("modified_files") or []
                syms = data.get("recent_symbols") or []
                parts.append(f"🔄 [Kortext Post-Compaction Recovery Baton (`session_{safe_id}.json`)]")
                parts.append(f"Project: `{data.get('workspace_project') or target_proj}`")
                if mod_files:
                    parts.append(f"Modified files before compaction: `{', '.join(str(f) for f in mod_files[-8:])}`")
                if syms:
                    parts.append(f"Recently inspected AST symbols: `{', '.join(str(s) for s in syms[-8:])}`")
        except Exception as exc:
            logger.debug("Suppressed in transcript_tokens:%d: %s", 446, exc)

    if workspace_dir:
        ws_path = Path(workspace_dir)
        ledger_path = ws_path / ".kortext" / "ledger.md"
        if ledger_path.is_file():
            try:
                ledger_lines = ledger_path.read_text(encoding="utf-8").splitlines()[:10]
                if ledger_lines:
                    parts.append("Compaction-Proof Ledger:\n" + "\n".join(ledger_lines))
            except Exception as exc:
                logger.debug("Suppressed in transcript_tokens:%d: %s", 457, exc)

        briefs_dir = ws_path / ".kortext" / "briefs"
        if briefs_dir.is_dir():
            try:
                brief_files = sorted(briefs_dir.glob("task_*_brief.md"), key=lambda p: p.stat().st_mtime, reverse=True)
                if brief_files:
                    latest_brief = brief_files[0].read_text(encoding="utf-8").splitlines()[:12]
                    if latest_brief:
                        parts.append(f"Active Task Brief (`{brief_files[0].name}`):\n" + "\n".join(latest_brief))
            except Exception as exc:
                logger.debug("Suppressed in transcript_tokens:%d: %s", 468, exc)

        invariants_file = ws_path / "journal" / "grounded_invariants.json"
        if invariants_file.is_file():
            try:
                inv_data = json.loads(invariants_file.read_text(encoding="utf-8"))
                if isinstance(inv_data, dict) and inv_data.get("invariants"):
                    inv_list = [str(i) for i in inv_data["invariants"][:5]]
                    parts.append("Grounded Invariants: " + "; ".join(inv_list))
            except Exception as exc:
                logger.debug("Suppressed in transcript_tokens:%d: %s", 478, exc)

    return " | ".join(parts) if parts else None

SessionTokenState.should_write_pre_compaction_baton = property(lambda self: self.stage2_precompact_reached)
