"""
Unit tests for TaskDAGTracker dependency gating and state machine.
"""

import pytest
from unittest.mock import MagicMock
from kortext.tasks.tracker import TaskDAGTracker


class TestTaskDAGTracker:
    @pytest.fixture
    def tracker(self):
        t = TaskDAGTracker()
        t.client = MagicMock()
        return t

    def test_create_task_ready_state(self, tracker):
        t_id = tracker.create_task(
            title="Compile FalkorDB",
            description="Run native build script",
            complexity=2
        )
        assert t_id.startswith("task_")
        assert tracker.client.query.called

    def test_get_ready_tasks_returns_unblocked(self, tracker):
        tracker.client.query.return_value = [
            ["id", "title", "complexity", "assigned_to", "description"],
            [
                ["task_12345678", "Build Native Engine", 3, "primary", "Step 1"]
            ]
        ]
        ready = tracker.get_ready_tasks()
        assert len(ready) == 1
        assert ready[0]["id"] == "task_12345678"
        assert ready[0]["complexity"] == 3

    def test_task_status_retry_limit(self, tracker):
        tracker.update_task_status("task_fail_1", "failed", error="Compile error")
        assert tracker.client.query.called

    def test_update_task_status_failed_sets_status_failed(self, tracker):
        tracker.update_task_status("task_fail_1", "failed", error="Compile error")
        assert tracker.client.query.called
        query_str = tracker.client.query.call_args[0][1]
        assert "t.status = 'failed'" in query_str

    def test_get_ready_tasks_cypher_positive_enforcement(self, tracker):
        tracker.client.query.return_value = None
        tracker.get_ready_tasks()
        assert tracker.client.query.called
        query_str = tracker.client.query.call_args[0][1]
        assert "ALL(d IN dep_statuses WHERE d = 'completed')" in query_str
        assert "[(t)-[:DEPENDS_ON]->(dep) | dep.status]" in query_str
        assert "UNION" in query_str
        assert "coalesce(size(t.depends_on), 0) = 0" in query_str
        assert "size(dep_statuses) = size(t.depends_on)" in query_str

    def test_create_task_with_dependencies_stores_dep_list(self, tracker):
        t_id = tracker.create_task(
            title="Step 2",
            description="Run dependent step",
            complexity=2,
            depends_on=["task_dep1", "task_dep2"],
        )
        assert t_id.startswith("task_")
        assert tracker.client.query.called
        # Verify params passed to create task included dep_list
        create_call = next(c for c in tracker.client.query.call_args_list if "MERGE (t:Task" in c[0][1])
        params = create_call[0][2]
        assert params["status"] == "pending"
        assert params["dep_list"] == ["task_dep1", "task_dep2"]

    def test_live_nonexistent_dependency_blocks_promotion(self):
        """Verifies that a task declaring a nonexistent dependency is never promoted to ready."""
        import uuid
        test_graph = f"test_task_dag_{uuid.uuid4().hex[:8]}"
        live_tracker = TaskDAGTracker(graph_name=test_graph)
        try:
            # 1. Independent task should become ready
            t_indep = live_tracker.create_task(title="Indep", description="No deps")
            # 2. Task with nonexistent dep should stay pending
            t_blocked = live_tracker.create_task(
                title="Blocked", description="Depends on missing", depends_on=["task_missing_999"]
            )

            ready_tasks = live_tracker.get_ready_tasks()
            ready_ids = [t["id"] for t in ready_tasks]
            assert t_indep in ready_ids
            assert t_blocked not in ready_ids

            # Verify in get_all_tasks that t_blocked status is still pending
            all_tasks = {t["id"]: t for t in live_tracker.get_all_tasks()}
            assert all_tasks[t_blocked]["status"] == "pending"
        finally:
            try:
                live_tracker.client.get_redis().execute_command("GRAPH.DELETE", test_graph)
            except Exception:
                pass


