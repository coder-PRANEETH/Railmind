import time

import pytest
import requests

import all_agent


def fresh_snapshot():
    all_agent._reset_embedded_twin()
    return all_agent._embedded_snapshot()


def run_pipeline(snapshot, extra_log=None):
    return all_agent.pipeline.invoke(all_agent._initial_state(snapshot, extra_log=extra_log))


def test_baseline_run_produces_three_scored_plans():
    final = run_pipeline(fresh_snapshot())
    resp = all_agent._format_response(final, "embedded")
    assert {p["id"] for p in resp["candidate_plans"]} == {"A", "B", "C"}
    for p in resp["candidate_plans"]:
        assert 0 <= p["score"] <= 100
    assert resp["recommended_action"]["score"] == max(p["score"] for p in resp["candidate_plans"])


def test_track_failure_triggers_reroutes_and_penalizes_inaction():
    snapshot = fresh_snapshot()
    snapshot["tracks"]["T23"]["health"] = 0.0
    snapshot["tracks"]["T23"]["status"] = "CLOSED"
    final = run_pipeline(snapshot)
    resp = all_agent._format_response(final, "embedded")

    assert "T23" in resp["failed_tracks"]
    # Trains crossing Nagpur-Raipur must be rerouted or held
    assert resp["rerouted_trains"] or resp["held_trains"]
    # Do-nothing plan C must be heavily penalized (stranded trains)
    by_id = {p["id"]: p for p in resp["candidate_plans"]}
    assert by_id["C"]["score"] < by_id["A"]["score"]
    assert by_id["C"]["score"] < by_id["B"]["score"]
    assert resp["recommended_action"]["id"] != "C"


def test_reroutes_avoid_the_failed_edge():
    snapshot = fresh_snapshot()
    snapshot["tracks"]["T23"]["health"] = 0.0
    snapshot["tracks"]["T23"]["status"] = "CLOSED"
    final = run_pipeline(snapshot)
    for s in final["routing_strategies"]:
        if not s["strategy_id"].startswith("R_REROUTE_"):
            continue
        route = s["new_route"]
        for i in range(len(route) - 1):
            assert {route[i], route[i + 1]} != {"NAGPUR_JUNCT", "RAIPUR_JUNCT"}


def test_routing_never_emits_degenerate_single_station_reroute():
    # A train sitting at its terminus while its route crosses a closed track
    # must not produce a 1-station reroute (which would freeze it forever).
    snapshot = fresh_snapshot()
    snapshot["tracks"]["T23"]["health"] = 0.0
    snapshot["tracks"]["T23"]["status"] = "CLOSED"
    for train in snapshot["trains"].values():
        route = train["route"]
        crosses = any(
            {route[i], route[i + 1]} == {"NAGPUR_JUNCT", "RAIPUR_JUNCT"}
            for i in range(len(route) - 1)
        )
        if crosses:
            train["current_station"] = route[-1]  # parked at the terminus
    final = run_pipeline(snapshot)
    for s in final["routing_strategies"]:
        if s["strategy_id"].startswith("R_REROUTE_"):
            assert len(s["new_route"]) >= 2


def test_storm_activates_emergency_weather_protocol():
    snapshot = fresh_snapshot()
    snapshot["weather"] = {"T05": "STORM"}
    final = run_pipeline(snapshot)
    assert final["weather_risk"]["T05"] == 85.0
    assert final["weather_strategies"][0]["strategy_id"] == "W1"


def test_mcdm_normalisation_orders_by_weighted_cost():
    values = [10.0, 20.0, 30.0]
    normed = all_agent._minmax(values)
    assert normed == [0.0, 0.5, 1.0]
    assert all_agent._minmax([5.0, 5.0]) == [0.0, 0.0]


def test_plan_payload_carries_preview_data():
    snapshot = fresh_snapshot()
    snapshot["tracks"]["T23"]["health"] = 0.0
    snapshot["tracks"]["T23"]["status"] = "CLOSED"
    final = run_pipeline(snapshot)
    resp = all_agent._format_response(final, "embedded")
    plan_a = next(p for p in resp["candidate_plans"] if p["id"] == "A")
    assert plan_a["failed_tracks"] == ["T23"]
    plan_c = next(p for p in resp["candidate_plans"] if p["id"] == "C")
    assert plan_c["rerouted_trains"] == []


def test_template_explanation_mentions_selected_plan():
    snapshot = fresh_snapshot()
    snapshot["tracks"]["T23"]["health"] = 0.0
    snapshot["tracks"]["T23"]["status"] = "CLOSED"
    final = run_pipeline(snapshot)
    text = all_agent._template_explanation(final)
    assert final["best_plan"]["plan_name"] in text
    assert "/100" in text


