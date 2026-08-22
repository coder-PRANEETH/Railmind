import pytest
from fastapi.testclient import TestClient

import all_agent


@pytest.fixture()
def client():
    all_agent._reset_embedded_twin()
    all_agent._twin_base_cache.update(
        {"base": None, "snapshot": None, "checked_at": float("inf")}  # force embedded
    )
    all_agent._last_run.update({"final": None, "twin_source": None, "ts": 0.0})
    all_agent._LEDGER.reset()
    try:
        yield TestClient(all_agent.app)
    finally:
        all_agent._twin_base_cache.update({"base": None, "snapshot": None, "checked_at": 0.0})
        all_agent._last_run.update({"final": None, "twin_source": None, "ts": 0.0})
        all_agent._LEDGER.reset()
        all_agent._reset_embedded_twin()


def test_run_returns_three_plans(client):
    resp = client.post("/run")
    assert resp.status_code == 200
    body = resp.json()
    assert {p["id"] for p in body["candidate_plans"]} == {"A", "B", "C"}
    assert body["twin_source"] == "embedded"
    assert body["explanation"]["source"] in {"rules", "llm"}


def test_state_serves_trains(client):
    resp = client.get("/state")
    assert resp.status_code == 200
    assert resp.json()["trains"]


def test_run_stream_rejects_unknown_condition(client):
    resp = client.get("/run-stream", params={"weather_track": "T05", "condition": "SNOW"})
    assert resp.status_code == 400


def test_inject_unknown_track_404s(client):
    resp = client.post("/simulate-track-failure/NOPE")
    assert resp.status_code == 404


def test_apply_plan_executes_the_approved_plan(client):
    ran = client.post("/run").json()
    approved = ran["recommended_action"]["id"]
    resp = client.post("/apply-plan", params={"plan_id": approved})
    assert resp.status_code == 200
    body = resp.json()
    assert body["executed_plan"] == approved
    # Apply must answer from the template, never a slow LLM call
    assert body["explanation"]["source"] == "rules"


def test_apply_unknown_plan_404s(client):
    client.post("/run")
    resp = client.post("/apply-plan", params={"plan_id": "Z"})
    assert resp.status_code in {400, 404, 422}


def test_reset_succeeds(client):
    assert client.post("/reset").status_code == 200


def test_reset_invalidates_cached_plans(client):
    # Inject → plans cached → reset reopens T23. Applying afterwards must run
    # a fresh pipeline against the reset twin, not replay the cached plan that
    # would silently re-close T23 and reroute trains around it.
    ran = client.post("/simulate-track-failure/T23").json()
    assert "T23" in ran["failed_tracks"]
    assert client.post("/reset").status_code == 200
    assert all_agent._last_run["final"] is None
    assert all_agent._embedded_snapshot()["tracks"]["T23"]["status"] != "CLOSED"

    resp = client.post("/apply-plan")
    assert resp.status_code == 200
    body = resp.json()
    assert not any("T23" in action for action in body["applied_actions"])
    assert "T23" not in body["failed_tracks"]
    assert all_agent._embedded_snapshot()["tracks"]["T23"]["status"] != "CLOSED"


# ── escalation endpoints ────────────────────────────────────────────────────

def test_escalation_starts_at_steady_state(client):
    body = client.get("/escalation").json()
    assert body["code"] == "L0"
    assert body["totals"]["open"] == 0
    assert body["resolution"] == "COMPLETE"


def test_injected_failure_opens_an_escalated_incident(client):
    run = client.post("/simulate-track-failure/T23").json()
    assert run["escalation"]["totals"]["open"] == 1
    incident = run["escalation"]["incidents"][0]
    assert incident["track_id"] == "T23"
    assert incident["level"] >= 1
    assert incident["resolution"] in ("UNRESOLVED", "PARTIAL", "BLOCKED")

    # The same state is served to the console's poll, not just to the runner.
    polled = client.get("/escalation").json()
    assert polled["incidents"][0]["id"] == incident["id"]


