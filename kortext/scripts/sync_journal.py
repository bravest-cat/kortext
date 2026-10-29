"""
Journal Backfill & Synchronization Utility.
Reads markdown journal files from journal/*.md and syncs any missing
episodes into the FalkorDB temporal knowledge graph via AgentNativeEpisodicDriver.
"""
import asyncio
import os
import re
from pathlib import Path
from typing import List, Set, Tuple


from kortext.episodic import AgentNativeEpisodicDriver
from kortext.falkordb_client import get_shared_falkordb_client

KORTEXT_ROOT = Path(__file__).resolve().parent.parent.parent
JOURNAL_DIR = Path(os.getenv("JOURNAL_DIR", str(KORTEXT_ROOT / "journal")))
FALKORDB_HOST = os.getenv("FALKORDB_HOST", "127.0.0.1")
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
FALKORDB_DB = os.getenv("KORTEXT_DATABASE", os.getenv("FALKORDB_DATABASE", "kortext"))


def get_existing_episodes_in_falkor() -> Set[str]:
    """Queries FalkorDB via shared UDS/TCP client for all existing Episodic/Episode node names."""
    existing: Set[str] = set()
    try:
        client = get_shared_falkordb_client(host=FALKORDB_HOST, port=FALKORDB_PORT)
        res = client.query(
            FALKORDB_DB,
            "MATCH (e) WHERE e:Episodic OR e:Episode RETURN e.name",
            read_only=True,
        )
        if res and len(res) > 1 and isinstance(res[1], list):
            for row in res[1]:
                if row and row[0]:
                    existing.add(str(row[0]))
    except Exception as e:
        print(f"[Sync] Warning: Failed to query existing episodes: {e}")
    return existing


def parse_journal_entries() -> List[Tuple[str, str, List[str], str | None]]:
    """Returns list of (episode_name, content, tags, project)."""
    if not JOURNAL_DIR.is_dir():
        return []

    entries = []
    # Only parse dated daily journal files (e.g. 2026-09-18.md), ignoring invariants.md and dreams
    files = sorted(JOURNAL_DIR.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"))
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
            sections = re.split(r"(?m)^##\s+", content)
            for sec in sections[1:]:
                lines = sec.strip().split("\n")
                if not lines:
                    continue
                header = lines[0].strip()
                # Remove leading time if present (e.g., '14:30 - Title' -> 'Title')
                title_match = re.search(r"^(?:\d{2}:\d{2}\s*[\-]\s*|Episode:\s*)(.+)$", header)
                episode_title = title_match.group(1).strip() if title_match else header

                body = "\n".join(lines[1:]).strip()
                tags = []
                tag_match = re.search(r"\*\*Tags:\*\*\s*(.*)", body)
                if tag_match:
                    tags = [t.strip() for t in tag_match.group(1).split(",")]

                project = None
                proj_match = re.search(r"\[Project:\s*([^\]]+)\]", body) or re.search(r"\*\*Project:\*\*\s*(.*)", body)
                if proj_match:
                    project = proj_match.group(1).strip()

                entries.append((episode_title, body, tags, project))
        except Exception as e:
            print(f"[Sync] Error reading {f}: {e}")

    return entries


async def sync_journal(dry_run: bool = False):
    print(f"=== Kortext Journal Sync -> FalkorDB ({FALKORDB_DB}) ===")
    existing = get_existing_episodes_in_falkor()
    print(f"Current episodes in FalkorDB: {len(existing)}")

    all_entries = parse_journal_entries()
    print(f"Total episodes found in markdown journals: {len(all_entries)}")

    to_sync = [e for e in all_entries if e[0] not in existing]
    if not to_sync:
        print("✅ All journal episodes are already indexed in FalkorDB!")
        return

    print(f"\nFound {len(to_sync)} episodes pending sync:")
    for title, _, tags, proj in to_sync:
        print(f"  • {title} (Project: {proj or 'default'}, Tags: {', '.join(tags) or 'none'})")

    if dry_run:
        return

    print("\nStarting indexing via AgentNativeEpisodicDriver directly into FalkorDB...")
    driver = AgentNativeEpisodicDriver()
    if hasattr(driver, "initialize"):
        await driver.initialize()

    success_count = 0
    for idx, (title, body, tags, proj) in enumerate(to_sync, 1):
        print(f"[{idx}/{len(to_sync)}] Indexing: '{title}'...")
        full_content = body
        if proj:
            full_content = f"[Project: {proj}]\n\n{body}"
        try:
            res = await driver.record_outcome(
                episode_name=title,
                content=full_content,
                tags=tags,
                project=proj,
            )
            success_count += 1
            print(f"  ✅ Indexed into FalkorDB ({res.get('entities_written', 0)} entities, {res.get('facts_written', 0)} facts)")
        except Exception as e:
            print(f"  ❌ Failed: {e}")

    if hasattr(driver, "close"):
        await driver.close()
    print(f"\n🎉 Successfully synced {success_count}/{len(to_sync)} episodes into FalkorDB!")


# Alias for backward compatibility
sync_journals = sync_journal


if __name__ == "__main__":
    asyncio.run(sync_journal())