def test_execution_log_has_structured_entries():
    final = run_pipeline(fresh_snapshot())
    resp = all_agent._format_response(final, "embedded")
    assert len(resp["execution_log"]) >= 5
    for entry in resp["execution_log"]:
        assert set(entry) == {"t", "source", "message"}
    # Newest first: Escalation grades the world after Master has chosen, so it
    # is the last stage to log and therefore the first entry.
    sources = [e["source"] for e in resp["execution_log"]]
    assert sources[0] == "Escalation"
    assert sources.index("Escalation") < sources.index("Master")


def test_twin_reset_clears_embedded_even_with_remote_base(monkeypatch):
    # Injections made while the remote was unreachable must not resurface
    # after a later fallback flip: reset must always clear the embedded twin.
    all_agent._reset_embedded_twin()
    all_agent._twin_base_cache["base"] = None
    all_agent._twin_base_cache["checked_at"] = float("inf")  # force embedded
    try:
        all_agent._twin_close_track("T23")
        assert all_agent._embedded_snapshot()["tracks"]["T23"]["status"] == "CLOSED"
    finally:
        all_agent._twin_base_cache["checked_at"] = 0.0

    class _FakeResp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(all_agent, "_twin_base", lambda: "http://fake-twin")
    monkeypatch.setattr(all_agent.requests, "post", lambda *a, **k: _FakeResp())
    all_agent._twin_reset()
    assert all_agent._embedded_snapshot()["tracks"]["T23"]["status"] != "CLOSED"


def test_format_response_use_llm_false_skips_llm(monkeypatch):
    calls = []

    def fake_llm(final):
        calls.append(1)
        return "LLM TEXT"

    monkeypatch.setattr(all_agent, "_llm_explanation", fake_llm)
    final = run_pipeline(fresh_snapshot())
    resp = all_agent._format_response(final, "embedded", use_llm=False)
    assert resp["explanation"]["source"] == "rules"
    assert not calls
    resp2 = all_agent._format_response(final, "embedded")
    assert resp2["explanation"] == {"text": "LLM TEXT", "source": "llm"}
    assert calls


def test_embedded_twin_mutations_persist_across_snapshots():
    all_agent._reset_embedded_twin()
    all_agent._twin_base_cache["base"] = None
    all_agent._twin_base_cache["checked_at"] = float("inf")  # force embedded
    try:
        all_agent._twin_close_track("T23")
        snap = all_agent._embedded_snapshot()
        assert snap["tracks"]["T23"]["status"] == "CLOSED"
        all_agent._twin_reset()
        snap = all_agent._embedded_snapshot()
        assert snap["tracks"]["T23"]["status"] != "CLOSED"
    finally:
        all_agent._twin_base_cache["checked_at"] = 0.0
        all_agent._reset_embedded_twin()


@pytest.fixture
def embedded_only():
    """Force the embedded twin and a clean last-run cache, restoring after."""
    all_agent._reset_embedded_twin()
    all_agent._twin_base_cache.update({"base": None, "snapshot": None, "checked_at": float("inf")})
    all_agent._last_run.update({"final": None, "twin_source": None, "ts": 0.0})
    yield
    all_agent._twin_base_cache.update({"base": None, "snapshot": None, "checked_at": 0.0})
    all_agent._last_run.update({"final": None, "twin_source": None, "ts": 0.0})
    all_agent._reset_embedded_twin()


def test_state_probe_body_is_reused_not_refetched(monkeypatch):
    all_agent._twin_base_cache.update({"base": None, "snapshot": None, "checked_at": 0.0})
    probe_body = {"trains": {}, "tracks": {}, "weather": {}}
    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return probe_body

    def fake_get(url, timeout):
        calls.append(url)
        if "127.0.0.1:8000" in url:
            return _Resp()
        raise requests.RequestException("dead")

    # The hermetic conftest fixture empties TWIN_CANDIDATES; this test needs a
    # probeable candidate (requests.get is stubbed, so nothing hits the network).
    monkeypatch.setattr(all_agent, "TWIN_CANDIDATES", ["http://127.0.0.1:8000"])
    monkeypatch.setattr(all_agent.requests, "get", fake_get)
    try:
        snapshot, source = all_agent.fetch_snapshot()
        assert source == "http://127.0.0.1:8000"
        assert snapshot == probe_body
        assert calls.count(f"{source}/api/state/") == 1  # probe body reused — no second GET
        all_agent.fetch_snapshot()  # cached-base path: exactly one fresh GET
        assert calls.count(f"{source}/api/state/") == 2
    finally:
        all_agent._twin_base_cache.update({"base": None, "snapshot": None, "checked_at": 0.0})


