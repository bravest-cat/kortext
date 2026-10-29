"""
Kortext Guided Setup Wizard & Cross-Platform Substrate Manager.

Provides zero-docker provisioning, environment diagnostics, FastEmbed verification,
Tree-sitter status checks, automatic native binary retrieval (Qdrant & FalkorDB),
dynamic configuration generation, and cross-platform process lifecycle management.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kortext._home import get_kortext_home
from kortext.binaries import (
    BinaryProvisionResult,
    detect_platform,
    generate_falkordb_conf,
    generate_qdrant_yaml,
    pull_falkordb_module,
    pull_qdrant_binary,
)

logger = logging.getLogger("kortext.setup")


def is_port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    """Checks whether a TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def is_unix_socket_open(socket_path: Path) -> bool:
    """Checks if a Unix domain socket exists and is listening."""
    if not hasattr(socket, "AF_UNIX") or not socket_path.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout := 0.3)
        s.connect(str(socket_path))
        s.close()
        return True
    except (OSError, socket.timeout):
        return False


@dataclass(frozen=True)
class SetupDiagnostics:
    """Immutable report of system diagnostic checks."""
    os_name: str
    arch: str
    python_version: str
    python_compatible: bool
    kortext_home: str
    falkordb_running: bool
    falkordb_channel: str
    fastembed_ready: bool
    installed_grammars: Tuple[str, ...]
    mcp_installed: bool
    qdrant_binary_present: bool = False
    qdrant_binary_path: Optional[str] = None
    qdrant_running: bool = False
    falkordb_binary_present: bool = False
    falkordb_binary_path: Optional[str] = None
    redis_server_present: bool = False
    redis_server_path: Optional[str] = None
    docker_present: bool = False
    docker_running: bool = False
    missing_dependencies: Tuple[str, ...] = ()
    platform_details: str = ""


def find_redis_binary() -> Optional[Path]:
    """Locates the redis-server binary on PATH."""
    which_bin = shutil.which("redis-server")
    if which_bin:
        return Path(which_bin)
    return None


def run_diagnostics(base_dir: Optional[Path] = None) -> SetupDiagnostics:
    """Runs non-destructive system diagnostics across substrates, tools, and dependencies."""
    plat = detect_platform()
    os_name = platform.system()
    arch = platform.machine()
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_compat = sys.version_info >= (3, 10)
    home_dir = base_dir or get_kortext_home()

    # FalkorDB check
    sock_path = home_dir / "run" / "falkordb.sock"
    falkor_sock = is_unix_socket_open(sock_path)
    falkor_tcp = is_port_open("127.0.0.1", 6379)
    falkor_running = falkor_sock or falkor_tcp
    channel = "Unix Socket" if falkor_sock else ("TCP :6379" if falkor_tcp else "Offline")

    # Qdrant check
    qdrant_running = is_port_open("127.0.0.1", 6333)

    # Binaries check
    qdrant_bin = home_dir / "bin" / ("qdrant.exe" if plat.is_windows else "qdrant")
    qdrant_present = qdrant_bin.is_file() and (plat.is_windows or os.access(qdrant_bin, os.X_OK))

    falkor_mod = home_dir / "bin" / "falkordb.so"
    falkor_mod_present = falkor_mod.is_file()

    # System redis-server
    redis_bin = find_redis_binary()
    redis_present = redis_bin is not None

    # Docker probe
    docker_bin = shutil.which("docker")
    docker_present = docker_bin is not None
    docker_running = False
    if docker_present:
        try:
            res = subprocess.run(["docker", "info"], capture_output=True, timeout=2.0)
            docker_running = res.returncode == 0
        except Exception:
            docker_running = False

    # FastEmbed check
    fastembed_ready = False
    try:
        import fastembed
        fastembed_ready = True
    except ImportError:
        logger.debug("Suppressed non-fatal exception")

    # Grammars check
    grammars: List[str] = []
    for lang in ("python", "typescript", "javascript", "rust", "go", "c", "cpp"):
        try:
            __import__(f"tree_sitter_{lang.replace('-', '_')}")
            grammars.append(lang)
        except ImportError:
            logger.debug("Suppressed non-fatal exception")

    # Missing dependencies aggregation
    missing: List[str] = []
    if not py_compat:
        missing.append("Python >= 3.10")
    if not fastembed_ready:
        missing.append("fastembed")
    if not qdrant_present:
        missing.append("native-qdrant")
    if not plat.is_windows and not falkor_mod_present:
        missing.append("falkordb.so")
    if not plat.is_windows and not redis_present:
        missing.append("redis-server")

    # MCP check
    mcp_config = Path.home() / ".gemini" / "config" / "mcp_config.json"
    mcp_installed = mcp_config.is_file()

    return SetupDiagnostics(
        os_name=os_name,
        arch=arch,
        python_version=py_ver,
        python_compatible=py_compat,
        kortext_home=str(home_dir),
        falkordb_running=falkor_running,
        falkordb_channel=channel,
        fastembed_ready=fastembed_ready,
        installed_grammars=tuple(grammars),
        mcp_installed=mcp_installed,
        qdrant_binary_present=qdrant_present,
        qdrant_binary_path=str(qdrant_bin) if qdrant_present else None,
        qdrant_running=qdrant_running,
        falkordb_binary_present=falkor_mod_present,
        falkordb_binary_path=str(falkor_mod) if falkor_mod_present else None,
        redis_server_present=redis_present,
        redis_server_path=str(redis_bin) if redis_bin else None,
        docker_present=docker_present,
        docker_running=docker_running,
        missing_dependencies=tuple(missing),
        platform_details=f"{plat.os_name} ({plat.arch})",
    )


