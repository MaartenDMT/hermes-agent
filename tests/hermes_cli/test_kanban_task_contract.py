import contextlib
import copy
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_task_contract import TaskContractError, decode_task_contract


def _work_contract() -> dict:
    return {
        "schema": "maos.hermes-task-contract.v1",
        "source": {
            "kind": "hermes-task",
            "owner": "hermes.board",
            "reference": "source:agent-wiki/operations/goals.md#1",
            "revision": "r1",
            "digest": "sha256:" + "a" * 64,
        },
        "lane": "platform",
        "acceptanceCriteria": ["The contract survives a database reopen"],
        "expectedArtifacts": ["operations/goals.md"],
        "owner": "maarten",
        "riskClass": "local-safe",
        "approvalRequirements": [],
        "route": {
            "eligibleHarnesses": ["codex"],
            "requiredCapabilities": ["python"],
            "requiredSkills": ["maarten-coding-workflow"],
            "autonomy": "A1",
        },
    }


def test_create_persists_typed_contract_with_accountable_owner(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id = kb.create_task(
            conn,
            title="Implement bounded task contract",
            assignee="implementation-worker",
            work_contract=_work_contract(),
        )

    with contextlib.closing(kb.connect(db_path)) as conn:
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.assignee == "implementation-worker"
    assert task.work_contract == _work_contract()
    assert task.work_contract["owner"] == "maarten"
    assert task.updated_at == task.created_at


@pytest.mark.parametrize(
    "native_field,native_value",
    [
        ("id", "caller-owned-id"),
        ("objective", "A competing task title"),
        ("project", {"id": "competing-project"}),
        ("state", "done"),
        ("dependencies", ["competing-parent"]),
        ("createdAt", "2026-09-09T00:00:00Z"),
        ("updatedAt", "2026-09-09T00:00:00Z"),
    ],
)
def test_contract_rejects_fields_owned_by_native_kanban(
    tmp_path, native_field, native_value
):
    db_path = tmp_path / "kanban.db"
    contract = _work_contract()
    contract[native_field] = native_value

    with contextlib.closing(kb.connect(db_path)) as conn:
        with pytest.raises(TaskContractError, match="unknown fields"):
            kb.create_task(
                conn,
                title="Reject duplicate lifecycle authority",
                assignee="implementation-worker",
                work_contract=contract,
            )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_invalid_contract_is_rejected_before_persistence(tmp_path):
    db_path = tmp_path / "kanban.db"
    contract = _work_contract()
    contract["route"]["autonomy"] = "A3"

    with contextlib.closing(kb.connect(db_path)) as conn:
        with pytest.raises(TaskContractError, match="autonomy"):
            kb.create_task(
                conn,
                title="Reject invalid contract",
                assignee="implementation-worker",
                work_contract=contract,
            )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_idempotency_key_refuses_a_different_contract(tmp_path):
    db_path = tmp_path / "kanban.db"
    original = _work_contract()
    changed = copy.deepcopy(original)
    changed["owner"] = "someone-else"

    with contextlib.closing(kb.connect(db_path)) as conn:
        first_id = kb.create_task(
            conn,
            title="Idempotent task",
            assignee="implementation-worker",
            idempotency_key="contract-key",
            work_contract=original,
        )
        assert kb.create_task(
            conn,
            title="Idempotent task",
            assignee="implementation-worker",
            idempotency_key="contract-key",
            work_contract=original,
        ) == first_id
        with pytest.raises(ValueError, match="different work_contract"):
            kb.create_task(
                conn,
                title="Idempotent task",
                assignee="implementation-worker",
                idempotency_key="contract-key",
                work_contract=changed,
            )

        task = kb.get_task(conn, first_id)
        assert task is not None
        assert task.work_contract == original
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_concurrent_idempotency_key_cannot_persist_different_contracts(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)):
        pass
    first = _work_contract()
    second = copy.deepcopy(first)
    second["owner"] = "second-owner"
    barrier = threading.Barrier(2)

    def create(contract):
        with contextlib.closing(kb.connect(db_path)) as conn:
            barrier.wait(timeout=5)
            try:
                return ("ok", kb.create_task(
                    conn,
                    title="Concurrent idempotent task",
                    idempotency_key="concurrent-contract-key",
                    work_contract=contract,
                ))
            except ValueError as exc:
                return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (first, second)))

    assert sorted(result[0] for result in results) == ["error", "ok"]
    assert "different work_contract" in next(
        result[1] for result in results if result[0] == "error"
    )
    with contextlib.closing(kb.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT id, contract_json FROM tasks WHERE idempotency_key = ?",
            ("concurrent-contract-key",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["contract_json"] in {
        kb.encode_task_contract(first),
        kb.encode_task_contract(second),
    }


def test_legacy_task_migration_is_idempotent_and_remains_untyped(tmp_path):
    db_path = tmp_path / "legacy-kanban.db"
    legacy = sqlite3.connect(str(db_path))
    legacy.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    legacy.execute(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    legacy.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'Legacy task', 'ready', 1234)"
    )
    legacy.commit()
    legacy.close()

    kb.init_db(db_path)
    kb.init_db(db_path)
    with contextlib.closing(kb.connect(db_path)) as migrated:
        task = kb.get_task(migrated, "legacy")
        columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(tasks)")
        }
        triggers = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }

    assert task is not None
    assert task.work_contract is None
    assert task.updated_at == 1234
    assert {"contract_json", "updated_at"} <= columns
    assert "trg_tasks_updated_at" in triggers