def test_apply_plan_executes_the_approved_cached_plan(embedded_only, monkeypatch):
    snapshot = all_agent._embedded_snapshot()
    snapshot["tracks"]["T23"]["health"] = 0.0
    snapshot["tracks"]["T23"]["status"] = "CLOSED"
    final = run_pipeline(snapshot)
    all_agent._record_run(final, "embedded")
    best_id = final["best_plan"]["plan_id"]
    approved = next(p for p in final["plans"] if p["plan_id"] == best_id)
    expected_reroutes = [
        f"Rerouted {a['train_id']}" for a in approved["actions"]
        if isinstance(a, dict) and a.get("strategy_id", "").startswith("R_REROUTE_")
    ]
    assert expected_reroutes  # scenario must actually produce reroutes

    # The twin diverged since approval — a recompute would see no failure and
    # execute a different plan under the same letter.
    all_agent._reset_embedded_twin()

    # Apply re-reads the twin afterwards to grade what actually landed, so the
    # invariant under test is narrower than "no I/O": the *pipeline* must not
    # run again, because a recompute would see no failure and execute a
    # different plan under the same letter.
    class _ExplodingPipeline:
        def invoke(self, *_args, **_kwargs):
            raise AssertionError("cached apply must not re-run the pipeline")

    monkeypatch.setattr(all_agent, "pipeline", _ExplodingPipeline())
    resp = all_agent.apply_plan()
    assert resp["executed_plan"] == best_id
    for entry in expected_reroutes:
        assert entry in resp["applied_actions"]


def test_apply_plan_recomputes_when_cache_is_stale(embedded_only, monkeypatch):
    final = run_pipeline(all_agent._embedded_snapshot())
    all_agent._record_run(final, "embedded")
    all_agent._last_run["ts"] = time.time() - 601  # outside the 10-minute window
    calls = []
    real_fetch = all_agent.fetch_snapshot
    monkeypatch.setattr(all_agent, "fetch_snapshot", lambda: calls.append(1) or real_fetch())
    resp = all_agent.apply_plan()
    assert calls  # stale cache → fresh pipeline run
    assert resp["executed_plan"] in {"A", "B", "C"}


def test_apply_plan_partial_failure_reports_applied_actions(embedded_only, monkeypatch):
    snapshot = all_agent._embedded_snapshot()
    snapshot["tracks"]["T23"]["health"] = 0.0
    snapshot["tracks"]["T23"]["status"] = "CLOSED"
    final = run_pipeline(snapshot)
    all_agent._record_run(final, "embedded")
    plan_a = next(p for p in final["plans"] if p["plan_id"] == "A")
    assert any(
        a.get("strategy_id", "").startswith("R_REROUTE_")
        for a in plan_a["actions"] if isinstance(a, dict)
    )

    def _down(train_id, route):
        raise requests.RequestException("twin went away")

    monkeypatch.setattr(all_agent, "_twin_reroute_train", _down)
    # Plan A closes tracks before rerouting, so the failure hits mid-loop
    resp = all_agent.apply_plan(plan_id="A")
    assert "Closed track T23" in resp["applied_actions"]
    assert not any(a.startswith("Rerouted") for a in resp["applied_actions"])
    assert any("partially executed" in e["message"] for e in resp["execution_log"])


def test_weather_protocol_changes_simulation_outcome():
    # Exercise the plans the planner actually produces: A carries the W1
    # closure, C only monitors — the weather deltas must show up in the
    # simulated outcomes, not just in the strategy labels.
    snapshot = fresh_snapshot()
    snapshot["weather"] = {"T05": "STORM"}
    final = run_pipeline(snapshot)
    by_id = {r["plan_id"]: r for r in final["simulation_results"]}
    assert by_id["A"]["delay"] > by_id["C"]["delay"]
    assert by_id["A"]["risk"] < by_id["C"]["risk"]


def _plan_weather_actions(plan):
    return [
        a for a in plan["actions"]
        if isinstance(a, dict) and a.get("strategy_id", "").startswith("W")
    ]


def test_storm_weather_strategies_follow_plan_doctrine():
    # Bundling every W strategy into every plan let an approved "Minimal
    # Intervention" close the stormy track and gave all plans identical
    # weather deltas (erased by min-max normalisation). Each plan must carry
    # only its doctrine's weather strategies.
    snapshot = fresh_snapshot()
    snapshot["weather"] = {"T05": "STORM"}
    final = run_pipeline(snapshot)
    assert final["weather_strategies"][0]["strategy_id"] == "W1"  # pipeline list unchanged
    by_id = {p["plan_id"]: p for p in final["plans"]}

    w_ids = {p: {a["strategy_id"] for a in _plan_weather_actions(by_id[p])} for p in "ABC"}
    assert "W1" in w_ids["A"]                       # aggressive: emergency closure
    assert w_ids["A"] <= {"W1", "W2"}
    assert w_ids["B"] == {"W2"}                     # restrictions only
    assert w_ids["C"] <= {"W4", "W5"} and w_ids["C"]  # monitoring only


