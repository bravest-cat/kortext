"""
Kortext Autonomous Task DAG Tracker.
Graph-backed task state machine with strict dependency gating, atomic task claiming,
lease expiration/heartbeat recovery, artifact-first handoffs, and failure loops.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
from pathlib import Path
import time
from typing import Any, Dict, List, Optional
import uuid

from kortext._home import get_kortext_home
from kortext.falkordb_client import get_shared_falkordb_client
from kortext.tasks.orchestration_mixin import (
    TaskCompletionArtifact,
    TaskOrchestrationMixin,
    write_hud_state_snapshot,
)

logger = logging.getLogger("kortext.tasks")

DEFAULT_LEASE_SECONDS = 900  # 15 minutes


class TaskDAGTracker(TaskOrchestrationMixin):
    """Manages Task DAG lifecycle in FalkorDB."""

    def __init__(self, graph_name: str = "kortext"):
        self.client = get_shared_falkordb_client()
        if hasattr(self.client, "resolve_graph_name"):
            self.graph = self.client.resolve_graph_name(graph_name)
        else:
            self.graph = graph_name
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        """Ensures FalkorDB schema indexes on :Task and :Project nodes."""
        try:
            if getattr(self.client, "_is_live_falkor_client", False) is True:
                self.client.ensure_indexes(
                    self.graph,
                    [("Task", "project_id"), ("Task", "id"), ("Task", "status"), ("Project", "id")],
                )
            else:
                self.client.query(self.graph, "CREATE INDEX FOR (n:Task) ON (n.project_id)")
        except Exception as e:
            logger.debug(f"Index creation on :Task(project_id) error: {e}")

    def _refresh_hud_snapshot(self, project_id: Optional[str] = None) -> None:
        """Refreshes ~/.kortext/run/hud_state.json with latest ready and in_progress task counts."""
        try:
            if getattr(self.client, "_is_live_falkor_client", False) is not True:
                return
            proj = project_id or "kortext"
            ready_cnt = len(self.get_ready_tasks(project_id=proj))
            in_prog_cnt = len(self.get_in_progress_tasks(project_id=proj))
            write_hud_state_snapshot(
                ready_tasks=ready_cnt,
                in_progress_tasks=in_prog_cnt,
                project_id=proj,
                falkor_ok=True,
            )
        except Exception as exc:
            logger.debug("HUD snapshot refresh skipped: %s", exc)

    def _has_dependency_cycle(
        self,
        task_id: str,
        dep_list: List[str],
        project_id: Optional[str] = None,
        check_graph: bool = True,
    ) -> bool:
        """Detects direct or transitive dependency cycles (Failure Mode #5: Dependency Deadlock)."""
        if not dep_list:
            return False
        if task_id in dep_list:
            return True
        if not check_graph:
            return False
        try:
            all_tasks = self.get_all_tasks(project_id=project_id)
            dep_map: Dict[str, List[str]] = {
                str(t.get("id")): [str(d) for d in (t.get("depends_on") or []) if d]
                for t in all_tasks
                if t.get("id")
            }
            visited: set[str] = set()
            stack = list(dep_list)
            while stack:
                curr = stack.pop()
                if curr == task_id:
                    return True
                if curr in visited:
                    continue
                visited.add(curr)
                stack.extend(dep_map.get(curr, []))
        except Exception as exc:
            # Fail closed: an unreadable graph must not let a cycle in.
            logger.warning("Dependency cycle check failed, rejecting task '%s': %s", task_id, exc)
            return True
        return False

    def create_task(
        self,
        title: str,
        description: str,
        complexity: int = 1,
        assigned_to: str = "primary",
        depends_on: Optional[List[str]] = None,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        dependencies: Optional[List[str]] = None,
        target_files: Optional[List[str]] = None,
        target_symbols: Optional[List[str]] = None,
    ) -> str:
        """Creates a new Task node, validates DAG acyclicity, links it to Project, and sets up dependency edges."""
        has_explicit_id = bool(isinstance(task_id, str) and task_id.strip())
        resolved_id = task_id.strip() if has_explicit_id else f"task_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()
        raw_deps = depends_on if depends_on is not None else dependencies
        dep_list = [d for d in (raw_deps or []) if d]
        t_files = [str(f).strip() for f in (target_files or []) if str(f).strip()]
        proj_id = project_id or "kortext"

        existing = self.client.query(
            self.graph,
            "MATCH (t:Task {id: $id}) WHERE coalesce(t.project_id, 'kortext') <> $project_id RETURN t.project_id LIMIT 1",
            {"id": resolved_id, "project_id": proj_id},
        )
        if existing and len(existing) > 1 and existing[1]:
            raise ValueError(f"Task id '{resolved_id}' already belongs to project '{existing[1][0][0]}'")

        if dep_list and self._has_dependency_cycle(
            resolved_id, dep_list, project_id=proj_id, check_graph=has_explicit_id
        ):
            raise ValueError(f"Circular task dependency detected for task '{resolved_id}' -> {dep_list}")

        cypher = """
        MERGE (p:Project {id: $project_id})
        MERGE (t:Task {id: $id})
        SET t.title = $title,
            t.description = $desc,
            t.status = $status,
            t.complexity = $comp,
            t.assigned_to = $assign,
            t.retry_count = coalesce(t.retry_count, 0),
            t.created_at = coalesce(t.created_at, $now),
            t.completed_at = null,
            t.claimed_at = null,
            t.lease_expires_at = null,
            t.depends_on = $dep_list,
            t.target_files = $target_files,
            t.project_id = $project_id
        MERGE (p)-[:HAS_TASK]->(t)
        RETURN t.id
        """
        status = "ready" if not dep_list else "pending"
        params = {
            "id": resolved_id,
            "title": title,
            "desc": description,
            "status": status,
            "comp": complexity,
            "assign": assigned_to,
            "now": now,
            "dep_list": dep_list,
            "target_files": t_files,
            "project_id": proj_id,
        }
        self.client.query(self.graph, cypher, params)

        if dep_list:
            for dep_id in dep_list:
                dep_cypher = """
                MATCH (t:Task {id: $id}), (dep:Task {id: $dep_id})
                WHERE coalesce(dep.project_id, 'kortext') = $project_id
                MERGE (t)-[:DEPENDS_ON {strict: true}]->(dep)
                """
                self.client.query(self.graph, dep_cypher, {"id": resolved_id, "dep_id": dep_id, "project_id": proj_id})

        link_cypher = """
        MATCH (waiting:Task), (new_t:Task {id: $id})
        WHERE $id IN coalesce(waiting.depends_on, [])
          AND coalesce(waiting.project_id, 'kortext') = $project_id
        MERGE (waiting)-[:DEPENDS_ON {strict: true}]->(new_t)
        """
        self.client.query(self.graph, link_cypher, {"id": resolved_id, "project_id": proj_id})
        self._refresh_hud_snapshot(proj_id)

        return resolved_id

    def reclaim_expired_leases(self, project_id: Optional[str] = None) -> List[str]:
        """
        Releases stalled 'in_progress' tasks whose lease_expires_at timestamp has passed
        back to 'ready' status so other workers can claim them (Failure Mode #3 recovery).
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        filter_clause = ""
        params: Dict[str, Any] = {"now": now_iso}
        if project_id and project_id.lower() != "all":
            filter_clause = "  AND coalesce(t.project_id, 'kortext') = $project_id\n"
            params["project_id"] = project_id

        cypher = f"""
        MATCH (t:Task)
        WHERE t.status = 'in_progress'
          AND t.lease_expires_at IS NOT NULL
          AND t.lease_expires_at < $now
        {filter_clause}SET t.status = 'ready',
            t.assigned_to = 'primary',
            t.claimed_at = null,
            t.lease_expires_at = null
        RETURN t.id
        """
        raw = self.client.query(self.graph, cypher, params)
        if not raw or len(raw) < 2 or not raw[1]:
            return []
        return [str(row[0]) for row in raw[1] if row and row[0] is not None]

    def heartbeat_task(
        self,
        task_id: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        tokens_spent_delta: int = 0,
    ) -> bool:
        """Renews the lease expiration timestamp for an active in_progress task and optionally increments token_budget_spent (FM7)."""
        now_dt = datetime.now(timezone.utc)
        lease_exp = (now_dt + timedelta(seconds=max(60, int(lease_seconds)))).isoformat()
        if tokens_spent_delta > 0:
            cypher = """
            MATCH (t:Task {id: $id})
            WHERE t.status = 'in_progress'
            SET t.lease_expires_at = $lease_expires_at,
                t.heartbeat_at = $now,
                t.token_budget_spent = coalesce(t.token_budget_spent, 0) + $tok_delta
            RETURN t.id
            """
            params: Dict[str, Any] = {
                "id": task_id,
                "lease_expires_at": lease_exp,
                "now": now_dt.isoformat(),
                "tok_delta": int(tokens_spent_delta),
            }
        else:
            cypher = """
            MATCH (t:Task {id: $id})
            WHERE t.status = 'in_progress'
            SET t.lease_expires_at = $lease_expires_at,
                t.heartbeat_at = $now
            RETURN t.id
            """
            params = {"id": task_id, "lease_expires_at": lease_exp, "now": now_dt.isoformat()}
        raw = self.client.query(self.graph, cypher, params)
        return bool(raw and len(raw) >= 2 and raw[1])

    def get_ready_tasks(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Finds all tasks whose upstream dependencies are completed (reclaiming expired leases first on live FalkorDB)."""
        try:
            if getattr(self.client, "_is_live_falkor_client", False) is True:
                self.reclaim_expired_leases(project_id=project_id)
        except Exception as exc:
            logger.debug("Automatic lease reclamation in get_ready_tasks skipped: %s", exc)

        filter_clause = ""
        params: Dict[str, Any] = {}
        if project_id and project_id.lower() != "all":
            filter_clause = "  AND coalesce(t.project_id, 'kortext') = $project_id\n"
            params["project_id"] = project_id

        cypher = f"""
        MATCH (t:Task)
        WHERE t.status IN ['pending', 'ready']
          AND coalesce(size(t.depends_on), 0) = 0
          AND NOT (t)-[:DEPENDS_ON]->(:Task)
        {filter_clause}SET t.status = 'ready'
        RETURN t.id, t.title, t.complexity, t.assigned_to, t.description
        UNION
        MATCH (t:Task)
        WHERE t.status IN ['pending', 'ready']
          AND (t)-[:DEPENDS_ON]->(:Task)
        {filter_clause}WITH t, [(t)-[:DEPENDS_ON]->(dep) | dep.status] AS dep_statuses
        WHERE (t.depends_on IS NULL OR size(dep_statuses) = size(t.depends_on))
          AND ALL(d IN dep_statuses WHERE d = 'completed')
        SET t.status = 'ready'
        RETURN t.id, t.title, t.complexity, t.assigned_to, t.description
        """
        raw = self.client.query(self.graph, cypher, params if params else None)
        if not raw or len(raw) < 2 or not raw[1]:
            return []

        return [
            {
                "id": row[0],
                "title": row[1],
                "complexity": row[2],
                "assigned_to": row[3],
                "description": row[4],
            }
            for row in raw[1]
        ]

    def claim_next_ready_task(
        self,
        worker_id: str = "kortext-task-runner",
        project_id: Optional[str] = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        workspace_mode: str = "shared",
        enforce_overlap_block: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Atomically transitions one unblocked ready task to 'in_progress' with a lease timeout
        and assigns it to worker_id in a single Cypher statement, checking file-ownership overlaps,
        recording base_commit, and generating a Context-Rich Task Brief (.kortext/briefs/task_<id>_brief.md).
        """
        try:
            if getattr(self.client, "_is_live_falkor_client", False) is True:
                self.reclaim_expired_leases(project_id=project_id)
        except Exception as exc:
            logger.debug("Lease reclamation skipped: %s", exc)

        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        lease_exp = (now_dt + timedelta(seconds=max(60, int(lease_seconds)))).isoformat()
        base_commit = self._get_git_head_commit()

        filter_clause = ""
        params: Dict[str, Any] = {
            "worker_id": worker_id,
            "now": now_iso,
            "lease_expires_at": lease_exp,
            "base_commit": base_commit or "",
        }
        if project_id and project_id.lower() != "all":
            filter_clause = "  AND coalesce(t.project_id, 'kortext') = $project_id\n"
            params["project_id"] = project_id

        cypher = f"""
        MATCH (t:Task)
        WHERE t.status IN ['pending', 'ready']
        {filter_clause}WITH t, [(t)-[:DEPENDS_ON]->(dep:Task) | dep.status] AS dep_statuses
        WHERE (coalesce(size(t.depends_on), 0) = 0 AND size(dep_statuses) = 0)
           OR (size(dep_statuses) = coalesce(size(t.depends_on), size(dep_statuses)) AND ALL(d IN dep_statuses WHERE d = 'completed'))
        WITH t ORDER BY t.complexity DESC, t.created_at ASC LIMIT 1
        SET t.status = 'in_progress',
            t.assigned_to = $worker_id,
            t.claimed_at = $now,
            t.lease_expires_at = $lease_expires_at,
            t.base_commit = $base_commit,
            t.updated_at = $now
        RETURN t.id, t.title, t.complexity, t.assigned_to, t.description, coalesce(t.target_files, [])
        """
        raw = self.client.query(self.graph, cypher, params)
        if not raw or len(raw) < 2 or not raw[1]:
            return None
        row = raw[1][0]
        claimed_id = str(row[0])
        raw_files = row[5] if len(row) > 5 and row[5] else []
        if isinstance(raw_files, list):
            target_files = [str(f).strip() for f in raw_files if str(f).strip()]
        elif isinstance(raw_files, str) and raw_files.strip():
            try:
                parsed = json.loads(raw_files)
                target_files = [str(f).strip() for f in parsed] if isinstance(parsed, list) else []
            except Exception:
                cleaned = raw_files.strip().strip("[]").replace("'", "").replace('"', "")
                target_files = [x.strip() for x in cleaned.split(",") if x.strip()]
        else:
            target_files = []

        overlap_info = (
            self.detect_file_overlaps(target_files, project_id=project_id, exclude_task_id=claimed_id)
            if target_files
            else {"overlapping_tasks": [], "coupled_tasks": [], "recommended_workspace_mode": "shared"}
        )

        has_collision = bool(overlap_info.get("overlapping_tasks") or overlap_info.get("coupled_tasks"))
        if has_collision and (enforce_overlap_block or os.getenv("KORTEXT_ENFORCE_WORKTREE_ISOLATION") == "1"):
            if workspace_mode not in ("worktree", "branch"):
                self.client.query(
                    self.graph,
                    "MATCH (t:Task {id: $id}) SET t.status = 'ready', t.assigned_to = null, "
                    "t.claimed_at = null, t.lease_expires_at = null",
                    {"id": claimed_id},
                )
                raise ValueError(
                    f"Task claim blocked (FM2 File Overwrite & Merge Hell Guard): task '{claimed_id}' overlaps "
                    f"with active tasks {overlap_info.get('overlapping_tasks') or overlap_info.get('coupled_tasks')}. "
                    "Re-claim with workspace_mode='worktree' or serialize via depends_on."
                )

        forbidden_files: List[str] = []
        for ov in overlap_info.get("overlapping_tasks", []):
            for f in ov.get("overlapping_files", []):
                if f not in forbidden_files:
                    forbidden_files.append(f)
        for cov in overlap_info.get("coupled_tasks", []):
            for f in cov.get("coupled_files", []):
                if f not in forbidden_files:
                    forbidden_files.append(f)

        brief_path = self.generate_task_brief(
            task_id=claimed_id,
            title=str(row[1] or ""),
            description=str(row[4] or ""),
            target_files=target_files,
            forbidden_files=forbidden_files,
            base_commit=base_commit,
        )
        self.update_compaction_ledger(project_id=project_id)
        self._refresh_hud_snapshot(project_id)
        return {
            "id": row[0],
            "title": row[1],
            "complexity": row[2],
            "assigned_to": row[3],
            "description": row[4],
            "status": "in_progress",
            "claimed_at": now_iso,
            "lease_expires_at": lease_exp,
            "base_commit": base_commit,
            "brief_path": brief_path,
            "target_files": target_files,
            "forbidden_files": forbidden_files,
            "recommended_workspace_mode": overlap_info["recommended_workspace_mode"],
            "overlapping_tasks": overlap_info["overlapping_tasks"],
            "coupled_tasks": overlap_info.get("coupled_tasks", []),
        }

    def get_in_progress_tasks(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Finds all tasks currently in 'in_progress' status for the active project."""
        filter_clause = ""
        params: Dict[str, Any] = {}
        if project_id and project_id.lower() != "all":
            filter_clause = "  AND coalesce(t.project_id, 'kortext') = $project_id\n"
            params["project_id"] = project_id

        cypher = f"""
        MATCH (t:Task)
        WHERE t.status = 'in_progress'
        {filter_clause}RETURN t.id, t.title, t.complexity, t.assigned_to, t.description
        """
        raw = self.client.query(self.graph, cypher, params if params else None)
        if not raw or len(raw) < 2 or not raw[1]:
            return []
        return [
            {
                "id": row[0],
                "title": row[1],
                "complexity": row[2],
                "assigned_to": row[3],
                "description": row[4],
                "status": "in_progress",
            }
            for row in raw[1]
        ]

    def complete_task(
        self,
        task_id: str,
        modified_files: Optional[List[str]] = None,
        test_command: str = "",
        tests_passed: bool = False,
        base_commit: Optional[str] = None,
        summary: str = "",
        episode_name: Optional[str] = None,
        handoffs_dir: Optional[Path] = None,
        reviewer_output: Optional[str] = None,
        require_review_for_complexity: Optional[int] = None,
        token_budget_spent: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Validates worker completion evidence via TaskCompletionArtifact, optionally enforces the
        Two-Stage Review Gate (FM6), marks task completed, and writes a durable artifact-first handoff
        (~/.kortext/handoffs/<task_id>.json and task_<id>_report.md) returning a bounded 5-line report
        + <150-token summary to prevent parent context bloat (FM4, FM5, FM6, FM7).
        """
        if not tests_passed:
            raise ValueError("Task completion rejected: tests_passed must be True.")
        if not test_command or not str(test_command).strip():
            raise ValueError("Task completion rejected: test_command must be provided.")

        review_verdict_info: Optional[Dict[str, Any]] = None
        enforce_review = os.getenv("KORTEXT_ENFORCE_REVIEW_VERDICT", "0") == "1"
        comp_threshold = require_review_for_complexity
        if comp_threshold is None and os.getenv("KORTEXT_REVIEW_COMPLEXITY_THRESHOLD"):
            try:
                comp_threshold = int(os.getenv("KORTEXT_REVIEW_COMPLEXITY_THRESHOLD", "0"))
            except ValueError:
                comp_threshold = None

        if comp_threshold is not None and comp_threshold > 0 and not enforce_review:
            try:
                matching = [t for t in self.get_all_tasks() if str(t.get("id")) == str(task_id)]
                if matching and int(matching[0].get("complexity", 1)) >= comp_threshold:
                    enforce_review = True
            except Exception:
                pass

        if reviewer_output is not None or enforce_review:
            review_verdict_info = self.validate_reviewer_verdict(reviewer_output or "")
            if not review_verdict_info.get("passed", False):
                rejections = 1
                try:
                    r_res = self.client.query(
                        self.graph,
                        "MATCH (t:Task {id: $id}) "
                        "SET t.review_rejections = coalesce(t.review_rejections, 0) + 1 "
                        "RETURN t.review_rejections",
                        {"id": task_id},
                    )
                    if r_res and len(r_res) > 1 and r_res[1] and r_res[1][0]:
                        rejections = int(r_res[1][0][0] or 1)
                except Exception:
                    pass
                cb_note = (
                    f" [CIRCUIT BREAKER TRIPPED: {rejections} consecutive review rejections — escalate to architect]"
                    if rejections >= 3
                    else ""
                )
                raise ValueError(
                    f"Task completion rejected (FM6 Two-Stage Review Gate): "
                    f"SPEC_VERDICT={review_verdict_info.get('spec_verdict')}, "
                    f"QUALITY_VERDICT={review_verdict_info.get('quality_verdict')}. Both must be PASS.{cb_note}"
                )

        from kortext.hooks.pipelines.post_tool import consume_test_execution_proof, has_recent_passing_test_run

        test_proof_verified = has_recent_passing_test_run(command=test_command)
        default_verify = "0" if os.getenv("PYTEST_CURRENT_TEST") else "1"
        if os.getenv("KORTEXT_VERIFY_TEST_PROOF", default_verify) == "1" and not test_proof_verified:
            raise ValueError(
                "Task completion rejected (FM4 Premature Victory Guard): no unused passing run of "
                f"`{test_command}` recorded by PostToolUse in ~/.kortext/run/last_test_run.json."
            )

        resolved_base_commit = base_commit or self._get_git_head_commit()
        artifact = TaskCompletionArtifact(
            task_id=task_id,
            modified_files=modified_files or [],
            test_command=test_command,
            tests_passed=tests_passed,
            base_commit=resolved_base_commit,
            summary=summary,
        )
        self.update_task_status(task_id, status="completed")
        if test_proof_verified:
            consume_test_execution_proof(task_id)
        if token_budget_spent is not None and token_budget_spent > 0:
            try:
                self.client.query(
                    self.graph,
                    "MATCH (t:Task {id: $id}) SET t.token_budget_spent = $tok",
                    {"id": task_id, "tok": int(token_budget_spent)},
                )
            except Exception as exc:
                logger.warning("Suppressed in tracker:%d: %s", 542, exc)
        if episode_name:
            self.link_task_to_outcome(task_id, episode_name)

        completed_at = datetime.now(timezone.utc).isoformat()
        payload = artifact.model_dump() if hasattr(artifact, "model_dump") else dict(artifact.__dict__)
        payload["completed_at"] = completed_at
        payload["handoff_ref"] = f"handoff:{task_id}"
        payload["test_proof_verified"] = test_proof_verified
        if review_verdict_info is not None:
            payload["review_verdict"] = review_verdict_info
        if token_budget_spent is not None:
            payload["token_budget_spent"] = int(token_budget_spent)

        kortext_handoffs_dir = get_kortext_home() / "handoffs"
        kortext_handoffs_dir.mkdir(parents=True, exist_ok=True)
        safe_task_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(task_id))
        canonical_handoff_path = kortext_handoffs_dir / f"{safe_task_id}.json"
        report_md_path = kortext_handoffs_dir / f"task_{safe_task_id}_report.md"

        target_dir = handoffs_dir or kortext_handoffs_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = target_dir / f"task_handoff_{safe_task_id}.json"
        artifact_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        try:
            canonical_handoff_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("Secondary handoff write skipped: %s", exc)

        short_sha = (resolved_base_commit or "HEAD")[:8]
        mod_list = modified_files or []
        five_line_summary = "\n".join([
            "STATUS: PASS",
            f"COMMITS: {short_sha} ({len(mod_list)} files)",
            f"TESTS: PASS ({test_command.strip()})",
            f"REPORT_PATH: {report_md_path}",
            "CONCERNS: none",
        ])
        report_body = "\n".join([
            f"# Task Report: `{task_id}`",
            "",
            "```text",
            five_line_summary,
            "```",
            "",
            "## Summary",
            (summary or "Completed and verified.").strip(),
            "",
            "## Modified Files",
            *(f"- `{f}`" for f in mod_list),
        ])
        try:
            report_md_path.write_text(report_body + "\n", encoding="utf-8")
        except Exception as exc:
            logger.debug("5-line report markdown write skipped: %s", exc)

        self.update_compaction_ledger()

        compact_summary = (summary or "").strip()
        if len(compact_summary) > 480:
            compact_summary = compact_summary[:477] + "..."

        result_dict: Dict[str, Any] = {
            "status": "completed",
            "task_id": task_id,
            "handoff_ref": f"handoff:{task_id}",
            "artifact_path": str(artifact_path if handoffs_dir is not None else canonical_handoff_path),
            "journal_artifact_path": str(artifact_path),
            "report_path": str(report_md_path),
            "report_5line": five_line_summary,
            "summary": compact_summary,
        }
        if review_verdict_info is not None:
            result_dict["review_verdict"] = review_verdict_info
        if token_budget_spent is not None:
            result_dict["token_budget_spent"] = int(token_budget_spent)
        return result_dict

    def update_task_status(self, task_id: str, status: str, error: Optional[str] = None) -> bool:
        """Updates task state, promotes newly unblocked dependent tasks, handles failure retries, and refreshes HUD."""
        now = datetime.now(timezone.utc).isoformat()
        proj_id: Optional[str] = None
        if status == "completed":
            cypher = """
            MATCH (t:Task {id: $id})
            SET t.status = 'completed',
                t.completed_at = $now,
                t.lease_expires_at = null
            RETURN t.id, coalesce(t.project_id, 'kortext')
            """
            raw = self.client.query(self.graph, cypher, {"id": task_id, "now": now})
            if raw and len(raw) >= 2 and raw[1] and len(raw[1][0]) >= 2:
                proj_id = str(raw[1][0][1])
            try:
                promote_cypher = """
                MATCH (w:Task)-[:DEPENDS_ON]->(t:Task {id: $id})
                WHERE w.status = 'pending'
                WITH w, [(w)-[:DEPENDS_ON]->(dep:Task) | dep.status] AS dep_statuses
                WHERE (w.depends_on IS NULL OR size(dep_statuses) = size(w.depends_on))
                  AND ALL(d IN dep_statuses WHERE d = 'completed')
                SET w.status = 'ready'
                RETURN w.id
                """
                self.client.query(self.graph, promote_cypher, {"id": task_id})
            except Exception as exc:
                logger.warning("Dependent task promotion failed, downstream tasks stay pending: %s", exc)
        elif status == "failed":
            cypher = """
            MATCH (t:Task {id: $id})
            SET t.retry_count = coalesce(t.retry_count, 0) + 1,
                t.status = 'failed',
                t.last_error = $err,
                t.lease_expires_at = null
            RETURN t.status, coalesce(t.project_id, 'kortext')
            """
            raw = self.client.query(self.graph, cypher, {"id": task_id, "err": error or ""})
            if raw and len(raw) >= 2 and raw[1] and len(raw[1][0]) >= 2:
                proj_id = str(raw[1][0][1])
        else:
            cypher = """
            MATCH (t:Task {id: $id})
            SET t.status = $status
            RETURN t.id, coalesce(t.project_id, 'kortext'), coalesce(t.title, ''), coalesce(t.description, '')
            """
            raw = self.client.query(self.graph, cypher, {"id": task_id, "status": status})
            t_title = ""
            t_desc = ""
            if raw and len(raw) >= 2 and raw[1] and len(raw[1][0]) >= 2:
                proj_id = str(raw[1][0][1])
                if len(raw[1][0]) >= 4:
                    t_title = str(raw[1][0][2] or "")
                    t_desc = str(raw[1][0][3] or "")
            if status == "in_progress":
                try:
                    self.generate_task_brief(
                        task_id=task_id,
                        title=t_title or task_id,
                        description=t_desc or t_title or task_id,
                    )
                except Exception as exc:
                    logger.debug("Brief generation on update_task_status(in_progress) skipped: %s", exc)
        self._refresh_hud_snapshot(proj_id)
        return True

    def get_all_tasks(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns all Task nodes with metadata and their dependencies."""
        where_clause = ""
        params: Dict[str, Any] = {}
        if project_id and project_id.lower() != "all":
            where_clause = "WHERE coalesce(t.project_id, 'kortext') = $project_id\n        "
            params["project_id"] = project_id

        cypher = f"""
        MATCH (t:Task)
        {where_clause}OPTIONAL MATCH (t)-[:DEPENDS_ON]->(dep:Task)
        RETURN t.id, t.title, t.description, t.status, t.complexity, t.assigned_to,
               t.retry_count, t.created_at, t.completed_at, t.last_error, collect(dep.id),
               coalesce(t.project_id, 'kortext')
        ORDER BY t.created_at DESC
        """
        raw = self.client.query(self.graph, cypher, params if params else None)
        if not raw or len(raw) < 2 or not raw[1]:
            return []

        tasks = []
        for row in raw[1]:
            deps = []
            if isinstance(row[10], list):
                deps = [d for d in row[10] if d]
            elif isinstance(row[10], str) and row[10]:
                cleaned = row[10].strip("[]").replace("'", "").replace('"', "")
                deps = [d.strip() for d in cleaned.split(",") if d.strip()]

            tasks.append({
                "id": str(row[0]),
                "title": str(row[1]),
                "description": str(row[2] or ""),
                "status": str(row[3] or "pending"),
                "complexity": int(row[4] or 1),
                "assigned_to": str(row[5] or "primary"),
                "retry_count": int(row[6] or 0),
                "created_at": str(row[7] or ""),
                "completed_at": str(row[8] or "") if row[8] is not None else None,
                "last_error": str(row[9] or "") if row[9] is not None else None,
                "depends_on": deps,
                "project_id": str(row[11]) if len(row) > 11 and row[11] is not None else "kortext",
            })
        return tasks

