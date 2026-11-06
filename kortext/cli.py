"""
Kortext Unified CLI: The Cognitive Knowledge OS & Code Intelligence Gateway.
"""

from __future__ import annotations
import logging
logger = logging.getLogger(__name__)

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from kortext.symbols.seek import SymbolSeekEngine
from kortext.dream import DreamEngine

ALLOWED_GRAMMARS = (
    "cpp",
    "jvm",
    "swift",
    "csharp",
    "all",
    "c",
    "java",
    "kotlin",
    "rust",
    "python",
    "typescript",
    "javascript",
    "go",
)


def cmd_status(args):
    """Checks native substrate health across FalkorDB, Qdrant, and Embedder."""
    from kortext.setup import run_diagnostics
    from kortext._qdrant import is_qdrant_online
    from kortext.retrieval.discovery import discover_vector_collections

    diag = run_diagnostics()
    qdrant_http_up = bool(diag.qdrant_running or is_qdrant_online())
    if qdrant_http_up:
        qdrant_status = "✅ ONLINE (HTTP :6333)"
    else:
        try:
            cols = discover_vector_collections()
            col_names = [c["name"] for c in cols if isinstance(c, dict) and "name" in c]
        except Exception:
            col_names = []
        qdir = Path(diag.kortext_home) / "qdrant"
        if col_names:
            qdrant_status = f"✅ EMBEDDED ({len(col_names)} collections: {', '.join(col_names)})"
        elif (qdir / "meta.json").is_file() or qdir.is_dir():
            qdrant_status = "✅ EMBEDDED (FastMCP Sole Custodian)"
        else:
            qdrant_status = "❌ OFFLINE"

    print("=== Kortext Substrate Health (Zero-Docker) ===")
    print(f"  Platform               : {diag.os_name} ({diag.arch})")
    print(f"  FalkorDB (Graph Memory): {'✅ ONLINE (' + diag.falkordb_channel + ')' if diag.falkordb_running else '❌ OFFLINE'}")
    print(f"  FastEmbed (Embedder)   : {'✅ READY (BAAI/bge-small-en-v1.5)' if diag.fastembed_ready else '⚠️  NOT INSTALLED'}")
    print(f"  Qdrant   (Vector Memory): {qdrant_status}")
    print(f"  Parsers  (Tree-sitter) : {', '.join(diag.installed_grammars) if diag.installed_grammars else 'None'}")
    print(f"  Home Dir (KORTEXT_HOME): {diag.kortext_home}")


def cmd_setup(args):
    """Guided Setup Wizard for cross-platform zero-docker installation."""
    from kortext.setup import run_guided_setup
    pull_binaries = not getattr(args, "skip_binaries", False)
    start_services = getattr(args, "start", False)
    run_guided_setup(
        non_interactive=getattr(args, "yes", False),
        pull_binaries=pull_binaries,
        start_services=start_services,
    )


def cmd_start(args):
    """Starts native FalkorDB and embedded substrates cross-platform."""
    import platform
    if platform.system() == "Windows":
        from kortext.setup import start_substrates
        success, msg = start_substrates()
        print(f"[{'SUCCESS' if success else 'NOTICE'}] {msg}")
    else:
        import subprocess
        script = Path(__file__).resolve().parent.parent / "scripts" / "start_kortext.sh"
        subprocess.run(["bash", str(script)], check=True)


def cmd_stop(args):
    """Stops native FalkorDB and embedded substrates cross-platform."""
    import platform
    if platform.system() == "Windows":
        from kortext.setup import stop_substrates
        success, msg = stop_substrates()
        print(f"[{'SUCCESS' if success else 'NOTICE'}] {msg}")
    else:
        import subprocess
        script = Path(__file__).resolve().parent.parent / "scripts" / "stop_kortext.sh"
        subprocess.run(["bash", str(script)], check=True)


def cmd_install(args):
    """Installs plugin, skills, agents, and substrate daemons."""
    import subprocess
    script = Path(__file__).resolve().parent.parent / "scripts" / "install_plugin.py"
    cmd = [sys.executable, str(script)]
    if getattr(args, "all", False):
        cmd.append("--all")
    subprocess.run(cmd, check=True)


def cmd_seek(args):
    """Deterministic Anti-Ripgrep AST Symbol Seek."""
    seeker = SymbolSeekEngine()
    result = seeker.seek_symbol(args.symbol)
    print(result)


