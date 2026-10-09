import sys

import pytest

from harness.skills import SkillScriptRunner, discover_skills


def write_skill(root, name="demo-skill", description="A valid demo skill."):
    skill = root / ".opencode" / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\nmetadata:\n  audience: developers\n---\n# Instructions\n")
    return skill


def test_discovery_is_on_demand_and_invalid_manifests_do_not_block(tmp_path):
    (tmp_path / ".git").mkdir()
    skill = write_skill(tmp_path)
    invalid = tmp_path / ".claude" / "skills" / "bad"
    invalid.mkdir(parents=True)
    (invalid / "SKILL.md").write_text("---\nname: wrong\n---\n")
    catalog = discover_skills(tmp_path, tmp_path / "global", project_roots=(".opencode/skills", ".claude/skills"), global_roots=())
    assert [item.name for item in catalog.summaries()] == ["demo-skill"]
    assert catalog.diagnostics[0].error_code == "skill_invalid_manifest"
    assert "# Instructions" in catalog.load("demo-skill").content
    assert (skill / "scripts").exists() is False  # Discovery neither creates nor runs resources.


@pytest.mark.asyncio
async def test_script_runner_enforces_containment_argv_output_and_policy(tmp_path):
    (tmp_path / ".git").mkdir()
    skill = write_skill(tmp_path)
    scripts = skill / "scripts"
    scripts.mkdir()
    (scripts / "echo.py").write_text("import sys\nprint('|'.join(sys.argv[1:]))\n")
    (scripts / "echo.sh").write_text("echo should-not-run\n")
    (scripts / "loud.py").write_text("print('x' * 1000)\n")
    catalog = discover_skills(tmp_path, tmp_path / "global", project_roots=(".opencode/skills",), global_roots=())
    record = catalog.records["demo-skill"]
    runner = SkillScriptRunner(workspace_root=tmp_path, python=sys.executable, policy="allow", allowed_extensions=(".py",),
                               default_timeout=2, max_timeout=2, max_output_bytes=20, max_concurrent=1)
    result = await runner.run(record, "scripts/echo.py", ["a;not-shell", "b"], None)
    assert result.ok and result.content.strip() == "a;not-shell|b"
    assert (await runner.run(record, "../outside.py", [], None)).error_code == "skill_script_path_forbidden"
    assert (await runner.run(record, "scripts/echo.sh", [], None)).error_code == "skill_script_runtime_not_allowed"
    loud = await runner.run(record, "scripts/loud.py", [], None)
    assert loud.ok and loud.truncated and len(loud.content.encode()) <= 20
    asking = SkillScriptRunner(workspace_root=tmp_path, python=sys.executable, policy="ask", allowed_extensions=(".py",),
                               default_timeout=2, max_timeout=2, max_output_bytes=20, max_concurrent=1)
    assert (await asking.run(record, "scripts/echo.py", [], None)).error_code == "skill_script_approval_required"
