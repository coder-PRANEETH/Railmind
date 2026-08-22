"""Tests for the escalation ladder and the completion signal.

The network here is synthetic on purpose: five stations with one chord and one
leaf, so a closure can either be routed around (chord) or genuinely strand a
train (leaf). The 21-station demo network cannot produce a BLOCKED incident
without closing several corridors, which would make these tests about topology
rather than about the classifier.

    A ─T1─ B ─T2─ C ─T3─ D ─T5─ E
    └──────── T4 ─────────┘        (chord A↔C)
"""

import escalation as esc
import pytest

STATIONS = ["A", "B", "C", "D", "E"]
EDGES = [("A", "B"), ("B", "C"), ("C", "D"), ("A", "C"), ("D", "E")]
TRACK_EDGES = {
    "T1": ("A", "B"),
    "T2": ("B", "C"),
    "T3": ("C", "D"),
    "T4": ("A", "C"),
    "T5": ("D", "E"),
}


def snapshot(closed=(), weather=None, trains=None):
    tracks = {}
    for tid, (src, dst) in TRACK_EDGES.items():
        is_closed = tid in closed
        tracks[tid] = {
            "track_id": tid,
            "source": src,
            "destination": dst,
            "health": 0.0 if is_closed else 0.9,
            "status": "CLOSED" if is_closed else "OPEN",
            "length_km": 100.0,
            "max_speed_kmh": 120.0,
        }
    return {
        "tracks": tracks,
        "trains": trains if trains is not None else default_trains(),
        "weather": weather or {},
        "stations": {s: {"station_id": s, "name": s} for s in STATIONS},
        "graph": {"nodes": STATIONS, "edges": EDGES},
    }


def default_trains():
    return {
        "TR1": {"train_id": "TR1", "route": ["A", "B", "C"], "current_station": "A",
                "passengers": 300, "held": False, "route_index": 0, "progress": 0.0},
        "TR2": {"train_id": "TR2", "route": ["C", "D", "E"], "current_station": "C",
                "passengers": 400, "held": False, "route_index": 0, "progress": 0.0},
    }


def only(payload, incident_id=None):
    incidents = payload["incidents"]
    if incident_id:
        return next(i for i in incidents if i["id"] == incident_id)
    assert len(incidents) == 1, f"expected exactly one incident, got {len(incidents)}"
    return incidents[0]


@pytest.fixture
def ledger():
    return esc.IncidentLedger()


# ── tier movement ───────────────────────────────────────────────────────────

def test_healthy_network_is_steady_state(ledger):
    payload = ledger.observe(snapshot(), now=1000.0)
    assert payload["code"] == "L0"
    assert payload["totals"]["open"] == 0
    assert payload["resolution"] == esc.COMPLETE


def test_failure_with_no_train_affected_stays_advisory(ledger):
    # T4 is the chord; neither train routes over it.
    payload = ledger.observe(snapshot(closed=["T4"]), now=1000.0)
    incident = only(payload)
    assert incident["code"] == "L1"
    assert incident["owner"] == "Section Controller"
    assert incident["affected_trains"] == []


def test_train_routed_into_the_failure_escalates_to_elevated(ledger):
    payload = ledger.observe(snapshot(closed=["T2"]), now=1000.0)
    incident = only(payload)
    assert incident["code"] == "L2"
    assert incident["owner"] == "Divisional Controller"
    assert incident["dispositions"]["TR1"] == esc.TRAIN_EXPOSED
    assert any(d["code"] == "TRAFFIC" for d in incident["drivers"])


def test_held_train_escalates_to_critical(ledger):
    trains = default_trains()
    trains["TR1"]["held"] = True
    payload = ledger.observe(snapshot(closed=["T2"], trains=trains), now=1000.0)
    incident = only(payload)
    assert incident["code"] == "L3"
    assert incident["owner"] == "Divisional Emergency Cell"
    assert incident["dispositions"]["TR1"] == esc.TRAIN_HELD


def test_stranded_train_with_no_alternate_path_is_emergency(ledger):
    # T5 is the only link to E — closing it leaves TR2 with nowhere to go.
    payload = ledger.observe(snapshot(closed=["T5"]), now=1000.0)
    incident = only(payload)
    assert incident["code"] == "L4"
    assert incident["owner"] == "Zonal Emergency Control Room"
    assert incident["dispositions"]["TR2"] == esc.TRAIN_BLOCKED
    assert {d["code"] for d in incident["drivers"]} >= {"NO_PATH", "SEVERED"}
    assert payload["severed_pairs"] > 0


