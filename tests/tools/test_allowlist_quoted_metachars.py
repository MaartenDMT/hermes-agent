"""Tests for the quote-aware allowlist shell-operator check.

Port of can1357/oh-my-pi#7553: `command_allowlist` glob rules (e.g.
``cargo *``) used to reject any command whose *quoted arguments* contained
shell metacharacters — a cargo benchmark regex filter like
``'^layer3/write/(a|b)$'`` disqualified the whole command even though the
metacharacters are literal to the shell. The matcher is now quote-aware,
while still rejecting genuinely compound commands and quoted payloads that
a ``-c``/``-e``-style option would hand to another interpreter.
"""

import pytest

from tools.approval import _command_matches_permanent_allowlist
from tools.approval_floors import _has_allowlist_shell_operator


class TestHasAllowlistShellOperator:
    # ------------------------------------------------------------------
    # Simple commands stay simple
    # ------------------------------------------------------------------

    def test_plain_command(self):
        assert not _has_allowlist_shell_operator("git status")

    def test_quoted_metacharacters_are_literal(self):
        # The motivating case: cargo bench regex filter (omp issue #7552).
        cmd = (
            "cargo bench --manifest-path layers/layer3/Cargo.toml "
            "--bench standardized_criterion -- "
            "'^layer3/write/file-wal/batch-(10|1000|10000)$'"
        )
        assert not _has_allowlist_shell_operator(cmd)

    def test_double_quoted_literal_metachars(self):
        assert not _has_allowlist_shell_operator('grep -r "a|b;c" src')

    def test_escaped_metachar_is_literal(self):
        assert not _has_allowlist_shell_operator("grep foo\\;bar file.txt")

    def test_unquoted_dollar_variable_is_simple(self):
        # Historical behavior: only `$(` was compound, bare $VAR was not.
        assert not _has_allowlist_shell_operator("echo $HOME")

    def test_unquoted_parens_alone_are_not_compound(self):
        # Parens without $ were never matched by the old regex either.
        assert not _has_allowlist_shell_operator("pytest -k (a and b)")

    # ------------------------------------------------------------------
    # Genuinely compound commands still rejected
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("cmd", [
        "git status; rm -rf /tmp/x",
        "git status && make",
        "git status || make",
        "cat foo | grep bar",
        "echo hi > /etc/passwd",
        "cat < seed",
        "echo `rm x`",
        "echo $(rm x)",
        "git status\nrm x",
        "git status & disown",
    ])
    def test_unquoted_operators_compound(self, cmd):
        assert _has_allowlist_shell_operator(cmd)

    def test_dollar_inside_double_quotes_is_active(self):
        # Expansion still happens inside double quotes.
        assert _has_allowlist_shell_operator('echo "$(rm x)"')
        assert _has_allowlist_shell_operator('echo "`rm x`"')
        assert _has_allowlist_shell_operator('echo "$HOME"')

    def test_unterminated_quote_is_compound(self):
        assert _has_allowlist_shell_operator("echo 'unterminated")

    # ------------------------------------------------------------------
    # Reinterpreted-argument options: quoted payloads become executable
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("cmd", [
        "sh -c 'rm -rf /tmp/x; echo done'",
        'bash -c "make | tee log"',
        "git -c alias.x='!touch /tmp/pwn; printf ok' x",
        'git -c alias.x="!touch /tmp/pwn; printf ok" x',
        "node --eval 'require(\"child_process\").exec(\"id\")>1'",
        "perl -e 'system(\"id\");'",
    ])
    def test_quoted_payload_with_interpreter_option(self, cmd):
        assert _has_allowlist_shell_operator(cmd)

    def test_interpreter_option_without_quoted_metachars_ok(self):
        # -c with a payload containing control chars (parens) is flagged...
        assert _has_allowlist_shell_operator("python -c 'print(1)'")
        # ...but a clean payload with no control characters at all is fine.
        assert not _has_allowlist_shell_operator("python -c 'import sys'")


class TestAllowlistGlobWithQuotedArgs:
    def test_cargo_glob_matches_quoted_regex_filter(self, monkeypatch):
        import tools.approval as mod
        monkeypatch.setattr(mod, "_permanent_approved", {"cargo *"})
        cmd = (
            "cargo bench --bench standardized_criterion -- "
            "'^layer3/write/file-wal/batch-(10|1000|10000)$'"
        )
        assert _command_matches_permanent_allowlist(cmd)

    def test_glob_still_refuses_compound(self, monkeypatch):
        import tools.approval as mod
        monkeypatch.setattr(mod, "_permanent_approved", {"cargo *"})
        assert not _command_matches_permanent_allowlist("cargo build && rm -rf /tmp/x")

    def test_glob_refuses_git_alias_payload(self, monkeypatch):
        import tools.approval as mod
        monkeypatch.setattr(mod, "_permanent_approved", {"git *"})
        assert not _command_matches_permanent_allowlist(
            "git -c alias.x='!touch /tmp/pwn; printf ok' x"
        )