def cmd_impact(args):
    """Multi-Hop Call Graph Blast Radius Traverser."""
    seeker = SymbolSeekEngine()
    direction = getattr(args, "direction", "upstream")
    result = seeker.trace_impact(args.symbol, depth=args.depth, direction=direction)

    output_json = getattr(args, "json", False)
    if not output_json:
        try:
            data = json.loads(result) if isinstance(result, str) else result
            if isinstance(data, dict) and "formatted_summary" in data:
                print(data["formatted_summary"])
                return
        except Exception:
            logger.debug("Suppressed non-fatal exception")
    print(result)


def cmd_init(args):
    """One-command initialization replacing setup, install, start, and index."""
    from kortext._home import get_kortext_home
    from kortext.setup import (
        provision_directories,
        provision_configurations,
        provision_binaries,
        start_substrates,
        run_diagnostics,
    )
    from kortext.ingest import run_cli_ingest

    target_path = Path(getattr(args, "path", None) or os.getcwd()).resolve()
    skip_binaries = getattr(args, "skip_binaries", False)
    no_plugin = getattr(args, "no_plugin", False)

    # 1. Setup
    diag = run_diagnostics()
    if not diag.python_compatible:
        print(f"Setup failed: Python {diag.python_version} is unsupported. Python >= 3.10 is required. Upgrade Python to continue.")
        sys.exit(1)

    home = get_kortext_home()
    provision_directories(home)
    provision_configurations(home)
    if not skip_binaries:
        try:
            provision_binaries(home)
        except Exception as exc:
            logger.debug("Binary provisioning note: %s", exc)
    print(f"Step 1/4: Setup complete (directories and configuration at {home}).")

    # 2. Install plugin
    if no_plugin:
        print("Step 2/4: Plugin install skipped (--no-plugin).")
    else:
        try:
            from scripts.install_plugin import install_plugin, _enable_plugin_sidecars
            install_plugin()
            _enable_plugin_sidecars(Path.home() / ".gemini")
            print("Step 2/4: Plugin installed and synchronized to ~/.gemini.")
        except Exception as exc:
            print(f"Plugin install failed: {exc}. Fix: run 'kortext install' manually or inspect permissions.")
            sys.exit(1)

    # 3. Start substrates
    try:
        success, msg = start_substrates(home)
        print(f"Step 3/4: Substrates running ({msg}).")
    except Exception as exc:
        print(f"Substrate start failed: {exc}. Fix: run 'kortext start' or check redis-server.")
        sys.exit(1)

    # 4. Index target
    try:
        run_cli_ingest(str(target_path), force=False)
        print(f"Step 4/4: Indexed workspace at {target_path}.")
    except Exception as exc:
        print(f"Indexing failed: {exc}. Fix: run 'kortext index {target_path} --force' after checking substrates.")
        sys.exit(1)

    print()
    print(f"Kortext initialization complete for {target_path}.")
    print("Next steps:")
    print("  - Run 'kortext doctor' to diagnose system health.")
    print("  - Run 'kortext seek <symbol>' to inspect symbol declarations.")
    print("  - Run 'kortext affected' to check test impact of changes.")
    print("  - Start visual dashboard with 'kortext dashboard'.")


def cmd_doctor(args):
    """Diagnoses an install across substrates, plugin, cache, and agy."""
    from kortext.doctor import run_doctor, format_doctor_report

    output_json = getattr(args, "json", False)
    checks = run_doctor()

    if output_json:
        print(json.dumps(checks, indent=2))
    else:
        print(format_doctor_report(checks))

    has_failure = any(c.get("status") == "fail" for c in checks)
    if has_failure:
        sys.exit(1)


def cmd_savings(args):
    """Report token savings telemetry and upper-bound savings estimate."""
    from kortext.savings import get_savings_summary

    days = getattr(args, "days", 30) or 30
    project = getattr(args, "project", None)
    summary = get_savings_summary(days=days, project=project)

    output_json = getattr(args, "json", False)
    if output_json:
        print(json.dumps(summary, indent=2))
        return

    proj_label = f" for project '{project}'" if project else ""
    print(f"=== Kortext Token Savings Report (Last {days} days{proj_label}) ===")
    counts = summary.get("counts_by_kind") or {}
    if counts:
        print("Counts by kind:")
        for kind, cnt in sorted(counts.items()):
            print(f"  - {kind}: {cnt}")
    else:
        print("Counts by kind: None recorded")
    print(f"Reply bytes served: {summary.get('reply_bytes_served', 0):,} bytes")
    print(f"ESTIMATE tokens saved: {summary.get('estimate_tokens_saved', 0):,} tokens")
    print("Note: This ESTIMATE assumes the agent would otherwise have read those files whole, so it is an upper bound.")


