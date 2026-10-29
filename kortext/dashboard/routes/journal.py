"""
Journal parsing and file reading route handlers.
"""

import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

router = APIRouter(tags=["journal"])

KORTEXT_ROOT = Path(__file__).resolve().parents[3]
JOURNAL_DIR = Path(os.getenv("JOURNAL_DIR", str(KORTEXT_ROOT / "journal")))


VALID_CATEGORIES = frozenset({
    "decision", "roadmap", "implementation", "experiment",
    "benchmark", "investigation", "research", "report", "dream",
})

CATEGORY_FOLDER_MAP = {
    "decisions": "decision",
    "roadmaps": "roadmap",
    "implementations": "implementation",
    "experiments": "experiment",
    "benchmarks": "benchmark",
    "investigations": "investigation",
    "research": "research",
    "reports": "report",
    "dreams": "report",
    "handoffs": "report",
}


def _extract_category(body: str, folder_hint: Optional[str] = None, title_hint: Optional[str] = None) -> str:
    """
    Extracts or infers category from markdown metadata, tags, folder location, title, or body keywords.
    Order of precedence:
    1. Explicit metadata (**Category:**, [Category: ...])
    2. Folder hint (journal/decisions, journal/roadmaps, etc.)
    3. Explicit tags (**Tags:**)
    4. Title semantic intent
    5. Body semantic keywords
    6. Default ("implementation")
    """
    # 1. Explicit metadata
    cat_match = (
        re.search(r"[-*]\s*\*\*Category:\*\*\s*([a-zA-Z0-9_\-]+)", body, re.IGNORECASE)
        or re.search(r"\*\*Category:\*\*\s*([a-zA-Z0-9_\-]+)", body, re.IGNORECASE)
        or re.search(r"\[Category:\s*([^\]]+)\]", body, re.IGNORECASE)
        or re.search(r"^category:\s*([a-zA-Z0-9_\-]+)", body, re.IGNORECASE | re.MULTILINE)
    )
    if cat_match:
        val = cat_match.group(1).strip().lower()
        if val in VALID_CATEGORIES:
            return "report" if val == "dream" else val
        if val in CATEGORY_FOLDER_MAP:
            return CATEGORY_FOLDER_MAP[val]

    # 2. Folder hint
    if folder_hint and folder_hint.lower() in CATEGORY_FOLDER_MAP:
        return CATEGORY_FOLDER_MAP[folder_hint.lower()]

    # 3. Tags classification
    tag_match = re.search(r"[-*]?\s*\*\*Tags:\*\*\s*(.*)", body, re.IGNORECASE)
    if tag_match:
        extracted_tags = [t.strip().lower() for t in re.split(r"[,;|]+", tag_match.group(1)) if t.strip()]
        # Check canonical categories first
        for canonical_cat in ("decision", "roadmap", "benchmark", "experiment", "investigation", "research", "report"):
            if canonical_cat in extracted_tags:
                return canonical_cat

        # Check domain-specific aliases and related terms
        for t in extracted_tags:
            if t in ("adr", "architectural-decision", "architecture-decision", "tradeoff"):
                return "decision"
            if t in ("milestone", "planning", "sprint-plan", "sprint"):
                return "roadmap"
            if t in ("benchmarks", "latency", "throughput", "performance", "perf"):
                return "benchmark"
            if t in ("prototype", "spike"):
                return "experiment"
            if t in ("debugging", "debug", "root-cause", "bugfix", "fix", "defect"):
                return "investigation"
            if t in ("survey", "exploration", "onboarding", "literature"):
                return "research"
            if t in ("audit", "summary", "handoff", "dream", "dream-mode", "review"):
                return "report"

    # 4. Title semantics
    if title_hint:
        t_low = title_hint.lower()
        if any(k in t_low for k in ("decision", "decided", "architecture decision")) or re.search(r"\badr\b", t_low):
            return "decision"
        if any(k in t_low for k in ("roadmap", "milestone", "bucket", "plan")):
            return "roadmap"
        if any(k in t_low for k in ("investigat", "root cause", "debug", "bug fix", "fixing", "resolved issue", "error resolution")):
            return "investigation"
        if any(k in t_low for k in ("benchmark", "latency", "load test", "throughput", "perf ")):
            return "benchmark"
        if any(k in t_low for k in ("experiment", "prototype", "spike")):
            return "experiment"
        if any(k in t_low for k in ("research", "survey", "exploration", "assessment")):
            return "research"
        if any(k in t_low for k in ("report", "summary", "handoff", "dream report", "dream cycle")):
            return "report"

    # 5. Body semantic keywords
    body_lower = body.lower()
    if "decision:" in body_lower or "adr:" in body_lower or "architectural decision" in body_lower or "decision rationale" in body_lower:
        return "decision"
    if "roadmap:" in body_lower or "milestone:" in body_lower or "agile buckets" in body_lower:
        return "roadmap"
    if "benchmark:" in body_lower or "benchmarking" in body_lower or "latency benchmark" in body_lower:
        return "benchmark"
    if "experiment:" in body_lower:
        return "experiment"
    if "investigation:" in body_lower or "root cause:" in body_lower or "diagnosed" in body_lower:
        return "investigation"
    if "dream cycle" in body_lower or "dream report" in body_lower or "session summary" in body_lower:
        return "report"

    return "implementation"


