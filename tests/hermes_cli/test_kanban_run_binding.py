"""Behavior tests for immutable MAOS dispatch bindings on Hermes runs."""

from __future__ import annotations

import contextlib
import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_db as kb


def _binding() -> dict:
    return {
        "schema": "maos.hermes-run-binding.v1",
        "sourceRevision": "sha256:" + "a" * 64,
        "parentHarness": "codex",
        "coordinator": "sol",
        "workers": [
            {
                "id": "worker-b",
                "provider": "codex",
                "model": "gpt-5.6-terra",
                "modelEvidence": "requested",
                "contractReference": "contract:worker-b",
            },
            {
                "id": "worker-a",
                "provider": "codex",
                "model": "gpt-5.6-sol",
                "modelEvidence": "runtime-confirmed",
                "contractReference": "contract:worker-a",
            },
        ],
        "leaseReference": "lease:task-run",
        "approvalReferences": ["approval:z", "approval:a"],
    }


def _claim(conn) -> tuple[str, int]:
    task_id = kb.create_task(conn, title="Bind a native Hermes run")
    assert kb.claim_task(conn, task_id) is not None
    task = kb.get_task(conn, task_id)
    assert task is not None and task.current_run_id is not None
    return task_id, task.current_run_id


def test_run_binding_validation_canonicalizes_unordered_collections():
    binding = _binding()

    normalized = kb.validate_run_binding(binding)

    assert [worker["id"] for worker in normalized["workers"]] == [
        "worker-a", "worker-b",
    ]
    assert normalized["approvalReferences"] == ["approval:a", "approval:z"]
    assert kb.encode_run_binding(binding) == kb.encode_run_binding(normalized)


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda value: value.update({"status": "running"}), "unknown"),
        (lambda value: value.update({"claim_lock": "private"}), "unknown"),
        (lambda value: value.update({"metadata": {}}), "unknown"),
        (lambda value: value["workers"][1].update({"pid": 7}), "unknown"),
        (lambda value: value["workers"].append(copy.deepcopy(value["workers"][0])), "duplicate"),
        (lambda value: value.update({"approvalReferences": ["approval:a", "approval:a"]}), "duplicate"),
        (lambda value: value.update({"leaseReference": "../private"}), "traversal"),
        (lambda value: value.update({"parentHarness": "ghp_abcdefghijklmnop"}), "secret"),
    ],
)
def test_run_binding_validation_rejects_forbidden_or_ambiguous_fields(mutate, match):
    binding = _binding()
    mutate(binding)

    with pytest.raises(kb.RunBindingError, match=match):
        kb.validate_run_binding(binding)