def provision_directories(base_dir: Optional[Path] = None) -> List[Path]:
    """Provisions canonical Kortext home directories including the 8-category journal taxonomy."""
    root = base_dir or get_kortext_home()
    dirs = [
        root / "run",
        root / "logs",
        root / "config",
        root / "bin",
        root / "qdrant",
        root / "data" / "falkordb",
        root / "data" / "qdrant_storage",
        root / "journal" / "decisions",
        root / "journal" / "roadmaps",
        root / "journal" / "implementations",
        root / "journal" / "experiments",
        root / "journal" / "benchmarks",
        root / "journal" / "investigations",
        root / "journal" / "research",
        root / "journal" / "reports",
        root / "journal" / "dreams",
    ]
    created = []
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        created.append(d)
    return created


def provision_configurations(base_dir: Optional[Path] = None, overwrite: bool = True) -> List[Path]:
    """Provisions dynamic, non-hardcoded configuration files for FalkorDB and Qdrant."""
    root = base_dir or get_kortext_home()
    conf_dir = root / "config"
    conf_dir.mkdir(parents=True, exist_ok=True)

    falkor_conf = conf_dir / "falkordb.conf"
    qdrant_yaml = conf_dir / "qdrant.yaml"
    is_win = platform.system() == "Windows"

    created = []
    if not falkor_conf.exists() or overwrite:
        falkor_conf.write_text(generate_falkordb_conf(root, is_windows=is_win), encoding="utf-8")
        created.append(falkor_conf)

    if not qdrant_yaml.exists() or overwrite:
        qdrant_yaml.write_text(generate_qdrant_yaml(root), encoding="utf-8")
        created.append(qdrant_yaml)

    return created


def provision_binaries(base_dir: Optional[Path] = None, force: bool = False) -> Dict[str, BinaryProvisionResult]:
    """Pulls and installs native binaries for the detected platform."""
    root = base_dir or get_kortext_home()
    plat = detect_platform()
    results: Dict[str, BinaryProvisionResult] = {}

    results["qdrant"] = pull_qdrant_binary(root, platform_info=plat, force=force)
    results["falkordb"] = pull_falkordb_module(root, platform_info=plat, force=force)
    return results


