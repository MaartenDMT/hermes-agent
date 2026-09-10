"""Saved cron output must identify its durable execution without changing its body."""

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cron import executions, jobs, scheduler


@pytest.fixture
def output_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", home / "cron" / "executions.db")
    with jobs.use_cron_store(home):
        yield home


@pytest.mark.parametrize("mode", ["bound", "legacy", "", "../escape", "A" * 32, 123])
def test_saved_output_preserves_body_and_separates_execution_identity(output_home, monkeypatch, mode):
    body = "# Result\n\nUTF-8: caf\u00e9\n"
    job_id = uuid.uuid4().hex[:12]
    if mode not in ("bound", "legacy"):
        with pytest.raises(ValueError, match="execution_id"):
            jobs.save_job_output(job_id, body, execution_id=mode)
        assert not output_home.exists()
        return
    if mode == "legacy":
        output = jobs.save_job_output(job_id, body)
        assert output.read_text(encoding="utf-8") == body
        assert len(list(output.parent.glob("*.md"))) == 1
        return

    instant = datetime.now(timezone.utc)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: instant)
    execution_ids = [uuid.uuid4().hex, uuid.uuid4().hex]
    outputs = [jobs.save_job_output(job_id, body, execution_id=eid) for eid in execution_ids]
    assert outputs[0] != outputs[1]
    for output, execution_id in zip(outputs, execution_ids):
        header, saved_body = output.read_text(encoding="utf-8").split("\n", 1)
        assert header.startswith("<!-- hermes-cron-output ") and header.endswith(" -->")
        metadata = json.loads(header.removeprefix("<!-- hermes-cron-output ").removesuffix(" -->"))
        assert metadata == {"schema_version": "hermes.cron-output.v1", "job_id": job_id,
                            "execution_id": execution_id}
        assert execution_id in output.name
        assert saved_body == body


@pytest.mark.parametrize("entry", ["direct", "claimed", "body-fallback"])
def test_native_no_agent_output_joins_its_execution(output_home, monkeypatch, entry):
    scripts = output_home / "scripts"
    scripts.mkdir(parents=True)
    receipt = uuid.uuid4().hex
    (scripts / "receipt.py").write_text(f"print({receipt!r})\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "run_agent", None)
    job = jobs.create_job(prompt=None, schedule="every 5m", repeat=3, deliver="local",
                          script="receipt.py", no_agent=True)
    if entry == "claimed":
        job["execution_id"] = executions.create_execution(job["id"], source="builtin")["id"]
    run = scheduler._run_one_job_body if entry == "body-fallback" else scheduler.run_one_job
    assert run(job) is True
    rows = executions.list_executions(job_id=job["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "completed", row
    outputs = list((output_home / "cron" / "output" / job["id"]).glob("*.md"))
    assert len(outputs) == 1
    header, body = outputs[0].read_text(encoding="utf-8").split("\n", 1)
    assert header.startswith("<!-- hermes-cron-output ") and header.endswith(" -->")
    metadata = json.loads(header.removeprefix("<!-- hermes-cron-output ").removesuffix(" -->"))
    assert metadata["job_id"] == row["job_id"] == job["id"]
    assert metadata["execution_id"] == row["id"]
    assert row["id"] in outputs[0].name
    assert receipt in body
    assert jobs.get_job(job["id"])["last_status"] == "ok"
