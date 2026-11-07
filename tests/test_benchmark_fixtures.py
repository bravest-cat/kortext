"""
Unit tests validating benchmark fixtures in tests/fixtures/.

Verifies:
1. tests/fixtures/longmemeval_sample.json:
   - Exactly 50 items across 5 abilities:
     - information_extraction: 12
     - knowledge_updates: 10
     - temporal_reasoning: 10
     - multi_session_reasoning: 10
     - abstention: 8
   - Abstention items have is_answerable=False and gold_answer=""
   - Non-abstention items have is_answerable=True and non-empty gold_answer
   - context_facts contains subject, relation, fact, object
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class TestLongMemEvalFixture:
    """Validation suite for longmemeval_sample.json."""

    @pytest.fixture(scope="module")
    def longmem_data(self):
        fixture_path = FIXTURES_DIR / "longmemeval_sample.json"
        assert fixture_path.exists(), f"Missing fixture file: {fixture_path}"
        with open(fixture_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data

    def test_item_count(self, longmem_data):
        assert len(longmem_data) == 50

    def test_schema_keys(self, longmem_data):
        expected_keys = {
            "id",
            "ability",
            "question",
            "subject",
            "gold_answer",
            "is_answerable",
            "context_facts",
        }
        for item in longmem_data:
            assert set(item.keys()) == expected_keys, f"Schema mismatch in item: {item.get('id')}"

    def test_abilities_distribution(self, longmem_data):
        abilities = {}
        for item in longmem_data:
            ab = item["ability"]
            abilities[ab] = abilities.get(ab, 0) + 1

        expected_distribution = {
            "information_extraction": 12,
            "knowledge_updates": 10,
            "temporal_reasoning": 10,
            "multi_session_reasoning": 10,
            "abstention": 8,
        }
        assert abilities == expected_distribution

    def test_abstention_invariants(self, longmem_data):
        for item in longmem_data:
            if item["ability"] == "abstention":
                assert item["is_answerable"] is False
                assert item["gold_answer"] == ""
            else:
                assert item["is_answerable"] is True
                assert isinstance(item["gold_answer"], str) and len(item["gold_answer"].strip()) > 0

    def test_context_facts_structure(self, longmem_data):
        required_fact_keys = {"subject", "relation", "fact", "object"}
        for item in longmem_data:
            facts = item["context_facts"]
            assert isinstance(facts, list) and len(facts) >= 1
            for fact in facts:
                assert set(fact.keys()) == required_fact_keys
                for k, v in fact.items():
                    assert isinstance(v, str) and len(v.strip()) > 0

    def test_ids_unique_and_formatted(self, longmem_data):
        ids = [item["id"] for item in longmem_data]
        assert len(ids) == len(set(ids)), "Duplicate IDs found in longmemeval fixture"
        for i, item in enumerate(longmem_data, 1):
            assert item["id"] == f"lme_{i:03d}"
