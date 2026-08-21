"""Tests for MCP tools interactive configuration in hermes_cli.tools_config."""

from unittest.mock import patch

from hermes_cli.tools_config import _configure_mcp_tools_interactive

# Patch targets: imports happen inside the function body, so patch at source
_PROBE = "tools.mcp_tool.probe_mcp_server_tools"
_CHECKLIST = "hermes_cli.curses_ui.curses_checklist"
_SAVE = "hermes_cli.tools_config.save_config"








def test_disabling_tool_writes_include_list(capsys):
    """Unchecking a tool produces an include list of the still-chosen tools.

    Standardized on tools.include (whitelist) across the codebase — the
    catalog flow, `hermes mcp configure`, and this UI all write the same
    shape so users don\'t see config drift across UIs.
    """
    config = {
        "mcp_servers": {
            "github": {"command": "npx"},
        }
    }
    tools = [
        ("create_issue", "Create an issue"),
        ("delete_repo", "Delete a repo"),
        ("search_repos", "Search repos"),
    ]

    # User unchecks delete_repo (index 1)
    with patch(_PROBE, return_value={"github": tools}), \
         patch(_CHECKLIST, return_value={0, 2}), \
         patch(_SAVE) as mock_save:
        _configure_mcp_tools_interactive(config)

    mock_save.assert_called_once()
    tools_cfg = config["mcp_servers"]["github"]["tools"]
    assert tools_cfg["include"] == ["create_issue", "search_repos"]
    assert "exclude" not in tools_cfg








def test_empty_tools_server_skipped(capsys):
    """Server with no tools shows info message and skips checklist."""
    config = {
        "mcp_servers": {
            "empty": {"command": "npx"},
        }
    }
    checklist_calls = []

    def fake_checklist(title, labels, pre_selected, **kwargs):
        checklist_calls.append(title)
        return pre_selected

    with patch(_PROBE, return_value={"empty": []}), \
         patch(_CHECKLIST, side_effect=fake_checklist), \
         patch(_SAVE):
        _configure_mcp_tools_interactive(config)

    assert len(checklist_calls) == 0
    captured = capsys.readouterr()
    assert "no tools found" in captured.out


def test_mcp_configure_all_selected_preserves_required_positive_allowlist(monkeypatch):
    from types import SimpleNamespace
    import hermes_cli.mcp_config as mc

    server = {
        "command": "npx",
        "tools": {
            "include": ["read_a"],
            "require_positive_include": True,
            "resources": False,
            "prompts": False,
        },
    }
    config = {"mcp_servers": {"safe": server}}
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(mc, "_get_mcp_servers", lambda: {"safe": server})
    monkeypatch.setattr(
        mc, "_probe_single_server", lambda *_a, **_k: [("read_a", "A"), ("read_b", "B")]
    )
    monkeypatch.setattr(
        "hermes_cli.curses_ui.curses_checklist", lambda *_a, **_k: {0, 1}
    )
    monkeypatch.setattr(mc, "load_config", lambda: config)
    saved = []
    monkeypatch.setattr(mc, "save_config", lambda value: saved.append(value))

    mc.cmd_mcp_configure(SimpleNamespace(name="safe"))

    assert saved[0]["mcp_servers"]["safe"]["tools"] == {
        "include": ["read_a", "read_b"],
        "require_positive_include": True,
        "resources": False,
        "prompts": False,
    }


def test_mcp_configure_empty_positive_allowlist_preselects_nothing(monkeypatch):
    from types import SimpleNamespace
    import hermes_cli.mcp_config as mc

    server = {
        "command": "npx",
        "tools": {"include": [], "require_positive_include": True},
    }
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(mc, "_get_mcp_servers", lambda: {"safe": server})
    monkeypatch.setattr(
        mc, "_probe_single_server", lambda *_a, **_k: [("read_a", "A"), ("read_b", "B")]
    )
    captured = []

    def checklist(_title, _labels, pre_selected):
        captured.append(pre_selected)
        return pre_selected

    monkeypatch.setattr("hermes_cli.curses_ui.curses_checklist", checklist)

    mc.cmd_mcp_configure(SimpleNamespace(name="safe"))

    assert captured == [set()]