def cmd_affected(args):
    """Find tests affected by git changes against base commit/branch."""
    from kortext.affected import find_affected_tests

    base = getattr(args, "base", "HEAD") or "HEAD"
    root = getattr(args, "root", None)
    res = find_affected_tests(workspace_root=root, base=base)

    output_json = getattr(args, "json", False)
    if output_json:
        print(json.dumps(res, indent=2))
        return

    if not res.get("complete"):
        print("Affected tests (INCOMPLETE - run full test suite):")
        print(f"Reason: {res.get('reason')}")
        if res.get("test_files"):
            print("Partially identified test files:")
            for tf in res["test_files"]:
                print(f"  - {tf}")
        print(f"Suggested command: {res.get('command')}")
        return

    test_files = res.get("test_files") or []
    if not test_files:
        print("No affected tests identified for these changes.")
        return

    print("Affected tests:")
    for item in res.get("affected_tests", []):
        f = item.get("file")
        funcs = item.get("functions") or []
        print(f"  {f}:")
        if funcs:
            for fn in funcs:
                print(f"    - {fn}")
        else:
            print("    - (whole file)")

    cmd = res.get("command")
    if cmd:
        print()
        print("Ready-to-run command:")
        print(f"  {cmd}")


def cmd_dream(args):
    """Cognitive Dream Mode REM Consolidation."""
    engine = DreamEngine()
    res = asyncio.run(engine.run_dream_cycle(
        project=args.project,
        purge_stale=args.purge_stale,
        dry_run=args.dry_run,
    ))
    print(res["markdown_report"])


def cmd_ingest(args):
    """Batch AST Ingestion."""
    from kortext.ingest import run_cli_ingest
    run_cli_ingest(args.root or os.getcwd(), force=args.force)


def cmd_server(args):
    """Run FastMCP Server."""
    from kortext.server import main as server_main
    server_main()


def cmd_dashboard(args):
    """Run Kortext Dashboard."""
    from kortext.dashboard.app import main as dashboard_main
    dashboard_main()


def cmd_grammars(args):
    """Manage Tree-sitter AST language grammars."""
    import subprocess
    from kortext.chunking.languages import print_language_status

    enable = getattr(args, "enable", None)
    disable = getattr(args, "disable", None)

    mapping = {
        "cpp": ["tree-sitter-c", "tree-sitter-cpp"],
        "jvm": ["tree-sitter-java", "tree-sitter-kotlin"],
        "swift": ["tree-sitter-swift"],
        "csharp": ["tree-sitter-c-sharp"],
        "c": ["tree-sitter-c"],
        "java": ["tree-sitter-java"],
        "kotlin": ["tree-sitter-kotlin"],
        "rust": ["tree-sitter-rust"],
        "python": ["tree-sitter-python"],
        "typescript": ["tree-sitter-typescript"],
        "javascript": ["tree-sitter-javascript"],
        "go": ["tree-sitter-go"],
        "all": [
            "tree-sitter-c",
            "tree-sitter-cpp",
            "tree-sitter-java",
            "tree-sitter-kotlin",
            "tree-sitter-swift",
            "tree-sitter-c-sharp",
        ],
    }

    if enable:
        norm_enable = str(enable).strip().lower()
        if norm_enable not in ALLOWED_GRAMMARS:
            raise ValueError(f"Unsupported grammar '{enable}'. Allowed choices: {list(ALLOWED_GRAMMARS)}")
        pkgs = mapping[norm_enable]
        try:
            subprocess.run(["uv", "pip", "install", *pkgs], check=True)
        except FileNotFoundError:
            subprocess.run([sys.executable, "-m", "pip", "install", *pkgs], check=True)
    elif disable:
        norm_disable = str(disable).strip().lower()
        if norm_disable not in ALLOWED_GRAMMARS or norm_disable == "all":
            raise ValueError(f"Unsupported grammar '{disable}'. Allowed choices: {[g for g in ALLOWED_GRAMMARS if g != 'all']}")
        pkgs = mapping.get(norm_disable, [f"tree-sitter-{norm_disable}"])
        try:
            subprocess.run(["uv", "pip", "uninstall", *pkgs], check=True)
        except FileNotFoundError:
            subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", *pkgs], check=True)

    print_language_status()