def test_escalation_events_record_the_reason(ledger):
    ledger.observe(snapshot(closed=["T2"]), now=1000.0)
    trains = default_trains()
    trains["TR1"]["held"] = True
    payload = ledger.observe(snapshot(closed=["T2"], trains=trains), now=1010.0)
    incident = only(payload)
    escalations = [e for e in incident["events"] if e["kind"] == "escalated"]
    assert escalations, "a tier movement must be recorded"
    assert escalations[-1]["from"] == "L2" and escalations[-1]["to"] == "L3"
    assert "brought to a stand" in escalations[-1]["reason"]


def test_recovery_de_escalates_to_steady_state(ledger):
    ledger.observe(snapshot(closed=["T2"]), now=1000.0)
    # Corridor reopened and TR1 clear of it.
    trains = default_trains()
    trains["TR1"]["route"] = ["A", "C"]
    payload = ledger.observe(snapshot(trains=trains), now=1020.0)
    incident = only(payload)
    assert incident["resolution"] == esc.COMPLETE
    assert incident["code"] == "L0"
    assert any(e["kind"] == "de-escalated" for e in incident["events"])


# ── dwell-driven escalation ─────────────────────────────────────────────────

def test_dwell_escalates_an_unacknowledged_incident(ledger):
    ledger.observe(snapshot(closed=["T4"]), now=1000.0)          # L1, sla 240s
    payload = ledger.observe(snapshot(closed=["T4"]), now=1000.0 + 239)
    assert only(payload)["code"] == "L1"
    payload = ledger.observe(snapshot(closed=["T4"]), now=1000.0 + 241)
    incident = only(payload)
    assert incident["code"] == "L2"
    assert any("handling level raised" in e["reason"] for e in incident["events"])


def test_acknowledging_restarts_the_dwell_timer(ledger):
    payload = ledger.observe(snapshot(closed=["T4"]), now=1000.0)
    incident_id = only(payload)["id"]
    ledger.acknowledge(incident_id, "Section Controller A", now=1200.0)
    # 239s after the acknowledgement, not after the incident opened.
    payload = ledger.observe(snapshot(closed=["T4"]), now=1200.0 + 239)
    incident = only(payload)
    assert incident["code"] == "L1"
    assert incident["acknowledged_by"] == "Section Controller A"


def test_dwell_still_escalates_work_that_is_acknowledged_but_never_resolved(ledger):
    payload = ledger.observe(snapshot(closed=["T4"]), now=1000.0)
    ledger.acknowledge(only(payload)["id"], "Section Controller A", now=1000.0)
    payload = ledger.observe(snapshot(closed=["T4"]), now=1000.0 + 241)
    incident = only(payload)
    assert incident["code"] == "L2"
    assert "acknowledged but unresolved" in " ".join(
        d["detail"] for d in incident["drivers"] if d["code"] == "DWELL")


def test_escalating_clears_a_stale_acknowledgement(ledger):
    payload = ledger.observe(snapshot(closed=["T2"]), now=1000.0)
    ledger.acknowledge(only(payload)["id"], "Divisional Controller B", now=1000.0)
    trains = default_trains()
    trains["TR1"]["held"] = True
    payload = ledger.observe(snapshot(closed=["T2"], trains=trains), now=1005.0)
    incident = only(payload)
    assert incident["code"] == "L3"
    # The acknowledgement was for a smaller problem; the new tier is unowned.
    assert incident["acknowledged_by"] is None


# ── completion signal: the four states ──────────────────────────────────────

def test_unresolved_before_anything_lands(ledger):
    payload = ledger.observe(snapshot(closed=["T2"]), now=1000.0)
    incident = only(payload)
    assert incident["resolution"] == esc.UNRESOLVED
    assert "No committed action has landed" in incident["resolution_reason"]


def test_partial_when_some_work_is_verified_and_some_is_not(ledger):
    snap = snapshot(closed=["T2"])
    ledger.observe(snap, now=1000.0)
    ledger.record_dispatch("A", [
        {"strategy_id": "T_CLOSE_T2", "track_id": "T2"},
        {"strategy_id": "R_REROUTE_TR1", "train_id": "TR1",
         "old_route": ["A", "B", "C"], "new_route": ["A", "C"]},
    ], snap, now=1001.0)

    # The closure is real on the twin; the reroute never landed.
    payload = ledger.observe(snapshot(closed=["T2"]), now=1002.0)
    incident = only(payload)
    assert incident["resolution"] == esc.PARTIAL
    assert incident["progress"] == {
        "trains_recovered": 0, "trains_total": 1, "actions_done": 1, "actions_total": 2,
    }
    states = {i["kind"]: i["state"] for i in incident["work_items"]}
    assert states == {"close_track": esc.WORK_DONE, "reroute": esc.WORK_PENDING}


