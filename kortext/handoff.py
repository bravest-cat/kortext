"""
Unified Cross-Agent Session & Task Handoff Manager.
Consolidates session batons and subagent task handoffs onto a single Pydantic schema
and canonical storage directory (get_kortext_home() / "handoffs") with glob-safe lookup
and atomic temp-file replacement.
"""
from __future__ import annotations

from datetime import datetime, timezone
import glob
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Union
import uuid

from pydantic import BaseModel, ConfigDict, Field

from kortext._home import get_kortext_home

logger = logging.getLogger(__name__)

_REPO_JOURNAL_HANDOFFS = (Path(__file__).resolve().parent.parent / "journal" / "handoffs").resolve()
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-:]+$")


def _atomic_write_json(target_path: Path, payload: Dict[str, Any]) -> None:
    """Atomically writes a JSON payload via a PID-scoped temporary file and os.replace."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(f".tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(target_path)


class HandoffRecord(BaseModel):
    """Unified immutable schema for both session batons and subagent task handoffs."""

    model_config = ConfigDict(frozen=True, extra="allow")

    baton_id: str
    task_id: Optional[str] = None
    handoff_ref: Optional[str] = None
    artifact_path: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None
    source_agent: str = "antigravity"
    target_agent: str = "any"
    current_scope: str = ""
    summary: str = ""
    open_hypotheses: List[str] = Field(default_factory=list)
    unresolved_questions: List[str] = Field(default_factory=list)
    active_blast_radius: List[str] = Field(default_factory=list)
    modified_files: List[str] = Field(default_factory=list)
    tests_passed: Optional[bool] = None
    status: str = "pending"
    root_conversation_id: Optional[str] = None


def _normalize_handoff_payload(raw: Dict[str, Any], file_path: Optional[Path] = None) -> Dict[str, Any]:
    """Normalizes either a task handoff or a session baton into the unified schema."""
    data = dict(raw)
    bid = str(data.get("baton_id") or data.get("task_id") or (file_path.stem if file_path else "unknown"))
    tid = data.get("task_id") or bid
    scope = str(data.get("current_scope") or data.get("summary") or "")
    summary = str(data.get("summary") or scope)
    blast = list(data.get("active_blast_radius") or data.get("modified_files") or [])
    mod_files = list(data.get("modified_files") or blast)
    now_iso = datetime.now(timezone.utc).isoformat()

    data.setdefault("baton_id", bid)
    data.setdefault("task_id", tid)
    data.setdefault("handoff_ref", f"handoff:{tid}")
    if file_path is not None:
        data.setdefault("artifact_path", str(file_path))
    data.setdefault("created_at", str(data.get("completed_at") or now_iso))
    data.setdefault("source_agent", "antigravity")
    data.setdefault("target_agent", "any")
    data["current_scope"] = scope
    data["summary"] = summary
    data.setdefault("open_hypotheses", [])
    data.setdefault("unresolved_questions", [])
    data["active_blast_radius"] = blast
    data["modified_files"] = mod_files
    data.setdefault("status", "pending")
    data.setdefault("root_conversation_id", raw.get("root_conversation_id"))
    return data


def _sanitize_lookup_id(raw_id: str) -> str:
    """Strips glob wildcards and path traversal characters from a baton/task ID."""
    cleaned = raw_id.strip()
    if cleaned.startswith("handoff:"):
        cleaned = cleaned[len("handoff:") :]
    cleaned = re.sub(r"[*?\[\]/\\]", "", cleaned)
    return cleaned


class HandoffManager:
    """Manages unified cross-agent context handoff batons and task handoff artifacts."""

    def __init__(self, handoffs_dir: Optional[Union[str, Path]] = None) -> None:
        default_home_dir = get_kortext_home() / "handoffs"
        if handoffs_dir is None:
            self.handoffs_dir = default_home_dir
            self._secondary_dir: Optional[Path] = _REPO_JOURNAL_HANDOFFS
        else:
            resolved = Path(handoffs_dir).expanduser().resolve()
            self.handoffs_dir = Path(handoffs_dir)
            if resolved == _REPO_JOURNAL_HANDOFFS:
                self._secondary_dir = default_home_dir
            elif resolved == default_home_dir.resolve():
                self._secondary_dir = _REPO_JOURNAL_HANDOFFS
            else:
                self._secondary_dir = None

        self.handoffs_dir.mkdir(parents=True, exist_ok=True)
        if self._secondary_dir is not None:
            try:
                self._secondary_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.warning("Suppressed in handoff:%d: %s", 120, exc)

    def _collect_handoff_files(self) -> List[Path]:
        """Collects handoff JSON files across primary (and optional secondary) handoff directories."""
        seen_names: set[str] = set()
        files: List[Path] = []
        dirs = [self.handoffs_dir]
        if self._secondary_dir is not None and self._secondary_dir != self.handoffs_dir:
            dirs.append(self._secondary_dir)

        for d in dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.json"), reverse=True):
                if f.name not in seen_names:
                    seen_names.add(f.name)
                    files.append(f)
        files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
        return files

    def create_baton(
        self,
        target_agent: Optional[str] = None,
        current_scope: Optional[str] = None,
        open_hypotheses: Optional[list[str]] = None,
        unresolved_questions: Optional[list[str]] = None,
        active_blast_radius: Optional[list[str]] = None,
        task_id: Optional[str] = None,
        tests_passed: Optional[bool] = None,
        root_conversation_id: Optional[str] = None,
    ) -> str:
        """Create a new cross-agent handoff baton using the unified HandoffRecord schema."""
        now = datetime.now(timezone.utc).isoformat()
        safe_tid = _sanitize_lookup_id(task_id) if task_id else None
        bid = safe_tid or f"baton_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        baton_path = self.handoffs_dir / f"{bid}.json"

        record = HandoffRecord(
            baton_id=bid,
            task_id=safe_tid or bid,
            handoff_ref=f"handoff:{safe_tid or bid}",
            artifact_path=str(baton_path),
            created_at=now,
            source_agent="antigravity",
            target_agent=target_agent or "any",
            current_scope=current_scope or "",
            summary=current_scope or "",
            open_hypotheses=list(open_hypotheses or []),
            unresolved_questions=list(unresolved_questions or []),
            active_blast_radius=list(active_blast_radius or []),
            modified_files=list(active_blast_radius or []),
            tests_passed=tests_passed,
            status="pending",
            root_conversation_id=root_conversation_id,
        )
        data = record.model_dump(exclude_none=True)

        _atomic_write_json(baton_path, data)

        if self._secondary_dir is not None:
            try:
                sec_path = self._secondary_dir / f"{bid}.json"
                _atomic_write_json(sec_path, data)
            except Exception as exc:
                logger.warning("Suppressed in handoff:%d: %s", 182, exc)

        return (
            f"✅ Created cross-agent handoff baton: {bid}\n"
            f"   → Target: {data['target_agent']}\n"
            f"   → Scope: {data['current_scope']}\n"
            f"   → Open hypotheses: {len(data['open_hypotheses'])}\n"
            f"   → Unresolved questions: {len(data['unresolved_questions'])}\n"
            f"   → Active blast radius: {len(data['active_blast_radius'])}\n"
            f"   → Path: {baton_path}"
        )

    def read_baton(
        self,
        baton_id: Optional[str] = None,
        target_agent: Optional[str] = None,
        root_conversation_id: Optional[str] = None,
    ) -> str:
        """Read a handoff baton or task handoff by ID or the latest pending baton for target_agent."""
        files = self._collect_handoff_files()
        if not files:
            return "⚠️ No cross-agent handoff batons found in handoffs directory."

        target_file: Optional[Path] = None
        if baton_id:
            safe_id = _sanitize_lookup_id(baton_id)
            if not safe_id:
                return f"⚠️ Invalid baton_id={baton_id!r}."
            exact = [f for f in files if f.stem in (safe_id, f"task_handoff_{safe_id}")]
            matches = exact or [f for f in files if safe_id in f.stem]
            if root_conversation_id:
                for candidate in matches:
                    try:
                        with open(candidate, "r", encoding="utf-8") as jf:
                            d = _normalize_handoff_payload(json.load(jf), candidate)
                        if d.get("root_conversation_id") == root_conversation_id:
                            target_file = candidate
                            break
                    except Exception:
                        continue
            if not target_file and matches:
                target_file = matches[0]
        else:
            for f in files:
                try:
                    with open(f, "r", encoding="utf-8") as jf:
                        d = _normalize_handoff_payload(json.load(jf), f)
                    if d.get("status") in ("pending", "completed"):
                        if d.get("status") == "pending" and (
                            not target_agent or d.get("target_agent") in (target_agent, "any")
                        ):
                            if root_conversation_id and d.get("root_conversation_id") != root_conversation_id:
                                continue
                            target_file = f
                            break
                except Exception:
                    continue

        if not target_file:
            return f"⚠️ No matching handoff baton found (baton_id={baton_id}, target_agent={target_agent})."

        with open(target_file, "r", encoding="utf-8") as jf:
            data = _normalize_handoff_payload(json.load(jf), target_file)
        return json.dumps(data, separators=(",", ":"))

    def list_batons(self, limit: int = 10, root_conversation_id: Optional[str] = None) -> str:
        """List recent handoff batons up to limit, optionally filtered by root_conversation_id."""
        files = self._collect_handoff_files()
        summaries = []
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as jf:
                    d = _normalize_handoff_payload(json.load(jf), f)
                if root_conversation_id and d.get("root_conversation_id") != root_conversation_id:
                    continue
                summaries.append({
                    "baton_id": d.get("baton_id"),
                    "task_id": d.get("task_id"),
                    "created_at": d.get("created_at"),
                    "target_agent": d.get("target_agent"),
                    "current_scope": d.get("current_scope"),
                    "status": d.get("status"),
                    "root_conversation_id": d.get("root_conversation_id"),
                })
                if len(summaries) >= limit:
                    break
            except Exception:
                logger.debug("Suppressed non-fatal exception", exc_info=True)
        return json.dumps({"pending_batons": summaries}, separators=(",", ":"))

    def complete_baton(self, baton_id: Optional[str] = None) -> str:
        """Mark a handoff baton as completed using glob-safe ID matching and atomic replacement."""
        if not baton_id:
            return "⚠️ Please provide baton_id to mark complete."
        safe_id = _sanitize_lookup_id(baton_id)
        if not safe_id:
            return f"⚠️ Baton '{baton_id}' not found."

        escaped = glob.escape(safe_id)
        matched = list(self.handoffs_dir.glob(f"*{escaped}*.json"))
        if not matched and self._secondary_dir is not None and self._secondary_dir.is_dir():
            matched = list(self._secondary_dir.glob(f"*{escaped}*.json"))

        if not matched:
            return f"⚠️ Baton '{baton_id}' not found."

        exact = [m for m in matched if m.stem in (safe_id, f"task_handoff_{safe_id}")]
        if not exact and len(matched) > 1:
            return f"⚠️ Baton '{baton_id}' is ambiguous: {sorted(m.stem for m in matched)}."
        target_path = (exact or matched)[0]
        with open(target_path, "r", encoding="utf-8") as jf:
            data = _normalize_handoff_payload(json.load(jf), target_path)
        now = datetime.now(timezone.utc).isoformat()
        data["status"] = "completed"
        data["completed_at"] = now
        _atomic_write_json(target_path, data)
        if self._secondary_dir is not None:
            try:
                sec_file = self._secondary_dir / target_path.name
                if sec_file.is_file():
                    _atomic_write_json(sec_file, data)
            except Exception as exc:
                logger.warning("Suppressed in handoff:%d: %s", 283, exc)
        return f"✅ Handoff baton '{data['baton_id']}' marked as completed."

    def execute(
        self,
        action: str = "create",
        target_agent: Optional[str] = None,
        current_scope: Optional[str] = None,
        open_hypotheses: Optional[list[str]] = None,
        unresolved_questions: Optional[list[str]] = None,
        active_blast_radius: Optional[list[str]] = None,
        baton_id: Optional[str] = None,
        root_conversation_id: Optional[str] = None,
    ) -> str:
        """Dispatch handoff action to the appropriate handler method."""
        action_norm = action.lower().strip()
        if action_norm == "create":
            return self.create_baton(
                target_agent=target_agent,
                current_scope=current_scope,
                open_hypotheses=open_hypotheses,
                unresolved_questions=unresolved_questions,
                active_blast_radius=active_blast_radius,
                task_id=baton_id,
                root_conversation_id=root_conversation_id,
            )
        elif action_norm == "read":
            return self.read_baton(
                baton_id=baton_id,
                target_agent=target_agent,
                root_conversation_id=root_conversation_id,
            )
        elif action_norm == "list":
            return self.list_batons(root_conversation_id=root_conversation_id)
        elif action_norm == "complete":
            return self.complete_baton(baton_id=baton_id)
        return f"⚠️ Unknown action '{action}'. Use create, read, list, or complete."
