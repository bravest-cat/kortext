#!/usr/bin/env python3
"""
Test Suite for Kortext Antigravity Plugin Manifests & Schemas.
"""
import json
import sys
import re
from pathlib import Path

KORTEXT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = KORTEXT_ROOT / "integrations" / "antigravity"


def test_plugin_json():
    p = PLUGIN_DIR / "plugin.json"
    assert p.is_file(), "plugin.json must exist"
    data = json.loads(p.read_text())
    assert data.get("name") == "kortext", f"Plugin name must be kortext, got {data.get('name')}"
    assert re.match(r"^[a-zA-Z0-9-_]+$", data["name"]), "Plugin name must match regex"
    assert "description" in data, "Plugin description required"
    assert "mcpServers" not in data, "mcpServers must NOT be declared in plugin.json (use mcp_config.json per Antigravity spec)"
    allowed_keys = {"$schema", "name", "description", "version"}
    extra_keys = set(data.keys()) - allowed_keys
    assert not extra_keys, f"plugin.json contains unexpected keys: {extra_keys}"
    print("✅ plugin.json schema valid.")


def test_mcp_config():
    assert not (PLUGIN_DIR / "mcp_config.json").exists(), "Plugin mcp_config.json should not exist to avoid plugin namespace duplication"
    host_mcp = Path.home() / ".gemini" / "config" / "mcp_config.json"
    if host_mcp.exists():
        data = json.loads(host_mcp.read_text())
        assert "mcpServers" in data, "mcpServers must be defined in mcp_config.json"
        assert isinstance(data["mcpServers"], dict), "mcpServers must be a dictionary"
        assert "kortext" in data["mcpServers"], "kortext must be configured in mcpServers"
        assert "cerebrum_kortext" not in data["mcpServers"], "cerebrum_kortext must not be in mcpServers"
        assert "kortext_kortext" not in data["mcpServers"], "kortext_kortext must not be in mcpServers"
    print("✅ Single canonical kortext MCP server verified in host mcp_config.json.")


def test_hooks_json():
    p = PLUGIN_DIR / "hooks.json"
    assert p.is_file(), "hooks.json must exist"
    data = json.loads(p.read_text())
    expected_hooks = [
        "ambient-grounding",
        "safety-gate",
        "cache-invalidation",
        "dag-stop-guard",
    ]
    for h in expected_hooks:
        assert f"kortext-{h}" in data, f"Hook {h} must be configured"
    print("✅ hooks.json definitions valid.")

    sys.path.insert(0, str(KORTEXT_ROOT / "scripts"))
    from install_plugin import build_hooks_dict

    cmd = build_hooks_dict("/x/py")["kortext-dag-stop-guard"]["Stop"][0]["command"]
    assert cmd == '"/x/py" -m kortext.hooks.runner stop', cmd
    assert "sh " not in json.dumps(build_hooks_dict("/x/py"))
    print("✅ runner-form hooks have no sh dependency.")


def test_subagent_yaml():
    agents_dir = PLUGIN_DIR / "agents"
    canonical_files = {
        "kortext-architect.md",
        "kortext-code-reviewer.md",
        "kortext-researcher.md",
        "kortext-task-runner.md",
        "kortext-security-auditor.md",
    }
    found_files = {p.name for p in agents_dir.glob("*.md")}
    assert found_files == canonical_files, f"Expected exactly {canonical_files}, got {found_files}"

    cerebrum_files = list(agents_dir.glob("cerebrum-*.md"))
    assert not cerebrum_files, f"Deprecated cerebrum agent files found: {cerebrum_files}"

    for filename in sorted(canonical_files):
        p = agents_dir / filename
        assert p.is_file(), f"{filename} must exist"
        text = p.read_text()
        assert text.startswith("---"), f"{p.name} must start with YAML frontmatter"
        assert "name:" in text, f"{p.name} missing 'name:'"
        assert "description:" in text, f"{p.name} missing 'description:'"
        assert "model:" in text, f"{p.name} missing 'model:'"
        assert "subagent: true" in text, f"{p.name} missing 'subagent: true'"
        assert "mainAgent: false" in text, f"{p.name} missing 'mainAgent: false'"
        assert "inheritMcp:" not in text, f"{p.name} must not contain unsupported 'inheritMcp:' frontmatter key"
    print("✅ agents/*.md YAML frontmatters valid.")


