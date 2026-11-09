"""Tests for _resolve_path_for_task() — TERMINAL_CWD-aware path resolution in file_tools_paths."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


class TestResolvePath:
    """Verify _resolve_path_for_task respects TERMINAL_CWD for worktree isolation."""

    def test_relative_path_uses_terminal_cwd(self, monkeypatch, tmp_path):
        """Relative paths resolve against TERMINAL_CWD, not process CWD."""
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        from tools.file_tools_paths import _resolve_path_for_task

        result = _resolve_path_for_task("foo/bar.py")
        assert result == (tmp_path / "foo" / "bar.py")


    def test_relative_path_prefers_recorded_session_cwd(self, monkeypatch, tmp_path):
        """The session's recorded cwd must win after the terminal changes directory."""
        start_dir = tmp_path / "start"
        live_dir = tmp_path / "worktree"
        start_dir.mkdir()
        live_dir.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(start_dir))

        from tools import file_tools_paths, terminal_tool

        task_id = "live-cwd"
        # The session's completed `cd` recorded the new directory.
        terminal_tool.record_session_cwd(task_id, str(live_dir))

        try:
            result = file_tools_paths._resolve_path_for_task("nested/file.txt", task_id=task_id)
        finally:
            terminal_tool.clear_session_cwd(task_id)

        assert result == live_dir / "nested" / "file.txt"


@pytest.mark.parametrize(
    ("backend", "raw"),
    [("docker", r"C:\Programming\agent-wiki\index.md"), ("docker", "C:/Programming/agent-wiki/index.md")],
)
def test_windows_path_on_container_backend_is_refused(backend, raw):
    from tools import file_tools_paths as paths

    with (patch.object(paths, "_terminal_env_type_for_task", return_value=backend),
          patch.object(paths, "_uses_container_paths", return_value=True)):
        with pytest.raises(ValueError, match="convention"):
            paths._resolve_path_for_task(raw)


def test_container_mount_on_windows_host_is_refused_but_a_drive_letter_is_not():
    from tools import file_tools_paths as paths

    with (patch.object(paths, "_terminal_env_type_for_task", return_value="local"),
          patch.object(paths, "_uses_container_paths", return_value=False),
          patch.object(paths.sys, "platform", "win32")):
        with pytest.raises(ValueError, match="convention"):
            paths._resolve_path_for_task("/mnt/programming/agent-wiki/index.md")
        assert "mnt" not in str(paths._resolve_path_for_task("/c/Users/me.txt")).lower()
        paths._resolve_path_for_task("/mnt/c/Users/me.txt")


def test_drive_path_under_the_sandbox_mount_maps_onto_it():
    from tools import file_tools_paths as paths

    env = SimpleNamespace(host_cwd=r"C:\proj", host_cwd_mount="/host-cwd")
    with (patch.object(paths, "_terminal_env_type_for_task", return_value="docker"),
          patch.object(paths, "_uses_container_paths", return_value=True),
          patch("tools.terminal_tool_lifecycle.get_active_env", return_value=env)):
        assert str(paths._resolve_path_for_task(r"C:\proj\src\a.py")) == "/host-cwd/src/a.py"
        with pytest.raises(ValueError, match="convention"):
            paths._resolve_path_for_task(r"C:\other\a.py")
        with pytest.raises(ValueError, match="convention"):
            paths._resolve_path_for_task(r"C:\proj\..\secret.py")


def test_write_to_a_foreign_path_reports_the_refusal_and_never_reaches_the_backend():
    import json

    from tools import file_tools, file_tools_paths as paths

    with (patch.object(paths, "_terminal_env_type_for_task", return_value="local"),
          patch.object(paths, "_uses_container_paths", return_value=False),
          patch.object(paths.sys, "platform", "win32"),
          patch.object(file_tools, "_get_file_ops") as ops):
        result = json.loads(file_tools.write_file_tool("/mnt/programming/a.txt", "x", task_id="t"))

    assert "convention" in result["error"]
    ops.return_value.write_file.assert_not_called()
