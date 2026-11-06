import os
import tempfile
import time
import pytest
from pathlib import Path

from kortext.falkordb_client import get_shared_falkordb_client
from kortext.episodic.driver import AgentNativeEpisodicDriver, _check_symbols_staleness_batch
from kortext.ingest import index_single_file


@pytest.mark.asyncio
async def test_grounded_symbol_staleness_lifecycle_on_real_code():
    """
    Test: record a fact grounded to a real indexed function in a temp repo,
    change the function body, re-index with production ingest, search, and assert the marker;
    revert the body and assert it disappears.
    """
    test_id = f"test_{int(time.time())}"
    test_symbols_db = f"test_syms_{test_id}"
    test_episodic_db = f"test_ep_{test_id}"
    test_col = f"test_col_{test_id}"

    client = get_shared_falkordb_client()
    driver = AgentNativeEpisodicDriver(db_name=test_episodic_db)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        code_file = tmp_path / "finance.py"
        orig_code = (
            "def compute_interest(principal, rate):\n"
            "    # Base calculation\n"
            "    return principal * rate\n"
        )
        code_file.write_text(orig_code, encoding="utf-8")

        # 1. Index with production ingest
        await index_single_file(
            file_path=code_file,
            collection=test_col,
            target_symbols_db=test_symbols_db,
            force=True,
        )

        # 2. Record a fact referencing compute_interest
        driver.record_fact(
            subject="compute_interest",
            relation="calculates",
            fact="compute_interest calculates simple interest from principal and rate",
            project=test_id,
            symbols_graph=test_symbols_db,
        )

        # Search: initially fresh
        res_fresh = await driver.search("compute_interest simple interest", project=test_id, symbols_graph=test_symbols_db)
        assert len(res_fresh) > 0
        assert not any("[may be out of date:" in r for r in res_fresh)

        # 3. Change function body
        modified_code = (
            "def compute_interest(principal, rate):\n"
            "    # Updated formula\n"
            "    rate_multiplier = 1.05\n"
            "    return principal * rate * rate_multiplier\n"
        )
        code_file.write_text(modified_code, encoding="utf-8")

        # Re-index with production ingest
        await index_single_file(
            file_path=code_file,
            collection=test_col,
            target_symbols_db=test_symbols_db,
            force=True,
        )

        # Search: must be marked stale
        res_stale = await driver.search("compute_interest simple interest", project=test_id, symbols_graph=test_symbols_db)
        assert len(res_stale) > 0
        assert any("[may be out of date: compute_interest changed since this was recorded]" in r for r in res_stale)

        # 4. Revert function body
        code_file.write_text(orig_code, encoding="utf-8")
        await index_single_file(
            file_path=code_file,
            collection=test_col,
            target_symbols_db=test_symbols_db,
            force=True,
        )

        # Search: marker disappears
        res_reverted = await driver.search("compute_interest simple interest", project=test_id, symbols_graph=test_symbols_db)
        assert len(res_reverted) > 0
        assert not any("[may be out of date:" in r for r in res_reverted)

    # Cleanup test graphs
    try:
        r = client.get_redis()
        r.execute_command("GRAPH.DELETE", test_symbols_db)
        r.execute_command("GRAPH.DELETE", test_episodic_db)
    except Exception:
        pass


@pytest.mark.asyncio
async def test_list_memories_schema_and_staleness():
    """
    Tests list_memories returns the exact required schema:
    id, kind ('fact'|'episode'), text, project, trust_level, created_at, superseded, stale, stale_symbols.
    """
    test_id = f"test_list_{int(time.time())}"
    test_symbols_db = f"test_syms_{test_id}"
    test_episodic_db = f"test_ep_{test_id}"

    client = get_shared_falkordb_client()
    driver = AgentNativeEpisodicDriver(db_name=test_episodic_db)

    # Record an episode and a fact
    await driver.record_outcome(
        episode_name=f"ep_{test_id}",
        content="Refactored database connector to support connection pooling",
        project=test_id,
        trust_level="tool_confirmed",
        category="implementation",
    )
    driver.record_fact(
        subject="ConnectionPool",
        relation="manages",
        fact="ConnectionPool manages max 50 live sockets",
        project=test_id,
        trust_level="tool_confirmed",
    )

    memories = driver.list_memories(project=test_id, symbols_graph=test_symbols_db)
    assert len(memories) >= 2

    required_keys = {
        "id",
        "kind",
        "text",
        "project",
        "trust_level",
        "created_at",
        "superseded",
        "stale",
        "stale_symbols",
    }
    for m in memories:
        assert set(m.keys()) == required_keys
        assert m["kind"] in ("fact", "episode")
        assert isinstance(m["id"], str)
        assert isinstance(m["text"], str)
        assert isinstance(m["project"], str)
        assert isinstance(m["trust_level"], str)
        assert isinstance(m["created_at"], str)
        assert isinstance(m["superseded"], bool)
        assert isinstance(m["stale"], bool)
        assert isinstance(m["stale_symbols"], list)

    try:
        r = client.get_redis()
        r.execute_command("GRAPH.DELETE", test_episodic_db)
    except Exception:
        pass


def test_staleness_benchmark_10k_symbols_under_20ms():
    """
    Measures batched staleness lookup on a 10,000-symbol graph.
    Must add under 20 ms p95.
    """
    bench_graph = "bench_10k_symbols_test"
    client = get_shared_falkordb_client()
    r = client.get_redis()

    try:
        r.execute_command("GRAPH.DELETE", bench_graph)
    except Exception:
        pass

    # Create 10,000 symbols in chunks
    for chunk_start in range(0, 10000, 2000):
        cypher = (
            "UNWIND range($start, $end) AS i "
            "CREATE (:Symbol {name: 'sym_' + toString(i), qualified_name: 'pkg.mod.sym_' + toString(i), "
            "body_hash: 'hash_' + toString(i), file_path: '/repo/mod.py'})"
        )
        client.query(bench_graph, cypher, {"start": chunk_start, "end": chunk_start + 1999})

    # Benchmark batched staleness check for 5 candidates, each referencing 3 symbols
    grounded_test = {
        i: [
            {"symbol": f"pkg.mod.sym_{i*100 + j}", "name": f"sym_{i*100 + j}", "hash": f"hash_{i*100 + j}", "file": "/repo/mod.py"}
            for j in range(3)
        ]
        for i in range(5)
    }

    latencies = []
    for _ in range(50):
        t0 = time.perf_counter()
        res = _check_symbols_staleness_batch(client, grounded_test, symbols_graph=bench_graph)
        t1 = time.perf_counter()
        latencies.append((t1 - t0) * 1000.0)

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]

    try:
        r.execute_command("GRAPH.DELETE", bench_graph)
    except Exception:
        pass

    print(f"\n10k symbols staleness p50: {p50:.2f}ms, p95: {p95:.2f}ms")
    assert p95 < 20.0, f"Staleness check p95 {p95:.2f}ms exceeded 20ms limit"
