"""
Memory vault resolution, indexing, and session logging for Kortext.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

import portalocker

from kortext._home import get_kortext_home
from kortext.trust.sanitizer import redact_secrets

logger = logging.getLogger("kortext.vault")


def _sanitize_for_markdown_field(text: str) -> str:
    """Collapses newlines to spaces and strips leading # to prevent markdown heading forgery."""
    collapsed = re.sub(r"[\r\n]+", " ", str(text)).strip()
    collapsed = re.sub(r"^#+\s*", "", collapsed).strip()
    return collapsed


def resolve_memory_dir(
    workspace_root: Optional[Union[str, Path]] = None,
    project: Optional[str] = None,
) -> Path:
    """
    Resolves the memory vault directory for a project using the precedence:
    1. JOURNAL_DIR environment variable if set.
    2. <workspace_root>/.kortext/memory/ when workspace is known.
    3. get_kortext_home()/journal/<project>/ otherwise.
    """
    env_dir = os.getenv("JOURNAL_DIR")
    if env_dir and env_dir.strip():
        return Path(env_dir.strip()).expanduser().resolve()

    if workspace_root:
        return Path(workspace_root).expanduser().resolve() / ".kortext" / "memory"

    proj = (
        str(project).strip()
        if (project is not None and str(project).strip())
        else (os.getenv("KORTEXT_PROJECT") or os.getenv("PROJECT") or "default").strip()
    )
    clean_proj = re.sub(r"[^A-Za-z0-9._-]", "", proj)
    if not clean_proj or clean_proj in (".", "..") or not clean_proj.strip("."):
        clean_proj = "default"
    return get_kortext_home() / "journal" / clean_proj