def test_run_stream_pipeline_reports_escalation(client):
    body = client.post("/run").json()
    assert "escalation" in body
    assert any(e["source"] == "Escalation" for e in body["execution_log"])


def test_acknowledge_records_the_owner(client):
    client.post("/simulate-track-failure/T23")
    body = client.post("/incidents/INC-001/acknowledge",
                       params={"owner": "Controller Rao"}).json()
    assert body["status"] == "success"
    incident = next(i for i in body["incidents"] if i["id"] == "INC-001")
    assert incident["acknowledged_by"] == "Controller Rao"
    assert any(e["kind"] == "acknowledged" for e in incident["events"])


def test_acknowledge_unknown_incident_404s(client):
    assert client.post("/incidents/INC-404/acknowledge").status_code == 404


def test_brief_falls_back_to_rules_without_a_key(client):
    client.post("/simulate-track-failure/T23")
    body = client.post("/incidents/INC-001/brief").json()
    assert body["source"] == "rules"
    assert body["incident_id"] == "INC-001"
    assert body["text"].startswith("SITUATION:")


def test_brief_for_unknown_incident_404s(client):
    assert client.post("/incidents/INC-404/brief").status_code == 404


def test_apply_plan_registers_work_items_and_regrades(client):
    client.post("/simulate-track-failure/T23")
    before = client.get("/escalation").json()["incidents"][0]
    assert before["progress"]["actions_total"] == 0

    applied = client.post("/apply-plan").json()
    incident = applied["escalation"]["incidents"][0]
    assert incident["progress"]["actions_total"] > 0
    # The closure the plan committed to is verifiable on the twin immediately.
    assert incident["progress"]["actions_done"] > 0
    assert any(e["source"] == "Escalation" for e in applied["execution_log"])


def test_reset_clears_the_ledger(client):
    client.post("/simulate-track-failure/T23")
    assert client.get("/escalation").json()["totals"]["open"] == 1
    client.post("/reset")
    after = client.get("/escalation").json()
    assert after["totals"]["open"] == 0
    assert after["incidents"] == []


# ── work orders (field work on the twin) ────────────────────────────────────

def _quiet_twin():
    # Exact task states below must not be disturbed by a random storm
    all_agent._get_embedded_twin().ambient_weather = False


def test_apply_plan_orders_field_work_for_the_sections_it_closes(client):
    client.post("/simulate-track-failure/T23")
    _quiet_twin()
    body = client.post("/apply-plan", params={"plan_id": "A"}).json()
    # The closure landed immediately; the field work went to the twin
    assert any("Closed track T23" in a for a in body["applied_actions"])
    proposal = body["work_order"]
    assert proposal["status"] == "DISPATCHED"
    assert proposal["twin_orders"] == [proposal["work_order_id"]]
    assert any(e["source"] == "Field" and f"{proposal['work_order_id']} (T23)" in e["message"]
               for e in body["execution_log"])

    orders = client.get("/workorders").json()["work_orders"]
    assert [o["target"] for o in orders] == ["T23"]
    order = orders[0]
    assert order["id"] == proposal["work_order_id"]
    assert order["incident_id"] == "INC-001"
    assert order["type"] == "CRITICAL_INCIDENT_RESPONSE"
    # Only the specialists' field requirements become twin tasks — the
    # closure was an operational action, already carried out
    assert [t["action"] for t in order["tasks"]] == ["DISPATCH_CREW", "REPAIR_TRACK"]
    assert order["tasks"][1]["depends_on"] == [order["tasks"][0]["id"]]

    # The escalation view carries the field work and the ledger's work items
    incident = body["escalation"]["incidents"][0]
    assert incident["work_order_id"] == order["id"]
    assert incident["field_work"]["id"] == order["id"]
    assert incident["field_work"]["status"] == "UNRESOLVED"
    kinds = {w["kind"]: w["state"] for w in incident["work_items"]}
    assert kinds["close_track"] == "DONE"
    assert kinds["dispatch_crew"] == "PENDING"     # proved by a crew on site, not by acceptance
    assert kinds["repair_track"] == "PENDING"
    assert incident["resolution"] != "COMPLETE"

    # Applying the plan again does not stack a second order on the section
    client.post("/apply-plan", params={"plan_id": "A"})
    assert len(client.get("/workorders").json()["work_orders"]) == 1

    # Play the field work out: the repair lands and the incident completes
    res = client.post("/workorders/tick", params={"ticks": 31}).json()
    assert res["work_orders"][0]["status"] == "COMPLETE"
    state = client.get("/state").json()
    assert next(t for t in state["tracks"] if t["id"] == "T23")["status"] == "OPEN"
    incident = client.get("/escalation").json()["incidents"][0]
    kinds = {w["kind"]: w["state"] for w in incident["work_items"]}
    assert kinds["dispatch_crew"] == "DONE" and kinds["repair_track"] == "DONE"


