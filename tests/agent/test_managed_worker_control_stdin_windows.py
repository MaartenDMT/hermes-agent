"""A managed worker's control pipe never reaches a child launched without ``stdin=`` on Windows.

``WorkerControls.read`` holds a synchronous read on the owner's control pipe for the whole turn.
A child that inherited that pipe as its standard input (the version probe's ``git rev-parse``,
run while the agent builds its User-Agent) blocked, so the worker never ran the turn and every
``hermes chat --oneshot`` through the gateway hung. The owner keeps the pipe open while the worker
works, so the test keeps its writer open the same way.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("windows")

REPO_ROOT = Path(__file__).resolve().parents[2]

_WORKER = textwrap.dedent("""
    import subprocess, sys, threading
    from agent import managed_worker
    managed_worker.private_control_stdin()
    frames = []
    reader = threading.Thread(target=lambda: frames.append(managed_worker.read_frame(sys.stdin.buffer)), daemon=True)
    reader.start()  # the pending control read WorkerControls holds during a turn
    try:
        child = subprocess.run(
            [sys.executable, "-c", "import sys; sys.stdout.write(repr(sys.stdin.read()))"],
            capture_output=True, text=True, timeout=10)
        seen = child.stdout
    except subprocess.TimeoutExpired:
        seen = "TIMEOUT"
    print("child saw:", seen, flush=True)
    sys.stdout.flush()
    reader.join(30)
    print("worker read:", frames, flush=True)
""")


def test_child_without_stdin_sees_nul_while_the_control_pipe_stays_open(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "HERMES_HOME": str(tmp_path / "home")}
    proc = subprocess.Popen([sys.executable, "-c", _WORKER], cwd=tmp_path, env=env, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdin is not None and proc.stdout is not None
    try:
        saw = proc.stdout.readline()
        assert saw == "child saw: ''\n", f"a child without stdin= held the control pipe: {saw!r}"
        proc.stdin.write(json.dumps({"type": "finish"}) + "\n")
        proc.stdin.flush()
        assert proc.stdout.readline() == "worker read: [{'type': 'finish'}]\n", "the worker lost its control frame"
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
