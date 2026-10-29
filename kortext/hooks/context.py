"""
Immutable HookContext dataclass and stdin parsing for Antigravity Lifecycle Hooks.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger("kortext.hooks.context")


def _freeze_dict(d: Optional[Mapping[str, Any]]) -> MappingProxyType:
    """Recursively converts a mapping into an immutable MappingProxyType."""
    if not d:
        return MappingProxyType({})
    frozen: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            frozen[k] = _freeze_dict(v)
        elif isinstance(v, list):
            frozen[k] = tuple(_freeze_dict(item) if isinstance(item, dict) else item for item in v)
        else:
            frozen[k] = v
    return MappingProxyType(frozen)


@dataclass(frozen=True)
class HookContext:
    """
    Immutable representation of Antigravity lifecycle hook execution context.
    Parsed from stdin JSON payload.
    """

    raw_payload: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    event: str = ""
    tool_call: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    cwd: str = ""
    workspace_paths: Tuple[str, ...] = ()
    transcript_path: str = ""
    session_id: str = ""
    turn_index: int = 0
    execution_num: int = 1
    tool_result: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    termination_reason: str = ""
    fully_idle: bool = True
    conversation_id: str = ""
    model_name: str = ""
    last_user_input: str = ""
    step_idx: int = -1
    artifact_dir: str = ""
    _cached_steps: Optional[Tuple[Mapping[str, Any], ...]] = field(
        default=None, init=False, repr=False, compare=False
    )

    @property
    def tool_error(self) -> Optional[str]:
        """Extracts error details from tool_result if the tool execution failed."""
        root_err = self.raw_payload.get("error")  # agy PostToolUse puts the error at the payload root
        if root_err:
            return str(root_err)
        if not self.tool_result:
            return None
        err = self.tool_result.get("error") or self.tool_result.get("errorMessage")
        if err:
            return str(err)
        if self.tool_result.get("isError") is True:
            return str(self.tool_result.get("content") or self.tool_result.get("output") or "tool_error")
        exit_code = self.tool_result.get("exit_code", self.tool_result.get("exitCode"))
        if exit_code not in (None, 0, "0"):
            stderr_val = str(self.tool_result.get("stderr") or "").strip()
            return stderr_val or f"exit_code={exit_code}"
        return None

    @property
    def tool_name(self) -> str:
        """Returns the normalized name of the tool being executed (stripping surface prefixes like 'client:')."""
        name = str(
            self.tool_call.get("name")
            or self.tool_call.get("tool_name")
            or self.raw_payload.get("tool_name")
            or self.raw_payload.get("toolName")
            or ""
        ).strip()
        if ":" in name:
            prefix, bare = name.split(":", 1)
            if prefix in ("client", "default_api", "mcp", "kortext") and bare:
                return bare
        return name

    @property
    def tool_args(self) -> Mapping[str, Any]:
        """Returns read-only mapping of tool call arguments."""
        args = (
            self.tool_call.get("args")
            or self.tool_call.get("arguments")
            or self.tool_call.get("tool_input")
            or self.raw_payload.get("tool_input")
            or self.raw_payload.get("toolInput")
            or {}
        )
        if isinstance(args, MappingProxyType):
            return args
        if isinstance(args, dict):
            return _freeze_dict(args)
        return MappingProxyType({})

    @property
    def command_line(self) -> str:
        """Returns the command line string for run_command tool calls."""
        args = self.tool_args
        cmd = args.get("CommandLine") or args.get("command") or args.get("cmd") or ""
        return str(cmd).strip()

    @property
    def target_file(self) -> str:
        """Returns the target file path for file modification tools."""
        args = self.tool_args
        target = (
            args.get("TargetFile")
            or args.get("target_file")
            or args.get("AbsolutePath")
            or args.get("path")
            or args.get("file_path")
            or ""
        )
        return str(target).strip()

    @property
    def is_first_turn(self) -> bool:
        """Indicates whether this is the initial turn (Turn 0) of the session."""
        return self.turn_index == 0

    @property
    def workspace_project(self) -> Optional[str]:
        """Resolves active project identifier from environment, workspacePaths, or cwd."""
        env_proj = os.getenv("KORTEXT_PROJECT") or os.getenv("PROJECT")
        if env_proj and env_proj.strip():
            return env_proj.strip()
        for wp in self.workspace_paths:
            if wp:
                name = Path(wp).name.strip()
                if name and name not in ("/", ".", ""):
                    return name
        if self.cwd:
            name = Path(self.cwd).name.strip()
            if name and name not in ("/", ".", ""):
                return name
        return None

    def read_transcript_steps(self, max_tail_bytes: int = 262_144) -> Tuple[Mapping[str, Any], ...]:
        """
        Reads recent steps from transcript.jsonl once per HookContext instance and caches the result.
        Bounds disk I/O to the last max_tail_bytes (256KB) on large multi-megabyte transcripts.
        If an entry has 'truncated_fields', resolves from transcript_full.jsonl.
        Returns an immutable tuple of step mappings.
        """
        if self._cached_steps is not None:
            return self._cached_steps

        if not self.transcript_path or not os.path.isfile(self.transcript_path):
            object.__setattr__(self, "_cached_steps", ())
            return ()

        def _read_tail_lines(p: Path, limit_bytes: int) -> List[str]:
            try:
                size = p.stat().st_size
                with open(p, "rb") as bf:
                    if size > limit_bytes > 0:
                        bf.seek(size - limit_bytes)
                        raw = bf.read().decode("utf-8", errors="replace")
                        lines = raw.splitlines()
                        return lines[1:] if len(lines) > 1 else lines
                    return bf.read().decode("utf-8", errors="replace").splitlines()
            except Exception:
                return []

        raw_t_lines = _read_tail_lines(Path(self.transcript_path), max_tail_bytes)
        needs_full = any('"truncated_fields"' in ln for ln in raw_t_lines)

        full_steps_by_id: Dict[str, Mapping[str, Any]] = {}
        full_steps_by_index: List[Mapping[str, Any]] = []

        if needs_full:
            full_path = Path(self.transcript_path).parent / "transcript_full.jsonl"
            if full_path.is_file():
                for line in _read_tail_lines(full_path, max_tail_bytes * 4):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        s = json.loads(line)
                        if isinstance(s, dict):
                            frozen_s = _freeze_dict(s)
                            full_steps_by_index.append(frozen_s)
                            sid = s.get("id") or s.get("step_id")
                            if sid is not None:
                                full_steps_by_id[str(sid)] = frozen_s
                    except Exception:
                        continue

        steps: List[Mapping[str, Any]] = []
        try:
            for line_idx, line in enumerate(raw_t_lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    step_data = json.loads(line)
                except Exception:
                    continue

                if not isinstance(step_data, dict):
                    continue

                if step_data.get("truncated_fields"):
                    resolved_step = None
                    sid = step_data.get("id") or step_data.get("step_id")
                    if sid is not None and str(sid) in full_steps_by_id:
                        resolved_step = full_steps_by_id[str(sid)]
                    elif line_idx < len(full_steps_by_index):
                        resolved_step = full_steps_by_index[line_idx]

                    if resolved_step:
                        steps.append(resolved_step)
                        continue

                steps.append(_freeze_dict(step_data))
        except Exception as e:
            logger.debug("Failed reading transcript steps from %s: %s", self.transcript_path, e)
            object.__setattr__(self, "_cached_steps", ())
            return ()

        result = tuple(steps)
        object.__setattr__(self, "_cached_steps", result)
        return result

    @classmethod
    def from_stdin(
        cls,
        raw_text: Optional[str] = None,
        event: str = "",
    ) -> "HookContext":
        """
        Parses HookContext from raw text (or sys.stdin) with safe fallback.
        Never throws on empty or invalid input.
        """
        if raw_text is None:
            try:
                raw_text = sys.stdin.read().strip()
            except Exception as e:
                logger.debug("Error reading sys.stdin: %s", e)
                raw_text = ""

        if not raw_text or not raw_text.strip():
            return cls(event=event)

        try:
            parsed = json.loads(raw_text)
            if not isinstance(parsed, dict):
                return cls(event=event)
        except Exception as e:
            logger.debug("Failed to decode JSON from stdin: %s", e)
            return cls(event=event)

        tool_call_data = parsed.get("toolCall") or parsed.get("tool_call") or {}
        if not isinstance(tool_call_data, dict):
            tool_call_data = {}

        tool_result_data = parsed.get("toolResult") or parsed.get("tool_result") or {}
        if not isinstance(tool_result_data, dict):
            tool_result_data = {}

        event_name = event or str(parsed.get("event") or "")

        turn_index = 0
        for turn_key in ("invocationNum", "turnIndex", "turn_index"):
            if turn_key in parsed:
                try:
                    turn_index = int(parsed[turn_key])
                    break
                except (ValueError, TypeError):
                    logger.debug("Suppressed non-fatal exception")

        execution_num = 1
        if "executionNum" in parsed:
            try:
                execution_num = int(parsed["executionNum"])
            except (ValueError, TypeError):
                execution_num = 1

        raw_ws = parsed.get("workspacePaths") or parsed.get("workspace_paths") or []
        workspace_paths = tuple(str(p) for p in raw_ws if p) if isinstance(raw_ws, (list, tuple)) else ()

        default_cwd = workspace_paths[0] if workspace_paths else os.getcwd()

        return cls(
            raw_payload=_freeze_dict(parsed),
            event=event_name,
            tool_call=_freeze_dict(tool_call_data),
            cwd=str(parsed.get("cwd") or default_cwd),
            workspace_paths=workspace_paths,
            transcript_path=str(parsed.get("transcriptPath") or parsed.get("transcript_path") or ""),
            session_id=str(parsed.get("sessionId") or parsed.get("session_id") or parsed.get("conversationId") or ""),
            turn_index=turn_index,
            execution_num=execution_num,
            tool_result=_freeze_dict(tool_result_data),
            termination_reason=str(parsed.get("terminationReason") or parsed.get("termination_reason") or ""),
            fully_idle=bool(parsed.get("fullyIdle", parsed.get("fully_idle", True))),
            conversation_id=str(parsed.get("conversationId") or parsed.get("conversation_id") or parsed.get("sessionId") or parsed.get("session_id") or ""),
            model_name=str(parsed.get("modelName") or parsed.get("model_name") or parsed.get("model") or ""),
            step_idx=int(parsed["stepIdx"]) if isinstance(parsed.get("stepIdx"), int) else -1,
            artifact_dir=str(parsed.get("artifactDirectoryPath") or ""),
            last_user_input=str(
                parsed.get("lastUserInput")
                or parsed.get("last_user_input")
                or parsed.get("prompt")
                or parsed.get("userPrompt")
                or ""
            ),
        )
