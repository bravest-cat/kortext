#!/usr/bin/env python3
"""
Kortext Antigravity Plugin Dynamic Installer.
Installs symlinks to Antigravity plugins without duplicating rules, skills, or manifests,
synchronizes mcp_config.json environment variables from .env on every run,
and wires Antigravity PreInvocation, PostInvocation, PreToolUse, PostToolUse, and Stop hooks.
"""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from typing import Dict

KORTEXT_ROOT = Path(__file__).resolve().parent.parent
INTEGRATIONS_DIR = KORTEXT_ROOT / "integrations" / "antigravity"
SCRIPTS_DIR = KORTEXT_ROOT / "scripts" / "hooks"

import sys
if str(KORTEXT_ROOT) not in sys.path:
    sys.path.insert(0, str(KORTEXT_ROOT))



def _load_dotenv_dict(env_path: Path) -> Dict[str, str]:
    """Parses key=value pairs from .env without mutating os.environ."""
    env_vars: Dict[str, str] = {}
    if not env_path.is_file():
        return env_vars
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip().strip("'").strip('"')
        if key:
            env_vars[key] = val
    return env_vars


def _require_redis() -> str:
    found = shutil.which("redis-server")
    if not found:
        print("⚠️ redis-server not found on PATH; the FalkorDB service will not start until it is installed.")
    return found or "redis-server"


def _venv_python() -> Path:
    scripts = "Scripts/python.exe" if os.name == "nt" else "bin/python3"
    return KORTEXT_ROOT / ".venv" / scripts


def build_hooks_dict(runner_python: str = "") -> dict:
    """Hook commands via run_hook.sh (symlinked dev plugin), or via the given python when copied (no sh needed)."""
    if runner_python:
        hook_cmd_prefix = f'"{runner_python}" -m kortext.hooks.runner'
    else:
        hook_cmd_prefix = 'sh "${KORTEXT_PLUGIN_DIR:-$HOME/.gemini/config/plugins/kortext}/run_hook.sh"'
    return {
        "kortext-ambient-grounding": {
            "PreInvocation": [
                {
                    "type": "command",
                    "command": f"{hook_cmd_prefix} pre_invoke",
                    "timeout": 5,
                }
            ],
        },
        "kortext-runaway-guard": {
            "PostInvocation": [
                {
                    "type": "command",
                    "command": f"{hook_cmd_prefix} post_invoke",
                    "timeout": 5,
                }
            ],
        },
        "kortext-safety-gate": {
            "PreToolUse": [
                {
                    "matcher": "(client:|mcp[:_]+kortext[:_]+|kortext:)?(run_command|grep_search|find_by_name|view_file|replace_file_content|multi_replace_file_content|write_to_file)",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{hook_cmd_prefix} pre_tool",
                            "timeout": 5,
                        }
                    ],
                }
            ]
        },
        "kortext-cache-invalidation": {
            "PostToolUse": [
                {
                    "matcher": "(client:|mcp[:_]+kortext[:_]+|kortext:)?(run_command|write_to_file|replace_file_content|multi_replace_file_content|seek_symbol|trace_symbol_impact|manage_tasks|record_system_outcome)",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{hook_cmd_prefix} post_tool",
                            "timeout": 10,
                        }
                    ],
                }
            ]
        },
        "kortext-dag-stop-guard": {
            "Stop": [
                {
                    "type": "command",
                    "command": f"{hook_cmd_prefix} stop",
                    "timeout": 15,
                }
            ]
        },
    }


