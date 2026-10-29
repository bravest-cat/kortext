"""
Task Orchestration, Pattern Routing, Brief Generation, and Handoff Mixin for TaskDAGTracker.
Keeps kortext/tasks/tracker.py modular and well below the 800-line ceiling.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from kortext._home import get_kortext_home

logger = logging.getLogger("kortext.tasks")


def _as_list(value: Any) -> List[str]:
    """FalkorDB returns list properties either as lists or as '[a, b]' strings."""
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        cleaned = value.strip().strip("[]").replace("'", "").replace('"', "")
        return [x.strip() for x in cleaned.split(",") if x.strip()]
    return []


def _same_path(a: str, b: str) -> bool:
    """Path equality that tolerates one side being relative: 'src/x.py' matches '/repo/src/x.py', not 'test_x.py'."""
    a, b = os.path.normpath(a), os.path.normpath(b)
    return a == b or a.endswith(os.sep + b) or b.endswith(os.sep + a)


def write_hud_state_snapshot(
    ready_tasks: Optional[int] = None,
    in_progress_tasks: Optional[int] = None,
    project_id: Optional[str] = None,
    falkor_ok: Optional[bool] = None,
    qdrant_ok: Optional[bool] = None,
    omlx_ok: Optional[bool] = None,
) -> None:
    """
    Atomically writes ~/.kortext/run/hud_state.json so status-line.sh and kortext_statusline.py
    can read sub-millisecond HUD state without spawning heavy Python/FalkorDB queries on every redraw.
    """
    try:
        home = get_kortext_home()
        run_dir = home / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        hud_file = run_dir / "hud_state.json"

        existing: Dict[str, Any] = {}
        if hud_file.is_file():
            try:
                existing = json.loads(hud_file.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:
                existing = {}

        if falkor_ok is not None:
            existing["falkor_ok"] = bool(falkor_ok)
        elif "falkor_ok" not in existing:
            existing["falkor_ok"] = True

        if qdrant_ok is not None:
            existing["qdrant_ok"] = bool(qdrant_ok)
        elif "qdrant_ok" not in existing:
            qdir = home / "qdrant"
            existing["qdrant_ok"] = bool(os.getenv("QDRANT_URL") or (qdir / "meta.json").is_file() or qdir.is_dir())

        if omlx_ok is not None:
            existing["omlx_ok"] = bool(omlx_ok)

        if ready_tasks is not None:
            existing["ready_tasks"] = int(ready_tasks)
        if in_progress_tasks is not None:
            existing["in_progress_tasks"] = int(in_progress_tasks)

        if project_id and (ready_tasks is not None or in_progress_tasks is not None):
            projects = existing.get("projects")
            if not isinstance(projects, dict):
                projects = {}
            proj_entry = projects.get(project_id, {})
            if not isinstance(proj_entry, dict):
                proj_entry = {}
            if ready_tasks is not None:
                proj_entry["ready_tasks"] = int(ready_tasks)
            if in_progress_tasks is not None:
                proj_entry["in_progress_tasks"] = int(in_progress_tasks)
            projects[project_id] = proj_entry
            existing["projects"] = projects

        existing["updated_at"] = time.time()

        tmp_file = hud_file.with_suffix(f".tmp.{os.getpid()}")
        tmp_file.write_text(json.dumps(existing), encoding="utf-8")
        tmp_file.replace(hud_file)
    except Exception as exc:
        logger.debug("HUD state snapshot write skipped: %s", exc)


try:
    from pydantic import BaseModel, Field, model_validator

    class TaskCompletionArtifact(BaseModel):
        task_id: str
        modified_files: List[str] = Field(default_factory=list)
        test_command: str
        tests_passed: bool
        base_commit: Optional[str] = None
        summary: str = ""

        @model_validator(mode="after")
        def _validate_completion_fields(self) -> "TaskCompletionArtifact":
            if not self.task_id or not str(self.task_id).strip():
                raise ValueError("task_id is required")
            if not isinstance(self.test_command, str) or not self.test_command.strip():
                raise ValueError("test_command is required for verified task completion")
            if not isinstance(self.tests_passed, bool) or not self.tests_passed:
                raise ValueError("tests_passed must be True to complete a task")
            self.task_id = self.task_id.strip()
            self.test_command = self.test_command.strip()
            return self
except Exception:
    class TaskCompletionArtifact:  # type: ignore
        def __init__(
            self,
            task_id: str,
            modified_files: Optional[List[str]] = None,
            test_command: str = "",
            tests_passed: bool = False,
            base_commit: Optional[str] = None,
            summary: str = "",
        ):
            if not task_id or not str(task_id).strip():
                raise ValueError("task_id is required")
            if not isinstance(test_command, str) or not test_command.strip():
                raise ValueError("test_command is required for verified task completion")
            if not isinstance(tests_passed, bool) or not tests_passed:
                raise ValueError("tests_passed must be True to complete a task")
            self.task_id = str(task_id).strip()
            self.modified_files = [str(f) for f in (modified_files or [])]
            self.test_command = test_command.strip()
            self.tests_passed = bool(tests_passed)
            self.base_commit = str(base_commit) if base_commit else None
            self.summary = str(summary or "")

        def model_dump(self) -> Dict[str, Any]:
            return {
                "task_id": self.task_id,
                "modified_files": self.modified_files,
                "test_command": self.test_command,
                "tests_passed": self.tests_passed,
                "base_commit": self.base_commit,
                "summary": self.summary,
            }


class TaskOrchestrationMixin:
    """Mixin providing 4-pattern routing, task briefs, compaction ledgers, file overlap detection, and review gates."""

    client: Any
    graph: str

    def _get_git_head_commit(self, cwd: Optional[Path] = None) -> Optional[str]:
        """Captures current git HEAD commit hash for diff-bundled review (FM6)."""
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(cwd or Path.cwd()),
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass
        return None

    def build_task_context_packet(
        self,
        target_symbols: Optional[List[str]] = None,
        target_files: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Pre-extracts compact AST interfaces for target_symbols so teammates start with zero-duplication context."""
        packet: List[Dict[str, Any]] = []
        if not target_symbols:
            return packet
        try:
            from kortext.symbols.seek import SymbolSeekEngine

            seeker = SymbolSeekEngine()
            for sym in target_symbols[:5]:
                raw = seeker.seek_symbol(sym, detail_level="skeleton")
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, dict) and parsed.get("matches"):
                    m = parsed["matches"][0]
                    packet.append({
                        "symbol": m.get("symbol") or sym,
                        "file": m.get("file") or m.get("file_path") or "",
                        "line": m.get("line", 1),
                        "signature": m.get("signature", ""),
                    })
        except Exception as exc:
            logger.debug("Context packet extraction skipped: %s", exc)
        return packet

    def recommend_execution_pattern(
        self,
        symbols: Optional[List[str]] = None,
        target_files: Optional[List[str]] = None,
        complexity: int = 1,
        project_id: Optional[str] = None,
        review_mode: bool = False,
    ) -> Dict[str, Any]:
        """
        Deterministic 4-Pattern Execution Router (Jose Parreño Garcia framework):
        Evaluates AST blast radius, module boundaries, and file overlaps to recommend:
          1. 'single_agent' (1x tokens): <= 3 files, single module, low blast radius
          2. 'subagents' (2-3x tokens): parallel read-heavy or isolated leaf-node edits
          3. 'parallel_specialist' (2-4x tokens): adversarial Red-Team / Architecture / QA review debate
          4. 'builder_validator' (2-4x tokens): high blast radius (>= 5 callers) or core architectural files
          5. 'agent_team' (4-15x tokens): >= 3 distinct modules requiring cross-layer interface negotiation
        """
        clean_files = [str(f).strip() for f in (target_files or []) if str(f).strip()]
        clean_syms = [str(s).strip() for s in (symbols or []) if str(s).strip()]

        impact_count = 0
        impacted_files: set[str] = set(clean_files)
        if clean_syms:
            try:
                from kortext.symbols.graph import SymbolGraphManager

                mgr = SymbolGraphManager()
                for sym in clean_syms[:4]:
                    imp = mgr.trace_symbol_impact(sym, direction="upstream", max_depth=2, token_budget=600)
                    direct = imp.get("direct_impacts", []) if isinstance(imp, dict) else []
                    impact_count += len(direct)
                    for item in direct:
                        if isinstance(item, dict) and item.get("source_file"):
                            impacted_files.add(str(item["source_file"]))
            except Exception as exc:
                logger.debug("Blast radius check in recommend_execution_pattern skipped: %s", exc)

        try:
            from kortext.symbols.clustering import get_relative_module

            modules = {get_relative_module(f) for f in impacted_files if f}
        except Exception:
            modules = {Path(f).parent.name for f in impacted_files if f}

        overlap_info = self.detect_file_overlaps(clean_files, project_id=project_id) if clean_files else {
            "overlapping_tasks": [],
            "recommended_workspace_mode": "shared",
        }

        num_files = len(impacted_files)
        num_modules = max(1, len(modules))

        if review_mode:
            pattern = "parallel_specialist"
            multiplier = "2-4x"
            team_size = 3
            rationale = (
                "Adversarial Red-Team / Specialist Review (Pattern 3): dispatch kortext-architect, "
                "kortext-security-auditor, and kortext-code-reviewer in parallel for independent falsification."
            )
        elif num_modules >= 3 and (complexity >= 7 or num_files >= 6):
            pattern = "agent_team"
            multiplier = "4-15x"
            team_size = min(4, num_modules)
            rationale = (
                f"Spans {num_modules} subsystems ({', '.join(sorted(modules)[:4])}) across {num_files} files; "
                "use orchestrated Agent Team (/teamwork-preview) with explicit file ownership."
            )
        elif impact_count >= 5 or complexity >= 7:
            pattern = "builder_validator"
            multiplier = "2-4x"
            team_size = 2
            rationale = (
                f"High blast radius ({impact_count} upstream callers across {num_files} files); "
                "pair 1 Builder agent with read-only kortext-reviewer Validator."
            )
        elif num_files > 3 or num_modules == 2:
            pattern = "subagents"
            multiplier = "2-3x"
            team_size = min(3, max(2, num_modules))
            rationale = (
                f"Touches {num_files} files across {num_modules} module(s) with bounded blast radius; "
                "use focused parallel subagents with isolated context packets."
            )
        else:
            pattern = "single_agent"
            multiplier = "1x"
            team_size = 1
            rationale = (
                f"Localized scope ({num_files} file(s), {impact_count} upstream callers); "
                "execute in a single agent session to avoid multi-agent coordination tax."
            )

        return {
            "recommended_pattern": pattern,
            "token_multiplier_estimate": multiplier,
            "recommended_team_size": team_size,
            "recommended_workspace_mode": overlap_info["recommended_workspace_mode"],
            "overlapping_tasks": overlap_info["overlapping_tasks"],
            "impact_count": impact_count,
            "impacted_file_count": num_files,
            "subsystems_touched": sorted(modules),
            "rationale": rationale,
        }

    def generate_task_brief(
        self,
        task_id: str,
        title: str,
        description: str,
        target_files: Optional[List[str]] = None,
        target_symbols: Optional[List[str]] = None,
        forbidden_files: Optional[List[str]] = None,
        base_commit: Optional[str] = None,
        workspace_dir: Optional[Path] = None,
    ) -> str:
        """
        Generates a self-contained Context-Rich Task Brief (.kortext/briefs/task_<id>_brief.md)
        containing pre-fetched AST skeletons, 1-hop callers, and forbidden files (FM1 & FM2).
        """
        ws = workspace_dir or Path(os.getenv("KORTEXT_WORKSPACE_ROOT") or Path.cwd())
        briefs_dir = ws / ".kortext" / "briefs"
        try:
            briefs_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            briefs_dir = get_kortext_home() / "briefs"
            briefs_dir.mkdir(parents=True, exist_ok=True)

        clean_files = [str(f).strip() for f in (target_files or []) if str(f).strip()]
        clean_syms = [str(s).strip() for s in (target_symbols or []) if str(s).strip()]
        forbid = [str(f).strip() for f in (forbidden_files or []) if str(f).strip()]
        packet = self.build_task_context_packet(target_symbols=clean_syms, target_files=clean_files)

        lines = [
            f"# Task Brief: `{task_id}` — {title}",
            f"- **Base Commit**: `{base_commit or 'HEAD'}`",
            f"- **Allowed Target Files**: {', '.join(f'`{f}`' for f in clean_files) if clean_files else 'Scoped by task description'}",
            f"- **Forbidden Files (Active Teammate Locks)**: {', '.join(f'`{f}`' for f in forbid) if forbid else 'None'}",
            "",
            "## Objective",
            description or title,
            "",
        ]
        if packet:
            lines.append("## Pre-Fetched AST Symbol Skeletons")
            for item in packet:
                lines.append(
                    f"- `{item.get('symbol')}` (`{item.get('file')}:{item.get('line')}`): `{item.get('signature')}`"
                )
            lines.append("")

        lines.extend([
            "## 5-Line Completion Report Contract (FM5)",
            f"Write full details to `~/.kortext/handoffs/task_{task_id}_report.md` and return ONLY:",
            "```text",
            "STATUS: PASS | FAIL | BLOCKED",
            "COMMITS: <short_sha> (<files_changed_count> files)",
            "TESTS: <passed>/<total> passing (<command>)",
            f"REPORT_PATH: ~/.kortext/handoffs/task_{task_id}_report.md",
            "CONCERNS: none | <1-sentence blocker>",
            "```",
        ])
        brief_path = briefs_dir / f"task_{task_id}_brief.md"
        try:
            brief_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:
            logger.debug("Task brief write skipped: %s", exc)
        return str(brief_path)

    def update_compaction_ledger(
        self,
        workspace_dir: Optional[Path] = None,
        project_id: Optional[str] = None,
        current_goal: Optional[str] = None,
        architectural_decisions: Optional[List[str]] = None,
    ) -> str:
        """
        Maintains the Compaction-Proof Progress Ledger (.kortext/ledger.md)
        so parent/orchestrator sessions recover full DAG state after context compaction.
        """
        ws = workspace_dir or Path(os.getenv("KORTEXT_WORKSPACE_ROOT") or Path.cwd())
        ledger_dir = ws / ".kortext"
        try:
            ledger_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            ledger_dir = get_kortext_home()
            ledger_dir.mkdir(parents=True, exist_ok=True)

        proj = project_id or ws.name or "kortext"
        all_tasks = self.get_all_tasks(project_id=proj)  # type: ignore[attr-defined]
        completed = [t for t in all_tasks if t.get("status") == "completed"]
        in_prog = [t for t in all_tasks if t.get("status") == "in_progress"]
        ready = [t for t in all_tasks if t.get("status") in ("ready", "pending")]

        goal_line = current_goal or f"Active DAG execution for project '{proj}' ({len(completed)}/{len(all_tasks)} completed)"
        lines = [
            f"# Kortext Compaction-Proof Progress Ledger ({proj})",
            f"- **Current Goal**: {goal_line}",
            f"- **Updated At**: {datetime.now(timezone.utc).isoformat()}",
            f"- **Progress**: {len(completed)} completed, {len(in_prog)} in_progress, {len(ready)} ready/pending",
            "",
            "## In-Progress Tasks",
        ]
        if in_prog:
            for t in in_prog[:10]:
                lines.append(f"- `{t.get('id')}`: {t.get('title')} (assigned: `{t.get('assigned_to')}`)")
        else:
            lines.append("- None")

        lines.append("\n## Recently Completed Tasks")
        if completed:
            for t in completed[:10]:
                lines.append(f"- [x] `{t.get('id')}`: {t.get('title')}")
        else:
            lines.append("- None")

        if architectural_decisions:
            lines.append("\n## Key Architectural Decisions")
            for dec in architectural_decisions[:10]:
                lines.append(f"- {dec}")

        ledger_path = ledger_dir / "ledger.md"
        try:
            ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:
            logger.debug("Compaction ledger write skipped: %s", exc)
        return str(ledger_path)

    @staticmethod
    def validate_reviewer_verdict(output_text: str) -> Dict[str, Any]:
        """
        Enforces Two-Stage Review Gate (FM6):
        Parses reviewer output for SPEC_VERDICT: PASS|FAIL and QUALITY_VERDICT: PASS|FAIL.
        """
        text = str(output_text or "")
        spec_match = re.search(r"SPEC_VERDICT\s*:\s*(PASS|FAIL)", text, re.IGNORECASE)
        qual_match = re.search(r"QUALITY_VERDICT\s*:\s*(PASS|FAIL)", text, re.IGNORECASE)
        spec_verdict = spec_match.group(1).upper() if spec_match else "MISSING"
        quality_verdict = qual_match.group(1).upper() if qual_match else "MISSING"
        passed = spec_verdict == "PASS" and quality_verdict == "PASS"
        return {
            "valid_schema": spec_verdict in ("PASS", "FAIL") and quality_verdict in ("PASS", "FAIL"),
            "spec_verdict": spec_verdict,
            "quality_verdict": quality_verdict,
            "passed": passed,
        }

    def build_review_diff_bundle(self, base_commit: str, target_ref: str = "HEAD", cwd: Optional[Path] = None) -> str:
        """Generates a git diff <base_commit>..<target_ref> bundle for isolated reviewer inspection (FM6)."""
        if not base_commit or not str(base_commit).strip():
            return ""
        try:
            res = subprocess.run(
                ["git", "diff", f"{base_commit.strip()}..{target_ref.strip()}"],
                cwd=str(cwd or Path.cwd()),
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            if res.returncode == 0:
                return res.stdout
        except Exception:
            pass
        return ""

    def detect_file_overlaps(
        self,
        target_files: List[str],
        project_id: Optional[str] = None,
        exclude_task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Detects overlapping target_files across active tasks and recommends git worktree isolation (branch) when collisions exist."""
        clean_targets = {os.path.normpath(str(f).strip()) for f in (target_files or []) if str(f).strip()}
        if not clean_targets:
            return {"overlapping_tasks": [], "recommended_workspace_mode": "shared"}

        filter_clause = ""
        params: Dict[str, Any] = {}
        if project_id and project_id.lower() != "all":
            filter_clause = "  AND coalesce(t.project_id, 'kortext') = $project_id\n"
            params["project_id"] = project_id

        cypher = f"""
        MATCH (t:Task)
        WHERE t.status = 'in_progress'
        {filter_clause}RETURN t.id, t.title, coalesce(t.target_files, [])
        """
        raw = self.client.query(self.graph, cypher, params if params else None)
        overlaps: List[Dict[str, Any]] = []
        coupled_overlaps: List[Dict[str, Any]] = []

        coupled_target_files: set[str] = set()
        if getattr(self.client, "_is_live_falkor_client", False) is True:
            try:
                sym_graph = os.getenv("KORTEXT_SYMBOLS_GRAPH", os.getenv("FALKORDB_SYMBOLS_GRAPH", "kortext_symbols"))
                target_list = sorted(clean_targets)
                coupling_q = (
                    "UNWIND $files AS fp "
                    "MATCH (f1:File)-[r:IMPORTS|REFERENCES]->(s:Symbol)-[:DEFINED_IN]->(f2:File) "
                    "WHERE f1.path ENDS WITH fp OR f2.path ENDS WITH fp "
                    "RETURN DISTINCT f1.path, f2.path LIMIT 50"
                )
                c_res = self.client.query(sym_graph, coupling_q, {"files": target_list}, read_only=True)
                if c_res and len(c_res) >= 2 and isinstance(c_res[1], list):
                    for crow in c_res[1]:
                        if crow and len(crow) >= 2 and any(
                            _same_path(str(crow[0]), t) or _same_path(str(crow[1]), t) for t in target_list
                        ):
                            coupled_target_files.add(str(crow[0]))
                            coupled_target_files.add(str(crow[1]))
            except Exception as exc:
                logger.debug("1-hop AST coupling check in detect_file_overlaps skipped: %s", exc)

        if raw and len(raw) >= 2 and raw[1]:
            for row in raw[1]:
                tid = str(row[0])
                if exclude_task_id and tid == exclude_task_id:
                    continue
                other_files = [os.path.normpath(x) for x in _as_list(row[2])]
                shared = sorted(clean_targets.intersection(set(other_files)))
                if shared:
                    overlaps.append({"task_id": tid, "title": str(row[1]), "overlapping_files": shared})
                elif coupled_target_files:
                    coupled_shared = sorted(
                        f for f in other_files
                        if any(_same_path(cf, f) for cf in coupled_target_files)
                    )
                    if coupled_shared:
                        coupled_overlaps.append({"task_id": tid, "title": str(row[1]), "coupled_files": coupled_shared})

        return {
            "overlapping_tasks": overlaps,
            "coupled_tasks": coupled_overlaps,
            "recommended_workspace_mode": "branch" if (overlaps or coupled_overlaps) else "shared",
        }

    def create_milestone_handoff(
        self,
        milestone_name: str,
        project_id: Optional[str] = None,
        target_agent: str = "orchestrator",
        open_hypotheses: Optional[List[str]] = None,
        unresolved_questions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Generates a Milestone Succession Baton for /teamwork-preview orchestrator rotation,
        packaging completed DAG tasks, modified files, and active project state into ~/.kortext/handoffs/.
        """
        from kortext.handoff import HandoffManager

        proj = project_id or "kortext"
        all_tasks = self.get_all_tasks(project_id=proj)  # type: ignore[attr-defined]
        completed = [t for t in all_tasks if t.get("status") == "completed"]
        pending_or_ready = [t for t in all_tasks if t.get("status") in ("pending", "ready", "in_progress")]

        mod_files: List[str] = []
        for t in completed:
            for tf in (t.get("target_files") or []):
                if tf and str(tf) not in mod_files:
                    mod_files.append(str(tf))

        safe_m = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in milestone_name.strip().lower()).strip("_") or "milestone"
        baton_id = f"milestone_{safe_m}"
        scope_summary = (
            f"Milestone '{milestone_name}' succession for project '{proj}': "
            f"{len(completed)} completed tasks, {len(pending_or_ready)} remaining tasks."
        )
        hm = HandoffManager()
        hm.create_baton(
            target_agent=target_agent,
            current_scope=scope_summary,
            open_hypotheses=open_hypotheses or [f"Remaining tasks: {', '.join(str(t.get('id')) for t in pending_or_ready[:5])}"],
            unresolved_questions=unresolved_questions or [],
            active_blast_radius=mod_files[:20],
            task_id=baton_id,
            tests_passed=True,
        )
        return {
            "status": "ok",
            "baton_id": baton_id,
            "handoff_ref": f"handoff:{baton_id}",
            "completed_task_count": len(completed),
            "remaining_task_count": len(pending_or_ready),
            "modified_files": mod_files[:20],
            "summary": scope_summary,
        }

    def create_tasks_batch(
        self,
        tasks: List[Dict[str, Any]],
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Creates multiple Task DAG nodes in a single batch call and checks for file-ownership collisions."""
        created_ids: List[str] = []
        overlaps_detected: List[Dict[str, Any]] = []
        for item in tasks:
            if not isinstance(item, dict):
                continue
            item_title = item.get("title") or item.get("name")
            if not item_title:
                continue
            t_files = item.get("target_files") or item.get("files_to_modify") or []
            tid = self.create_task(  # type: ignore[attr-defined]
                title=str(item_title),
                description=str(item.get("description") or ""),
                complexity=int(item.get("complexity", 1)),
                assigned_to=str(item.get("assigned_to") or "primary"),
                depends_on=item.get("depends_on"),
                dependencies=item.get("dependencies"),
                project_id=item.get("project_id") or project_id,
                task_id=item.get("task_id") or item.get("id"),
                target_files=t_files,
            )
            created_ids.append(tid)
            if t_files:
                ov = self.detect_file_overlaps(t_files, project_id=project_id, exclude_task_id=tid)
                if ov["overlapping_tasks"]:
                    overlaps_detected.append({"task_id": tid, "overlaps": ov["overlapping_tasks"]})

        return {
            "status": "created_batch",
            "created_task_ids": created_ids,
            "count": len(created_ids),
            "file_overlaps": overlaps_detected,
            "recommended_workspace_mode": "branch" if overlaps_detected else "shared",
        }

    def link_task_to_outcome(self, task_id: str, episode_name: str) -> bool:
        """Links a completed Task node to an Episodic outcome node."""
        cypher = """
        MATCH (t:Task {id: $task_id}), (e:Episodic {name: $episode_name})
        MERGE (t)-[:PRODUCED_OUTCOME]->(e)
        RETURN count(e)
        """
        raw = self.client.query(
            self.graph,
            cypher,
            {"task_id": task_id, "episode_name": episode_name},
        )
        if not raw or len(raw) < 2 or not raw[1]:
            return False
        try:
            return int(raw[1][0][0]) > 0
        except (ValueError, TypeError, IndexError):
            return False

    def get_graph_data(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """Formats all tasks into nodes and edges for Vis.js rendering."""
        from kortext.tasks.graph_view import format_visjs_graph_data

        tasks = self.get_all_tasks(project_id=project_id)  # type: ignore[attr-defined]
        return format_visjs_graph_data(tasks)

    def claim_exclusive_files(
        self, task_id: str, target_files: List[str], project_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Adds files to an in_progress task's ownership set unless another in_progress task owns them.
        While a worker runs with KORTEXT_TASK_ID set, the PreToolUse hook denies its edits to files
        another task owns.
        """
        rows = self.client.query(
            self.graph,
            "MATCH (t:Task {id: $id}) RETURN t.status, coalesce(t.target_files, [])",
            {"id": task_id},
        )
        if not rows or len(rows) < 2 or not rows[1]:
            return {"status": "error", "error": f"Task '{task_id}' not found"}
        if rows[1][0][0] != "in_progress":
            return {"status": "error", "error": f"Task '{task_id}' is {rows[1][0][0]}; claim it first"}
        conflicts = self.detect_file_overlaps(target_files, project_id=project_id, exclude_task_id=task_id)["overlapping_tasks"]
        if conflicts:
            return {"status": "conflict", "conflicts": conflicts}
        owned = sorted({os.path.normpath(f) for f in _as_list(rows[1][0][1]) + _as_list(list(target_files))})
        self.client.query(self.graph, "MATCH (t:Task {id: $id}) SET t.target_files = $files", {"id": task_id, "files": owned})
        return {"status": "claimed", "task_id": task_id, "files": owned}

    def execute_action(
        self,
        action: str = "get_ready",
        title: Optional[str] = None,
        description: Optional[str] = None,
        complexity: int = 1,
        assigned_to: str = "primary",
        depends_on: Optional[List[str]] = None,
        dependencies: Optional[List[str]] = None,
        task_id: Optional[str] = None,
        status: Optional[str] = None,
        error: Optional[str] = None,
        project_id: Optional[str] = None,
        episode_name: Optional[str] = None,
        tasks: Optional[List[Dict[str, Any]]] = None,
        target_files: Optional[List[str]] = None,
        modified_files: Optional[List[str]] = None,
        test_command: Optional[str] = None,
        tests_passed: Optional[bool] = None,
        base_commit: Optional[str] = None,
        milestone_name: Optional[str] = None,
        reviewer_output: Optional[str] = None,
        token_budget_spent: Optional[int] = None,
    ) -> str:
        """Executes a manage_tasks action and returns a serialized JSON payload."""
        act = action.lower().strip()
        effective_deps = depends_on if depends_on is not None else dependencies
        if act == "create":
            if tasks and isinstance(tasks, list):
                return json.dumps(self.create_tasks_batch(tasks, project_id=project_id), separators=(",", ":"))
            if not title:
                return json.dumps({"error": "Missing 'title' (or 'tasks' batch list) for task creation"}, separators=(",", ":"))
            create_kwargs: Dict[str, Any] = {
                "title": title,
                "description": description or "",
                "complexity": complexity,
                "assigned_to": assigned_to,
                "depends_on": effective_deps,
                "project_id": project_id,
                "task_id": task_id,
                "dependencies": dependencies,
            }
            if target_files is not None:
                create_kwargs["target_files"] = target_files
            tid = self.create_task(**create_kwargs)  # type: ignore[attr-defined]
            overlap_info = self.detect_file_overlaps(target_files or [], project_id=project_id, exclude_task_id=tid)
            pattern_info = self.recommend_execution_pattern(
                target_files=target_files or [],
                complexity=complexity,
                project_id=project_id,
            )
            return json.dumps(
                {
                    "status": "created",
                    "task_id": tid,
                    "recommended_pattern": pattern_info["recommended_pattern"],
                    "recommended_workspace_mode": overlap_info["recommended_workspace_mode"],
                    "overlapping_tasks": overlap_info["overlapping_tasks"],
                },
                separators=(",", ":"),
            )
        if act in ("get_ready", "ready"):
            return json.dumps({"ready_tasks": self.get_ready_tasks(project_id=project_id)}, separators=(",", ":"))  # type: ignore[attr-defined]
        if act in ("get_in_progress", "in_progress"):
            return json.dumps({"in_progress_tasks": self.get_in_progress_tasks(project_id=project_id)}, separators=(",", ":"))  # type: ignore[attr-defined]
        if act == "claim":
            return json.dumps(
                {"claimed_task": self.claim_next_ready_task(worker_id=assigned_to, project_id=project_id)},  # type: ignore[attr-defined]
                separators=(",", ":"),
            )
        if act == "check_overlaps":
            return json.dumps(
                self.detect_file_overlaps(target_files or [], project_id=project_id, exclude_task_id=task_id),
                separators=(",", ":"),
            )
        if act == "claim_files":
            if not task_id or not target_files:
                return json.dumps({"error": "claim_files needs 'task_id' and 'target_files'"}, separators=(",", ":"))
            return json.dumps(self.claim_exclusive_files(task_id, target_files, project_id=project_id), separators=(",", ":"))
        if act == "heartbeat":
            if not task_id:
                return json.dumps({"error": "Missing 'task_id' for heartbeat"}, separators=(",", ":"))
            renewed = self.heartbeat_task(task_id, tokens_spent_delta=int(token_budget_spent or 0))  # type: ignore[attr-defined]
            return json.dumps({"task_id": task_id, "renewed": renewed}, separators=(",", ":"))
        if act == "reclaim_expired":
            reclaimed = self.reclaim_expired_leases(project_id=project_id)  # type: ignore[attr-defined]
            return json.dumps({"reclaimed_tasks": reclaimed, "count": len(reclaimed)}, separators=(",", ":"))
        if act == "complete_task":
            if not task_id:
                return json.dumps({"error": "Missing 'task_id' for complete_task"}, separators=(",", ":"))
            try:
                res = self.complete_task(  # type: ignore[attr-defined]
                    task_id=task_id,
                    modified_files=modified_files or [],
                    test_command=test_command or "",
                    tests_passed=bool(tests_passed),
                    base_commit=base_commit,
                    summary=description or "",
                    episode_name=episode_name,
                    reviewer_output=reviewer_output,
                    token_budget_spent=token_budget_spent,
                )
                return json.dumps(res, separators=(",", ":"))
            except Exception as exc:
                return json.dumps({"error": str(exc)}, separators=(",", ":"))
        if act == "validate_review":
            verdict = self.validate_reviewer_verdict(reviewer_output or description or "")
            return json.dumps(verdict, separators=(",", ":"))
        if act in ("recommend_pattern", "route"):
            return json.dumps(
                self.recommend_execution_pattern(
                    symbols=depends_on,
                    target_files=target_files,
                    complexity=complexity,
                    project_id=project_id,
                ),
                separators=(",", ":"),
            )
        if act in ("milestone_handoff", "create_milestone_handoff"):
            m_name = milestone_name or title or task_id or description or "milestone"
            return json.dumps(
                self.create_milestone_handoff(
                    milestone_name=str(m_name),
                    project_id=project_id,
                    target_agent=assigned_to or "orchestrator",
                ),
                separators=(",", ":"),
            )
        if act in ("list_all", "list"):
            return json.dumps({"tasks": self.get_all_tasks(project_id=project_id)}, separators=(",", ":"))  # type: ignore[attr-defined]
        if act == "update_status":
            if not task_id or not status:
                return json.dumps({"error": "Missing 'task_id' or 'status'"}, separators=(",", ":"))
            ok = self.update_task_status(task_id, status=status, error=error)  # type: ignore[attr-defined]
            if episode_name and status == "completed":
                self.link_task_to_outcome(task_id, episode_name)
            return json.dumps({"task_id": task_id, "updated": ok, "status": status}, separators=(",", ":"))
        return json.dumps({"error": f"Unknown action: {action}"}, separators=(",", ":"))