class TestAllowlistRegexEntries:
    _DISPATCHER_COMMAND = (
        "python operations/automatic-worker-dispatch/auto_dispatch.py "
        "--task-id 123 --project readersbase --mode dry-run"
    )
    _DISPATCHER_REGEX = (
        r"regex:python operations/automatic-worker-dispatch/auto_dispatch\.py "
        r"--task-id [0-9]+ --project [a-z]+ --mode dry-run"
    )

    def test_regex_matches_bounded_dispatcher_command(self, monkeypatch):
        import tools.approval as mod
        monkeypatch.setattr(mod, "_permanent_approved", {self._DISPATCHER_REGEX})

        assert _command_matches_permanent_allowlist(self._DISPATCHER_COMMAND)

    @pytest.mark.parametrize("command", [
        "python operations/automatic-worker-dispatch/auto_dispatch.py "
        "--project readersbase --mode dry-run",
        "python operations/automatic-worker-dispatch/auto_dispatch.py "
        "--task-id 123 --project readersbase --mode dry-run --verbose",
        "python operations/automatic-worker-dispatch/auto_dispatch.py "
        "--task-id 123 --task-id 456 --project readersbase --mode dry-run",
        "python operations/automatic-worker-dispatch/auto_dispatch.py "
        "--project readersbase --task-id 123 --mode dry-run",
    ])
    def test_regex_refuses_wrong_dispatcher_argument_shape(self, monkeypatch, command):
        import tools.approval as mod
        monkeypatch.setattr(mod, "_permanent_approved", {self._DISPATCHER_REGEX})

        assert not _command_matches_permanent_allowlist(command)

    @pytest.mark.parametrize("command", [
        "python operations/automatic-worker-dispatch/auto_dispatch.py "
        "--task-id 123 --project readersbase --mode dry-run && id",
        "python operations/automatic-worker-dispatch/auto_dispatch.py "
        "--task-id 123 --project readersbase --mode dry-run || id",
        "python operations/automatic-worker-dispatch/auto_dispatch.py "
        "--task-id 123 --project readersbase --mode dry-run; id",
        "python operations/automatic-worker-dispatch/auto_dispatch.py "
        "--task-id 123 --project readersbase --mode dry-run | id",
        "python operations/automatic-worker-dispatch/auto_dispatch.py "
        "--task-id 123 --project readersbase --mode dry-run > output",
        "python operations/automatic-worker-dispatch/auto_dispatch.py "
        "--task-id $(id) --project readersbase --mode dry-run",
        "python operations/automatic-worker-dispatch/auto_dispatch.py "
        "--task-id 123 --project readersbase --mode 'dry-run",
    ])
    def test_regex_runs_after_shell_operator_guard(self, monkeypatch, command):
        import tools.approval as mod
        monkeypatch.setattr(mod, "_permanent_approved", {r"regex:.*"})

        assert not _command_matches_permanent_allowlist(command)

    @pytest.mark.parametrize("pattern", ["regex:", "regex:(", "regex:["])
    def test_malformed_or_empty_regex_fails_closed(self, monkeypatch, pattern):
        import tools.approval as mod
        monkeypatch.setattr(mod, "_permanent_approved", {pattern})

        assert not _command_matches_permanent_allowlist("git status")

    def test_regex_never_falls_through_to_glob_matching(self, monkeypatch):
        import tools.approval as mod
        monkeypatch.setattr(mod, "_permanent_approved", {r"regex:git .*"})

        assert not _command_matches_permanent_allowlist("regex:git .status")

    def test_overflowing_regex_fails_closed(self, monkeypatch):
        import tools.approval as mod
        monkeypatch.setattr(mod, "_permanent_approved", {r"regex:a{999999999999999999999}"})

        assert not _command_matches_permanent_allowlist("a")

    def test_recursive_regex_failure_fails_closed_without_glob_fallback(self, monkeypatch):
        import tools.approval as mod
        import tools.approval_floors as floors
        monkeypatch.setattr(mod, "_permanent_approved", {r"regex:git .*"})

        def raise_engine_error(*_args):
            raise RecursionError("deep regex")

        monkeypatch.setattr(floors.re, "fullmatch", raise_engine_error)

        assert not _command_matches_permanent_allowlist("regex:git .status")