def _cleanup_dangling_symlinks_and_duplicate_rules(home: Path) -> None:
    """
    Repairs any dangling GEMINI.md symlinks created by older installer versions
    and removes duplicate rule copies from ~/.gemini/config/rules/ and .agents/rules/
    (while preserving official Antigravity agents.json, skills.json, and rules.json manifests).
    """
    for candidate in [
        home / ".gemini" / "GEMINI.md",
        home / ".gemini" / "config" / "GEMINI.md",
        home / ".gemini" / "antigravity-cli" / "rules",
    ]:
        if candidate.is_symlink() and not candidate.exists():
            candidate.unlink()
            print(f"🧹 Removed dangling symlink: {candidate}")

    # Remove invalid .agents/rules/AGENTS.md placement
    invalid_agents_rule = KORTEXT_ROOT / ".agents" / "rules" / "AGENTS.md"
    if invalid_agents_rule.is_file() or invalid_agents_rule.is_symlink():
        invalid_agents_rule.unlink()
        print(f"🧹 Removed invalid duplicate rule file: {invalid_agents_rule}")

    rules_src = INTEGRATIONS_DIR / "rules"
    if rules_src.is_dir():
        plugin_rule_names = {f.name for f in rules_src.glob("*.md")}
        for dup_dir in [home / ".gemini" / "config" / "rules", KORTEXT_ROOT / ".agents" / "rules"]:
            if dup_dir.is_dir() and not dup_dir.is_symlink():
                for rule_name in plugin_rule_names:
                    dup_file = dup_dir / rule_name
                    if dup_file.is_file() or dup_file.is_symlink():
                        dup_file.unlink()
                        print(f"🧹 Removed duplicate rule copy: {dup_file}")

    # Remove duplicate workspace .agents/skills and .agents/agents copies that collide with plugin scope
    for ws_dup_dir in [
        KORTEXT_ROOT / ".agents" / "skills",
        KORTEXT_ROOT / ".agents" / "agents",
        KORTEXT_ROOT / ".agents" / "rules",
    ]:
        if ws_dup_dir.is_symlink():
            ws_dup_dir.unlink()
            print(f"🧹 Removed duplicate workspace symlink: {ws_dup_dir}")
        elif ws_dup_dir.is_dir():
            shutil.rmtree(ws_dup_dir)
            print(f"🧹 Removed duplicate workspace customization dir: {ws_dup_dir}")