def cmd_scip(args):
    """Ingest SCIP Protobuf index into FalkorDB."""
    from kortext.symbols.scip import auto_index_and_ingest, SCIPImporter

    target = getattr(args, "target", ".") or "."
    default_graph = os.getenv("KORTEXT_SYMBOLS_GRAPH", os.getenv("FALKORDB_SYMBOLS_GRAPH", "kortext_symbols"))
    graph = getattr(args, "graph", default_graph) or default_graph
    purge_existing = not getattr(args, "no_purge", False)

    if getattr(args, "auto", False):
        result = auto_index_and_ingest(target, graph_name=graph, purge_existing=purge_existing)
        if result.get("status") == "missing_indexer":
            print(f"⚠️  {result.get('message', 'SCIP indexer not found')}")
            if result.get("install_hint"):
                print(f"   Install it via: {result['install_hint']}")
            return
        elif result.get("status") == "error":
            print(f"❌ Error: {result.get('message', 'SCIP auto-indexing failed')}")
            return
    else:
        importer = SCIPImporter(graph_name=graph)
        result = importer.ingest_index(target, purge_existing=purge_existing)

    print(
        f"✅ SCIP Ingestion Complete: {result.get('documents_indexed', 0)} documents, "
        f"{result.get('symbols_indexed', 0)} symbols, {result.get('references_indexed', 0)} references, "
        f"{result.get('relationships_indexed', 0)} inter-symbol relations in {result.get('duration_seconds', 0)}s."
    )


def cmd_clean_graphs(args):
    """Purge lingering test graphs from FalkorDB."""
    from kortext.falkordb_client import get_shared_falkordb_client

    try:
        r = get_shared_falkordb_client().get_redis()
        prod_graphs = {"kortext", "kortext_symbols", "antigravity_docs"}
        stale_names = {
            "cerebrum",
            "cerebrum_symbols",
            "my_custom_collection_symbols",
            "custom_microservice_symbols",
            "symbols",
            "episodic",
        }
        all_graphs = r.execute_command("GRAPH.LIST") or []
        graphs = [
            g for g in all_graphs
            if str(g) not in prod_graphs
            and ("test" in str(g).lower() or str(g) in stale_names)
        ]
        for g in graphs:
            r.execute_command("GRAPH.DELETE", g)
        print(f"✅ Deleted {len(graphs)} test graph(s): {graphs}")
    except Exception as exc:
        print(f"❌ Failed to clean test graphs: {exc}")


def cmd_watch(args):
    """Start real-time incremental file watcher daemon."""
    root = getattr(args, "root", ".") or "."
    collection = getattr(args, "collection", None)
    try:
        from kortext.watcher import run_watcher
        kwargs = {"watch_root": root}
        if collection:
            kwargs["collection"] = collection
        run_watcher(**kwargs)
    except (ImportError, AttributeError):
        from kortext.watcher import start_watcher, COLLECTION
        coll = collection or COLLECTION
        asyncio.run(start_watcher(watch_root=root, collection=coll))