def test_complete_only_when_the_twin_confirms_every_action(ledger):
    snap = snapshot(closed=["T2"])
    ledger.observe(snap, now=1000.0)
    ledger.record_dispatch("A", [
        {"strategy_id": "R_REROUTE_TR1", "train_id": "TR1",
         "old_route": ["A", "B", "C"], "new_route": ["A", "C"]},
    ], snap, now=1001.0)

    trains = default_trains()
    trains["TR1"]["route"] = ["A", "C"]
    # The corridor is back in service and the train is on the alternate route.
    payload = ledger.observe(snapshot(trains=trains), now=1002.0)
    incident = only(payload)
    assert incident["resolution"] == esc.COMPLETE
    assert incident["work_items"][0]["state"] == esc.WORK_DONE
    assert incident["progress"]["trains_recovered"] == 1


def test_reroute_that_never_landed_keeps_the_incident_off_complete(ledger):
    """An apply that returned success but changed nothing must not read COMPLETE.

    This is the whole point of verifying work against the twin instead of
    trusting the executor's own report.
    """
    snap = snapshot(closed=["T2"])
    ledger.observe(snap, now=1000.0)
    ledger.record_dispatch("A", [
        {"strategy_id": "R_REROUTE_TR1", "train_id": "TR1",
         "old_route": ["A", "B", "C"], "new_route": ["A", "C"]},
    ], snap, now=1001.0)
    payload = ledger.observe(snapshot(closed=["T2"]), now=1002.0)   # nothing changed
    incident = only(payload)
    assert incident["resolution"] != esc.COMPLETE
    assert incident["work_items"][0]["state"] == esc.WORK_PENDING


def test_blocked_when_no_corridor_remains(ledger):
    payload = ledger.observe(snapshot(closed=["T5"]), now=1000.0)
    incident = only(payload)
    assert incident["resolution"] == esc.BLOCKED
    assert "track restoration" in incident["resolution_reason"]


def test_blocked_outranks_partial(ledger):
    """Half-done work on an unrecoverable incident must not read as progress."""
    snap = snapshot(closed=["T5"])
    ledger.observe(snap, now=1000.0)
    ledger.record_dispatch("A", [{"strategy_id": "T_CLOSE_T5", "track_id": "T5"}],
                           snap, now=1001.0)
    payload = ledger.observe(snapshot(closed=["T5"]), now=1002.0)
    incident = only(payload)
    assert incident["progress"]["actions_done"] == 1      # some work IS verified
    assert incident["resolution"] == esc.BLOCKED          # but the rest is impossible


def test_held_train_that_still_has_a_path_is_not_blocked(ledger):
    trains = default_trains()
    trains["TR1"]["held"] = True
    payload = ledger.observe(snapshot(closed=["T2"], trains=trains), now=1000.0)
    incident = only(payload)
    assert incident["dispositions"]["TR1"] == esc.TRAIN_HELD
    assert incident["resolution"] == esc.UNRESOLVED       # recoverable, just not yet


def test_network_resolution_reports_the_worst_open_incident(ledger):
    payload = ledger.observe(snapshot(closed=["T2", "T5"]), now=1000.0)
    assert payload["totals"]["open"] == 2
    assert payload["resolution"] == esc.BLOCKED
    assert payload["code"] == "L4"


def test_completion_signal_change_is_logged_as_an_event(ledger):
    snap = snapshot(closed=["T2"])
    ledger.observe(snap, now=1000.0)
    ledger.record_dispatch("A", [{"strategy_id": "T_CLOSE_T2", "track_id": "T2"}],
                           snap, now=1001.0)
    payload = ledger.observe(snapshot(closed=["T2"]), now=1002.0)
    statuses = [e for e in only(payload)["events"] if e["kind"] == "status"]
    assert any("PARTIAL" in e["reason"] for e in statuses)


# ── ledger bookkeeping ──────────────────────────────────────────────────────

def test_repeated_observation_does_not_duplicate_an_incident(ledger):
    for t in range(5):
        payload = ledger.observe(snapshot(closed=["T2"]), now=1000.0 + t)
    assert len(payload["incidents"]) == 1


def test_repeated_dispatch_does_not_duplicate_work_items(ledger):
    snap = snapshot(closed=["T2"])
    ledger.observe(snap, now=1000.0)
    action = [{"strategy_id": "T_CLOSE_T2", "track_id": "T2"}]
    ledger.record_dispatch("A", action, snap, now=1001.0)
    ledger.record_dispatch("A", action, snap, now=1002.0)
    payload = ledger.observe(snap, now=1003.0)
    assert len(only(payload)["work_items"]) == 1