def test_bind_run_contract_persists_one_canonical_immutable_value(tmp_path):
    db_path = tmp_path / "kanban.db"
    binding = _binding()
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id, run_id = _claim(conn)

        first = kb.bind_run_contract(conn, task_id, run_id, binding)
        second = kb.bind_run_contract(conn, task_id, run_id, copy.deepcopy(binding))
        raw = conn.execute(
            "SELECT maos_run_binding_json FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()[0]

    assert first.maos_run_binding == second.maos_run_binding
    assert first.maos_run_binding == kb.validate_run_binding(binding)
    assert raw == kb.encode_run_binding(binding)


@pytest.mark.parametrize("run_id", [True, 1.5, "1", 0])
def test_bind_run_contract_rejects_ambiguous_run_ids(tmp_path, run_id):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id, _ = _claim(conn)
        with pytest.raises(kb.RunBindingError, match="run_id"):
            kb.bind_run_contract(conn, task_id, run_id, _binding())


def test_bind_run_contract_commits_before_another_connection_reads(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id, run_id = _claim(conn)
        bound = kb.bind_run_contract(conn, task_id, run_id, _binding())

    with contextlib.closing(kb.connect(db_path)) as reader:
        observed = kb.get_run(reader, run_id)

    assert bound.maos_run_binding == kb.validate_run_binding(_binding())
    assert observed is not None
    assert observed.maos_run_binding == bound.maos_run_binding


def test_run_binding_migration_and_legacy_null_binding_are_supported(tmp_path):
    db_path = tmp_path / "legacy-kanban.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
            status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
            completed_at INTEGER, workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT, claim_lock TEXT, claim_expires INTEGER
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            profile TEXT, step_key TEXT, status TEXT NOT NULL, claim_lock TEXT,
            claim_expires INTEGER, worker_pid INTEGER, max_runtime_seconds INTEGER,
            last_heartbeat_at INTEGER, started_at INTEGER NOT NULL, ended_at INTEGER,
            outcome TEXT, summary TEXT, metadata TEXT, error TEXT
        );
        """
    )
    legacy.commit()
    legacy.close()

    kb.init_db(db_path)
    kb.init_db(db_path)
    with contextlib.closing(kb.connect(db_path)) as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")
        }
        task_id, run_id = _claim(conn)
        run = kb.get_run(conn, run_id)

    assert "maos_run_binding_json" in columns
    assert task_id
    assert run is not None and run.maos_run_binding is None


def test_persisted_run_binding_cannot_be_replaced_or_cleared_by_sql(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id, run_id = _claim(conn)
        kb.bind_run_contract(conn, task_id, run_id, _binding())
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE task_runs SET maos_run_binding_json = NULL WHERE id = ?",
                (run_id,),
            )
        run = kb.get_run(conn, run_id)

    assert run is not None
    assert run.maos_run_binding == kb.validate_run_binding(_binding())


def test_bind_run_contract_requires_the_current_running_task_attempt(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id, run_id = _claim(conn)
        other_task_id, _ = _claim(conn)

        with pytest.raises(kb.RunBindingError, match="belong"):
            kb.bind_run_contract(conn, other_task_id, run_id, _binding())
        assert kb.complete_task(conn, task_id)
        with pytest.raises(kb.RunBindingError, match="current running"):
            kb.bind_run_contract(conn, task_id, run_id, _binding())


def test_bind_run_contract_rejects_a_stale_prior_attempt_after_retry(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id, first_run_id = _claim(conn)
        assert kb.block_task(conn, task_id, reason="retry")
        assert kb.unblock_task(conn, task_id)
        assert kb.claim_task(conn, task_id) is not None
        task = kb.get_task(conn, task_id)
        assert task is not None and task.current_run_id is not None
        assert task.current_run_id != first_run_id

        with pytest.raises(kb.RunBindingError, match="current running"):
            kb.bind_run_contract(conn, task_id, first_run_id, _binding())


def test_bind_run_contract_rejects_a_different_immutable_value(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id, run_id = _claim(conn)
        kb.bind_run_contract(conn, task_id, run_id, _binding())
        changed = _binding()
        changed["parentHarness"] = "other-harness"

        with pytest.raises(kb.RunBindingConflictError, match="different"):
            kb.bind_run_contract(conn, task_id, run_id, changed)


def test_bind_run_contract_rejects_an_open_transaction_without_mutating_it(tmp_path):
    db_path = tmp_path / "kanban.db"
    class RollbackOuterTransaction(Exception):
        pass

    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id, run_id = _claim(conn)
        before = kb.get_task(conn, task_id)
        assert before is not None
        with pytest.raises(RollbackOuterTransaction):
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET priority = 7 WHERE id = ?", (task_id,))
                with pytest.raises(kb.RunBindingError, match="dedicated connection"):
                    kb.bind_run_contract(conn, task_id, run_id, _binding())
                assert conn.execute(
                    "SELECT priority FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()[0] == 7
                raise RollbackOuterTransaction()
        after = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)

    assert after is not None and after.priority == before.priority
    assert run is not None and run.maos_run_binding is None


def test_bind_run_contract_race_persists_only_one_contract(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id, run_id = _claim(conn)
    first = _binding()
    second = _binding()
    second["parentHarness"] = "other-harness"
    barrier = threading.Barrier(2)

    def bind(contract):
        with contextlib.closing(kb.connect(db_path)) as conn:
            barrier.wait(timeout=5)
            try:
                kb.bind_run_contract(conn, task_id, run_id, contract)
                return "ok"
            except kb.RunBindingConflictError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(bind, (first, second)))

    assert sorted(outcomes) == ["conflict", "ok"]
    with contextlib.closing(kb.connect(db_path)) as conn:
        run = kb.get_run(conn, run_id)
    assert run is not None
    assert run.maos_run_binding in [
        kb.validate_run_binding(first), kb.validate_run_binding(second),
    ]


def test_malformed_blob_run_binding_does_not_break_native_run_reads(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id, run_id = _claim(conn)
        conn.execute(
            "UPDATE task_runs SET maos_run_binding_json = ? WHERE id = ?",
            (sqlite3.Binary(b'{"schema":"maos.hermes-run-binding.v1","schema":"duplicate"}'), run_id),
        )
        conn.commit()
        run = kb.get_run(conn, run_id)
        with pytest.raises(kb.RunBindingConflictError, match="malformed"):
            kb.bind_run_contract(conn, task_id, run_id, _binding())

    assert task_id
    assert run is not None
    assert run.status == "running"
    assert run.maos_run_binding is None


def test_invalid_utf8_text_run_binding_does_not_break_get_or_list_reads(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id, run_id = _claim(conn)
        conn.execute(
            "UPDATE task_runs SET maos_run_binding_json = CAST(? AS TEXT) WHERE id = ?",
            (bytes([255]), run_id),
        )
        run = kb.get_run(conn, run_id)
        runs = kb.list_runs(conn, task_id)

    assert run is not None and run.status == "running"
    assert run.maos_run_binding is None
    assert [item.id for item in runs] == [run_id]
    assert runs[0].maos_run_binding is None
