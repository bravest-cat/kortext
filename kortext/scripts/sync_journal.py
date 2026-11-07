"""
Journal Backfill & Synchronization Utility.
Reads markdown journal files from memory vault and syncs missing
episodes into the FalkorDB temporal knowledge graph via AgentNativeEpisodicDriver.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

from kortext.episodic import AgentNativeEpisodicDriver
from kortext.falkordb_client import get_shared_falkordb_client
from kortext.trust.sanitizer import CausalSanitizer, SecurityViolation
from kortext.vault import resolve_memory_dir

FALKORDB_HOST = os.getenv("FALKORDB_HOST", "127.0.0.1")
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
FALKORDB_DB = os.getenv("KORTEXT_DATABASE", os.getenv("FALKORDB_DATABASE", "kortext"))


@dataclass
class JournalEntry:
    title: str
    date: str
    body: str
    category: str = "implementation"
    tags: List[str] = field(default_factory=list)
    project: Optional[str] = None
    supersedes: List[str] = field(default_factory=list)
    trust_level: str = "speculative"


def get_existing_episodes_in_falkor(
    host: Optional[str] = None,
    port: Optional[int] = None,
    database: Optional[str] = None,
) -> Dict[str, Set[str]]:
    """
    Queries FalkorDB for existing episodes.
    Returns mapping from lower-cased title to a set of associated dates or names.
    """
    h = host or FALKORDB_HOST
    p = port or FALKORDB_PORT
    db = database or FALKORDB_DB

    existing: Dict[str, Set[str]] = {}
    try:
        client = get_shared_falkordb_client(host=h, port=p)
        res = client.query(
            db,
            "MATCH (e) WHERE e:Episodic OR e:Episode RETURN e.name, coalesce(e.date, ''), coalesce(e.created_at, '')",
            read_only=True,
        )
        if res and len(res) > 1 and isinstance(res[1], list):
            for row in res[1]:
                if not row or not row[0]:
                    continue
                name = str(row[0])
                date_val = str(row[1]) if len(row) > 1 and row[1] else ""
                if not date_val and len(row) > 2 and row[2]:
                    date_val = str(row[2])[:10]

                # Strip trailing date disambiguation if present (e.g. 'Title (2026-10-01)' -> 'Title')
                base_title = name
                m = re.search(r"^(.*?)\s*\(\d{4}-\d{2}-\d{2}\)$", name)
                if m:
                    base_title = m.group(1).strip()

                key = base_title.lower().strip()
                existing.setdefault(key, set()).add(date_val)
                existing.setdefault(name.lower().strip(), set()).add(date_val)
    except Exception as e:
        print(f"[Sync] Warning: Failed to query existing episodes: {e}")
    return existing


def parse_journal_entries(
    root: Optional[Union[str, Path]] = None,
) -> List[JournalEntry]:
    """
    Parses daily markdown journals and dedicated category directories.
    Dedupes entries by date plus title.
    """
    journal_dir = Path(root).expanduser().resolve() if root else resolve_memory_dir()
    if not journal_dir.is_dir():
        return []

    entries: List[JournalEntry] = []
    seen_keys: Set[Tuple[str, str]] = set()

    # 1. Parse daily markdown journal files (YYYY-MM-DD.md)
    daily_files = sorted(journal_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"))
    for f in daily_files:
        date_str = f.stem
        try:
            content = f.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[Sync] Error reading {f}: {e}")
            continue

        sections = re.split(r"(?m)^##\s+", content)
        for sec in sections[1:]:
            lines = sec.strip().splitlines()
            if not lines:
                continue
            header = lines[0].strip()

            title_match = re.search(r"^(?:\d{2}:\d{2}\s*[-]\s*|Episode:\s*)(.+)$", header)
            episode_title = title_match.group(1).strip() if title_match else header

            body = "\n".join(lines[1:]).strip()

            cat = "implementation"
            cm = re.search(r"\*\*Category:\*\*\s*([a-zA-Z0-9_\-]+)", body, re.IGNORECASE) or re.search(
                r"[-*]\s*\*\*Category:\*\*\s*([a-zA-Z0-9_\-]+)", body, re.IGNORECASE
            )
            if cm:
                cat = cm.group(1).strip().lower()

            tags: List[str] = []
            tag_match = re.search(r"\*\*Tags:\*\*\s*(.*)", body, re.IGNORECASE) or re.search(
                r"[-*]\s*\*\*Tags:\*\*\s*(.*)", body, re.IGNORECASE
            )
            if tag_match:
                tags = [t.strip() for t in re.split(r"[,;|]+", tag_match.group(1)) if t.strip()]

            project: Optional[str] = None
            proj_match = re.search(r"\[Project:\s*([^\]]+)\]", body) or re.search(
                r"\*\*Project:\*\*\s*(.*)", body
            ) or re.search(r"[-*]\s*\*\*Project:\*\*\s*(.*)", body)
            if proj_match:
                project = proj_match.group(1).strip()

            supersedes: List[str] = []
            sm = re.search(r"\*\*Supersedes:\*\*\s*(.*)", body, re.IGNORECASE) or re.search(
                r"[-*]\s*\*\*Supersedes:\*\*\s*(.*)", body, re.IGNORECASE
            )
            if sm:
                supersedes = [s.strip() for s in re.split(r"[,;|]+", sm.group(1)) if s.strip()]

            # Always enforce speculative trust level for Markdown backfills
            dedup_key = (date_str, episode_title.lower())
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            entries.append(
                JournalEntry(
                    title=episode_title,
                    date=date_str,
                    body=body,
                    category=cat,
                    tags=tags,
                    project=project,
                    supersedes=supersedes,
                    trust_level="speculative",
                )
            )

    # 2. Parse category subdirectories (decisions/*.md, etc.)
    for sub in journal_dir.iterdir():
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        folder_cat = sub.name.lower()
        if folder_cat.endswith("s"):
            folder_cat = folder_cat[:-1]

        sub_files = sorted(sub.glob("*.md"))
        for sf in sub_files:
            try:
                content = sf.read_text(encoding="utf-8")
            except Exception as e:
                print(f"[Sync] Error reading {sf}: {e}")
                continue

            h1 = re.search(r"^#\s+(.*)", content, re.MULTILINE)
            episode_title = h1.group(1).strip() if h1 else sf.stem.replace("_", " ").title()

            date_str = sf.stem[:10] if len(sf.stem) >= 10 and sf.stem[:4].isdigit() else "2026-11-07"
            dm = re.search(r"[-*]\s*\*\*Date:\*\*\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", content)
            if dm:
                date_str = dm.group(1).strip()

            cat = folder_cat
            cm = re.search(r"[-*]\s*\*\*Category:\*\*\s*([a-zA-Z0-9_\-]+)", content, re.IGNORECASE)
            if cm:
                cat = cm.group(1).strip().lower()

            tags = []
            tm = re.search(r"[-*]\s*\*\*Tags:\*\*\s*(.*)", content, re.IGNORECASE)
            if tm:
                tags = [t.strip() for t in re.split(r"[,;|]+", tm.group(1)) if t.strip()]

            project = None
            pm = re.search(r"[-*]\s*\*\*Project:\*\*\s*(.*)", content) or re.search(
                r"\[Project:\s*([^\]]+)\]", content
            )
            if pm:
                project = pm.group(1).strip()

            supersedes = []
            sm = re.search(r"[-*]\s*\*\*Supersedes:\*\*\s*(.*)", content, re.IGNORECASE)
            if sm:
                supersedes = [s.strip() for s in re.split(r"[,;|]+", sm.group(1)) if s.strip()]

            dedup_key = (date_str, episode_title.lower())
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            entries.append(
                JournalEntry(
                    title=episode_title,
                    date=date_str,
                    body=content,
                    category=cat,
                    tags=tags,
                    project=project,
                    supersedes=supersedes,
                    trust_level="speculative",
                )
            )

    return entries


async def sync_journal(
    root: Optional[Union[str, Path]] = None,
    dry_run: bool = False,
    host: Optional[str] = None,
    port: Optional[int] = None,
    database: Optional[str] = None,
) -> int:
    """
    Rebuilds FalkorDB episodic knowledge graph from markdown journal files.
    Dedupes by date plus title. Capped at speculative trust.
    Runs every entry through CausalSanitizer.
    """
    db_name = database or FALKORDB_DB
    h = host or FALKORDB_HOST
    p = port or FALKORDB_PORT

    print(f"=== Kortext Journal Rebuild -> FalkorDB ({db_name}) ===")
    existing = get_existing_episodes_in_falkor(host=h, port=p, database=db_name)
    print(f"Current episode groups in FalkorDB: {len(existing)}")

    all_entries = parse_journal_entries(root=root)
    print(f"Total entries found in markdown journals: {len(all_entries)}")

    # Detect title collisions across dates
    title_dates: Dict[str, Set[str]] = {}
    for entry in all_entries:
        title_dates.setdefault(entry.title.lower(), set()).add(entry.date)

    to_sync: List[Tuple[JournalEntry, str]] = []
    for entry in all_entries:
        t_lower = entry.title.lower()
        has_multi_dates = len(title_dates.get(t_lower, set())) > 1

        # Check if this exact (date, title) already exists
        already_indexed = False
        if t_lower in existing:
            known_dates = existing[t_lower]
            if entry.date in known_dates or (len(known_dates) == 1 and "" in known_dates and not has_multi_dates):
                already_indexed = True

        name_with_date = f"{entry.title} ({entry.date})"
        if name_with_date.lower() in existing:
            already_indexed = True

        if already_indexed:
            continue

        target_ep_name = name_with_date if has_multi_dates else entry.title
        to_sync.append((entry, target_ep_name))

    if not to_sync:
        print("All journal episodes are already indexed in FalkorDB.")
        return 0

    print(f"\nFound {len(to_sync)} episodes pending sync:")
    for entry, ep_name in to_sync:
        print(f"  - {ep_name} [{entry.date}] (Category: {entry.category}, Project: {entry.project or 'default'})")

    if dry_run:
        return len(to_sync)

    print("\nStarting indexing via AgentNativeEpisodicDriver into FalkorDB...")
    driver = AgentNativeEpisodicDriver(host=h, port=p, db_name=db_name)
    if hasattr(driver, "initialize"):
        await driver.initialize()

    sanitizer = CausalSanitizer()
    success_count = 0

    for idx, (entry, ep_name) in enumerate(to_sync, 1):
        full_content = entry.body
        if entry.project:
            full_content = f"[Project: {entry.project}]\n\n{entry.body}"

        # SECURITY: Rebuilt entries are strictly capped at speculative trust.
        # Run through CausalSanitizer before persisting.
        try:
            sanitizer.sanitize(
                subject=ep_name,
                relation="RECORDED",
                fact=full_content,
                source_tier=3,
                trust_level="speculative",
            )
        except SecurityViolation as sv:
            print(f"  - [{idx}/{len(to_sync)}] BLOCKED by CausalSanitizer: '{ep_name}' ({sv})")
            continue

        try:
            res = await driver.record_outcome(
                episode_name=ep_name,
                content=full_content,
                tags=entry.tags,
                project=entry.project,
                supersedes=entry.supersedes,
                trust_level="speculative",
                category=entry.category,
                date=entry.date,
            )
            success_count += 1
            print(
                f"  - [{idx}/{len(to_sync)}] Indexed: '{ep_name}' "
                f"({res.get('entities_written', 0)} entities, {res.get('facts_written', 0)} facts)"
            )
        except Exception as e:
            print(f"  - [{idx}/{len(to_sync)}] Failed to index '{ep_name}': {e}")

    if hasattr(driver, "close"):
        await driver.close()

    print(f"\nSuccessfully synced {success_count}/{len(to_sync)} episodes into FalkorDB.")
    return success_count


if __name__ == "__main__":
    asyncio.run(sync_journal())