def test_minimal_intervention_defers_field_work(client):
    client.post("/simulate-track-failure/T23")
    _quiet_twin()
    body = client.post("/apply-plan", params={"plan_id": "C"}).json()
    assert body["work_order"]["status"] == "DEFERRED"
    assert body["work_order"]["twin_orders"] == []
    assert any(e["source"] == "Field" and "defers field work" in e["message"]
               for e in body["execution_log"])
    assert client.get("/workorders").json()["work_orders"] == []


def test_work_order_verbs_are_carried_out_by_the_twin(client):
    client.post("/simulate-track-failure/T23")
    _quiet_twin()
    client.post("/simulate-weather/T23", params={"condition": "STORM"})
    res = client.post("/workorders/incident-response/T23")
    assert res.status_code == 200
    wo = res.json()["work_order"]
    assert wo["status"] == "UNRESOLVED" and wo["incident_id"] == "INC-001"
    assert client.post("/workorders/incident-response/T23").status_code == 409

    res = client.post("/workorders/tick", params={"ticks": 1}).json()
    order = res["work_orders"][0]
    assert order["status"] == "BLOCKED"
    assert order["tasks"][1]["blocking_reason"] == "Severe storm prevents crew access to T23"
    field_work = res["escalation"]["incidents"][0]["field_work"]
    assert field_work["status"] == "BLOCKED"
    assert field_work["blocked_reason"] == "Severe storm prevents crew access to T23"

    client.post("/simulate-weather/T23", params={"condition": "CLEAR"})
    res = client.post(f"/workorders/{wo['id']}/retry")
    assert res.status_code == 200
    assert res.json()["work_order"]["status"] == "PARTIAL"
    res = client.post(f"/workorders/{wo['id']}/retry")
    assert res.status_code == 200 and res.json()["work_order"]["status"] == "PARTIAL"

    res = client.post(f"/workorders/{wo['id']}/cancel", params={"reason": "Stood down"})
    assert res.status_code == 200
    assert res.json()["work_order"]["status"] == "CANCELLED"
    assert res.json()["work_order"]["cancel_reason"] == "Stood down"
    assert client.post(f"/workorders/{wo['id']}/cancel").status_code == 400

    assert client.post("/workorders/WO-404/retry").status_code == 404
    assert client.post("/workorders/WO-404/cancel").status_code == 404
    assert client.post("/workorders/tick", params={"ticks": 0}).status_code == 400
    assert client.post("/workorders/incident-response/NOPE").status_code == 404


def test_state_carries_field_work_and_reset_clears_it(client):
    client.post("/simulate-track-failure/T23")
    client.post("/workorders/incident-response/T23")
    state = client.get("/state").json()
    assert state["work_orders"][0]["target"] == "T23"
    assert isinstance(state["sim_tick"], int)
    assert state["crews"] == {}
    client.post("/reset")
    assert client.get("/workorders").json()["work_orders"] == []
    assert client.get("/state").json()["work_orders"] == []
