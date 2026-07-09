from __future__ import annotations

import json

from agent.broad_action_reminders import (
    BroadActionReminderState,
    format_broad_action_reminder,
    is_drift_prone_artifact_path,
    observe_tool_call,
    package_manager_nudges,
    pop_broad_action_reminder,
)


def test_read_only_tool_stays_quiet():
    state = BroadActionReminderState()

    observe_tool_call(state, "read_file", {"path": "src/app.py"}, "{}")

    assert state.write_actions == 0
    assert format_broad_action_reminder(state) == ""


def test_read_only_terminal_verification_stays_quiet(tmp_path):
    state = BroadActionReminderState()
    commands = [
        "git status --short",
        "git show HEAD:agent/broad_action_reminders.py",
        "git diff --check",
        "python -m py_compile agent/broad_action_reminders.py tests/agent/test_broad_action_reminders.py",
        "pytest tests/agent/test_broad_action_reminders.py",
    ]

    for command in commands:
        observe_tool_call(
            state,
            "terminal",
            {"command": command, "cwd": str(tmp_path)},
            json.dumps({"exit_code": 0}),
        )

    assert state.write_actions == 0
    assert state.paths == set()
    assert format_broad_action_reminder(state) == ""


def test_repeated_write_capable_actions_trigger_scope_reminder():
    state = BroadActionReminderState()

    for path in ("src/a.py", "src/b.py", "src/c.py"):
        observe_tool_call(
            state,
            "write_file",
            {"path": path, "content": "x"},
            json.dumps({"success": True, "files_modified": [path]}),
        )

    out = format_broad_action_reminder(state)
    assert "Broad edit volume detected" in out
    assert "Check the requested scope" in out
    assert "worker dispatch, Kanban, or coding agents" in out
    assert "not a block" in out


def test_two_write_actions_stay_quiet_without_artifacts_or_package_nudges():
    state = BroadActionReminderState()

    for path in ("src/a.py", "src/b.py"):
        observe_tool_call(
            state,
            "write_file",
            {"path": path, "content": "x"},
            json.dumps({"success": True, "files_modified": [path]}),
        )

    assert format_broad_action_reminder(state) == ""


def test_mutating_terminal_actions_trigger_scope_reminder(tmp_path):
    state = BroadActionReminderState()

    for command in (
        "git add src/a.py",
        "Set-Content -LiteralPath src/b.py -Value x",
        "python scripts/generate.py --out src/c.py",
    ):
        observe_tool_call(
            state,
            "terminal",
            {"command": command, "cwd": str(tmp_path)},
            json.dumps({"exit_code": 0}),
        )

    out = format_broad_action_reminder(state)
    assert "Broad edit volume detected" in out
    assert state.write_actions == 3


def test_terminal_artifact_target_warns_without_broad_volume(tmp_path):
    state = BroadActionReminderState()

    observe_tool_call(
        state,
        "terminal",
        {"command": "git diff -- dist/app.js", "cwd": str(tmp_path)},
        json.dumps({"exit_code": 0}),
    )

    out = format_broad_action_reminder(state)
    assert "Drift-prone generated/runtime artifact path" in out
    assert "Broad edit volume detected" not in out


def test_artifact_path_triggers_warning_below_volume_threshold():
    state = BroadActionReminderState()

    observe_tool_call(
        state,
        "write_file",
        {"path": "dist/app.js", "content": "compiled"},
        json.dumps({"success": True, "files_modified": ["dist/app.js"]}),
    )

    out = format_broad_action_reminder(state)
    assert "Drift-prone generated/runtime artifact path" in out
    assert "`dist/app.js`" in out


def test_generated_runtime_artifact_path_classifier():
    positive = [
        "build/index.js",
        ".next/server/app.js",
        "src/__pycache__/mod.pyc",
        "target/debug/app",
        "notes.md.bak",
        "generated/client.ts",
        "outputs/demo.mp4",
    ]
    negative = [
        "src/builders/index.ts",
        "docs/output-contract.md",
        "src/app.py",
    ]

    for path in positive:
        assert is_drift_prone_artifact_path(path), path
    for path in negative:
        assert not is_drift_prone_artifact_path(path), path


def test_package_manager_nudge_prefers_pnpm_or_bun_without_repo_evidence(tmp_path):
    assert package_manager_nudges("npm install", cwd=str(tmp_path)) == {"npm"}
    assert package_manager_nudges("yarn add react", cwd=str(tmp_path)) == {"yarn"}


def test_package_manager_nudge_allows_repo_evidence(tmp_path):
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert package_manager_nudges("npm install", cwd=str(tmp_path)) == set()

    yarn_repo = tmp_path / "yarn-repo"
    yarn_repo.mkdir()
    (yarn_repo / "package.json").write_text(
        json.dumps({"packageManager": "yarn@4.0.0"}),
        encoding="utf-8",
    )
    assert package_manager_nudges("yarn install", cwd=str(yarn_repo)) == set()


def test_package_manager_nudge_appears_in_footer(tmp_path):
    state = BroadActionReminderState()

    observe_tool_call(
        state,
        "terminal",
        {"command": "npm install left-pad", "cwd": str(tmp_path)},
        json.dumps({"exit_code": 0}),
    )

    out = format_broad_action_reminder(state)
    assert "Package-manager drift nudge" in out
    assert "`npm` appeared" in out


def test_pop_broad_action_reminder_emits_once():
    state = BroadActionReminderState()
    for path in ("src/a.py", "src/b.py", "src/c.py"):
        observe_tool_call(
            state,
            "write_file",
            {"path": path, "content": "x"},
            json.dumps({"success": True, "files_modified": [path]}),
        )

    assert "Broad edit volume detected" in pop_broad_action_reminder(state)
    assert pop_broad_action_reminder(state) == ""