def install_plugin():
    home = Path.home()
    plugin_name = "kortext"

    # Link into canonical ~/.gemini/config/plugins/kortext (falling back to copy on Windows without symlink privileges)
    canonical_plugin_dir = home / ".gemini" / "config" / "plugins" / plugin_name
    canonical_plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    if canonical_plugin_dir.is_symlink() or canonical_plugin_dir.is_file():
        canonical_plugin_dir.unlink()
    elif canonical_plugin_dir.is_dir():
        shutil.rmtree(canonical_plugin_dir)
    try:
        os.symlink(INTEGRATIONS_DIR, canonical_plugin_dir)
        print(f"📦 Linked {plugin_name} plugin to: {canonical_plugin_dir}")
    except OSError:
        shutil.copytree(INTEGRATIONS_DIR, canonical_plugin_dir, dirs_exist_ok=True)
        print(f"📦 Copied {plugin_name} plugin to: {canonical_plugin_dir} (symlink fallback)")

    # A copied plugin cannot rely on sh or run_hook.sh: call the venv python directly (the symlink case would edit the repo copy).
    if not canonical_plugin_dir.is_symlink():
        venv_python = _venv_python()
        runner_py = str(venv_python) if venv_python.is_file() else sys.executable
        (canonical_plugin_dir / "hooks.json").write_text(
            json.dumps(build_hooks_dict(runner_py), indent=2), encoding="utf-8"
        )

    # Remove duplicate CLI plugin symlink if present (Antigravity syncs plugins across surfaces natively)
    dup_cli_plugin = home / ".gemini" / "antigravity-cli" / "plugins" / plugin_name
    if dup_cli_plugin.is_symlink() or dup_cli_plugin.is_file():
        dup_cli_plugin.unlink()
        print(f"🧹 Removed duplicate CLI plugin symlink: {dup_cli_plugin}")

    # Clean up legacy cerebrum plugin
    for stale_parent in [home / ".gemini" / "config" / "plugins", home / ".gemini" / "antigravity-cli" / "plugins"]:
        stale_cerebrum = stale_parent / "cerebrum"
        if stale_cerebrum.is_symlink() or stale_cerebrum.is_file():
            stale_cerebrum.unlink()
            print(f"🧹 Cleaned legacy cerebrum plugin link: {stale_cerebrum}")
        elif stale_cerebrum.is_dir():
            shutil.rmtree(stale_cerebrum)
            print(f"🧹 Cleaned legacy cerebrum plugin directory: {stale_cerebrum}")

    hooks_file = INTEGRATIONS_DIR / "hooks.json"
    hooks_payload = build_hooks_dict()
    with open(hooks_file, "w", encoding="utf-8") as f:
        json.dump(hooks_payload, f, indent=2)

    mcp_config_src = INTEGRATIONS_DIR / "mcp_config.json"
    if mcp_config_src.is_file() or mcp_config_src.is_symlink():
        mcp_config_src.unlink()
        print(f"🧹 Cleaned plugin mcp_config.json: {mcp_config_src}")

    host_mcp_config = home / ".gemini" / "config" / "mcp_config.json"
    host_mcp_config.parent.mkdir(parents=True, exist_ok=True)
    if host_mcp_config.is_file():
        try:
            with open(host_mcp_config, "r", encoding="utf-8") as f:
                host_mcp_payload = json.load(f)
        except json.JSONDecodeError:
            host_mcp_payload = {}
    else:
        host_mcp_payload = {}

    if "mcpServers" not in host_mcp_payload or not isinstance(host_mcp_payload["mcpServers"], dict):
        host_mcp_payload["mcpServers"] = {}

    for stale_key in ["cerebrum_kortext", "kortext_kortext"]:
        if stale_key in host_mcp_payload["mcpServers"]:
            del host_mcp_payload["mcpServers"][stale_key]
            print(f"🧹 Removed stale server key: {stale_key}")

    kortext_bin = KORTEXT_ROOT / ".venv" / "bin" / "kortext-server"
    server_cmd = str(kortext_bin) if kortext_bin.is_file() else "kortext-server"

    dotenv_vals = _load_dotenv_dict(KORTEXT_ROOT / ".env")
    embed_base = dotenv_vals.get("EMBED_BASE_URL", "").strip()
    synced_env = {
        "WORKSPACE_ROOTS": dotenv_vals.get("WORKSPACE_ROOTS", str(KORTEXT_ROOT)),
        "LOCAL_LLM_BASE_URL": dotenv_vals.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8000/v1"),
        "EMBED_MODEL_NAME": dotenv_vals.get(
            "EMBED_MODEL_NAME",
            "mlx-community--Qwen3-Embedding-8B-4bit-DWQ" if embed_base else "BAAI/bge-small-en-v1.5",
        ),
        "RETRIEVE_RESULTS": dotenv_vals.get("RETRIEVE_RESULTS", "5"),
        "RETRIEVE_TOP_K": dotenv_vals.get("RETRIEVE_TOP_K", "30"),
        "FALKORDB_HOST": dotenv_vals.get("FALKORDB_HOST", "127.0.0.1"),
        "FALKORDB_PORT": dotenv_vals.get("FALKORDB_PORT", "6379"),
        "FALKORDB_DATABASE": dotenv_vals.get("FALKORDB_DATABASE", "kortext"),
        "FALKORDB_SOCKET": dotenv_vals.get("FALKORDB_SOCKET", str(home / ".kortext" / "run" / "falkordb.sock")),
        "QDRANT_COLLECTION": dotenv_vals.get("QDRANT_COLLECTION", "kortext_codebase"),
        "JOURNAL_DIR": dotenv_vals.get("JOURNAL_DIR", str(KORTEXT_ROOT / "journal")),
        "KORTEXT_HOME": dotenv_vals.get("KORTEXT_HOME", str(home / ".kortext")),
    }
    if embed_base:
        synced_env["EMBED_BASE_URL"] = embed_base
    if "QDRANT_URL" in dotenv_vals and dotenv_vals["QDRANT_URL"]:
        synced_env["QDRANT_URL"] = dotenv_vals["QDRANT_URL"]

    host_mcp_payload["mcpServers"]["kortext"] = {
        "command": server_cmd,
        "env": synced_env,
    }

    with open(host_mcp_config, "w", encoding="utf-8") as f:
        json.dump(host_mcp_payload, f, indent=2)
    print(f"🔌 Synchronized canonical kortext MCP server and .env in: {host_mcp_config}")

    # Verify sidecar manifests in integrations/antigravity/sidecars/*
    sidecars_src = INTEGRATIONS_DIR / "sidecars"
    if sidecars_src.is_dir():
        for sc_dir in sidecars_src.iterdir():
            if sc_dir.is_dir():
                src_sc_json = sc_dir / "sidecar.json"
                if src_sc_json.is_file():
                    with open(src_sc_json, "r", encoding="utf-8") as f:
                        sc_data = json.load(f)
                    assert ("command" in sc_data or "builtin" in sc_data) and "args" in sc_data, (
                        f"Invalid sidecar manifest: {src_sc_json}"
                    )
                    print(f"🚗 Verified portable sidecar manifest: {src_sc_json}")

    global_skills_dir = home / ".gemini" / "config" / "skills"
    if global_skills_dir.is_dir():
        skills_src = INTEGRATIONS_DIR / "skills"
        if skills_src.is_dir():
            for skill_dir in skills_src.iterdir():
                target_skill = global_skills_dir / skill_dir.name
                if target_skill.is_symlink():
                    target_skill.unlink()
                    print(f"🧹 Cleaned duplicate skill symlink: {skill_dir.name}")
                elif target_skill.is_dir() and not (target_skill / ".git").exists():
                    shutil.rmtree(target_skill)
                    print(f"🧹 Cleaned duplicate skill dir: {skill_dir.name}")

    # Clean duplicate kortext/cerebrum hook entries from global ~/.gemini/config/hooks.json
    global_hooks_file = home / ".gemini" / "config" / "hooks.json"
    if global_hooks_file.is_file():
        try:
            g_hooks = json.loads(global_hooks_file.read_text(encoding="utf-8"))
            if isinstance(g_hooks, dict):
                stale_hook_keys = [
                    k for k in g_hooks
                    if k.startswith(("kortext-", "cerebrum-", "cortex-"))
                ]
                if stale_hook_keys:
                    for k in stale_hook_keys:
                        del g_hooks[k]
                    global_hooks_file.write_text(json.dumps(g_hooks, indent=2) + "\n", encoding="utf-8")
                    print(f"🧹 Removed duplicate global hooks from {global_hooks_file}: {stale_hook_keys}")
        except Exception as exc:
            print(f"⚠️ Could not clean global hooks.json: {exc}")

    # Persist ~/.kortext/run/runtime.env so plugin hooks and sidecars resolve KORTEXT_ROOT even when copied via agy plugin install
    runtime_env_dir = home / ".kortext" / "run"
    runtime_env_dir.mkdir(parents=True, exist_ok=True)
    venv_py = KORTEXT_ROOT / ".venv" / "bin" / "python3"
    py_exec = str(venv_py) if venv_py.is_file() else "python3"
    (runtime_env_dir / "runtime.env").write_text(
        f'export KORTEXT_ROOT="{KORTEXT_ROOT}"\nexport KORTEXT_PYTHON="{py_exec}"\n',
        encoding="utf-8",
    )

    _cleanup_dangling_symlinks_and_duplicate_rules(home)

    if SCRIPTS_DIR.is_dir():
        for script in SCRIPTS_DIR.glob("*.py"):
            st = os.stat(script)
            os.chmod(script, st.st_mode | stat.S_IEXEC)