def cmd_benchmark(args):
    """Run AST and vector benchmark suite against FalkorDB."""
    from kortext.benchmarks.runner import run_benchmark
    run_benchmark(
        root=getattr(args, "root", None),
        k=getattr(args, "k", 3),
        out_path=getattr(args, "out", None),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kortext",
        description="Kortext Knowledge OS: Cognitive Memory Hierarchy, AST Intelligence & Anti-Ripgrep Engine",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # init
    p_init = subparsers.add_parser("init", help="One-command initialization (setup, install, start, index)")
    p_init.add_argument("path", nargs="?", default=".", help="Workspace path to initialize and index (default: '.')")
    p_init.add_argument("--no-plugin", action="store_true", help="Skip Antigravity plugin installation")
    p_init.add_argument("--skip-binaries", action="store_true", help="Skip downloading substrate binaries")
    p_init.set_defaults(func=cmd_init)

    # doctor
    p_doctor = subparsers.add_parser("doctor", help="Diagnose Kortext installation health")
    p_doctor.add_argument("--json", action="store_true", help="Output diagnostic report in JSON format")
    p_doctor.set_defaults(func=cmd_doctor)

    # savings
    p_savings = subparsers.add_parser("savings", help="Report token savings telemetry and estimates")
    p_savings.add_argument("--days", type=int, default=30, help="Number of trailing days to inspect (default: 30)")
    p_savings.add_argument("--project", default=None, help="Filter savings by project name")
    p_savings.add_argument("--json", action="store_true", help="Output savings summary in JSON format")
    p_savings.set_defaults(func=cmd_savings)

    # affected
    p_affected = subparsers.add_parser("affected", help="Find tests affected by git changes")
    p_affected.add_argument("--base", default="HEAD", help="Base commit or branch to diff against (default: HEAD)")
    p_affected.add_argument("--root", default=None, help="Root directory path (default: cwd)")
    p_affected.add_argument("--json", action="store_true", help="Output affected tests in JSON format")
    p_affected.set_defaults(func=cmd_affected)

    # setup
    p_setup = subparsers.add_parser("setup", help="Interactive guided setup wizard for zero-docker onboarding")
    p_setup.add_argument("--yes", "-y", action="store_true", help="Non-interactive setup with default values")
    p_setup.add_argument("--skip-binaries", action="store_true", help="Skip pulling native substrate binaries (Qdrant/FalkorDB)")
    p_setup.add_argument("--start", action="store_true", help="Automatically start substrate services after setup")
    p_setup.set_defaults(func=cmd_setup)

    # start
    p_start = subparsers.add_parser("start", help="Start native substrates (FalkorDB, Embedded Qdrant)")
    p_start.set_defaults(func=cmd_start)

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop native substrates")
    p_stop.set_defaults(func=cmd_stop)

    # install
    p_install = subparsers.add_parser("install", help="Install Antigravity plugins, skills, and daemons")
    p_install.add_argument("--all", action="store_true", help="Install all integrations, LaunchAgents, and daemons")
    p_install.set_defaults(func=cmd_install)

    # status
    p_status = subparsers.add_parser("status", help="Check status of memory engines")
    p_status.set_defaults(func=cmd_status)

    # seek
    p_seek = subparsers.add_parser("seek", help="Deterministic AST symbol seek")
    p_seek.add_argument("symbol", help="Symbol identifier name")
    p_seek.set_defaults(func=cmd_seek)

    # impact
    p_impact = subparsers.add_parser("impact", help="Trace caller blast radius")
    p_impact.add_argument("symbol", help="Symbol identifier name")
    p_impact.add_argument("--depth", type=int, default=3, help="Max traversal depth")
    p_impact.add_argument("--direction", default="upstream", choices=["upstream", "downstream"], help="Traversal direction: upstream (callers) or downstream (callees)")
    p_impact.add_argument("--json", action="store_true", help="Output raw JSON instead of formatted markdown summary")
    p_impact.set_defaults(func=cmd_impact)

    # dream
    p_dream = subparsers.add_parser("dream", help="Trigger Cognitive Dream Mode")
    p_dream.add_argument("--project", default="kortext", help="Project label")
    p_dream.add_argument("--purge-stale", action=argparse.BooleanOptionalAction, default=True, help="Purge stale facts")
    p_dream.add_argument("--dry-run", action="store_true", help="Preview without modifying")
    p_dream.set_defaults(func=cmd_dream)

    # ingest / index
    p_ingest = subparsers.add_parser("ingest", aliases=["index"], help="Batch ingest code into Qdrant & FalkorDB")
    p_ingest.add_argument("--root", default=None, help="Root directory path")
    p_ingest.add_argument("--force", action="store_true", help="Force re-indexing")
    p_ingest.set_defaults(func=cmd_ingest)

    # server
    p_server = subparsers.add_parser("server", help="Start FastMCP server daemon")
    p_server.set_defaults(func=cmd_server)

    # dashboard
    p_dash = subparsers.add_parser("dashboard", help="Start visual dashboard on :8004")
    p_dash.set_defaults(func=cmd_dashboard)

    # grammars
    p_grammars = subparsers.add_parser("grammars", help="Manage Tree-sitter language grammars")
    p_grammars.add_argument("--enable", choices=list(ALLOWED_GRAMMARS), help="Language or family to enable")
    p_grammars.add_argument("--disable", choices=[g for g in ALLOWED_GRAMMARS if g != "all"], help="Language grammar to uninstall")
    p_grammars.add_argument("--status", action="store_true", help="Display language status")
    p_grammars.set_defaults(func=cmd_grammars)

    # scip
    default_scip_graph = os.getenv("KORTEXT_SYMBOLS_GRAPH", os.getenv("FALKORDB_SYMBOLS_GRAPH", "kortext_symbols"))
    p_scip = subparsers.add_parser("scip", help="Ingest SCIP Protobuf index into FalkorDB")
    p_scip.add_argument("target", nargs="?", default=".", help="Path to index.scip or project root (default: '.')")
    p_scip.add_argument("--auto", action="store_true", help="Auto-detect project type and generate index.scip if missing")
    p_scip.add_argument("--graph", default=default_scip_graph, help=f"FalkorDB symbols graph name (default: '{default_scip_graph}')")
    p_scip.add_argument("--no-purge", action="store_true", help="Do not purge previous file relations")
    p_scip.set_defaults(func=cmd_scip)

    # clean-graphs
    p_clean = subparsers.add_parser("clean-graphs", help="Purge lingering test graphs from FalkorDB")
    p_clean.set_defaults(func=cmd_clean_graphs)

    # watch
    p_watch = subparsers.add_parser("watch", help="Start real-time incremental file watcher daemon")
    p_watch.add_argument("--root", default=".", help="Directory to monitor (default: '.')")
    p_watch.add_argument("--collection", default=None, help="Qdrant collection name")
    p_watch.set_defaults(func=cmd_watch)

    # boost
    def cmd_boost(args):
        """Enrich a prompt with Tier 0 AST symbols, Tier 2 invariants, and active Task DAG context."""
        from kortext.server import boost_prompt_context
        fn = getattr(boost_prompt_context, "fn", boost_prompt_context)
        res = asyncio.run(
            fn(
                prompt=args.prompt,
                project=args.project,
                token_budget=args.budget,
                workspace_root=args.workspace_root,
            )
        )
        if getattr(args, "json", False):
            print(res)
            return
        try:
            payload = json.loads(res)
            print(payload.get("boosted_prompt", res))
        except Exception:
            print(res)

    p_boost = subparsers.add_parser("boost", help="Enrich a coding prompt with grounded AST, invariant, and task context")
    p_boost.add_argument("prompt", help="Raw coding prompt to enrich")
    p_boost.add_argument("--project", default=None, help="Project scope (default: auto-detected from workspace)")
    p_boost.add_argument("--budget", type=int, default=1200, help="Token budget (default: 1200)")
    p_boost.add_argument("--workspace-root", default=None, help="Workspace root directory")
    p_boost.add_argument("--json", action="store_true", help="Output full JSON payload instead of boosted prompt markdown")
    p_boost.set_defaults(func=cmd_boost)

    # memory
    def cmd_memory_rebuild(args):
        from kortext.scripts.sync_journal import sync_journal
        root_path = Path(args.root).resolve() if args.root else None
        asyncio.run(sync_journal(root=root_path, dry_run=args.dry_run))

    def cmd_memory_index(args):
        from kortext.vault import generate_memory_index, resolve_memory_dir
        root_path = Path(args.root).resolve() if args.root else resolve_memory_dir()
        idx_path = generate_memory_index(root_path)
        print(f"Memory index generated: {idx_path}")

    p_memory = subparsers.add_parser("memory", help="Memory vault utilities (rebuild, index)")
    p_mem_subs = p_memory.add_subparsers(dest="memory_subcommand", required=True)

    p_mem_rebuild = p_mem_subs.add_parser("rebuild", help="Rebuild FalkorDB episodic memory from Markdown journals")
    p_mem_rebuild.add_argument("--root", default=None, help="Root path of memory vault / journals")
    p_mem_rebuild.add_argument("--dry-run", action="store_true", help="Inspect entries without persisting to FalkorDB")
    p_mem_rebuild.set_defaults(func=cmd_memory_rebuild)

    p_mem_index = p_mem_subs.add_parser("index", help="Regenerate INDEX.md in memory vault")
    p_mem_index.add_argument("--root", default=None, help="Root path of memory vault / journals")
    p_mem_index.set_defaults(func=cmd_memory_index)

    # benchmark
    p_bench = subparsers.add_parser("benchmark", help="Run AST and vector benchmark suite against FalkorDB")
    p_bench.add_argument("--root", default=None, help="Root directory path to index and benchmark (default: repo root)")
    p_bench.add_argument("--k", type=int, default=3, help="Number of repetitions per task for pass@k / pass^k (default: 3)")
    p_bench.add_argument("--out", default=None, help="Output path for benchmark results JSON (default: data/benchmark_results.json)")
    p_bench.set_defaults(func=cmd_benchmark)

    # help
    p_help = subparsers.add_parser("help", help="Show this help message")
    p_help.set_defaults(func=lambda _: parser.print_help())

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