def test_medium_risk_weather_gives_plan_a_real_mitigation():
    # RAIN/FOG emit only W3/W4 (medium band) — "Safety First" must degrade
    # to the strongest available mitigation, never an empty weather set.
    snapshot = fresh_snapshot()
    snapshot["weather"] = {"T05": "RAIN"}
    final = run_pipeline(snapshot)
    by_id = {p["plan_id"]: p for p in final["plans"]}
    a_ids = {a["strategy_id"] for a in _plan_weather_actions(by_id["A"])}
    assert "W3" in a_ids


def test_unmitigated_storm_costs_the_do_nothing_plan():
    # Plan C leaves trains running through the storm — its simulated risk
    # must exceed Plan A's, which closes and reroutes around the track.
    snapshot = fresh_snapshot()
    snapshot["weather"] = {"T05": "STORM"}
    final = run_pipeline(snapshot)
    by_id = {r["plan_id"]: r for r in final["simulation_results"]}
    assert by_id["C"]["risk"] > by_id["A"]["risk"]


def test_storm_weather_deltas_differentiate_plan_scores():
    # With doctrine-specific W strategies the weather component must genuinely
    # differ across A/B/C — identical contributions would normalise away.
    snapshot = fresh_snapshot()
    snapshot["weather"] = {"T05": "STORM"}
    final = run_pipeline(snapshot)
    by_id = {p["plan_id"]: p for p in final["plans"]}

    def w_contribution(plan):
        sim = all_agent._simulate_plan(
            {"plan_id": plan["plan_id"], "plan_name": plan["plan_name"],
             "actions": _plan_weather_actions(plan)},
            snapshot)
        return (sim["delay"], sim["risk"])

    contributions = [w_contribution(by_id[p]) for p in "ABC"]
    assert len(set(contributions)) == 3


def _stormy_track_run():
    """Inject a storm on an open track, run the pipeline, cache the run."""
    snapshot = all_agent._embedded_snapshot()
    track_id = next(
        tid for tid in sorted(snapshot["tracks"])
        if snapshot["tracks"][tid]["status"] != "CLOSED"
    )
    all_agent._twin_set_weather(track_id, "STORM")
    final = run_pipeline(all_agent._embedded_snapshot())
    assert final["weather_strategies"][0]["strategy_id"] == "W1"
    all_agent._record_run(final, "embedded")
    return track_id


def test_apply_plan_a_executes_weather_protocol_closure(embedded_only):
    track_id = _stormy_track_run()
    resp = all_agent.apply_plan(plan_id="A")
    assert any(f"Closed track {track_id}" in a for a in resp["applied_actions"])
    assert all_agent._embedded_snapshot()["tracks"][track_id]["status"] == "CLOSED"


def test_apply_plan_c_leaves_stormy_track_open(embedded_only):
    # Minimal Intervention monitors only — approving it must not run the W1
    # closure that belongs to Safety First.
    track_id = _stormy_track_run()
    resp = all_agent.apply_plan(plan_id="C")
    assert not any(a.startswith("Closed track") for a in resp["applied_actions"])
    assert all_agent._embedded_snapshot()["tracks"][track_id]["status"] != "CLOSED"


def test_run_stream_rejects_unknown_weather_condition():
    from fastapi.testclient import TestClient

    client = TestClient(all_agent.app)
    resp = client.get("/run-stream", params={"weather_track": "T23", "condition": "SNOW"})
    assert resp.status_code == 400
    assert "SNOW" in resp.json()["detail"]


def test_run_stream_surfaces_pipeline_crash_as_log_event(embedded_only, monkeypatch):
    from fastapi.testclient import TestClient

    class _BoomPipeline:
        def stream(self, state):
            raise RuntimeError("boom")

    monkeypatch.setattr(all_agent, "pipeline", _BoomPipeline())
    client = TestClient(all_agent.app)
    resp = client.get("/run-stream")
    assert resp.status_code == 200
    # EventSource dispatches `event: error` to source.onerror without any
    # payload — the failure must arrive as a log event the timeline renders.
    assert "event: error" not in resp.text
    assert "event: log" in resp.text
    assert "pipeline failed: boom" in resp.text
    assert '"source": "System"' in resp.text
