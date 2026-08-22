"""Django backend endpoints for the twin's work-order layer.

Exercised through Django's test client against the real views and the
in-process twin — no database, no server. Each test resets the default twin
so orders from one test never leak into the next."""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DJANGO_DIR = REPO_ROOT / "railmind_django"
if str(DJANGO_DIR) not in sys.path:
    sys.path.insert(0, str(DJANGO_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "railmind_django.settings")

import django  # noqa: E402

django.setup()

from django.test import Client  # noqa: E402


@pytest.fixture()
def client():
    c = Client()
    assert c.post("/api/reset/").status_code == 200
    return c


def _post(client, path, body=None, headers=None):
    return client.post(path, data=body or {}, content_type="application/json", **(headers or {}))


def _create_template(client, track_id="T23", headers=None, **extra):
    body = {"template": "CRITICAL_INCIDENT_RESPONSE", "track_id": track_id, **extra}
    return _post(client, "/api/workorders/", body, headers)


def test_state_exposes_work_orders_and_sim_tick(client):
    state = client.get("/api/state/").json()
    assert state["work_orders"] == []
    assert state["crews"] == {}
    assert isinstance(state["sim_tick"], int)


def test_create_template_order_and_play_it_forward(client):
    _post(client, "/api/track/close/", {"track_id": "T23"})
    res = _create_template(client, incident_id="INC-001")
    assert res.status_code == 201
    wo = res.json()["work_order"]
    assert wo["id"] == "WO-001"
    assert wo["incident_id"] == "INC-001"
    assert wo["status"] == "UNRESOLVED"
    assert [t["action"] for t in wo["tasks"]] == ["CLOSE_TRACK", "DISPATCH_CREW", "REPAIR_TRACK"]
    assert wo["estimated_ticks_remaining"] == 29

    listed = client.get("/api/workorders/").json()
    assert [w["id"] for w in listed["work_orders"]] == ["WO-001"]

    res = _post(client, "/api/tick/", {"ticks": 1})
    assert res.status_code == 200
    assert res.json()["work_orders"][0]["status"] == "PARTIAL"

    detail = client.get("/api/workorders/WO-001/").json()["work_order"]
    statuses = {t["id"]: t["status"] for t in detail["tasks"]}
    assert statuses == {"task_1": "COMPLETED", "task_2": "IN_PROGRESS", "task_3": "PENDING"}

    res = _post(client, "/api/tick/", {"ticks": 28})
    detail = res.json()["work_orders"][0]
    assert detail["status"] == "COMPLETE"
    assert detail["completion_percentage"] == 100
    state = client.get("/api/state/").json()
    assert state["tracks"]["T23"]["status"] == "OPEN"
    assert state["tracks"]["T23"]["health"] >= 0.9


def test_storm_blocks_then_retry_resumes(client):
    _post(client, "/api/track/close/", {"track_id": "T23"})
    _post(client, "/api/weather/set/", {"track_id": "T23", "condition": "STORM"})
    _create_template(client)
    _post(client, "/api/tick/", {"ticks": 1})

    detail = client.get("/api/workorders/WO-001/").json()["work_order"]
    assert detail["status"] == "BLOCKED"
    blocked = detail["tasks"][1]
    assert blocked["status"] == "BLOCKED"
    assert blocked["blocking_reason"] == "Severe storm prevents crew access to T23"

    _post(client, "/api/weather/set/", {"track_id": "T23", "condition": "CLEAR"})
    res = _post(client, "/api/workorders/WO-001/retry/")
    assert res.status_code == 200
    assert res.json()["retried"] == ["task_2"]
    assert res.json()["work_order"]["status"] == "PARTIAL"

    # Nothing left to retry now
    res = _post(client, "/api/workorders/WO-001/retry/", {"task_id": "task_1"})
    assert res.status_code == 400
    assert "only BLOCKED" in res.json()["error"]

    _post(client, "/api/tick/", {"ticks": 1})
    detail = client.get("/api/workorders/WO-001/").json()["work_order"]
    assert detail["tasks"][1]["status"] == "IN_PROGRESS"
    assert client.get("/api/state/").json()["crews"]["CREW-001"]["status"] == "EN_ROUTE"


def test_explicit_order_with_params(client):
    body = {
        "id": "WO-CUSTOM",
        "target": "T05",
        "type": "PRECAUTIONARY",
        "auto_retry": True,
        "tasks": [
            {"action": "SPEED_RESTRICT", "target": "T05", "params": {"speed_kmh": 40}},
            {"action": "RESTORE_SIGNAL", "target": "T05", "ticks_required": 2, "depends_on": ["task_1"]},
        ],
    }
    res = _post(client, "/api/workorders/", body)
    assert res.status_code == 201, res.json()
    wo = res.json()["work_order"]
    assert wo["id"] == "WO-CUSTOM"
    assert wo["type"] == "PRECAUTIONARY"
    assert wo["auto_retry"] is True
    assert [t["id"] for t in wo["tasks"]] == ["task_1", "task_2"]

    _post(client, "/api/tick/", {"ticks": 3})
    detail = client.get("/api/workorders/WO-CUSTOM/").json()["work_order"]
    assert detail["status"] == "COMPLETE"
    state = client.get("/api/state/").json()
    assert state["tracks"]["T05"]["speed_restriction_kmh"] == 40.0
    src = state["tracks"]["T05"]["source"]
    assert state["stations"][src]["active_signals"]["T05"] == "GREEN"


def test_cancel_endpoint(client):
    _create_template(client)
    _post(client, "/api/tick/", {"ticks": 1})
    res = _post(client, "/api/workorders/WO-001/cancel/", {"reason": "Stood down"})
    assert res.status_code == 200
    wo = res.json()["work_order"]
    assert wo["status"] == "CANCELLED"
    assert wo["cancel_reason"] == "Stood down"
    assert [t["status"] for t in wo["tasks"]] == ["COMPLETED", "CANCELLED", "CANCELLED"]
    assert client.get("/api/state/").json()["crews"] == {}
    res = _post(client, "/api/workorders/WO-001/cancel/")
    assert res.status_code == 400


def test_validation_and_not_found(client):
    assert client.get("/api/workorders/WO-404/").status_code == 404
    assert _post(client, "/api/workorders/WO-404/cancel/").status_code == 404
    assert _post(client, "/api/workorders/WO-404/retry/").status_code == 404

    res = _post(client, "/api/workorders/", {"template": "NOPE", "track_id": "T23"})
    assert res.status_code == 400
    res = _create_template(client, track_id="T99")
    assert res.status_code == 404
    res = _post(client, "/api/workorders/", {"template": "CRITICAL_INCIDENT_RESPONSE"})
    assert res.status_code == 400
    res = _post(client, "/api/workorders/", {"target": "T23"})
    assert res.status_code == 400
    res = _post(client, "/api/workorders/", {"target": "T23", "tasks": [
        {"action": "CLOSE_TRACK", "target": "T23", "depends_on": ["ghost"]},
    ]})
    assert res.status_code == 400
    assert "unknown task" in res.json()["error"]
    res = _post(client, "/api/workorders/", {"target": "T23", "tasks": [
        {"action": "TELEPORT", "target": "T23"},
    ]})
    assert res.status_code == 400
    res = _post(client, "/api/workorders/", {"target": "T23", "tasks": [
        {"action": "REROUTE_TRAIN", "target": "TR99"},
    ]})
    assert res.status_code == 400
    assert "unknown train" in res.json()["error"]

    # The request describes the work; it can never claim the work is done
    res = _post(client, "/api/workorders/", {"target": "T23", "tasks": [
        {"action": "REPAIR_TRACK", "target": "T23", "status": "COMPLETED"},
    ]})
    assert res.status_code == 400
    assert "Unsupported task field" in res.json()["error"]
    res = _post(client, "/api/workorders/", {"target": "T23", "tasks": [
        {"action": "REPAIR_TRACK", "target": "T23", "rollback": {"health": "x"}},
    ]})
    assert res.status_code == 400
    assert client.get("/api/workorders/").json()["work_orders"] == []

    for ticks in (0, 201, "5", True):
        res = _post(client, "/api/tick/", {"ticks": ticks})
        assert res.status_code == 400, ticks


def test_non_object_bodies_are_rejected_not_crashed(client):
    _create_template(client)
    for path in ("/api/tick/", "/api/workorders/",
                 "/api/workorders/WO-001/cancel/", "/api/workorders/WO-001/retry/"):
        for body in ("[1]", '"x"', "5"):
            res = client.post(path, data=body, content_type="application/json")
            assert res.status_code == 400, (path, body)
            assert res.json()["error"] == "Request body must be a JSON object"
    res = _post(client, "/api/workorders/", {"target": "TR00", "tasks": [
        {"action": "REROUTE_TRAIN", "target": "TR00", "params": {"destination": []}},
    ]})
    assert res.status_code == 400
    res = _post(client, "/api/workorders/", {"target": "T23", "tasks": [
        {"action": "CLOSE_TRACK", "target": "T23", "depends_on": "task_1"},
    ]})
    assert res.status_code == 400


def test_work_orders_are_per_session(client):
    session = _post(client, "/api/copy/").json()["session_id"]
    res = _create_template(client, headers={"HTTP_X_SESSION_ID": session})
    assert res.status_code == 201
    assert client.get("/api/workorders/").json()["work_orders"] == []
    sandbox = client.get("/api/workorders/", HTTP_X_SESSION_ID=session).json()["work_orders"]
    assert [w["id"] for w in sandbox] == ["WO-001"]
    assert client.get("/api/workorders/", HTTP_X_SESSION_ID="nope").status_code == 404