def install_all():
    home = Path.home()
    kortext_home = home / ".kortext"
    for d in ["bin", "run", "logs", "config", "data/falkordb", "qdrant", "handoffs"]:
        (kortext_home / d).mkdir(parents=True, exist_ok=True)
    print(f"📦 Provisioned storage directories in {kortext_home}")

    try:
        from kortext.setup import provision_binaries, provision_configurations

        bin_results = provision_binaries(kortext_home)
        for _, b_res in bin_results.items():
            if b_res.success:
                print(f"📦 {b_res.message}")
            else:
                print(f"ℹ️ {b_res.message}")
        provision_configurations(kortext_home, overwrite=True)
    except Exception as exc:
        print(f"⚠️ Binary/config provisioning fallback: {exc}")
        bin_so = kortext_home / "bin" / "falkordb.so"
        if not bin_so.is_file():
            build_script = KORTEXT_ROOT / "scripts" / "build_falkordb_native.sh"
            subprocess.run(["bash", str(build_script)], check=False)

    import platform

    sys_name = platform.system()
    if sys_name == "Darwin":
        launch_dir = home / "Library" / "LaunchAgents"
        launch_dir.mkdir(parents=True, exist_ok=True)

        # Unload and remove legacy *cerebrum* and redundant watcher LaunchAgents (PostToolUse hook handles incremental re-indexing)
        for stale_plist in list(launch_dir.glob("*cerebrum*.plist")) + list(launch_dir.glob("*kortext.watcher*.plist")):
            try:
                subprocess.run(["launchctl", "unload", str(stale_plist)], check=False, capture_output=True)
                stale_plist.unlink(missing_ok=True)
                print(f"🧹 Unloaded and removed redundant/legacy LaunchAgent: {stale_plist.name}")
            except Exception as e:
                print(f"⚠️ Could not remove {stale_plist}: {e}")

        daemons_dir = INTEGRATIONS_DIR / "daemons"
        if daemons_dir.is_dir():
            venv_py = KORTEXT_ROOT / ".venv" / "bin" / "python3"
            py_exec = str(venv_py) if venv_py.is_file() else sys.executable
            redis_bin = _require_redis()
            dotenv_vals = _load_dotenv_dict(KORTEXT_ROOT / ".env")
            embedded_qdrant = not bool(dotenv_vals.get("QDRANT_URL", "").strip())
            for plist in daemons_dir.glob("*.plist"):
                target_plist = launch_dir / plist.name
                if plist.name == "com.kortext.watcher.plist" or (embedded_qdrant and plist.name == "ai.kortext.qdrant.plist"):
                    if target_plist.exists():
                        subprocess.run(["launchctl", "unload", str(target_plist)], check=False, capture_output=True)
                        target_plist.unlink(missing_ok=True)
                    print(f"🔒 Skipped {plist.name} (PostToolUse hook & Sole Custodian active)")
                    continue
                content = (
                    plist.read_text(encoding="utf-8")
                    .replace("{{HOME}}", str(home))
                    .replace("{{PROJECT_DIR}}", str(KORTEXT_ROOT))
                    .replace("{{PYTHON_BIN}}", py_exec)
                    .replace("{{REDIS_BIN}}", redis_bin)
                )
                target_plist.write_text(content, encoding="utf-8")
                print(f"📦 Installed substrate LaunchAgent: {plist.name}")
    elif sys_name == "Linux":
        systemd_dir = home / ".config" / "systemd" / "user"
        systemd_dir.mkdir(parents=True, exist_ok=True)
        venv_py = KORTEXT_ROOT / ".venv" / "bin" / "python3"
        py_exec = str(venv_py) if venv_py.is_file() else sys.executable
        redis_bin = _require_redis()
        falkor_unit = systemd_dir / "kortext-falkordb.service"
        falkor_unit.write_text(
            f"[Unit]\nDescription=Kortext FalkorDB Graph Substrate\nAfter=network.target\n\n"
            f"[Service]\nExecStart={redis_bin} {home}/.kortext/config/falkordb.conf --daemonize no\n"
            f"WorkingDirectory={home}/.kortext\nRestart=on-failure\n\n[Install]\nWantedBy=default.target\n",
            encoding="utf-8",
        )
        dash_unit = systemd_dir / "kortext-dashboard.service"
        dash_unit.write_text(
            f"[Unit]\nDescription=Kortext Web Dashboard\nAfter=network.target\n\n"
            f"[Service]\nExecStart={py_exec} -m kortext.dashboard.app\n"
            f"WorkingDirectory={KORTEXT_ROOT}\nEnvironment=PYTHONPATH={KORTEXT_ROOT}\nRestart=always\n\n"
            f"[Install]\nWantedBy=default.target\n",
            encoding="utf-8",
        )
        print(f"📦 Installed Linux systemd user units in: {systemd_dir}")

    install_plugin()


def _enable_plugin_sidecars(gemini_root: Path) -> None:
    """Configures kortext sidecars in ~/.gemini/config/config.json: dream enabled on projectId, watcher disabled when PostToolUse hook is active."""
    config_json_path = gemini_root / "config" / "config.json"
    config_json_path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if config_json_path.is_file():
        try:
            data = json.loads(config_json_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
    sidecars = data.get("sidecars")
    if not isinstance(sidecars, dict):
        sidecars = {}
    sidecars["kortext/kortext-dream"] = {"enabled": True}
    sidecars["kortext/kortext-watcher"] = {"enabled": False}
    data["sidecars"] = sidecars
    config_json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"⚙️  Configured kortext sidecars (dream=True, watcher=False) in: {config_json_path}")


def main():
    import sys

    print(f"🧠 Installing Kortext Antigravity Plugin from {KORTEXT_ROOT}...")
    if "--all" in sys.argv:
        install_all()
    else:
        install_plugin()
    _enable_plugin_sidecars(Path.home() / ".gemini")
    print("🎉 Kortext Antigravity Plugin installed and synchronized dynamically!")


if __name__ == "__main__":
    main()