def test_dependency_edges_remain_native_and_advance_updated_at(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        first_parent = kb.create_task(conn, title="First parent")
        second_parent = kb.create_task(conn, title="Second parent")
        child = kb.create_task(
            conn,
            title="Dependent child",
            parents=[first_parent],
            work_contract=_work_contract(),
        )
        before_link = kb.get_task(conn, child)
        assert before_link is not None

        kb.link_tasks(conn, second_parent, child)
        after_link = kb.get_task(conn, child)
        assert after_link is not None
        assert after_link.updated_at > before_link.updated_at
        assert after_link.work_contract == _work_contract()
        assert kb.parent_ids(conn, child) == sorted([first_parent, second_parent])

        kb.unlink_tasks(conn, second_parent, child)
        after_unlink = kb.get_task(conn, child)
        assert after_unlink is not None
        assert after_unlink.updated_at > after_link.updated_at
        assert after_unlink.work_contract == _work_contract()
        assert kb.parent_ids(conn, child) == [first_parent]


def test_decomposed_children_start_with_native_monotonic_updated_at(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        root = kb.create_task(conn, title="Root", triage=True)
        children = kb.decompose_triage_task(
            conn,
            root,
            root_assignee=None,
            children=[
                {"title": "First", "parents": []},
                {"title": "Second", "parents": [0]},
            ],
            auto_promote=False,
        )
        assert children is not None
        first = kb.get_task(conn, children[0])
        second = kb.get_task(conn, children[1])

    assert first is not None and second is not None
    assert first.updated_at == first.created_at
    assert second.updated_at > second.created_at


def test_deleting_archived_parent_advances_dependent_native_revision(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        parent = kb.create_task(conn, title="Parent")
        child = kb.create_task(
            conn,
            title="Child",
            parents=[parent],
            work_contract=_work_contract(),
        )
        assert kb.archive_task(conn, parent) is True
        before_delete = kb.get_task(conn, child)
        assert before_delete is not None and before_delete.status == "ready"

        assert kb.delete_archived_task(conn, parent) is True
        after_delete = kb.get_task(conn, child)

    assert after_delete is not None
    assert after_delete.status == "ready"
    assert after_delete.updated_at > before_delete.updated_at
    assert after_delete.work_contract == _work_contract()


def test_malformed_stored_contract_fails_closed_without_breaking_task_reads(tmp_path):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task_id = kb.create_task(conn, title="Corrupted contract")
        conn.execute(
            "UPDATE tasks SET contract_json = ? WHERE id = ?",
            ('{"schema":"maos.hermes-task-contract.v1","schema":"duplicate"}', task_id),
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.work_contract is None
    with pytest.raises(TaskContractError, match="duplicate JSON key"):
        decode_task_contract(
            '{"schema":"maos.hermes-task-contract.v1","schema":"duplicate"}'
        )