def _atomic_write_text(target_path: Path, text: str) -> None:
    """Writes text to target_path atomically using a temporary file in the same directory."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = target_path.parent
    tmp_file = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=temp_dir, delete=False, encoding="utf-8") as f:
            tmp_file = Path(f.name)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, target_path)
    finally:
        if tmp_file and tmp_file.exists():
            try:
                tmp_file.unlink()
            except OSError:
                pass


def generate_memory_index(memory_dir: Path, project: Optional[str] = None) -> Path:
    """
    Generates INDEX.md in memory_dir from Markdown files alone, capped at 40 lines.
    Includes project name, last-updated date, current non-superseded decisions and facts,
    last 10 log entries with relative file links, and open tasks if available.
    """
    memory_dir = Path(memory_dir).resolve()
    index_path = memory_dir / "INDEX.md"

    if not memory_dir.is_dir():
        memory_dir.mkdir(parents=True, exist_ok=True)

    detected_project = project
    all_dates: List[str] = []
    superseded_patterns: Set[str] = set()

    # Parsed items
    # decisions: list of (date, title, rel_link)
    decisions: List[Tuple[str, str, str]] = []
    # facts: list of fact strings
    facts: List[str] = []
    # log_entries: list of (date, time, title, rel_link)
    log_entries: List[Tuple[str, str, str, str]] = []

    # 1. Parse daily logs (YYYY-MM-DD.md)
    daily_files = sorted(memory_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"), reverse=True)
    for df in daily_files:
        date_str = df.stem
        all_dates.append(date_str)
        rel_link = df.name
        try:
            content = df.read_text(encoding="utf-8")
        except Exception:
            continue

        sections = re.split(r"(?m)^##\s+", content)
        for sec in sections[1:]:
            lines = sec.strip().splitlines()
            if not lines:
                continue
            header = lines[0].strip()
            body = "\n".join(lines[1:]).strip()

            time_str = "00:00"
            tm = re.search(r"^(\d{2}:\d{2})\s*[-]\s*(.+)$", header)
            if tm:
                time_str = tm.group(1).strip()
                title = tm.group(2).strip()
            else:
                title = header

            # Check project
            if not detected_project:
                pm = re.search(r"\[Project:\s*([^\]]+)\]", body) or re.search(r"\*\*Project:\*\*\s*(.*)", body)
                if pm:
                    detected_project = pm.group(1).strip()

            # Supersedes
            sm = re.search(r"\*\*Supersedes:\*\*\s*(.*)", body, re.IGNORECASE) or re.search(
                r"[-*]\s*\*\*Supersedes:\*\*\s*(.*)", body, re.IGNORECASE
            )
            if sm:
                for pat in re.split(r"[,;|]+", sm.group(1)):
                    p_clean = pat.strip().lower()
                    if p_clean:
                        superseded_patterns.add(p_clean)

            # Category
            cat = "implementation"
            cm = re.search(r"\*\*Category:\*\*\s*([a-zA-Z0-9_\-]+)", body, re.IGNORECASE) or re.search(
                r"[-*]\s*\*\*Category:\*\*\s*([a-zA-Z0-9_\-]+)", body, re.IGNORECASE
            )
            if cm:
                cat = cm.group(1).strip().lower()

            if cat == "decision":
                decisions.append((date_str, title, rel_link))

            # Fact check
            fm = re.search(r"\*\*Facts?:\*\*\s*(.*)", body, re.IGNORECASE)
            if fm:
                f_txt = fm.group(1).strip()
                if f_txt and f_txt not in facts:
                    facts.append(f_txt)

            log_entries.append((date_str, time_str, title, rel_link))

    # 2. Parse dedicated category directories (e.g. decisions/*.md)
    for sub in memory_dir.iterdir():
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        folder_name = sub.name.lower()
        sub_files = sorted(sub.glob("*.md"), reverse=True)
        for sf in sub_files:
            rel_link = f"{folder_name}/{sf.name}"
            try:
                content = sf.read_text(encoding="utf-8")
            except Exception:
                continue

            h1 = re.search(r"^#\s+(.*)", content, re.MULTILINE)
            title = h1.group(1).strip() if h1 else sf.stem.replace("_", " ").title()

            try:
                mtime_date = datetime.fromtimestamp(sf.stat().st_mtime).strftime("%Y-%m-%d")
            except Exception:
                mtime_date = datetime.now().strftime("%Y-%m-%d")
            date_str = sf.stem[:10] if len(sf.stem) >= 10 and sf.stem[:4].isdigit() else mtime_date
            dm = re.search(r"[-*]\s*\*\*Date:\*\*\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", content)
            if dm:
                date_str = dm.group(1).strip()
            all_dates.append(date_str)

            if not detected_project:
                pm = re.search(r"[-*]\s*\*\*Project:\*\*\s*(.*)", content) or re.search(r"\[Project:\s*([^\]]+)\]", content)
                if pm:
                    detected_project = pm.group(1).strip()

            sm = re.search(r"[-*]\s*\*\*Supersedes:\*\*\s*(.*)", content)
            if sm:
                for pat in re.split(r"[,;|]+", sm.group(1)):
                    p_clean = pat.strip().lower()
                    if p_clean:
                        superseded_patterns.add(p_clean)

            if folder_name == "decisions":
                decisions.append((date_str, title, rel_link))

    # Determine project name and last updated date
    proj_name = (
        detected_project
        or (memory_dir.parent.name if memory_dir.name == "memory" else memory_dir.name)
        or os.getenv("KORTEXT_PROJECT")
        or "kortext"
    )
    last_updated = max(all_dates) if all_dates else datetime.now().strftime("%Y-%m-%d")

    # Filter out superseded decisions
    active_decisions: List[Tuple[str, str, str]] = []
    seen_decisions: Set[str] = set()
    for d_date, d_title, d_link in decisions:
        d_key = d_title.strip().lower()
        if d_key in seen_decisions:
            continue
        is_superseded = any(pat in d_key for pat in superseded_patterns)
        if not is_superseded:
            seen_decisions.add(d_key)
            active_decisions.append((d_date, d_title, d_link))

    # Sort log entries descending by date + time, limit to last 10
    log_entries.sort(key=lambda x: f"{x[0]} {x[1]}", reverse=True)
    dedup_logs: List[Tuple[str, str, str, str]] = []
    seen_log_keys: Set[str] = set()
    for l_date, l_time, l_title, l_link in log_entries:
        l_key = f"{l_date}:{l_title.strip().lower()}"
        if l_key in seen_log_keys:
            continue
        seen_log_keys.add(l_key)
        dedup_logs.append((l_date, l_time, l_title, l_link))
        if len(dedup_logs) >= 10:
            break

    # Look for open tasks from ledger.md if cheap to find
    open_tasks: List[str] = []
    task_candidates = [
        memory_dir.parent / "ledger.md",
        memory_dir.parent.parent / ".kortext" / "ledger.md",
        memory_dir / "ledger.md",
    ]
    for tc in task_candidates:
        if tc.is_file():
            try:
                for line in tc.read_text(encoding="utf-8").splitlines():
                    clean_l = line.strip()
                    if clean_l.startswith("- [ ]") or "in_progress" in clean_l.lower():
                        open_tasks.append(clean_l[:80])
                        if len(open_tasks) >= 3:
                            break
            except Exception:
                pass
            if open_tasks:
                break

    # Build Markdown structure ensuring at most 40 lines
    lines: List[str] = [
        f"# Memory Index: {proj_name}",
        f"- **Last Updated:** {last_updated}",
        "",
        "## Current Decisions & Facts",
    ]

    # Add active decisions and facts
    added_dec = 0
    for d_date, d_title, d_link in active_decisions[:6]:
        lines.append(f"- [{d_title}]({d_link}) ({d_date})")
        added_dec += 1
    for f in facts[:3]:
        lines.append(f"- {f[:80]}")
    if added_dec == 0 and not facts:
        lines.append("- None recorded.")

    lines.append("")
    lines.append("## Recent Log")
    for l_date, l_time, l_title, l_link in dedup_logs:
        lines.append(f"- [{l_title}]({l_link}) ({l_date})")
    if not dedup_logs:
        lines.append("- No activity.")

    if open_tasks:
        lines.append("")
        lines.append("## Open Tasks")
        for t in open_tasks[:3]:
            lines.append(t)

    # Strictly cap at 40 lines
    if len(lines) > 40:
        lines = lines[:40]

    content = "\n".join(lines) + "\n"
    _atomic_write_text(index_path, content)
    return index_path


def record_session_log(context: Any, modified_paths: Sequence[str]) -> Optional[Path]:
    """
    Appends or updates one entry per session in the daily log file.
    Idempotent: updates the session entry if already present.
    Never calls FalkorDB. Regenerates INDEX.md.
    """
    ws = getattr(context, "cwd", None)
    if not ws:
        wps = getattr(context, "workspace_paths", ())
        ws = wps[0] if wps else None

    proj = getattr(context, "workspace_project", None)
    if not proj and ws:
        proj = Path(ws).name
    if not proj:
        proj = "default"

    memory_dir = resolve_memory_dir(workspace_root=ws, project=proj)
    memory_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.now().strftime("%H:%M")
    journal_file = memory_dir / f"{today}.md"

    session_id = str(getattr(context, "session_id", "") or "session")
    short_sess = session_id[:8] if len(session_id) >= 8 else session_id

    # Redact secrets, collapse newlines and leading '#', and truncate last user input
    raw_user_input = getattr(context, "last_user_input", "") or ""
    redacted_input = redact_secrets(str(raw_user_input).strip())
    sanitized_input = _sanitize_for_markdown_field(redacted_input)
    truncated_input = sanitized_input[:200] if len(sanitized_input) > 200 else sanitized_input
    if not truncated_input:
        truncated_input = "Session automated modifications"

    short_paths = [_sanitize_for_markdown_field(str(p)) for p in modified_paths]
    file_bullets = "\n".join(f"- `{p}`" for p in short_paths if p)
    if not file_bullets:
        file_bullets = "- `none`"

    entry_block = (
        f"## {now_time} - Session {short_sess}\n\n"
        f"**User Request:** {truncated_input}\n\n"
        f"**Modified Files:**\n{file_bullets}\n\n"
        f"**Category:** implementation\n"
        f"**Project:** {proj}\n"
    )

    lock_file = memory_dir / ".journal.lock"
    try:
        with portalocker.Lock(str(lock_file), timeout=1.0):
            if journal_file.is_file():
                try:
                    content = journal_file.read_text(encoding="utf-8")
                except Exception:
                    content = ""
            else:
                content = ""

            # Check if an entry for this session already exists
            sess_pattern = re.compile(
                rf"(?ms)^##\s+\d{{2}}:\d{{2}}\s*-\s*Session\s+{re.escape(short_sess)}\b.*?(?=(?:\n##\s+|\Z))"
            )

            if sess_pattern.search(content):
                # Update existing session entry in place using function replacement
                new_content = sess_pattern.sub(lambda _: entry_block.strip(), content).strip() + "\n"
            else:
                # Append new entry
                stripped = content.strip()
                if stripped:
                    new_content = stripped + "\n\n" + entry_block.strip() + "\n"
                else:
                    new_content = entry_block.strip() + "\n"

            _atomic_write_text(journal_file, new_content)
            try:
                generate_memory_index(memory_dir, project=proj)
            except Exception as exc:
                logger.debug("Failed to regenerate memory index in record_session_log: %s", exc)
    except portalocker.exceptions.BaseLockException:
        logger.debug("Daily journal lock timed out; skipping session log for session %s", short_sess)
        return None

    return journal_file