@router.get("/api/journal")
async def get_journal():
    """Parses all journal files and returns chronological entries with categories."""
    if not JOURNAL_DIR.is_dir():
        return {"entries": []}

    entries = []
    seen_keys = set()

    # 1. Parse daily markdown journals (e.g. journal/2026-10-10.md)
    daily_files = sorted(JOURNAL_DIR.glob("*.md"), reverse=True)
    for f in daily_files:
        if f.name.startswith(".") or f.stem in ("README", "invariants"):
            continue
        date_str = f.stem
        try:
            content = f.read_text(encoding="utf-8")
            sections = re.split(r"(?m)^##\s+", content)
            for sec in sections[1:]:
                lines = sec.strip().split("\n")
                if not lines:
                    continue
                header = lines[0].strip()
                body = "\n".join(lines[1:]).strip()

                tags = []
                project = None
                tag_match = re.search(r"[-*]?\s*\*\*Tags:\*\*\s*(.*)", body, re.IGNORECASE)
                if tag_match:
                    tags = [t.strip() for t in re.split(r"[,;|]+", tag_match.group(1)) if t.strip()]
                proj_match = re.search(r"\[Project:\s*([^\]]+)\]", body) or re.search(r"\*\*Project:\*\*\s*(.*)", body)
                if proj_match:
                    project = proj_match.group(1).strip()

                category = _extract_category(body, title_hint=header)

                dedup_key = f"{date_str}:{header.lower()}"
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                entries.append({
                    "date": date_str,
                    "title": header,
                    "body": body,
                    "category": category,
                    "tags": tags,
                    "project": project,
                })
        except Exception:
            continue

    # 2. Parse dedicated category directories (e.g. journal/decisions/*.md, journal/reports/*.md)
    for folder_name, cat_name in CATEGORY_FOLDER_MAP.items():
        sub_dir = JOURNAL_DIR / folder_name
        if not sub_dir.is_dir():
            continue
        sub_files = sorted(sub_dir.glob("*.md"), reverse=True)
        for sf in sub_files:
            if sf.name.startswith("."):
                continue
            try:
                content = sf.read_text(encoding="utf-8")
                # Check if it has a top-level # Title
                title = sf.stem.replace("_", " ").replace("-", " ").title()
                date_str = sf.stem[:10] if len(sf.stem) >= 10 and sf.stem[:4].isdigit() else "2026-10-10"
                body = content

                h1_match = re.search(r"^#\s+(.*)", content)
                if h1_match:
                    title = h1_match.group(1).strip()

                date_match = re.search(r"[-*]\s*\*\*Date:\*\*\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", content)
                if date_match:
                    date_str = date_match.group(1)

                tags = []
                tag_match = re.search(r"[-*]\s*\*\*Tags:\*\*\s*(.*)", content)
                if tag_match:
                    tags = [t.strip() for t in tag_match.group(1).split(",") if t.strip()]

                project = None
                proj_match = re.search(r"[-*]\s*\*\*Project:\*\*\s*(.*)", content) or re.search(r"\[Project:\s*([^\]]+)\]", content)
                if proj_match:
                    project = proj_match.group(1).strip()

                category = _extract_category(content, folder_hint=folder_name, title_hint=title)

                dedup_key = f"{date_str}:{title.lower()}"
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                entries.append({
                    "date": date_str,
                    "title": title,
                    "body": body,
                    "category": category,
                    "tags": tags,
                    "project": project,
                })
            except Exception:
                continue

    # Sort descending by date
    entries.sort(key=lambda e: str(e.get("date", "")), reverse=True)
    return {"entries": entries}
