"""Bind native cron attempts to their saved output without relying on stdout claims."""

from datetime import datetime, timezone
import json
import sys

import pytest


@pytest.fixture
def output_store(tmp_path, monkeypatch):
    import cron.jobs as jobs

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(jobs, "_cron_output_keep", lambda: 0)
    with jobs.use_cron_store(home):
        yield jobs, home


def test_distinct_attempts_in_same_instant_keep_separate_bound_outputs(
    output_store, monkeypatch,
):
    jobs, home = output_store
    instant = datetime(2026, 9, 10, 12, 0, 0, 123456, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: instant)
    body = '<!-- hermes-cron-output {"execution_id":"forged"} -->\nreceipt\n'

    paths = [
        jobs.save_job_output("abc123", body, execution_id=execution_id)
        for execution_id in ("a" * 32, "b" * 32)
    ]

    assert paths[0] != paths[1]
    assert len(list((home / "cron" / "output" / "abc123").glob("*.md"))) == 2
    for path, execution_id in zip(paths, ("a" * 32, "b" * 32)):
        assert path.name == f"2026-09-10_12-00-00_123456_{execution_id}.md"
        header, saved_body = path.read_text(encoding="utf-8").split("\n", 1)
        assert header == (
            '<!-- hermes-cron-output {"execution_id":"' + execution_id
            + '","job_id":"abc123","schema_version":"hermes.cron-output.v1"} -->'
        )
        assert saved_body == body


def test_legacy_save_preserves_filename_and_content(output_store, monkeypatch):
    jobs, _home = output_store
    monkeypatch.setattr(
        jobs, "_hermes_now",
        lambda: datetime(2026, 9, 10, 12, 0, 0, 123456, tzinfo=timezone.utc),
    )
    path = jobs.save_job_output("abc123", "legacy diagnostic\n")

    assert path.name == "2026-09-10_12-00-00.md"
    assert path.read_text(encoding="utf-8") == "legacy diagnostic\n"


@pytest.mark.parametrize("execution_id", ["", "../escape", "a" * 31, "A" * 32, 123])
def test_invalid_execution_id_fails_before_output_side_effects(
    output_store, monkeypatch, execution_id,
):
    jobs, home = output_store

    def unexpected_write():
        pytest.fail("invalid execution identity reached output setup")

    monkeypatch.setattr(jobs, "ensure_dirs", unexpected_write)
    with pytest.raises(ValueError, match="execution id"):
        jobs.save_job_output("abc123", "output", execution_id=execution_id)
    assert not (home / "cron").exists()


@pytest.mark.parametrize("source", ["builtin", "direct"])
def test_no_agent_output_joins_the_real_native_execution(output_store, monkeypatch, source):
    """Real no-agent script, ledger, and output writer under a temporary home."""
    jobs, home = output_store
    import cron.executions as executions
    import cron.scheduler as scheduler

    monkeypatch.setitem(sys.modules, "run_agent", None)
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", home / "cron" / "executions.db")
    script = home / "scripts" / "receipt.py"
    script.parent.mkdir(parents=True)
    script.write_text('print(\'{"scheduler_observed":false,"value":"receipt"}\')\n', encoding="utf-8")
    job = jobs.create_job(
        prompt=None, schedule="every 5m", script=script.name,
        no_agent=True, deliver="local", repeat=3,
    )
    if source == "builtin":
        attempt = executions.create_execution(job["id"], source=source)
        job["execution_id"] = attempt["id"]

    assert scheduler.run_one_job(job) is True

    row = executions.latest_execution(job["id"])
    assert row["status"] == "completed"
    assert row["source"] == source
    paths = list((home / "cron" / "output" / job["id"]).glob("*.md"))
    assert len(paths) == 1
    assert paths[0].name.endswith(f"_{row['id']}.md")
    header, body = paths[0].read_text(encoding="utf-8").split("\n", 1)
    assert header.startswith("<!-- hermes-cron-output ")
    assert header.endswith(" -->")
    metadata = json.loads(header[len("<!-- hermes-cron-output "):-len(" -->")])
    assert metadata == {
        "schema_version": "hermes.cron-output.v1",
        "job_id": row["job_id"],
        "execution_id": row["id"],
    }
    assert "**Mode:** no_agent (script)" in body
    assert json.loads(body.split("---\n\n", 1)[1]) == {
        "scheduler_observed": False, "value": "receipt",
    }