def test_skills_yaml():
    canonical_12_skills = {
        "kortext-boost",
        "anti-ripgrep-seek",
        "kortext-rag",
        "to-spec",
        "to-tickets",
        "subagent-driven-development",
        "tdd-workflow",
        "systematic-debugging",
        "code-review",
        "security-review",
        "expand-contract-refactor",
        "verification-before-completion",
    }
    skills_dir = PLUGIN_DIR / "skills"
    found_skills = {p.name for p in skills_dir.iterdir() if p.is_dir()}
    assert found_skills == canonical_12_skills, (
        f"Expected exact 12 canonical skills {sorted(canonical_12_skills)}, got {sorted(found_skills)}"
    )
    for name in sorted(canonical_12_skills):
        p = skills_dir / name / "SKILL.md"
        assert p.is_file(), f"{name}/SKILL.md must exist"
    assert not (skills_dir / "cerebrum-rag").exists(), "cerebrum-rag directory must not exist"
    assert not (skills_dir / "cerebrum-architect").exists(), "cerebrum-architect directory must not exist"
    for redundant in [
        "coding-standards",
        "python-patterns",
        "backend-patterns",
        "api-design",
        "error-handling",
        "git-workflow",
        "verification-loop",
        "task-orchestrator",
        "agentic-engineering",
        "architecture-decision-records",
        "autonomous-loops",
        "codebase-onboarding",
        "database-migrations",
        "kortext-architect",
        "search-first",
        "thermo-nuclear-code-quality-review",
        "token-budget-advisor",
        "wayfinder",
    ]:
        assert not (skills_dir / redundant).exists(), f"Redundant skill {redundant} must be removed"
    for ws_dup in ["skills", "agents", "rules"]:
        assert not (KORTEXT_ROOT / ".agents" / ws_dup).exists(), f"Duplicate .agents/{ws_dup} must not exist"
    for skill_file in skills_dir.glob("*/SKILL.md"):
        raw_bytes = skill_file.read_bytes()
        assert len(raw_bytes) <= 24576, f"{skill_file.name} exceeds Antigravity 24 KB limit ({len(raw_bytes)} bytes)"
        text = raw_bytes.decode("utf-8")
        assert text.startswith("---"), f"{skill_file} must start with YAML frontmatter"
        assert "name: " in text
        assert "description: Use " in text, f"{skill_file} description must follow CSO 'Use when/before...' format"
        m_desc = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
        assert m_desc is not None, f"{skill_file} missing single-line description"
        desc_text = m_desc.group(1).strip().strip("'\"")
        assert len(desc_text) <= 240, (
            f"{skill_file.parent.name}/SKILL.md description is {len(desc_text)} chars (> 240 char Antigravity 1.4.6 budget)"
        )
        assert "```mermaid" not in text, f"{skill_file} must not contain token-bloating mermaid diagrams"
    print("✅ 12 canonical skills/*/SKILL.md YAML frontmatters, CSO descriptions (<=240 chars), 24 KB limits, and deduplication valid.")


def test_sidecars_json():
    sidecars_dir = PLUGIN_DIR / "sidecars"
    assert sidecars_dir.is_dir(), "sidecars directory must exist"

    expected_sidecars = ["kortext-watcher", "kortext-dream"]
    for sidecar_name in expected_sidecars:
        sc_dir = sidecars_dir / sidecar_name
        assert sc_dir.is_dir(), f"Sidecar directory {sidecar_name} must exist"

        manifest_path = sc_dir / "sidecar.json"
        assert manifest_path.is_file(), f"sidecar.json must exist for {sidecar_name}"

        try:
            data = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as e:
            raise AssertionError(f"sidecar.json for {sidecar_name} is not valid JSON: {e}") from e

        assert "displayName" not in data, f"Duplicate camelCase displayName must not be present in {sidecar_name}"
        assert "restartPolicy" not in data, f"Duplicate camelCase restartPolicy must not be present in {sidecar_name}"
        assert "display_name" in data and isinstance(data["display_name"], str) and data["display_name"], (
            f"display_name must be a non-empty string in {sidecar_name}"
        )
        assert "description" in data and isinstance(data["description"], str) and data["description"], (
            f"description must be a non-empty string in {sidecar_name}"
        )
        has_cmd = "command" in data and isinstance(data["command"], str) and bool(data["command"])
        has_builtin = "builtin" in data and isinstance(data["builtin"], str) and bool(data["builtin"])
        assert has_cmd or has_builtin, (
            f"Either 'command' or 'builtin' must be specified as a non-empty string in {sidecar_name}"
        )
        assert "args" in data and isinstance(data["args"], list), (
            f"args must be a list in {sidecar_name}"
        )
        assert all(isinstance(a, str) for a in data["args"]), (
            f"all items in args must be strings in {sidecar_name}"
        )
        assert "restart_policy" in data and isinstance(data["restart_policy"], str), (
            f"restart_policy must be specified in {sidecar_name}"
        )
        assert data["restart_policy"] in ("always", "on-failure", "never", "unless-stopped"), (
            f"restart_policy '{data['restart_policy']}' is invalid in {sidecar_name}"
        )
        if "env" in data:
            assert isinstance(data["env"], dict), f"env must be a dict in {sidecar_name}"
            assert all(isinstance(k, str) and isinstance(v, str) for k, v in data["env"].items()), (
                f"env entries must be string key-value pairs in {sidecar_name}"
            )

    print("✅ sidecars/*/sidecar.json manifests valid.")


def test_rules_not_always_on():
    rules_dir = PLUGIN_DIR / "rules"
    assert rules_dir.is_dir(), "rules directory must exist"
    rule_files = list(rules_dir.glob("*.md"))
    assert len(rule_files) >= 8, "Expected at least 8 rule files"
    for rf in rule_files:
        text = rf.read_text()
        assert "trigger: always_on" not in text, f"{rf.name} must not use trigger: always_on (causes context bloat)"
        assert "trigger: model_decision" in text or "trigger: glob" in text, (
            f"{rf.name} must use trigger: model_decision or trigger: glob"
        )
    print("✅ rules/*.md use model_decision or scoped glob triggers (zero upfront bloat).")


if __name__ == "__main__":
    print("=== Running Kortext Plugin Manifest Validation Suite ===")
    test_plugin_json()
    test_mcp_config()
    test_hooks_json()
    test_subagent_yaml()
    test_skills_yaml()
    test_sidecars_json()
    test_rules_not_always_on()
    print("🎉 All Kortext manifest tests passed!")
