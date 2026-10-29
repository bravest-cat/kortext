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


def main():
    parser = argparse.ArgumentParser(
        prog="kortext",
        description="Kortext Knowledge OS: Cognitive Memory Hierarchy, AST Intelligence & Anti-Ripgrep Engine",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

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
    p_dream.add_argument("--purge-stale", action="store_true", default=True, help="Purge stale facts")
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

    # help
    p_help = subparsers.add_parser("help", help="Show this help message")
    p_help.set_defaults(func=lambda _: parser.print_help())

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