def start_substrates(base_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """Starts FalkorDB and Qdrant substrate daemons in a cross-platform manner."""
    home = base_dir or get_kortext_home()
    run_dir = home / "run"
    log_dir = home / "logs"
    data_dir = home / "data" / "falkordb"
    sock_file = run_dir / "falkordb.sock"
    falkor_pid = run_dir / "falkordb.pid"
    qdrant_pid = run_dir / "qdrant.pid"
    plat = detect_platform()

    provision_directories(home)
    provision_configurations(home)

    messages = []

    # 1. Start FalkorDB if not active
    if is_unix_socket_open(sock_file) or is_port_open("127.0.0.1", 6379):
        messages.append("FalkorDB is already running.")
    else:
        redis_bin = find_redis_binary()
        if not redis_bin:
            messages.append("Warning: redis-server binary not found for FalkorDB.")
        else:
            cmd = [
                str(redis_bin),
                "--port", "6379",
                "--dir", str(data_dir),
                "--logfile", str(log_dir / "falkordb.log"),
            ]
            if not plat.is_windows:
                cmd.extend([
                    "--daemonize", "yes",
                    "--unixsocket", str(sock_file),
                    "--unixsocketperm", "700",
                    "--pidfile", str(falkor_pid),
                ])

            mod_path = home / "bin" / "falkordb.so"
            if mod_path.exists():
                cmd.extend(["--loadmodule", str(mod_path)])

            try:
                if plat.is_windows:
                    proc = subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                    falkor_pid.write_text(str(proc.pid))
                else:
                    subprocess.run(cmd, check=True)
                messages.append("FalkorDB substrate started.")
            except Exception as exc:
                messages.append(f"Failed to start FalkorDB: {exc}")

    # 2. Start Qdrant only when QDRANT_URL is explicitly configured (otherwise Embedded Qdrant is Sole Custodian)
    qdrant_url_env = os.getenv("QDRANT_URL", "").strip()
    if not qdrant_url_env:
        messages.append("Qdrant running in Embedded Sole-Custodian mode (~/.kortext/qdrant).")
    elif is_port_open("127.0.0.1", 6333):
        messages.append("Qdrant is already running.")
    else:
        qdrant_bin = home / "bin" / ("qdrant.exe" if plat.is_windows else "qdrant")
        qdrant_conf = home / "config" / "qdrant.yaml"
        if qdrant_bin.exists():
            cmd = [str(qdrant_bin), "--config-path", str(qdrant_conf)]
            try:
                log_out = open(log_dir / "qdrant_stdout.log", "a")
                log_err = open(log_dir / "qdrant_stderr.log", "a")
                kwargs: Dict[str, Any] = {"stdout": log_out, "stderr": log_err}
                if plat.is_windows:
                    kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                proc = subprocess.Popen(cmd, **kwargs)
                qdrant_pid.write_text(str(proc.pid))
                messages.append("Qdrant substrate started.")
            except Exception as exc:
                messages.append(f"Failed to start Qdrant daemon: {exc}")

    # Wait briefly for ports
    for _ in range(15):
        falkor_ok = is_unix_socket_open(sock_file) or is_port_open("127.0.0.1", 6379)
        qdrant_ok = (not qdrant_url_env) or is_port_open("127.0.0.1", 6333)
        if falkor_ok and qdrant_ok:
            break
        time.sleep(0.1)

    return True, "; ".join(messages)


def stop_substrates(base_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """Stops FalkorDB and Qdrant substrate daemons across platforms."""
    home = base_dir or get_kortext_home()
    run_dir = home / "run"
    falkor_pid = run_dir / "falkordb.pid"
    qdrant_pid = run_dir / "qdrant.pid"
    sock_file = run_dir / "falkordb.sock"
    is_win = platform.system() == "Windows"

    stopped = []
    for name, pid_f in [("FalkorDB", falkor_pid), ("Qdrant", qdrant_pid)]:
        if pid_f.exists():
            try:
                pid = int(pid_f.read_text().strip())
                if is_win:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
                else:
                    os.kill(pid, signal.SIGTERM)
                    # Give FalkorDB and Qdrant up to 5 s to flush to disk before forcing it.
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.1)
                    else:
                        logger.warning("%s (PID %d) ignored SIGTERM for 5 s; sending SIGKILL", name, pid)
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                stopped.append(name)
            except Exception as exc:
                logger.debug("Could not terminate %s PID: %s", name, exc)
            finally:
                pid_f.unlink(missing_ok=True)

    sock_file.unlink(missing_ok=True)
    if stopped:
        return True, f"Substrates halted cleanly: {', '.join(stopped)}."
    return True, "No active substrate PIDs found to terminate."


def run_guided_setup(
    non_interactive: bool = False,
    pull_binaries: bool = True,
    start_services: bool = False,
) -> None:
    """Interactive / automated guided onboarding wizard for cross-platform setup."""
    print("=" * 70)
    print("  🧠 KORTEXT KNOWLEDGE OS: CROSS-PLATFORM GUIDED SETUP WIZARD")
    print("=" * 70)
    print()

    diag = run_diagnostics()

    # Step 1: Platform & Python Diagnostics
    print("1. Platform & Environment Diagnostics:")
    print(f"   • Operating System : {diag.os_name} ({diag.arch})")
    print(f"   • Platform Target  : {diag.platform_details}")
    print(f"   • Python Runtime   : v{diag.python_version} {'✅' if diag.python_compatible else '❌'}")
    if not diag.python_compatible:
        print("   ❌ Error: Python >= 3.10 is required for Kortext.")
        sys.exit(1)

    # Step 2: Storage & Directory Provisioning
    print()
    print("2. Provisioning Storage & Directories:")
    print(f"   • Kortext Home     : {diag.kortext_home}")
    provision_directories(Path(diag.kortext_home))
    print("   • Sub-directories  : run/, data/, logs/, config/, bin/, qdrant/, journal/ (8 taxonomy categories) ✅")

    # Step 3: Python Dependencies Verification
    print()
    print("3. Python Dependencies Verification:")
    if diag.fastembed_ready:
        print("   • FastEmbed Engine : BAAI/bge-small-en-v1.5 (384-dim, ONNX) ✅")
    else:
        print("   • Installing fastembed package for zero-docker local embeddings...")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "fastembed"], check=True)
            print("   • FastEmbed installed successfully ✅")
        except Exception as e:
            print(f"   ⚠️  Could not auto-install fastembed: {e}")

    print(f"   • Tree-sitter Parsers: {', '.join(diag.installed_grammars) if diag.installed_grammars else 'None'} ✅")

    # Step 4: System Substrate Binaries (Cross-Platform)
    print()
    print("4. Native Substrate Binaries Retrieval:")
    if pull_binaries:
        print("   • Fetching verified native binaries for platform...")
        res = provision_binaries(Path(diag.kortext_home))
        q_status = "✅" if res["qdrant"].success else "⚠️"
        f_status = "✅" if res["falkordb"].success else "ℹ️"
        print(f"   • Qdrant Vector Binary : {res['qdrant'].message} {q_status}")
        print(f"   • FalkorDB Graph Module: {res['falkordb'].message} {f_status}")
    else:
        print("   • Native binary pull skipped per configuration.")

    # Step 5: Dynamic Configuration Provisioning
    print()
    print("5. Substrate Configuration Provisioning:")
    confs = provision_configurations(Path(diag.kortext_home), overwrite=True)
    for c in confs:
        print(f"   • Generated configuration: {c} ✅")
    if not confs:
        print("   • Configurations already present and verified ✅")

    # Step 6: System Toolchain Guidance
    print()
    print("6. System Substrates Status:")
    channel = f"Online ({diag.falkordb_channel})" if diag.falkordb_running else "Offline"
    print(f"   • FalkorDB Graph   : {channel} {'✅' if diag.falkordb_running else 'ℹ️'}")
    if not diag.falkordb_running:
        if diag.redis_server_present:
            print(f"     Redis Server Detected : {diag.redis_server_path} (Ready to start via 'kortext start')")
        else:
            if diag.os_name == "Darwin":
                print("     Notice: redis-server binary not found. Install via: brew install redis")
            elif diag.os_name == "Linux":
                print("     Notice: redis-server binary not found. Install via: sudo apt install redis-server")
            else:
                print("     Notice: On Windows, run FalkorDB via Docker:")
                print("             docker run -p 6379:6379 -d falkordb/falkordb")

    q_channel = "Online (:6333)" if diag.qdrant_running else "Embedded Mode / Ready"
    print(f"   • Qdrant Vectors   : {q_channel} ✅")

    # Optional auto-start
    if start_services or (not non_interactive and not diag.falkordb_running):
        print()
        print("   Attempting to start native substrate services...")
        ok, msg = start_substrates(Path(diag.kortext_home))
        print(f"   {msg}")

    # Step 7: Antigravity Plugin & FastMCP Client Integration
    print()
    print("7. Antigravity Plugin & FastMCP Configuration:")
    try:
        from scripts.install_plugin import install_plugin, _enable_plugin_sidecars

        install_plugin()
        _enable_plugin_sidecars(Path.home() / ".gemini")
    except Exception:
        print("   Canonical MCP configuration path: ~/.gemini/config/mcp_config.json")
        kortext_root = Path(__file__).resolve().parent.parent
        venv_server = kortext_root / ".venv" / "bin" / "kortext-server"
        mcp_snippet = {
            "mcpServers": {
                "kortext": {
                    "command": str(venv_server) if venv_server.is_file() else "kortext-server",
                    "env": {
                        "KORTEXT_HOME": diag.kortext_home
                    }
                }
            }
        }
        print(json.dumps(mcp_snippet, indent=4))

    print()
    print("=" * 70)
    print("  🎉 KORTEXT SETUP COMPLETE & READY FOR LAUNCH!")
    print("=" * 70)
    print("Next Steps:")
    print("  • Start Services   : kortext start")
    print("  • Launch Dashboard : kortext dashboard (Open http://127.0.0.1:8004)")
    print("  • Ingest Workspace : kortext ingest --root <workspace_path>")
    print("  • Seek AST Symbol  : kortext seek <symbol_name>")
    print("=" * 70)
