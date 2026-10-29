"""
Common utilities for Kortext Antigravity Lifecycle Hooks.
Provides non-blocking socket checks, JSON payload parsing, transcript analysis, and response formatting.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Automatically detect KORTEXT_ROOT from environment or script location
KORTEXT_ROOT = Path(
    os.environ.get(
        "KORTEXT_ROOT",
        str(Path(__file__).resolve().parent.parent.parent),
    )
)


def get_hook_payload() -> Dict[str, Any]:
    """Reads and parses JSON payload from stdin."""
    try:
        raw = sys.stdin.read().strip()
        if not raw:
            return {}
        return json.loads(raw)
    except Exception as e:
        print(f"[KortextHookError] Failed to parse stdin payload: {e}", file=sys.stderr)
        return {}


def emit_hook_response(response: Dict[str, Any]):
    """Outputs JSON response to stdout and exits cleanly."""
    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()
    sys.exit(0)


def check_tcp_port(host: str, port: int, timeout_sec: float = 0.2) -> bool:
    """Performs a non-blocking TCP socket connect check."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_sec)
            return sock.connect_ex((host, port)) == 0
    except Exception:
        return False


def check_unix_socket(socket_path: Path, timeout_sec: float = 0.2) -> bool:
    """Performs a non-blocking Unix Domain Socket connect check."""
    if not socket_path.is_socket():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_sec)
            sock.connect(str(socket_path))
            return True
    except Exception:
        return False


def check_http_health(
    host: str = "127.0.0.1",
    port: int = 6333,
    path: str = "/healthz",
    timeout_sec: float = 0.2,
) -> bool:
    """Performs a non-blocking HTTP health check."""
    try:
        import urllib.request
        url = f"http://{host}:{port}{path}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return resp.status < 500
    except Exception:
        return False


def read_transcript_steps(transcript_path: str) -> List[Dict[str, Any]]:
    """
    Reads steps from transcript.jsonl. If a step entry has 'truncated_fields',
    attempts to load the full step from transcript_full.jsonl located in the same directory
    per Antigravity specification.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return []

    full_path = Path(transcript_path).parent / "transcript_full.jsonl"
    full_steps: List[Dict[str, Any]] = []
    full_steps_by_id: Dict[str, Dict[str, Any]] = {}

    if full_path.is_file():
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f_full:
                for line in f_full:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        s = json.loads(line)
                        if isinstance(s, dict):
                            full_steps.append(s)
                            sid = s.get("id") or s.get("step_id")
                            if sid is not None:
                                full_steps_by_id[str(sid)] = s
                    except Exception:
                        continue
        except Exception as e:
            print(f"[KortextHookError] Failed to read transcript_full.jsonl: {e}", file=sys.stderr)

    steps: List[Dict[str, Any]] = []
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line_idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    step = json.loads(line)
                except Exception:
                    continue

                if not isinstance(step, dict):
                    continue

                if step.get("truncated_fields"):
                    full_step = None
                    sid = step.get("id") or step.get("step_id")
                    if sid is not None and str(sid) in full_steps_by_id:
                        full_step = full_steps_by_id[str(sid)]
                    elif line_idx < len(full_steps):
                        full_step = full_steps[line_idx]

                    if full_step and isinstance(full_step, dict):
                        step = full_step

                steps.append(step)
    except Exception as e:
        print(f"[KortextHookError] Transcript scan failed: {e}", file=sys.stderr)

    return steps


def analyze_transcript_modifications(transcript_path: str) -> Tuple[bool, bool]:
    """
    Scans transcript.jsonl (and transcript_full.jsonl if truncated_fields is present) to determine:
    1. Were any file-modifying tools executed?
    2. Was record_system_outcome or trigger_dream_mode executed?
    Never performs raw string matching on messages to avoid false positives from system banners.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return False, True  # Fallback: don't block if transcript is inaccessible

    modified_files = False
    recorded_outcome = False

    file_tools = {"write_to_file", "replace_file_content", "multi_replace_file_content"}
    outcome_tools = {"record_system_outcome", "trigger_dream_mode"}

    try:
        steps = read_transcript_steps(transcript_path)
        for step in steps:
            tool_calls = step.get("tool_calls", [])
            if not isinstance(tool_calls, list):
                continue

            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                name = tc.get("name") or tc.get("tool_name") or ""
                args = tc.get("args", {}) or {}
                target_tool = args.get("ToolName", "") if isinstance(args, dict) else ""

                for ot in outcome_tools:
                    if ot in name or ot == target_tool or (isinstance(args, dict) and ot in str(args)):
                        recorded_outcome = True
                    elif name == "run_command" and isinstance(args, dict) and ot in args.get("CommandLine", ""):
                        recorded_outcome = True

                for ft in file_tools:
                    if ft in name:
                        modified_files = True

    except Exception as e:
        print(f"[KortextHookError] Transcript scan failed: {e}", file=sys.stderr)
        return False, True

    return modified_files, recorded_outcome
