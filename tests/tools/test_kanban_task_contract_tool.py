import contextlib
import copy
import json

import pytest
from jsonschema import Draft202012Validator

from hermes_cli import kanban_db as kb
from hermes_cli import kanban as kanban_cli
from tools import kanban_tools as kt


def _work_contract() -> dict:
    return {
        "schema": "maos.hermes-task-contract.v1",
        "source": {
            "kind": "hermes-task",
            "owner": "hermes.board",
            "reference": "source:agent-wiki/operations/goals.md#1",
            "revision": "r1",
            "digest": "sha256:" + "b" * 64,
        },
        "lane": "platform",
        "acceptanceCriteria": ["The tool validates before persistence"],
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


def test_kanban_create_round_trips_the_typed_contract(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")

    result = json.loads(
        kt._handle_create(
            {
                "title": "Typed tool task",
                "assignee": "implementation-worker",
                "work_contract": _work_contract(),
            }
        )
    )

    assert result["ok"] is True
    with contextlib.closing(kb.connect(db_path)) as conn:
        task = kb.get_task(conn, result["task_id"])
    assert task is not None
    assert task.work_contract == _work_contract()

    shown = json.loads(kt._handle_show({"task_id": result["task_id"]}))
    listed = json.loads(kt._handle_list({}))
    assert shown["task"]["work_contract"] == _work_contract()
    assert shown["task"]["updated_at"] == task.updated_at
    summary = next(item for item in listed["tasks"] if item["id"] == task.id)
    assert summary["work_contract"] == _work_contract()
    assert summary["updated_at"] == task.updated_at
    assert kanban_cli._task_to_dict(task)["work_contract"] == _work_contract()
    assert kanban_cli._task_to_dict(task)["updated_at"] == task.updated_at


def test_kanban_create_rejects_invalid_contract_without_a_task_row(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    invalid = copy.deepcopy(_work_contract())
    invalid["state"] = "done"

    result = json.loads(
        kt._handle_create(
            {
                "title": "Invalid typed tool task",
                "assignee": "implementation-worker",
                "work_contract": invalid,
            }
        )
    )

    assert "ok" not in result
    assert "unknown fields: state" in result["error"]
    with contextlib.closing(kb.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_tool_schema_and_runtime_reject_the_same_unknown_nested_field(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    contract = copy.deepcopy(_work_contract())
    contract["route"]["workerModel"] = "hidden-duplicate"

    schema = kt.KANBAN_CREATE_SCHEMA["parameters"]["properties"]["work_contract"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["route"]["additionalProperties"] is False

    result = json.loads(
        kt._handle_create(
            {
                "title": "Unknown nested field",
                "assignee": "implementation-worker",
                "work_contract": contract,
            }
        )
    )
    assert "ok" not in result
    assert "work_contract.route has unknown fields" in result["error"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda contract: contract["source"].update({"reference": "../outside"}),
        lambda contract: contract.update({"owner": "ghp_abcdefghijklmnop"}),
        lambda contract: contract.update({"owner": "maarten\n"}),
        lambda contract: contract.update({"acceptanceCriteria": [" "]}),
        lambda contract: contract.update({"approvalRequirements": ["x"] * 21}),
    ],
)
def test_tool_schema_rejects_runtime_invalid_bounded_values(mutate):
    contract = _work_contract()
    mutate(contract)
    schema = kt.KANBAN_CREATE_SCHEMA["parameters"]["properties"]["work_contract"]

    assert list(Draft202012Validator(schema).iter_errors(contract))
