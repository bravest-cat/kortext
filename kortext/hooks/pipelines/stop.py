"""
Stop pipeline: Task DAG readiness verification, automatic outcome extraction / gating,
dirty-file test verification gate (PrematureStopGuard), and MINJA causal memory fact sanitization.
Includes strict executionNum == 1 guard to prevent infinite Stop continuation loops.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kortext._home import get_kortext_home
from kortext.hooks.context import HookContext
from kortext.hooks.types import InterceptorResult

logger = logging.getLogger("kortext.hooks.pipelines.stop")

FILE_MODIFYING_TOOLS = frozenset({
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
})

OUTCOME_TOOLS = frozenset({
    "record_system_outcome",
    "trigger_dream_mode",
})

_EXPLICIT_RULE_RE = re.compile(r"(?:^|\n)\s*(?:Rule|Invariant)\s*:\s*([^\n]{12,240})", re.IGNORECASE)


def _analyze_transcript_outcomes(context: HookContext) -> Tuple[bool, bool, List[str]]:
    """
    Scans cached transcript steps to verify whether files were modified and
    whether record_system_outcome or trigger_dream_mode was executed.
    Returns (modified_files_bool, recorded_outcome_bool, modified_file_paths).
    """
    steps = context.read_transcript_steps()
    if not steps:
        return False, True, []

    modified_files = False
    recorded_outcome = False
    modified_paths: List[str] = []

    for step in steps:
        tool_calls = step.get("tool_calls")
        if isinstance(tool_calls, (list, tuple)):
            for tc in tool_calls:
                if not isinstance(tc, (dict, MappingProxyType)):
                    continue
                name = str(tc.get("name") or tc.get("tool_name") or "")
                args = tc.get("args") or {}

                # Check outcome tools
                if any(ot in name for ot in OUTCOME_TOOLS):
                    recorded_outcome = True
                elif isinstance(args, (dict, MappingProxyType)):
                    target_tool = str(args.get("ToolName", ""))
                    if any(ot == target_tool for ot in OUTCOME_TOOLS):
                        recorded_outcome = True
                    elif name == "run_command" and any(ot in str(args.get("CommandLine", "")) for ot in OUTCOME_TOOLS):
                        recorded_outcome = True

                # Check modification tools
                if any(ft in name for ft in FILE_MODIFYING_TOOLS):
                    modified_files = True
                    if isinstance(args, (dict, MappingProxyType)):
                        tf = (
                            args.get("TargetFile")
                            or args.get("target_file")
                            or args.get("path")
                            or args.get("file_path")
                        )
                        if tf and str(tf) not in modified_paths:
                            modified_paths.append(str(tf))

    return modified_files, recorded_outcome, modified_paths


def _extract_learned_facts_from_steps(steps: Sequence[Any]) -> List[Dict[str, Any]]:
    """Extracts candidate learned facts from transcript steps, tool calls, and explicit Rule:/Invariant: statements."""
    facts: List[Dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, (dict, MappingProxyType)):
            continue
        for key in ("learned_facts", "facts"):
            val = step.get(key)
            if isinstance(val, (list, tuple)):
                for item in val:
                    if isinstance(item, (dict, MappingProxyType)):
                        facts.append(dict(item))
                    elif isinstance(item, str):
                        facts.append({
                            "subject": "Session",
                            "relation": "LEARNED",
                            "fact": item,
                            "source_tier": 2,
                        })
        content_txt = step.get("content") or step.get("text")
        if isinstance(content_txt, str) and ("rule:" in content_txt.lower() or "invariant:" in content_txt.lower()):
            for m in _EXPLICIT_RULE_RE.finditer(content_txt):
                rule_val = m.group(1).strip()
                if rule_val:
                    facts.append({
                        "subject": "Session",
                        "relation": "LEARNED_RULE",
                        "fact": rule_val,
                        "source_tier": 3,
                    })
        tool_calls = step.get("tool_calls", ())
        if isinstance(tool_calls, (list, tuple)):
            for tc in tool_calls:
                if isinstance(tc, (dict, MappingProxyType)):
                    args = tc.get("args") or {}
                    if isinstance(args, (dict, MappingProxyType)):
                        tc_facts = args.get("facts")
                        if isinstance(tc_facts, (list, tuple)):
                            for item in tc_facts:
                                if isinstance(item, (dict, MappingProxyType)):
                                    facts.append(dict(item))
    return facts


class GroundTruthTestGate:
    """
    Test verification gate (FM4 PrematureStopGuard) ensuring tests ran after code edits before stopping.
    Checks both session transcript test commands and ~/.kortext/run/dirty_files.json.
    """

    def __init__(self, enforce: bool = False):
        self.enforce = enforce or os.getenv("KORTEXT_TEST_GATE_ENFORCE", "0") == "1"

    def process(self, context: HookContext) -> InterceptorResult:
        if context.execution_num != 1:
            return InterceptorResult.allow()

        dirty_file = get_kortext_home() / "run" / "dirty_files.json"
        dirty_paths: List[str] = []
        if dirty_file.is_file():
            try:
                d_data = json.loads(dirty_file.read_text(encoding="utf-8"))
                if isinstance(d_data, dict):
                    dirty_paths = [str(f) for f in (d_data.get("files") or []) if f]
            except Exception:
                dirty_paths = []

        if not self.enforce:
            return InterceptorResult.allow()

        steps = context.read_transcript_steps()
        ran_tests = False
        for step in steps:
            for tc in step.get("tool_calls", ()):
                if isinstance(tc, (dict, MappingProxyType)):
                    name = tc.get("name") or ""
                    args = tc.get("args") or {}
                    cmd_str = str(args.get("CommandLine", ""))
                    if name == "run_command" and any(t in cmd_str for t in ("pytest", "npm test", "cargo test", "go test")):
                        ran_tests = True
                        break

        if not ran_tests or dirty_paths:
            dirty_hint = f" Dirty untested files: {', '.join(Path(p).name for p in dirty_paths[:4])}." if dirty_paths else ""
            return InterceptorResult.continue_execution(
                reason=f"🛑 [GroundTruthTestGate] Test enforcement active: You must execute test suite before stopping.{dirty_hint}"
            )

        return InterceptorResult.allow()


PrematureStopGuard = GroundTruthTestGate


def _extract_session_dag_activity(context: HookContext) -> Tuple[bool, set[str]]:
    """Checks whether the current session transcript interacted with manage_tasks and collects referenced task IDs."""
    steps = context.read_transcript_steps()
    used_dag = False
    task_ids: set[str] = set()
    for step in steps:
        if not isinstance(step, (dict, MappingProxyType)):
            continue
        for tc in step.get("tool_calls", ()):
            if not isinstance(tc, (dict, MappingProxyType)):
                continue
            name = str(tc.get("name") or tc.get("tool_name") or "")
            args = tc.get("args") or {}
            if not isinstance(args, (dict, MappingProxyType)):
                args = {}
            target_tool = str(args.get("ToolName", ""))
            if "manage_tasks" in name or target_tool == "manage_tasks":
                used_dag = True
                tid = args.get("task_id")
                if tid:
                    task_ids.add(str(tid))
    return used_dag, task_ids


class ProjectDAGGuard:
    """
    Verifies that no uncompleted ready or in_progress tasks created or claimed within the current
    session/conversation remain in the active project's Task DAG before allowing termination.
    """

    def _filter_session_tasks(
        self,
        context: HookContext,
        tasks: Sequence[Dict[str, Any]],
        session_used_dag: bool,
        referenced_task_ids: set[str],
    ) -> List[Dict[str, Any]]:
        session_id = (context.session_id or "").strip()
        has_transcript = bool(context.transcript_path and Path(context.transcript_path).is_file())
        filtered: List[Dict[str, Any]] = []
        for t in tasks:
            tid = str(t.get("id") or "")
            t_session = str(t.get("session_id") or t.get("conversation_id") or "").strip()
            assigned = str(t.get("assigned_to") or "").strip()
            if session_id and (t_session == session_id or assigned == session_id):
                filtered.append(t)
                continue
            if tid and tid in referenced_task_ids:
                filtered.append(t)
                continue
            if t_session and session_id and t_session != session_id:
                continue
            if session_used_dag or not has_transcript:
                filtered.append(t)
        return filtered

    def process(self, context: HookContext) -> InterceptorResult:
        if (
            context.execution_num != 1
            or not context.fully_idle
            or (context.termination_reason and context.termination_reason != "model_stop")
        ):
            return InterceptorResult.allow()

        try:
            from kortext.falkordb_client import FalkorDBClient
            from kortext.hooks.pipelines.pre_invoke import _is_falkor_substrate_available
            from kortext.tasks.tracker import TaskDAGTracker, write_hud_state_snapshot

            if not _is_falkor_substrate_available(TaskDAGTracker, FalkorDBClient):
                return InterceptorResult.allow()

            tracker = TaskDAGTracker()
            project_id = context.workspace_project or "kortext"
            ready_tasks = tracker.get_ready_tasks(project_id=project_id)
            in_prog_fn = getattr(tracker, "get_in_progress_tasks", None)
            in_progress_tasks = in_prog_fn(project_id=project_id) if callable(in_prog_fn) else []

            write_hud_state_snapshot(
                ready_tasks=len(ready_tasks),
                in_progress_tasks=len(in_progress_tasks),
                project_id=project_id,
            )

            session_used_dag, ref_ids = _extract_session_dag_activity(context)
            scoped_ready = self._filter_session_tasks(context, ready_tasks, session_used_dag, ref_ids)
            scoped_in_prog = self._filter_session_tasks(context, in_progress_tasks, session_used_dag, ref_ids)

            active_tasks = list(scoped_ready) + list(scoped_in_prog)
            if active_tasks:
                task_list = ", ".join([f"{t.get('id')} [{t.get('status', 'ready')}] ({t.get('title', '')})" for t in active_tasks[:3]])
                reason = (
                    f"🛑 [Kortext DAG Gate] Execution cannot stop: Active ready or in_progress tasks remain in DAG "
                    f"for project '{project_id}': {task_list}. Please complete or resolve these tasks before terminating."
                )
                return InterceptorResult.continue_execution(
                    reason=reason,
                    data=MappingProxyType({
                        "ready_tasks": tuple(scoped_ready),
                        "in_progress_tasks": tuple(scoped_in_prog),
                    }),
                )
        except Exception as e:
            logger.debug("ProjectDAGGuard bypassed due to: %s", e)

        return InterceptorResult.allow()


class OutcomePersistenceGuard:
    """
    Ensures modified files are persisted to episodic memory.
    When require_active_dag=True (default in StopPipeline) and KORTEXT_OUTCOME_GUARD_ENFORCE!=1,
    automatically extracts and records a lightweight background outcome for small ad-hoc edits
    instead of forcing a full-context LLM continuation turn.
    """

    def __init__(self, enforce: bool = True, require_active_dag: bool = False):
        self.enforce = enforce
        self.require_active_dag = require_active_dag

    def _auto_extract_outcome(self, context: HookContext, modified_paths: List[str]) -> bool:
        """Automatically persists a deduplicated lightweight session modification fact without blocking the agent."""
        if os.getenv("KORTEXT_AUTO_RECORD_MODIFIED_FILES", "1").strip() == "0":
            return True
        try:
            from kortext.episodic.driver import AgentNativeEpisodicDriver
            from kortext.falkordb_client import FalkorDBClient
            from kortext.hooks.pipelines.pre_invoke import _is_falkor_substrate_available

            if not _is_falkor_substrate_available(AgentNativeEpisodicDriver, FalkorDBClient):
                return True

            driver = AgentNativeEpisodicDriver()
            project_id = context.workspace_project or "kortext"
            session_id = context.session_id or "auto_stop_hook"
            short_files = [Path(p).name for p in modified_paths[:5]]
            fact_text = f"Modified {len(modified_paths)} file(s) in session: {', '.join(short_files)}"

            try:
                driver.client.query(
                    driver.db_name,
                    "MATCH (s:Entity {name: $sub})-[r:RELATES_TO {relation: 'MODIFIED_FILES', source_session: $sess}]->() "
                    "SET r.superseded = true, r.dormant = true",
                    {"sub": project_id, "sess": session_id},
                )
            except Exception as exc:
                logger.debug("Suppressed in stop:%d: %s", 325, exc)

            driver.record_fact(
                subject=project_id,
                relation="MODIFIED_FILES",
                fact=fact_text,
                object_name=short_files[0] if short_files else "workspace",
                source_session=session_id,
                source_tier=2,
                project=project_id,
            )
            return True
        except Exception as exc:
            logger.debug("Background outcome auto-extraction skipped: %s", exc)
            return False

    def process(self, context: HookContext) -> InterceptorResult:
        if (
            context.execution_num != 1
            or not context.fully_idle
            or (context.termination_reason and context.termination_reason != "model_stop")
        ):
            return InterceptorResult.allow()

        modified_files, recorded_outcome, modified_paths = _analyze_transcript_outcomes(context)
        if not modified_files or recorded_outcome:
            return InterceptorResult.allow()

        if self.require_active_dag and os.getenv("KORTEXT_OUTCOME_GUARD_ENFORCE", "0") != "1":
            has_active_dag = False
            try:
                from kortext.tasks.tracker import TaskDAGTracker

                tracker = TaskDAGTracker()
                project_id = context.workspace_project or "kortext"
                if tracker.get_ready_tasks(project_id=project_id) or tracker.get_in_progress_tasks(project_id=project_id):
                    has_active_dag = True
            except Exception:
                has_active_dag = False

            if not has_active_dag:
                auto_ok = self._auto_extract_outcome(context, modified_paths)
                return InterceptorResult.allow(
                    data=MappingProxyType({
                        "auto_persisted_outcome": auto_ok,
                        "modified_files": tuple(modified_paths),
                    })
                )

        if not self.enforce:
            return InterceptorResult.allow()

        reason = (
            "🧠 [Kortext Stop Guard] Codebase files were modified in this session without "
            "recording an outcome. Please call `record_system_outcome` to persist your decisions, "
            "changes, and verified behavior into Kortext memory before finishing."
        )
        return InterceptorResult.continue_execution(reason=reason)


class MINJAFactSanitizer:
    """
    Sanitizes candidate learned facts with CausalSanitizer and persists valid facts to the episodic graph.
    """

    def __init__(self, persist: bool = True, driver: Optional[Any] = None):
        self.persist = persist
        self.driver = driver

    def process(self, context: HookContext) -> InterceptorResult:
        raw_facts: List[Dict[str, Any]] = []

        # 1. Extract from payload
        payload = context.raw_payload
        for k in ("learned_facts", "facts"):
            val = payload.get(k)
            if isinstance(val, (list, tuple)):
                for item in val:
                    if isinstance(item, (dict, MappingProxyType)):
                        raw_facts.append(dict(item))
                    elif isinstance(item, str):
                        raw_facts.append({
                            "subject": "Session",
                            "relation": "LEARNED",
                            "fact": item,
                            "source_tier": 2,
                        })

        # 2. Extract from cached transcript steps if present (unless already recorded by caller)
        if not payload.get("_facts_already_recorded"):
            steps = context.read_transcript_steps()
            if steps:
                raw_facts.extend(_extract_learned_facts_from_steps(steps))

        if not raw_facts:
            return InterceptorResult.allow(data=MappingProxyType({"sanitized_facts": ()}))

        # 3. Sanitize with CausalSanitizer
        sanitized: List[Dict[str, Any]] = []
        try:
            from kortext.trust.sanitizer import CausalSanitizer

            sanitizer = CausalSanitizer()
            for f in raw_facts:
                sub = str(f.get("subject") or f.get("source") or "Session")
                rel = str(f.get("relation") or "LEARNED")
                fact_text = str(f.get("fact") or f.get("target") or "")
                tier = int(f.get("source_tier", 2))

                try:
                    sanitizer.sanitize(subject=sub, relation=rel, fact=fact_text, source_tier=tier)
                    sanitized.append(f)
                except Exception as exc:
                    logger.warning("[MINJAFactSanitizer] Fact blocked by CausalSanitizer: %s", exc)
                    continue
        except Exception as e:
            # Fail closed: an unsanitized fact must never reach memory.
            logger.warning("CausalSanitizer unavailable, dropping %d facts: %s", len(raw_facts), e)
            sanitized = []

        # 4. Persist sanitized facts to FalkorDB episodic graph
        if sanitized and self.persist:
            try:
                active_driver = self.driver
                if active_driver is None:
                    from kortext.episodic.driver import AgentNativeEpisodicDriver

                    active_driver = AgentNativeEpisodicDriver()

                session_name = context.session_id or "session_learned_facts"
                project_id = context.workspace_project or "kortext"
                for sf in sanitized:
                    sub = str(sf.get("subject") or sf.get("source") or "Session")
                    rel = str(sf.get("relation") or "LEARNED")
                    obj = str(sf.get("target") or sf.get("object") or rel)
                    fact_text = str(sf.get("fact") or str(sf))
                    tier = int(sf.get("source_tier", 2))
                    try:
                        active_driver.record_fact(
                            subject=sub,
                            relation=rel,
                            fact=fact_text,
                            object_name=obj,
                            source_session=session_name,
                            source_tier=tier,
                            project=project_id,
                        )
                    except Exception as persist_err:
                        logger.debug("Fact persistence skipped: %s", persist_err)
            except Exception as driver_err:
                logger.debug("Episodic driver unavailable during Stop hook: %s", driver_err)

        return InterceptorResult.allow(data=MappingProxyType({"sanitized_facts": tuple(sanitized)}))


class IntegrityModeAuditor:
    """
    Implements /teamwork-preview Integrity Mode ('development' | 'demo' | 'benchmark')
    anti-mock auditing: inspects modified production Python files to ensure no mock-inspection
    or test-cheating patterns were introduced.
    """

    def __init__(self, mode: Optional[str] = None):
        self.mode = (mode or os.getenv("KORTEXT_INTEGRITY_MODE", "")).strip().lower()

    def process(self, context: HookContext) -> InterceptorResult:
        if not self.mode or self.mode not in ("development", "demo", "benchmark"):
            return InterceptorResult.allow()

        modified_files, _, modified_paths = _analyze_transcript_outcomes(context)
        if not modified_files or not modified_paths:
            return InterceptorResult.allow()

        bad_markers = (
            "_sandbox_" + "task_graph_query",
            "_mock_" + "name",
            "tests." + "conftest",
            "unittest." + "mock",
        )
        violations: List[str] = []
        for fp in modified_paths:
            p = Path(fp)
            if not p.is_file() or p.suffix != ".py" or "tests" in p.parts or "scripts" in p.parts:
                continue
            try:
                src = p.read_text(encoding="utf-8", errors="ignore")
                for marker in bad_markers:
                    if marker in src:
                        violations.append(f"{p.name} (contains '{marker}')")
            except Exception:
                continue

        if violations:
            return InterceptorResult.continue_execution(
                reason=(
                    f"🛑 [IntegrityModeAuditor ({self.mode})] Anti-mock violation in production code: "
                    f"{', '.join(violations)}. Remove test/mock inspection from production modules before stopping."
                )
            )
        return InterceptorResult.allow()


class StopPipeline:
    """Dispatches Stop interceptors sequentially with an executionNum == 1 loop-prevention guard."""

    def __init__(self, interceptors: Optional[Sequence[Any]] = None):
        self.interceptors = tuple(interceptors) if interceptors is not None else (
            GroundTruthTestGate(),
            IntegrityModeAuditor(),
            ProjectDAGGuard(),
            OutcomePersistenceGuard(
                enforce=os.getenv("KORTEXT_OUTCOME_GUARD_ENFORCE", "0") == "1",
                require_active_dag=True,
            ),
            MINJAFactSanitizer(),
        )

    def execute(self, context: HookContext) -> InterceptorResult:
        # Critical Antigravity Stop hook invariant (hooks.md):
        # Never block termination if executionNum != 1, or if not a natural model_stop, or if background tasks are active.
        if (
            context.execution_num != 1
            or not context.fully_idle
            or (context.termination_reason and context.termination_reason != "model_stop")
        ):
            return InterceptorResult.stop()

        merged_data: Dict[str, Any] = {}
        for interceptor in self.interceptors:
            try:
                res = interceptor.process(context)
                if res.decision == "continue":
                    return res
                if res.data:
                    for k, v in res.data.items():
                        merged_data[k] = v
            except Exception as e:
                logger.debug("Stop interceptor %s failed: %s", type(interceptor).__name__, e)
                continue

        return InterceptorResult.stop(data=MappingProxyType(merged_data))