def test_second_failure_opens_a_second_incident(ledger):
    ledger.observe(snapshot(closed=["T2"]), now=1000.0)
    payload = ledger.observe(snapshot(closed=["T2", "T4"]), now=1001.0)
    assert {i["id"] for i in payload["incidents"]} == {"INC-001", "INC-002"}


def test_severe_weather_opens_its_own_incident_and_clears(ledger):
    payload = ledger.observe(snapshot(weather={"T1": "STORM"}), now=1000.0)
    incident = only(payload)
    assert incident["kind"] == "SEVERE_WEATHER"
    assert incident["track_id"] == "T1"
    payload = ledger.observe(snapshot(weather={"T1": "CLEAR"}), now=1001.0)
    assert only(payload)["resolution"] == esc.COMPLETE


def test_rain_is_not_an_escalation(ledger):
    payload = ledger.observe(snapshot(weather={"T1": "RAIN"}), now=1000.0)
    assert payload["totals"]["open"] == 0


def test_work_item_for_a_vanished_train_reads_failed(ledger):
    snap = snapshot(closed=["T2"])
    ledger.observe(snap, now=1000.0)
    ledger.record_dispatch("A", [
        {"strategy_id": "R_REROUTE_TR1", "train_id": "TR1",
         "old_route": ["A", "B", "C"], "new_route": ["A", "C"]},
    ], snap, now=1001.0)
    trains = default_trains()
    del trains["TR1"]
    payload = ledger.observe(snapshot(closed=["T2"], trains=trains), now=1002.0)
    item = only(payload)["work_items"][0]
    assert item["state"] == esc.WORK_FAILED


def test_hold_work_item_completes_when_the_train_is_stopped(ledger):
    snap = snapshot(closed=["T5"])
    ledger.observe(snap, now=1000.0)
    ledger.record_dispatch("A", [
        {"strategy_id": "R_HOLD_TR2", "train_id": "TR2", "old_route": ["C", "D", "E"]},
    ], snap, now=1001.0)
    trains = default_trains()
    trains["TR2"]["held"] = True
    payload = ledger.observe(snapshot(closed=["T5"], trains=trains), now=1002.0)
    item = only(payload)["work_items"][0]
    assert item["state"] == esc.WORK_DONE
    assert "held short" in item["detail"]


def test_weather_protocol_closure_registers_as_work(ledger):
    snap = snapshot(weather={"T1": "STORM"})
    ledger.observe(snap, now=1000.0)
    ledger.record_dispatch("A", [
        {"strategy_id": "W1", "actions": ["close_track_T1+reroute_all_trains"]},
    ], snap, now=1001.0)
    payload = ledger.observe(snapshot(closed=["T1"], weather={"T1": "STORM"}), now=1002.0)
    weather_incident = next(i for i in payload["incidents"] if i["kind"] == "SEVERE_WEATHER")
    assert any(i["kind"] == "weather_closure" and i["state"] == esc.WORK_DONE
               for i in weather_incident["work_items"])


def test_reset_empties_the_ledger(ledger):
    ledger.observe(snapshot(closed=["T2"]), now=1000.0)
    ledger.reset()
    payload = ledger.observe(snapshot(), now=1001.0)
    assert payload["incidents"] == []
    assert payload["code"] == "L0"


def test_acknowledge_unknown_incident_raises(ledger):
    with pytest.raises(KeyError):
        ledger.acknowledge("INC-999", "Nobody", now=1000.0)


def test_snapshot_without_graph_block_still_finds_paths(ledger):
    """Topology falls back to the track list; a trimmed snapshot must not
    report every train as having no route."""
    snap = snapshot(closed=["T2"])
    snap.pop("graph")
    payload = ledger.observe(snap, now=1000.0)
    assert only(payload)["dispositions"]["TR1"] == esc.TRAIN_EXPOSED


# ── pure helpers ────────────────────────────────────────────────────────────

def test_severed_pairs_is_zero_on_a_connected_network():
    assert esc.severed_pairs(esc.open_graph(snapshot())) == 0


def test_severed_pairs_counts_the_partition():
    # Closing T5 isolates E: 4 pairs (E with each of A, B, C, D).
    assert esc.severed_pairs(esc.open_graph(snapshot(closed=["T5"]))) == 4


def test_is_blocking_covers_failed_health_without_closed_status():
    assert esc.is_blocking({"status": "OPEN", "health": 0.1})
    assert not esc.is_blocking({"status": "OPEN", "health": 0.6})
