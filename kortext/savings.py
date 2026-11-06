"""
Savings telemetry recorder and estimation engine.

Records lookups served by kortext and estimate token savings compared to whole-file reads.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from kortext._home import get_kortext_home

logger = logging.getLogger("kortext.savings")

MAX_SAVINGS_FILE_BYTES = 5 * 1024 * 1024  # 5 MB rotation limit


def get_savings_file(home: Optional[Path] = None) -> Path:
    base = home or get_kortext_home()
    run_dir = base / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    s_file = run_dir / "savings.jsonl"
    # Create with mode 0600 on POSIX if it does not yet exist
    if not s_file.exists():
        try:
            import os as _os
            fd = _os.open(str(s_file), _os.O_CREAT | _os.O_WRONLY, 0o600)
            _os.close(fd)
        except Exception:
            pass
    return s_file


def record_savings_event(
    event: str,
    session_id: str = "",
    project: str = "",
    reply_bytes: int = 0,
    file_bytes: int = 0,
    files: Optional[Sequence[str]] = None,
    home: Optional[Path] = None,
) -> None:
    """
    Appends a savings event to savings.jsonl.
    Must be cheap (under 2 ms), bounded (rotates at 5 MB), and never raises.
    prompt or query text is never stored.
    """
    try:
        s_file = get_savings_file(home)

        # Rotate if size >= 5 MB
        try:
            if s_file.is_file() and s_file.stat().st_size >= MAX_SAVINGS_FILE_BYTES:
                rotated = s_file.with_name(s_file.name + ".1")
                try:
                    rotated.unlink(missing_ok=True)
                except OSError:
                    pass
                s_file.replace(rotated)
        except Exception:
            pass

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": str(event),
            "session_id": str(session_id or ""),
            "project": str(project or ""),
            "reply_bytes": max(0, int(reply_bytes)),
            "file_bytes": max(0, int(file_bytes)),
            "files": list(files) if files else [],
        }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with open(s_file, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        logger.debug("Failed to record savings event: %s", exc)


def extract_files_and_sizes_from_reply(
    reply: Any,
    workspace_root: Optional[Path] = None,
) -> Tuple[int, List[str]]:
    """
    Extracts distinct file paths and sums their sizes on disk from tool output.
    """
    root = workspace_root or Path.cwd()
    raw_text = ""
    parsed_json: Optional[Any] = None

    if isinstance(reply, str):
        raw_text = reply
        try:
            parsed_json = json.loads(reply)
        except Exception:
            pass
    elif isinstance(reply, dict):
        parsed_json = reply
        raw_text = json.dumps(reply)
    elif isinstance(reply, (list, tuple)):
        parsed_json = list(reply)
        raw_text = json.dumps(parsed_json)

    candidate_paths: Set[str] = set()

    def _collect_paths(obj: Any) -> None:
        if isinstance(obj, dict):
            for k in ("file_path", "filePath", "file", "path", "source_file"):
                v = obj.get(k)
                if isinstance(v, str) and len(v) > 2 and ("." in v or "/" in v or "\\" in v):
                    candidate_paths.add(v)
            for v in obj.values():
                if isinstance(v, (dict, list, tuple)):
                    _collect_paths(v)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                _collect_paths(item)

    if parsed_json is not None:
        _collect_paths(parsed_json)

    # Regex scan for file paths in raw text if none extracted or in markdown
    if not candidate_paths and raw_text:
        matches = re.findall(r'["\'](?:file_path|path|source_file|file)["\']\s*:\s*["\']([^"\']+)["\']', raw_text)
        for m in matches:
            if len(m) > 2 and ("." in m or "/" in m):
                candidate_paths.add(m)

    distinct_files: List[str] = []
    total_bytes = 0

    for fp in candidate_paths:
        p = Path(fp)
        if not p.is_absolute():
            p = (root / p).resolve()
        try:
            if p.is_file():
                size = p.stat().st_size
                distinct_files.append(str(p))
                total_bytes += size
        except OSError:
            pass

    return total_bytes, distinct_files


def get_savings_summary(
    days: int = 30,
    project: Optional[str] = None,
    home: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Computes savings metrics:
    - Counts by kind
    - Total reply bytes served
    - ESTIMATE tokens saved = (distinct_file_bytes - reply_bytes) / 4
    """
    base = home or get_kortext_home()
    s_file = base / "run" / "savings.jsonl"
    r_file = base / "run" / "savings.jsonl.1"

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))

    counts_by_kind: Dict[str, int] = {}
    total_reply_bytes = 0
    total_file_bytes = 0

    files_to_read = [r_file, s_file]
    for pf in files_to_read:
        if not pf.is_file():
            continue
        try:
            with open(pf, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts_str = record.get("timestamp")
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str)
                            if ts.tzinfo is None:
                                ts = ts.replace(tzinfo=timezone.utc)
                            if ts < cutoff:
                                continue
                        except Exception:
                            pass

                    rec_proj = record.get("project")
                    if project and rec_proj and rec_proj != project and project.lower() != "all":
                        continue

                    ev = str(record.get("event") or "unknown")
                    counts_by_kind[ev] = counts_by_kind.get(ev, 0) + 1

                    r_bytes = int(record.get("reply_bytes") or 0)
                    f_bytes = int(record.get("file_bytes") or 0)
                    total_reply_bytes += r_bytes
                    total_file_bytes += f_bytes
        except Exception as exc:
            logger.debug("Error reading savings file %s: %s", pf, exc)

    # Estimate of tokens saved: (file_bytes - reply_bytes) / 4
    saved_bytes = max(0, total_file_bytes - total_reply_bytes)
    estimate_tokens_saved = saved_bytes // 4

    return {
        "days": days,
        "project": project,
        "counts_by_kind": counts_by_kind,
        "reply_bytes_served": total_reply_bytes,
        "distinct_file_bytes": total_file_bytes,
        "estimate_tokens_saved": estimate_tokens_saved,
        "note": "ESTIMATE assumes the agent would otherwise have read those files whole, so it is an upper bound.",
    }
