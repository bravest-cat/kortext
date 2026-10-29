"""
Unit tests for DreamEngine consolidation, pruning and skill distillation.
"""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import yaml

from kortext.dream import DreamEngine


class TestDreamConsolidation:
    @pytest.fixture
    def dream_engine(self):
        return DreamEngine()

    @pytest.mark.asyncio
    async def test_dream_cycle_emits_logs_without_polluting_stdout(self, dream_engine, monkeypatch, capsys, caplog, tmp_path):
        """Verifies run_dream_cycle uses standard logger and does not pollute stdout (FastMCP stdio safety)."""
        import logging

        async def mock_prune(*args, **kwargs):
            return {"superseded_count": 0, "archived_count": 0, "active_count": 0, "details": [], "contradictions": []}
        async def mock_cluster(*args, **kwargs):
            return []
        async def mock_induce(*args, **kwargs):
            return {"active": [], "drift_audit": {"drifted_count": 0, "total_grounded": 0, "verified_count": 0, "orphaned_count": 0, "drifted": []}, "inducted": []}

        monkeypatch.setattr(dream_engine, "prune_stale_facts", mock_prune)
        monkeypatch.setattr(dream_engine, "cluster_subsystems", mock_cluster)
        monkeypatch.setattr(dream_engine, "induce_invariants", mock_induce)
        monkeypatch.setattr("kortext.dream.DREAMS_DIR", tmp_path)

        with caplog.at_level(logging.INFO):
            await dream_engine.run_dream_cycle(project="test_proj", dry_run=False)

        captured = capsys.readouterr()
        # Verify stdout is completely clean to protect MCP stdio JSON-RPC framing
        assert captured.out == ""
        # Verify log records were emitted via logger.info
        assert any("[DreamEngine]" in record.message for record in caplog.records)
        assert any("Dream Report persisted to" in record.message for record in caplog.records)

    def test_kortext_dream_engine_alias(self):
        """Verify that KortextDreamEngine is an alias for DreamEngine."""
        from kortext.dream import KortextDreamEngine

        engine = KortextDreamEngine()
        assert isinstance(engine, DreamEngine)

    def test_distill_skill_creates_skill_markdown_with_yaml_frontmatter(self, dream_engine, tmp_path):
        """Verifies distill_skill generates SKILL.md with valid YAML frontmatter in workspace .agents/skills."""
        skill_name = "graph-query-optimizer"
        description = "Optimizes FalkorDB Cypher queries for sub-5ms traversal."
        content = "# Graph Query Optimizer\n\nAlways use parameterized queries."

        created_path = dream_engine.distill_skill(
            skill_name=skill_name,
            description=description,
            skill_content=content,
            global_scope=False,
            workspace_root=str(tmp_path),
        )

        expected_dir = tmp_path / ".agents" / "skills" / skill_name
        expected_file = expected_dir / "SKILL.md"
        assert created_path == expected_file
        assert expected_file.exists()

        text = expected_file.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert f"name: {skill_name}" in text
        assert f"description: {description}" in text
        assert "Always use parameterized queries." in text

    def test_distill_skill_global_scope(self, dream_engine, tmp_path, monkeypatch):
        """Verifies distill_skill generates SKILL.md under ~/.gemini/config/plugins/kortext/skills when global_scope is True."""
        fake_home = tmp_path / "home"
        monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

        skill_name = "global-reasoning"
        description = "Global reasoning workflow."
        content = "# Global Reasoning\n\nDirect prompt instructions."

        created_path = dream_engine.distill_skill(
            skill_name=skill_name,
            description=description,
            skill_content=content,
            global_scope=True,
        )

        expected_file = fake_home / ".gemini" / "config" / "plugins" / "kortext" / "skills" / skill_name / "SKILL.md"
        assert created_path == expected_file
        assert expected_file.exists()
        text = expected_file.read_text(encoding="utf-8")
        assert f"name: {skill_name}" in text

    @pytest.mark.usefixtures("real_falkordb")
    def test_consolidate_facts_creates_macro_and_supersedes_candidates(self, dream_engine):
        """Verifies consolidate_facts creates macro-truth fact and marks candidates as superseded/dormant."""
        import uuid
        test_db = f"test_dream_consol_{uuid.uuid4().hex[:6]}"
        dream_engine.episodic_db = test_db

        try:
            # Seed 2 facts
            dream_engine._query(
                test_db,
                "MERGE (s:Entity {name: 'ServiceA'}) MERGE (t:Entity {name: 'ServiceB'}) "
                "CREATE (s)-[r1:RELATES_TO {id: 'fact_c1', fact: 'Fact 1', project: 'test_proj'}]->(t) "
                "CREATE (s)-[r2:RELATES_TO {id: 'fact_c2', fact: 'Fact 2', project: 'test_proj'}]->(t)",
            )

            result = dream_engine.consolidate_facts(
                macro_fact="Consolidated ServiceA-ServiceB relationship",
                superseded_fact_ids=["fact_c1", "fact_c2"],
                project="test_proj",
            )

            assert result["status"] == "ok"
            assert result["project"] == "test_proj"
            assert result["macro_fact"] == "Consolidated ServiceA-ServiceB relationship"
            assert result["superseded_count"] == 2
            macro_id = result["macro_fact_id"]

            # Verify macro fact in FalkorDB
            macro_rows = dream_engine._query(
                test_db,
                "MATCH ()-[r:RELATES_TO]->() WHERE r.id = $id RETURN r.is_macro, r.project, r.fact",
                {"id": macro_id},
            )
            assert macro_rows and len(macro_rows) > 1 and macro_rows[1]
            assert macro_rows[1][0][0] in (True, "true")
            assert macro_rows[1][0][1] == "test_proj"

            # Verify candidates superseded
            cand_rows = dream_engine._query(
                test_db,
                "MATCH ()-[r:RELATES_TO]->() WHERE r.id IN ['fact_c1', 'fact_c2'] "
                "RETURN r.id, r.superseded, r.dormant, r.superseded_by ORDER BY r.id",
            )
            assert len(cand_rows[1]) == 2
            for row in cand_rows[1]:
                assert row[1] in (True, "true")
                assert row[2] in (True, "true")
                assert row[3] == macro_id
        finally:
            try:
                dream_engine._get_client().execute_command("GRAPH.DELETE", test_db)
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_dream_cycle_executes_phase4_and_phase5(self, dream_engine, monkeypatch, tmp_path):
        """Verifies run_dream_cycle runs Phase 4 consolidations, Phase 5 distillations, and writes reports to both dreams/ and reports/."""
        import uuid
        test_db = f"test_dream_cycle_{uuid.uuid4().hex[:6]}"
        dream_engine.episodic_db = test_db

        async def mock_prune(*args, **kwargs):
            return {"superseded_count": 0, "archived_count": 0, "active_count": 0, "details": [], "contradictions": []}
        async def mock_cluster(*args, **kwargs):
            return []
        async def mock_induce(*args, **kwargs):
            return {"active": [], "drift_audit": {"drifted_count": 0, "total_grounded": 0, "verified_count": 0, "orphaned_count": 0, "drifted": []}, "inducted": []}

        monkeypatch.setattr(dream_engine, "prune_stale_facts", mock_prune)
        monkeypatch.setattr(dream_engine, "cluster_subsystems", mock_cluster)
        monkeypatch.setattr(dream_engine, "induce_invariants", mock_induce)

        try:
            # Seed 1 candidate fact
            dream_engine._query(
                test_db,
                "MERGE (s:Entity {name: 'Node1'}) MERGE (t:Entity {name: 'Node2'}) "
                "CREATE (s)-[r:RELATES_TO {id: 'fact_seed_1', fact: 'Seed 1', project: 'cycle_proj'}]->(t)",
            )

            res = await dream_engine.run_dream_cycle(
                project="cycle_proj",
                dry_run=False,
                consolidations=[
                    {
                        "macro_fact": "Unified Node Architecture",
                        "superseded_fact_ids": ["fact_seed_1"],
                    }
                ],
                distilled_skills=[
                    {
                        "name": "auto-synthesized-skill",
                        "description": "Synthesized during dream cycle",
                        "content": "# Synthesized Skill\n\nLearned workflow.",
                    }
                ],
                workspace_root=str(tmp_path),
            )

            assert res["status"] == "ok"
            assert len(res["consolidations"]) == 1
            assert res["consolidated_facts_count"] == 1
            assert len(res["distilled_skills"]) == 1
            assert res["distilled_skills_count"] == 1

            # Verify report exists in both journal/dreams and journal/reports
            dreams_dir = tmp_path / "journal" / "dreams"
            reports_dir = tmp_path / "journal" / "reports"
            dream_files = list(dreams_dir.glob("*-dream.md"))
            report_files = list(reports_dir.glob("*-dream.md"))
            assert len(dream_files) == 1
            assert len(report_files) == 1
            assert dream_files[0].read_text() == report_files[0].read_text()

            # Verify distilled skill exists
            skill_file = tmp_path / ".agents" / "skills" / "auto-synthesized-skill" / "SKILL.md"
            assert skill_file.exists()
        finally:
            try:
                dream_engine._get_client().execute_command("GRAPH.DELETE", test_db)
            except Exception:
                pass

    def test_consolidate_facts_empty_superseded_ids(self, dream_engine):
        """Verifies consolidate_facts works cleanly when no candidate fact IDs are provided."""
        import uuid
        test_db = f"test_dream_empty_{uuid.uuid4().hex[:6]}"
        dream_engine.episodic_db = test_db

        try:
            res = dream_engine.consolidate_facts(
                macro_fact="Standalone macro architecture truth",
                superseded_fact_ids=[],
                project="standalone_proj",
            )
            assert res["status"] == "ok"
            assert res["superseded_count"] == 0
            assert res["superseded_fact_ids"] == []
            assert res["macro_fact_id"].startswith("fact_macro_")

            macro_rows = dream_engine._query(
                test_db,
                "MATCH ()-[r:RELATES_TO]->() WHERE r.id = $id RETURN r.is_macro, r.project",
                {"id": res["macro_fact_id"]},
            )
            assert macro_rows and len(macro_rows) > 1 and macro_rows[1]
            assert macro_rows[1][0][0] in (True, "true")
            assert macro_rows[1][0][1] == "standalone_proj"
        finally:
            try:
                dream_engine._get_client().execute_command("GRAPH.DELETE", test_db)
            except Exception:
                pass

    def test_distill_skill_preserves_existing_frontmatter(self, dream_engine, tmp_path):
        """Verifies distill_skill does not double-wrap YAML frontmatter if already present."""
        raw_content = "---\nname: preformatted-skill\ndescription: Already preformatted\n---\n\n# Preformatted Skill"
        path = dream_engine.distill_skill(
            skill_name="preformatted-skill",
            description="Ignored description",
            skill_content=raw_content,
            workspace_root=str(tmp_path),
        )
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert text.count("---") == 2
        assert "Already preformatted" in text

    @pytest.mark.asyncio
    async def test_dream_cycle_dry_run_does_not_persist_or_mutate(self, dream_engine, monkeypatch, tmp_path):
        """Verifies run_dream_cycle in dry-run mode returns dry-run summary and leaves disk pristine."""
        async def mock_prune(*args, **kwargs):
            return {"superseded_count": 0, "archived_count": 0, "active_count": 0, "details": [], "contradictions": []}
        async def mock_cluster(*args, **kwargs):
            return []
        async def mock_induce(*args, **kwargs):
            return {"active": [], "drift_audit": {"drifted_count": 0, "total_grounded": 0, "verified_count": 0, "orphaned_count": 0, "drifted": []}, "inducted": []}

        monkeypatch.setattr(dream_engine, "prune_stale_facts", mock_prune)
        monkeypatch.setattr(dream_engine, "cluster_subsystems", mock_cluster)
        monkeypatch.setattr(dream_engine, "induce_invariants", mock_induce)

        res = await dream_engine.run_dream_cycle(
            project="dry_proj",
            dry_run=True,
            consolidations=[{"macro_fact": "Dry Macro Fact", "superseded_fact_ids": ["f1"]}],
            distilled_skills=[{"name": "dry-skill", "description": "Dry run skill", "content": "# Dry Skill"}],
            workspace_root=str(tmp_path),
        )

        assert res["status"] == "ok"
        assert res["dream_report_path"] == "dry-run"
        assert res["reports_path"] == "dry-run"
        assert len(res["consolidations"]) == 1
        assert res["consolidations"][0]["status"] == "dry_run"
        assert len(res["distilled_skills"]) == 1
        assert res["distilled_skills"][0]["status"] == "dry_run"

        # Check that no files were written to disk
        assert not (tmp_path / "journal").exists()
        assert not (tmp_path / ".agents").exists()

    @pytest.mark.usefixtures("real_falkordb")
    @pytest.mark.asyncio
    async def test_run_dream_cycle_all_five_phases_populated(self, dream_engine, monkeypatch, tmp_path):
        """
        Verifies end-to-end run_dream_cycle() with all 5 phases fully populated:
        - Phase 1: Synaptic pruning & stale fact archiving
        - Phase 2: Louvain architectural subsystem clustering
        - Phase 3: Abstract invariant induction & AST drift audit
        - Phase 4: Fact consolidation (micro-facts marked superseded=true and dormant=true)
        - Phase 5: Skill distillation (SKILL.md with valid YAML frontmatter and markdown body)
        Also verifies dual persistence of the Dream Report to both journal/dreams and journal/reports.
        """
        test_db = f"test_dream_all_phases_{uuid.uuid4().hex[:6]}"
        dream_engine.episodic_db = test_db

        # Seed micro-facts for Phase 4 consolidation and an old fact
        old_time = (datetime.now(timezone.utc) - timedelta(hours=8000)).isoformat()
        dream_engine._query(
            test_db,
            "MERGE (s:Entity {name: 'StorageNode'}) MERGE (t:Entity {name: 'PersistenceNode'}) "
            "CREATE (s)-[r0:RELATES_TO {id: 'fact_decay_p0', fact: 'Ancient storage method', importance: 1.0, created_at: $old_time}]->(t) "
            "CREATE (s)-[r1:RELATES_TO {id: 'micro_fact_1', fact: 'FalkorDB stores graph nodes', project: 'e2e_proj'}]->(t) "
            "CREATE (s)-[r2:RELATES_TO {id: 'micro_fact_2', fact: 'Qdrant stores dense embeddings', project: 'e2e_proj'}]->(t)",
            {"old_time": old_time},
        )

        # Mock Phase 1 prune: returns populated superseded and archived counts
        async def mock_prune(*args, **kwargs):
            return {
                "superseded_count": 2,
                "archived_count": 1,
                "active_count": 5,
                "details": [
                    {
                        "stale_name": "LegacyMemoryProtocol",
                        "superseded_by": "FastMCP Sole Custodian",
                        "reason": "Direct embedded client deprecated",
                    }
                ],
                "contradictions": [],
            }

        # Mock Phase 2 cluster: returns populated subsystems
        async def mock_cluster(*args, **kwargs):
            return [
                {
                    "id": "subsystem_storage",
                    "name": "SubstrateResilience",
                    "file_count": 3,
                    "description": "FalkorDB and Qdrant persistence substrate",
                    "files": [
                        "kortext/falkordb_client.py",
                        "kortext/qdrant_client.py",
                        "kortext/dream.py",
                    ],
                }
            ]

        # Mock Phase 3 induce: returns active invariants and drift audit
        async def mock_induce(*args, **kwargs):
            return {
                "active": [
                    {
                        "domain": "SUBSTRATE",
                        "rule": "Never instantiate QdrantClient directly in embedded mode",
                        "rationale": "Sole Custodian invariant prevents SQLite lock contention",
                    }
                ],
                "drift_audit": {
                    "total_grounded": 12,
                    "verified_count": 11,
                    "drifted_count": 1,
                    "orphaned_count": 0,
                    "drifted": [
                        {
                            "symbol_name": "embedded_qdrant_client",
                            "file_path": "/workspace/legacy_client.py",
                            "rule": "Never instantiate QdrantClient directly in embedded mode",
                        }
                    ],
                },
                "inducted": [
                    {
                        "domain": "SUBSTRATE",
                        "rule": "Never instantiate QdrantClient directly in embedded mode",
                        "rationale": "Sole Custodian invariant prevents SQLite lock contention",
                    }
                ],
            }

        monkeypatch.setattr(dream_engine, "prune_stale_facts", mock_prune)
        monkeypatch.setattr(dream_engine, "cluster_subsystems", mock_cluster)
        monkeypatch.setattr(dream_engine, "induce_invariants", mock_induce)

        try:
            res = await dream_engine.run_dream_cycle(
                project="e2e_proj",
                dry_run=False,
                consolidations=[
                    {
                        "macro_fact": "Unified Dual-Substrate Storage Architecture (Graph + Vectors)",
                        "superseded_fact_ids": ["micro_fact_1", "micro_fact_2"],
                    }
                ],
                distilled_skills=[
                    {
                        "name": "substrate-sole-custodian",
                        "description": "Enforce FastMCP sole custodian protocol for all vector access",
                        "content": "# Substrate Sole Custodian\n\nAll vector operations must route through FastMCP.",
                    }
                ],
                workspace_root=str(tmp_path),
            )

            # Assert overall status and execution duration
            assert res["status"] == "ok"
            assert res["duration_seconds"] >= 0.0


            # 2. Phase 1 verification: Synaptic pruning populated
            assert res["superseded_count"] == 2
            assert res["archived_count"] == 1

            # 3. Phase 2 verification: Louvain subsystems clustered
            assert res["subsystems_count"] == 1
            assert res["subsystem_clusters"] == 1

            # 4. Phase 3 verification: Invariants and drift audit populated
            assert res["invariants_count"] == 1
            assert res["invariants_inducted"] == 1
            assert res["invariant_drift"]["drifted_count"] == 1
            assert res["invariant_drift"]["verified_count"] == 11

            # 5. Phase 4 verification: Fact consolidation populated
            assert res["consolidated_facts_count"] == 1
            assert len(res["consolidations"]) == 1
            macro_fact_id = res["consolidations"][0]["macro_fact_id"]
            assert macro_fact_id.startswith("fact_macro_")

            # Verify micro-facts received r.superseded = true, r.dormant = true, and r.superseded_by
            cand_rows = dream_engine._query(
                test_db,
                "MATCH ()-[r:RELATES_TO]->() WHERE r.id IN ['micro_fact_1', 'micro_fact_2'] "
                "RETURN r.id, r.superseded, r.dormant, r.superseded_by ORDER BY r.id",
            )
            assert len(cand_rows[1]) == 2
            for row in cand_rows[1]:
                assert row[1] in (True, "true"), "Micro-fact must have superseded = true"
                assert row[2] in (True, "true"), "Micro-fact must have dormant = true"
                assert row[3] == macro_fact_id

            # Verify macro-fact has is_macro = true, superseded = false, dormant = false
            macro_rows = dream_engine._query(
                test_db,
                "MATCH ()-[r:RELATES_TO]->() WHERE r.id = $id RETURN r.is_macro, r.superseded, r.dormant, r.fact",
                {"id": macro_fact_id},
            )
            assert macro_rows and len(macro_rows) > 1 and macro_rows[1]
            assert macro_rows[1][0][0] in (True, "true")
            assert macro_rows[1][0][1] in (False, "false", None)
            assert macro_rows[1][0][2] in (False, "false", None)

            # 6. Phase 5 verification: Distilled skill generated with valid YAML frontmatter and markdown body
            assert res["distilled_skills_count"] == 1
            assert len(res["distilled_skills"]) == 1
            skill_path = Path(res["distilled_skills"][0]["path"])
            assert skill_path.exists()

            raw_skill_text = skill_path.read_text(encoding="utf-8")
            parts = raw_skill_text.split("---", 2)
            assert len(parts) >= 3, "Skill file must contain YAML frontmatter enclosed in '---'"
            frontmatter_data = yaml.safe_load(parts[1])
            assert isinstance(frontmatter_data, dict)
            assert frontmatter_data["name"] == "substrate-sole-custodian"
            assert frontmatter_data["description"] == "Enforce FastMCP sole custodian protocol for all vector access"
            body = parts[2].strip()
            assert body == "# Substrate Sole Custodian\n\nAll vector operations must route through FastMCP."

            # 7. Dual Persistence verification: Dream Report in journal/dreams/ and journal/reports/
            dreams_dir = tmp_path / "journal" / "dreams"
            reports_dir = tmp_path / "journal" / "reports"
            dream_files = list(dreams_dir.glob("*-dream.md"))
            report_files = list(reports_dir.glob("*-dream.md"))

            assert len(dream_files) == 1, "Dream report must exist in journal/dreams/"
            assert len(report_files) == 1, "Dream report must exist in journal/reports/"
            assert dream_files[0].read_text(encoding="utf-8") == report_files[0].read_text(encoding="utf-8")
            assert dream_files[0].read_text(encoding="utf-8") == res["markdown_report"]
            assert res["dream_report_path"] == str(dream_files[0])
            assert res["reports_path"] == str(report_files[0])

            # Verify report contains sections for all phases
            report_content = res["markdown_report"]
            assert "## 1. Synaptic Pruning & Fact Cleanup" in report_content
            assert "LegacyMemoryProtocol" in report_content
            assert "## 2. Synthesized Architectural Subsystems" in report_content
            assert "SubstrateResilience" in report_content
            assert "## 3. Induced Codebase Invariants" in report_content
            assert "Never instantiate QdrantClient directly" in report_content
            assert "## 4. AST Grounding & Invariant Drift Audit" in report_content
            assert "embedded_qdrant_client" in report_content
            assert "## 5. Consolidated Macro Facts" in report_content
            assert "Unified Dual-Substrate Storage Architecture" in report_content
            assert "## 6. Distilled Agent Skills" in report_content
            assert "substrate-sole-custodian" in report_content

        finally:
            try:
                dream_engine._get_client().execute_command("GRAPH.DELETE", test_db)
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_dual_persistence_default_directories(self, dream_engine, monkeypatch, tmp_path):
        """Verifies dream report persists to both default DREAMS_DIR and default REPORTS_DIR when workspace_root is None."""
        custom_dreams = tmp_path / "default_dreams"
        custom_reports = tmp_path / "default_reports"
        monkeypatch.setattr("kortext.dream.DREAMS_DIR", custom_dreams)
        monkeypatch.setattr("kortext.dream.REPORTS_DIR", custom_reports)

        async def mock_prune(*args, **kwargs):
            return {"superseded_count": 0, "archived_count": 0, "active_count": 0, "details": [], "contradictions": []}
        async def mock_cluster(*args, **kwargs):
            return []
        async def mock_induce(*args, **kwargs):
            return {"active": [], "drift_audit": {"drifted_count": 0, "total_grounded": 0, "verified_count": 0, "orphaned_count": 0, "drifted": []}, "inducted": []}

        monkeypatch.setattr(dream_engine, "prune_stale_facts", mock_prune)
        monkeypatch.setattr(dream_engine, "cluster_subsystems", mock_cluster)
        monkeypatch.setattr(dream_engine, "induce_invariants", mock_induce)

        res = await dream_engine.run_dream_cycle(project="default_path_test", dry_run=False, workspace_root=None)

        assert res["status"] == "ok"
        dream_files = list(custom_dreams.glob("*-dream.md"))
        report_files = list(custom_reports.glob("*-dream.md"))

        assert len(dream_files) == 1
        assert len(report_files) == 1
        assert dream_files[0].read_text(encoding="utf-8") == report_files[0].read_text(encoding="utf-8")
        assert dream_files[0].read_text(encoding="utf-8") == res["markdown_report"]
        assert res["dream_report_path"] == str(dream_files[0])
        assert res["reports_path"] == str(report_files[0])

    @pytest.mark.usefixtures("real_falkordb")
    def test_fact_consolidation_flags_all_superseded_micro_facts(self, dream_engine):
        """Verifies that every candidate micro-fact receives r.superseded = true and r.dormant = true upon consolidation."""
        test_db = f"test_micro_flags_{uuid.uuid4().hex[:6]}"
        dream_engine.episodic_db = test_db

        try:
            # Seed 3 micro facts
            dream_engine._query(
                test_db,
                "MERGE (s:Entity {name: 'AuthService'}) MERGE (t:Entity {name: 'TokenService'}) "
                "CREATE (s)-[:RELATES_TO {id: 'm_fact_1', project: 'security_subsystem', fact: 'JWT validation in gateway'}]->(t) "
                "CREATE (s)-[:RELATES_TO {id: 'm_fact_2', project: 'security_subsystem', fact: 'Token refresh on 401'}]->(t) "
                "CREATE (s)-[:RELATES_TO {id: 'm_fact_3', project: 'security_subsystem', fact: 'Session blacklisting via Redis'}]->(t)",
            )

            res = dream_engine.consolidate_facts(
                macro_fact="Consolidated Auth Architecture: JWT validation with Redis blacklisting",
                superseded_fact_ids=["m_fact_1", "m_fact_2", "m_fact_3"],
                project="security_subsystem",
            )

            assert res["status"] == "ok"
            assert res["superseded_count"] == 3
            macro_id = res["macro_fact_id"]

            # Query all 3 micro-facts
            rows = dream_engine._query(
                test_db,
                "MATCH ()-[r:RELATES_TO]->() WHERE r.id IN ['m_fact_1', 'm_fact_2', 'm_fact_3'] "
                "RETURN r.id, r.superseded, r.dormant, r.superseded_by ORDER BY r.id",
            )
            assert len(rows[1]) == 3
            for r in rows[1]:
                assert r[1] in (True, "true"), f"Fact {r[0]} must be marked superseded = true"
                assert r[2] in (True, "true"), f"Fact {r[0]} must be marked dormant = true"
                assert r[3] == macro_id

            # Verify macro-fact properties
            macro_row = dream_engine._query(
                test_db,
                "MATCH ()-[r:RELATES_TO]->() WHERE r.id = $id "
                "RETURN r.is_macro, r.superseded, r.dormant, r.relation, r.project",
                {"id": macro_id},
            )
            assert len(macro_row[1]) == 1
            m_props = macro_row[1][0]
            assert m_props[0] in (True, "true")  # is_macro
            assert m_props[1] in (False, "false", None)  # superseded
            assert m_props[2] in (False, "false", None)  # dormant
            assert m_props[3] == "MACRO_FACT"
            assert m_props[4] == "security_subsystem"
        finally:
            try:
                dream_engine._get_client().execute_command("GRAPH.DELETE", test_db)
            except Exception:
                pass

    def test_distill_skill_frontmatter_and_markdown_body_syntax(self, dream_engine, tmp_path):
        """Verifies distill_skill output produces parseable YAML frontmatter and well-formed markdown body."""
        skill_name = "falkordb-uds-optimizer"
        description = "Optimizes FalkorDB IPC throughput over Unix Domain Sockets."
        body_markdown = (
            "# FalkorDB UDS Optimizer\n\n"
            "## Directives\n"
            "1. Connect via `~/.kortext/run/falkordb.sock`.\n"
            "2. Never access raw files directly.\n"
        )

        skill_file = dream_engine.distill_skill(
            skill_name=skill_name,
            description=description,
            skill_content=body_markdown,
            workspace_root=str(tmp_path),
        )

        assert skill_file.is_file()
        raw_text = skill_file.read_text(encoding="utf-8")

        # Must have exactly opening and closing frontmatter delimiters
        assert raw_text.startswith("---\n")
        parts = raw_text.split("---", 2)
        assert len(parts) == 3

        # Parse YAML frontmatter
        parsed_meta = yaml.safe_load(parts[1])
        assert isinstance(parsed_meta, dict)
        assert parsed_meta["name"] == skill_name
        assert parsed_meta["description"] == description

        # Markdown body
        extracted_body = parts[2].strip()
        assert extracted_body == body_markdown.strip()

    def test_surface_potential_contradictions_staged_for_agent_review(self, dream_engine):
        """Verifies surface_potential_contradictions detects conflicting active facts on same subject/relation."""
        import uuid
        test_db = f"test_dream_contra_{uuid.uuid4().hex[:6]}"
        dream_engine.episodic_db = test_db

        try:
            # Seed 2 facts with same subject, relation, and object but conflicting facts
            dream_engine._query(
                test_db,
                "MERGE (s:Entity {name: 'QdrantService'}) MERGE (t:Entity {name: 'NetworkPort'}) "
                "CREATE (s)-[r1:RELATES_TO {id: 'fact_port_6333', relation: 'RUNS_ON_PORT', fact: 'Port is 6333'}]->(t) "
                "CREATE (s)-[r2:RELATES_TO {id: 'fact_port_6334', relation: 'RUNS_ON_PORT', fact: 'Port is 6334'}]->(t)",
            )

            contradictions = dream_engine.surface_potential_contradictions(project="kortext")
            assert len(contradictions) >= 1
            contra = contradictions[0]
            assert contra["subject"] == "QdrantService"
            assert contra["object"] == "NetworkPort"
            assert contra["relation"] == "RUNS_ON_PORT"
            assert "6333" in contra["fact_a"] or "6333" in contra["fact_b"]
            assert "6334" in contra["fact_a"] or "6334" in contra["fact_b"]
        finally:
            try:
                dream_engine._get_client().execute_command("GRAPH.DELETE", test_db)
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_dream_cycle_stages_proposals_for_agent_review(self, dream_engine, monkeypatch, tmp_path):
        """Verifies run_dream_cycle surfaces potential contradictions and stages them in staged_proposals."""
        import uuid
        test_db = f"test_dream_staged_{uuid.uuid4().hex[:6]}"
        dream_engine.episodic_db = test_db

        async def mock_cluster(*args, **kwargs):
            return []
        async def mock_induce(*args, **kwargs):
            return {"active": [], "drift_audit": {"drifted_count": 0, "total_grounded": 0, "verified_count": 0, "orphaned_count": 0, "drifted": []}, "inducted": []}

        monkeypatch.setattr(dream_engine, "cluster_subsystems", mock_cluster)
        monkeypatch.setattr(dream_engine, "induce_invariants", mock_induce)

        try:
            dream_engine._query(
                test_db,
                "MERGE (s:Entity {name: 'Model'}) MERGE (t:Entity {name: 'Dimension'}) "
                "CREATE (s)-[r1:RELATES_TO {id: 'f_dim_384', relation: 'EMBED_DIM', fact: 'Dimension 384'}]->(t) "
                "CREATE (s)-[r2:RELATES_TO {id: 'f_dim_768', relation: 'EMBED_DIM', fact: 'Dimension 768'}]->(t)",
            )

            res = await dream_engine.run_dream_cycle(
                project="test_stage",
                dry_run=False,
                workspace_root=str(tmp_path),
            )

            assert res["status"] == "ok"
            assert "staged_proposals" in res
            proposals = res["staged_proposals"]
            assert "contradictions" in proposals
            assert len(proposals["contradictions"]) >= 1
            # Verify markdown report includes Section 7 for staged proposals
            assert "## 7. Staged Proposals for Agent Review & Reflection" in res["markdown_report"]
        finally:
            try:
                dream_engine._get_client().execute_command("GRAPH.DELETE", test_db)
            except Exception:
                pass




