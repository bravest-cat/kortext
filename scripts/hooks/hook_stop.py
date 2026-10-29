#!/usr/bin/env python3
"""
Antigravity Stop Hook (thin wrapper delegating to kortext.hooks.runner).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
for p in (str(SCRIPT_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from common import (
    get_hook_payload,
    read_transcript_steps,
)
from kortext.hooks.context import HookContext
from kortext.hooks.runner import emit_response, run_hook
from kortext.trust.sanitizer import CausalSanitizer, SecurityViolation


def record_learned_facts(
    facts: List[Dict[str, Any]],
    episode_name: str = "stop_hook_episode",
    driver: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Sanitizes and persists learned facts via CausalSanitizer and EpisodicDriver."""
    if not facts:
        return []

    sanitizer = CausalSanitizer()
    if driver is None:
        from kortext.episodic.driver import AgentNativeEpisodicDriver

        driver = AgentNativeEpisodicDriver()

    recorded: List[Dict[str, Any]] = []
    for item in facts:
        if not isinstance(item, dict):
            continue
        subj = str(item.get("subject", "")).strip()
        rel = str(item.get("relation", "RELATES_TO")).strip()
        fact = str(item.get("fact", "")).strip()
        tier = int(item.get("source_tier", 2))
        if not subj or not fact:
            continue
        try:
            sanitizer.sanitize(subj, rel, fact, source_tier=tier)
            try:
                driver.record_fact(
                    subject=subj,
                    relation=rel,
                    fact=fact,
                    source_session=episode_name,
                    source_tier=tier,
                )
            except TypeError:
                driver.record_fact(
                    subject=subj,
                    relation=rel,
                    fact=fact,
                    episode_name=episode_name,
                )
            recorded.append(item)
        except SecurityViolation:
            continue
        except Exception:
            continue

    return recorded


def extract_learned_facts_from_transcript(transcript_path: str) -> List[Dict[str, Any]]:
    """Extracts learned_facts from transcript steps and record_system_outcome tool calls."""
    steps = read_transcript_steps(transcript_path)
    facts: List[Dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_facts = step.get("learned_facts")
        if isinstance(step_facts, list):
            for f in step_facts:
                if isinstance(f, dict):
                    facts.append(f)

        tool_calls = step.get("tool_calls", [])
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                name = str(tc.get("name") or tc.get("tool_name") or "")
                args = tc.get("args", {}) or {}
                if not isinstance(args, dict):
                    continue
                target_tool = str(args.get("ToolName", ""))
                if "record_system_outcome" in name or target_tool == "record_system_outcome":
                    tc_facts = args.get("facts")
                    if isinstance(tc_facts, list):
                        for f in tc_facts:
                            if isinstance(f, dict):
                                facts.append(f)
    return facts


def main() -> None:
    payload = get_hook_payload()
    transcript_path = str(payload.get("transcriptPath", "") or "")
    facts_recorded = False
    if transcript_path:
        extracted = extract_learned_facts_from_transcript(transcript_path)
        if extracted:
            try:
                record_learned_facts(extracted)
                facts_recorded = True
            except Exception:
                pass
    ctx_payload = {**payload, "_facts_already_recorded": True} if facts_recorded else payload
    ctx = HookContext.from_stdin(raw_text=json.dumps(ctx_payload), event="stop")
    res = run_hook("stop", ctx)
    emit_response(event="stop", decision=res.decision, reason=res.reason, data=res.data)


if __name__ == "__main__":
    main()
