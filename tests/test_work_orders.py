"""The twin's work-order execution layer: field work happens over simulation
ticks, and a task is COMPLETED only when the railway's physical state proves
it — never because the order was accepted."""

import pytest

from railmind.data_loader import load_bundled_network
from railmind.graph import build_network_from_data
from railmind.models import SignalStatus, TrackStatus, TrainState
from railmind.twin import DigitalTwin
from railmind.work_orders import (
    FieldTask,
    TaskAction,
    TaskStatus,
    WorkOrder,
    WorkOrderStatus,
)

import escalation as esc


@pytest.fixture()
def twin():
    stations, tracks = load_bundled_network()
    graph = build_network_from_data(stations, tracks)
    t = DigitalTwin(graph)
    t.seed_trains(5)
    # Exact tick counts below assume no random storm lands on the section
    # under repair; only injected weather applies in these scenarios.
    t.ambient_weather = False
    return t


def _station(sid, lat=0.0, lon=0.0):
    return {"station_id": sid, "name": sid, "lat": lat, "lon": lon}


def _track(tid, src, dst, length_km=100.0, health=0.9, max_speed_kmh=100.0):
    return {"track_id": tid, "source": src, "destination": dst, "health": health,
            "length_km": length_km, "max_speed_kmh": max_speed_kmh}


def _two_station_twin(length_km=100.0):
    stations = [_station("A"), _station("B", 1.0, 1.0)]
    tracks = [_track("TX", "A", "B", length_km=length_km)]
    t = DigitalTwin(build_network_from_data(stations, tracks))
    t.ambient_weather = False
    return t


def _triangle_twin():
    # A-B direct (T1) with a long bypass A-C-B (T3 + T2): closing T1 leaves
    # an alternate route, so reroutes have somewhere to go.
    stations = [_station("A"), _station("B", 1.0, 0.0), _station("C", 0.5, 1.0)]
    tracks = [
        _track("T1", "A", "B", length_km=100.0),
        _track("T2", "C", "B", length_km=200.0),
        _track("T3", "A", "C", length_km=200.0),
    ]
    t = DigitalTwin(build_network_from_data(stations, tracks))
    t.ambient_weather = False
    t.state.trains["TR00"] = TrainState(
        train_id="TR00", current_station="A", route=["A", "B"], speed_kmh=80.0,
        passengers=300, delayed_minutes=0.0,
    )
    return t


def _order(twin, *tasks, target=None, **kwargs):
    """Register a work order from plain task dicts, ids auto-assigned."""
    target = target or tasks[0]["target"]
    return twin.register_work_order({"target": target, "tasks": list(tasks), **kwargs})


def _task(action, target, ticks=None, **params):
    task = {"action": action, "target": target}
    if ticks is not None:
        task["ticks_required"] = ticks
    depends_on = params.pop("depends_on", None)
    if depends_on:
        task["depends_on"] = depends_on
    if params:
        task["params"] = params
    return task


def _statuses(wo):
    return [t.status for t in wo.tasks]


# ---------------------------------------------------------------------------
# Registration and the model
# ---------------------------------------------------------------------------

def test_registration_assigns_ids_and_starts_unresolved(twin):
    wo = twin.create_incident_response("T23", incident_id="INC-001")
    assert wo.id == "WO-001"
    assert [t.id for t in wo.tasks] == ["task_1", "task_2", "task_3"]
    assert _statuses(wo) == [TaskStatus.PENDING] * 3
    assert wo.status == WorkOrderStatus.UNRESOLVED
    assert wo.completion_percentage == 0
    assert wo.events[0].kind == "registered"
    assert twin.get_work_order("WO-001") is wo

    second = _order(twin, _task("CLOSE_TRACK", "T05"))
    assert second.id == "WO-002"
    assert second.tasks[0].id == "task_1"
    assert second.tasks[0].ticks_required == 1      # default for CLOSE_TRACK


def test_registration_rejects_bad_orders(twin):
    with pytest.raises(ValueError, match="unknown track"):
        _order(twin, _task("CLOSE_TRACK", "NOPE"))
    with pytest.raises(ValueError, match="unknown train"):
        _order(twin, _task("REROUTE_TRAIN", "TR99"))
    with pytest.raises(ValueError, match="at least one task"):
        twin.register_work_order({"target": "T23", "tasks": []})
    with pytest.raises(ValueError, match="Duplicate task id"):
        twin.register_work_order({"target": "T23", "tasks": [
            {"id": "x", "action": "CLOSE_TRACK", "target": "T23"},
            {"id": "x", "action": "DISPATCH_CREW", "target": "T23"},
        ]})
    with pytest.raises(ValueError, match="unknown task"):
        twin.register_work_order({"target": "T23", "tasks": [
            {"id": "a", "action": "REPAIR_TRACK", "target": "T23", "depends_on": ["ghost"]},
        ]})
    with pytest.raises(ValueError, match="cycle"):
        twin.register_work_order({"target": "T23", "tasks": [
            {"id": "a", "action": "CLOSE_TRACK", "target": "T23", "depends_on": ["b"]},
            {"id": "b", "action": "DISPATCH_CREW", "target": "T23", "depends_on": ["a"]},
        ]})
    with pytest.raises(ValueError, match="speed_kmh"):
        _order(twin, _task("SPEED_RESTRICT", "T23", speed_kmh=-5))
    with pytest.raises(ValueError, match="restored_health"):
        _order(twin, _task("REPAIR_TRACK", "T23", restored_health=1.5))

    twin.create_incident_response("T23", work_order_id="WO-DUP")
    with pytest.raises(ValueError, match="Duplicate work order id"):
        twin.create_incident_response("T23", work_order_id="WO-DUP")


def test_task_state_machine_rejects_illegal_moves():
    task = FieldTask(id="t", action=TaskAction.CLOSE_TRACK, target="T1")
    task.transition(TaskStatus.IN_PROGRESS)
    task.transition(TaskStatus.COMPLETED)
    with pytest.raises(ValueError, match="illegal transition"):
        task.transition(TaskStatus.PENDING)
    blocked = FieldTask(id="b", action=TaskAction.REPAIR_TRACK, target="T1")
    blocked.transition(TaskStatus.BLOCKED, blocking_reason="storm")
    assert blocked.blocking_reason == "storm"
    blocked.transition(TaskStatus.PENDING)
    assert blocked.blocking_reason is None


def test_work_order_status_aggregation_follows_completion_signal():
    def order(*statuses, cancelled=False):
        tasks = [FieldTask(id=f"t{i}", action=TaskAction.CLOSE_TRACK, target="T1", status=s)
                 for i, s in enumerate(statuses)]
        return WorkOrder(id="WO", target="T1", tasks=tasks, cancelled=cancelled).recalculate_status()

    S = TaskStatus
    assert order(S.COMPLETED, S.IN_PROGRESS, S.PENDING) == WorkOrderStatus.PARTIAL
    assert order(S.COMPLETED, S.COMPLETED, S.COMPLETED) == WorkOrderStatus.COMPLETE
    assert order(S.COMPLETED, S.BLOCKED, S.PENDING) == WorkOrderStatus.BLOCKED
    assert order(S.PENDING, S.PENDING) == WorkOrderStatus.UNRESOLVED
    assert order(S.UNRESOLVED, S.PENDING) == WorkOrderStatus.UNRESOLVED
    assert order(S.COMPLETED, S.UNRESOLVED) == WorkOrderStatus.PARTIAL
    # BLOCKED outranks everything but cancellation, even verified work
    assert order(S.COMPLETED, S.COMPLETED, S.BLOCKED) == WorkOrderStatus.BLOCKED
    assert order(S.COMPLETED, S.IN_PROGRESS, cancelled=True) == WorkOrderStatus.CANCELLED


# ---------------------------------------------------------------------------
# Execution over ticks
# ---------------------------------------------------------------------------

def test_close_track_runs_through_closing_to_closed(twin):
    wo = _order(twin, _task("CLOSE_TRACK", "T23", ticks=3))
    task = wo.tasks[0]

    twin.tick()
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.ticks_remaining == 2
    assert task.progress == pytest.approx(1 / 3)
    assert twin.state.tracks["T23"].status == TrackStatus.CLOSING
    assert wo.status == WorkOrderStatus.PARTIAL
    # A closing section already holds traffic and is hidden from routing
    detour = twin.find_route("NAGPUR_JUNCT", "RAIPUR_JUNCT")
    assert len(detour) > 2

    twin.tick()
    twin.tick()
    assert task.status == TaskStatus.COMPLETED
    assert task.completed_tick == 3
    assert task.ticks_remaining == 0
    assert twin.state.tracks["T23"].status == TrackStatus.CLOSED
    # Graph and state agree
    u, v = twin.state.tracks["T23"].source, twin.state.tracks["T23"].destination
    assert twin.graph.graph.edges[u, v]["status"] == TrackStatus.CLOSED
    assert wo.status == WorkOrderStatus.COMPLETE
    assert wo.completion_percentage == 100
    # Closure is operational, not damage: health is untouched
    assert twin.state.tracks["T23"].health > 0.0


def test_one_tick_task_starts_and_completes_in_the_same_tick(twin):
    wo = _order(twin, _task("CLOSE_TRACK", "T23", ticks=1))
    twin.tick()
    assert wo.tasks[0].status == TaskStatus.COMPLETED
    assert wo.tasks[0].started_tick == wo.tasks[0].completed_tick == 1


def test_repair_restores_track_over_ticks_with_transitional_state(twin):
    twin.close_track("T23")
    assert twin.state.tracks["T23"].health == 0.0
    wo = _order(twin, _task("REPAIR_TRACK", "T23", ticks=5))
    task = wo.tasks[0]

    healths = []
    for _ in range(4):
        twin.tick()
        track = twin.state.tracks["T23"]
        assert task.status == TaskStatus.IN_PROGRESS
        assert track.status == TrackStatus.UNDER_REPAIR
        healths.append(track.health)
    # Work is visible while it happens: health climbs, but the section
    # still holds traffic until the repair is verified.
    assert healths == sorted(healths) and healths[-1] > healths[0]
    assert len(twin.find_route("NAGPUR_JUNCT", "RAIPUR_JUNCT")) > 2

    twin.tick()
    track = twin.state.tracks["T23"]
    assert task.status == TaskStatus.COMPLETED
    assert track.status == TrackStatus.OPEN
    assert track.health == pytest.approx(0.95)
    assert wo.status == WorkOrderStatus.COMPLETE
    assert twin.find_route("NAGPUR_JUNCT", "RAIPUR_JUNCT") == ["NAGPUR_JUNCT", "RAIPUR_JUNCT"]
    assert "operational" in task.detail


def test_repair_verification_rejects_a_result_that_is_still_critical(twin):
    twin.close_track("T23")
    wo = _order(twin, _task("REPAIR_TRACK", "T23", ticks=2, restored_health=0.1))
    twin.tick()
    twin.tick()
    task = wo.tasks[0]
    assert task.status == TaskStatus.UNRESOLVED
    assert task.ticks_remaining == 0
    assert "still below" in task.detail
    # Execution finished, but the railway says the section is not fit
    assert twin.state.tracks["T23"].status == TrackStatus.DEGRADED
    assert wo.status == WorkOrderStatus.UNRESOLVED
    assert wo.events[-2].kind == "unresolved"


def test_dependencies_gate_start_and_chain_within_a_tick(twin):
    twin.close_track("T23")
    wo = twin.create_incident_response("T23", incident_id="INC-001")
    close, dispatch, repair = wo.tasks
    eta = wo.estimated_ticks_remaining()
    assert eta == 29     # 1 + (10 - 1) + (20 - 1): each hop starts the tick its dependency ends

    twin.tick()
    assert close.status == TaskStatus.COMPLETED
    assert dispatch.status == TaskStatus.IN_PROGRESS and dispatch.ticks_remaining == 9
    assert repair.status == TaskStatus.PENDING
    assert wo.status == WorkOrderStatus.PARTIAL
    assert twin.crews["CREW-001"].status.value == "EN_ROUTE"

    for _ in range(9):
        twin.tick()
    assert dispatch.status == TaskStatus.COMPLETED
    assert twin.crews["CREW-001"].status.value == "ON_SITE"
    assert repair.status == TaskStatus.IN_PROGRESS and repair.ticks_remaining == 19
    assert twin.state.tracks["T23"].status == TrackStatus.UNDER_REPAIR

    for _ in range(18):
        twin.tick()
    assert repair.status == TaskStatus.IN_PROGRESS and repair.ticks_remaining == 1
    assert wo.estimated_ticks_remaining() == 1

    twin.tick()
    assert _statuses(wo) == [TaskStatus.COMPLETED] * 3
    assert wo.status == WorkOrderStatus.COMPLETE
    assert twin.sim_tick == eta
    assert twin.state.tracks["T23"].status == TrackStatus.OPEN
    assert twin.crews == {}          # stood down with the order


def test_storm_blocks_crew_work_until_an_explicit_retry(twin):
    twin.close_track("T23")
    twin.set_weather("T23", "STORM")
    wo = twin.create_incident_response("T23")
    close, dispatch, repair = wo.tasks

    twin.tick()
    assert close.status == TaskStatus.COMPLETED
    assert dispatch.status == TaskStatus.BLOCKED
    assert dispatch.blocking_reason == "Severe storm prevents crew access to T23"
    assert repair.status == TaskStatus.PENDING
    assert wo.status == WorkOrderStatus.BLOCKED
    assert twin.crews == {}

    for _ in range(5):
        twin.tick()
    assert dispatch.status == TaskStatus.BLOCKED

    # The weather clearing is not enough on its own for the MVP
    twin.set_weather("T23", "CLEAR")
    twin.tick()
    assert dispatch.status == TaskStatus.BLOCKED

    assert twin.retry_task(wo.id) == ["task_2"]
    assert dispatch.status == TaskStatus.PENDING
    assert wo.status == WorkOrderStatus.PARTIAL
    twin.tick()
    assert dispatch.status == TaskStatus.IN_PROGRESS
    assert twin.crews["CREW-001"].target == "T23"


def test_storm_mid_repair_stops_work_in_place(twin):
    twin.close_track("T23")
    wo = _order(twin, _task("REPAIR_TRACK", "T23", ticks=10))
    task = wo.tasks[0]
    for _ in range(3):
        twin.tick()
    assert task.ticks_remaining == 7
    health_before = twin.state.tracks["T23"].health

    twin.set_weather("T23", "STORM")
    twin.tick()
    assert task.status == TaskStatus.BLOCKED
    assert task.ticks_remaining == 7
    assert twin.state.tracks["T23"].status == TrackStatus.UNDER_REPAIR
    assert twin.state.tracks["T23"].health == health_before
    assert wo.status == WorkOrderStatus.BLOCKED

    twin.set_weather("T23", "CLEAR")
    twin.retry_task(wo.id, "task_1")
    twin.tick()
    assert task.status == TaskStatus.IN_PROGRESS and task.ticks_remaining == 6
    for _ in range(6):
        twin.tick()
    assert task.status == TaskStatus.COMPLETED
    assert twin.state.tracks["T23"].status == TrackStatus.OPEN


def test_auto_retry_resumes_when_the_blocking_condition_clears(twin):
    twin.close_track("T23")
    twin.set_weather("T23", "STORM")
    wo = _order(twin, _task("REPAIR_TRACK", "T23", ticks=3), auto_retry=True)
    task = wo.tasks[0]
    twin.tick()
    assert task.status == TaskStatus.BLOCKED
    twin.tick()
    assert task.status == TaskStatus.BLOCKED
    twin.set_weather("T23", "CLEAR")
    twin.tick()
    assert task.status == TaskStatus.IN_PROGRESS
    assert [e.kind for e in wo.events if e.task_id == "task_1"][-2:] == ["retried", "started"]


def test_retry_rejects_tasks_that_are_not_blocked(twin):
    wo = twin.create_incident_response("T23")
    with pytest.raises(ValueError, match="only BLOCKED"):
        twin.retry_task(wo.id, "task_1")
    with pytest.raises(ValueError, match="Unknown task"):
        twin.retry_task(wo.id, "task_9")
    assert twin.retry_task(wo.id) == []
    with pytest.raises(KeyError):
        twin.retry_task("WO-404")


def test_dependent_of_a_failed_task_is_blocked_not_silently_pending(twin):
    twin.close_track("T23")
    wo = twin.register_work_order({"target": "T23", "tasks": [
        {"id": "fix", "action": "REPAIR_TRACK", "target": "T23", "ticks_required": 1,
         "params": {"restored_health": 0.05}},
        {"id": "signal", "action": "RESTORE_SIGNAL", "target": "T23", "depends_on": ["fix"]},
    ]})
    twin.tick()
    fix, signal = wo.tasks
    assert fix.status == TaskStatus.UNRESOLVED
    assert signal.status == TaskStatus.BLOCKED
    assert signal.blocking_reason == "Depends on fix which is UNRESOLVED"
    assert wo.status == WorkOrderStatus.BLOCKED


def test_repair_waits_for_a_sibling_dispatch_even_without_a_declared_dependency(twin):
    twin.close_track("T23")
    wo = _order(twin,
                _task("DISPATCH_CREW", "T23", ticks=3),
                _task("REPAIR_TRACK", "T23", ticks=2))
    dispatch, repair = wo.tasks
    twin.tick()
    assert dispatch.status == TaskStatus.IN_PROGRESS
    assert repair.status == TaskStatus.PENDING
    assert repair.detail == "Waiting for task_1 (crew for T23)"
    assert twin.state.tracks["T23"].status == TrackStatus.CLOSED   # nobody on the track yet
    twin.tick()
    twin.tick()
    # The crew lands and the repair starts in the same tick, no retry needed
    assert dispatch.status == TaskStatus.COMPLETED
    assert repair.status == TaskStatus.IN_PROGRESS and repair.ticks_remaining == 1


def test_repair_is_blocked_when_the_sibling_dispatch_never_arrives(twin):
    twin.close_track("T23")
    wo = twin.register_work_order({"target": "T23", "tasks": [
        {"id": "crew", "action": "DISPATCH_CREW", "target": "T23", "ticks_required": 1},
        {"id": "fix", "action": "REPAIR_TRACK", "target": "T23", "ticks_required": 2},
    ]})
    twin.tick()
    # A completed dispatch lets the repair proceed in the same tick...
    assert wo.tasks[0].status == TaskStatus.COMPLETED
    assert wo.tasks[1].status == TaskStatus.IN_PROGRESS

    other = twin.register_work_order({"target": "T05", "tasks": [
        {"id": "crew", "action": "DISPATCH_CREW", "target": "T05", "ticks_required": 3},
        {"id": "fix", "action": "REPAIR_TRACK", "target": "T05", "ticks_required": 2},
    ]})
    twin.set_weather("T05", "STORM")
    twin.tick()
    assert other.tasks[0].status == TaskStatus.BLOCKED
    assert other.tasks[1].status == TaskStatus.PENDING            # still waiting, not blocked twice
    # ...but a dispatch that ends without completing blocks the repair
    other.tasks[0].transition(TaskStatus.CANCELLED)
    twin.tick()
    assert other.tasks[1].status == TaskStatus.BLOCKED
    assert other.tasks[1].blocking_reason == "Crew for T05 did not arrive (crew is CANCELLED)"


def test_close_and_repair_on_one_section_never_overlap(twin):
    twin.close_track("T23")
    repair = _order(twin, _task("REPAIR_TRACK", "T23", ticks=4))
    closure = _order(twin, _task("CLOSE_TRACK", "T23", ticks=2))
    twin.tick()
    assert repair.tasks[0].status == TaskStatus.IN_PROGRESS
    assert closure.tasks[0].status == TaskStatus.BLOCKED
    assert closure.tasks[0].blocking_reason.startswith(
        "Another active operation (WO-001/task_1) is already executing REPAIR_TRACK on T23")
    for _ in range(3):
        twin.tick()
    assert repair.status == WorkOrderStatus.COMPLETE
    assert twin.state.tracks["T23"].status == TrackStatus.OPEN

    # Inside one order the later task simply waits its turn
    both = _order(twin, _task("CLOSE_TRACK", "T05", ticks=2), _task("REPAIR_TRACK", "T05", ticks=2))
    close, fix = both.tasks
    twin.tick()
    assert close.status == TaskStatus.IN_PROGRESS
    assert fix.status == TaskStatus.PENDING
    assert fix.detail == "Waiting for task_1 to finish CLOSE_TRACK on T05"
    twin.tick()
    assert close.status == TaskStatus.COMPLETED
    assert fix.status == TaskStatus.IN_PROGRESS
    assert twin.state.tracks["T05"].status == TrackStatus.UNDER_REPAIR


def test_a_blocked_task_still_holds_its_section_against_other_orders(twin):
    twin.close_track("T23")
    repair = _order(twin, _task("REPAIR_TRACK", "T23", ticks=10))
    twin.tick()
    twin.set_weather("T23", "STORM")
    twin.tick()
    assert repair.tasks[0].status == TaskStatus.BLOCKED
    assert twin.state.tracks["T23"].status == TrackStatus.UNDER_REPAIR

    closure = _order(twin, _task("CLOSE_TRACK", "T23", ticks=3))
    twin.tick()
    assert closure.tasks[0].status == TaskStatus.BLOCKED
    assert closure.tasks[0].blocking_reason == (
        "Another active operation (WO-001/task_1) is already holding REPAIR_TRACK on T23")
    assert twin.state.tracks["T23"].status == TrackStatus.UNDER_REPAIR    # nobody piled on

    # Cancelling both, in either order, leaves the section as the repair found it
    twin.cancel_work_order(repair.id)
    assert twin.state.tracks["T23"].status == TrackStatus.CLOSED
    twin.cancel_work_order(closure.id)
    assert twin.state.tracks["T23"].status == TrackStatus.CLOSED

    twin.set_weather("T23", "CLEAR")
    again = _order(twin, _task("REPAIR_TRACK", "T23", ticks=1))
    twin.tick()
    assert again.status == WorkOrderStatus.COMPLETE
    assert twin.state.tracks["T23"].status == TrackStatus.OPEN


def test_auto_retry_does_not_override_an_operator_closure(twin):
    twin.close_track("T23")
    wo = _order(twin, _task("REPAIR_TRACK", "T23", ticks=6), auto_retry=True)
    task = wo.tasks[0]
    twin.tick()
    twin.tick()
    twin.close_track("T23")                      # the section fails again under the crew
    twin.tick()
    assert task.status == TaskStatus.BLOCKED
    assert task.blocking_reason == "T23 is CLOSED: section changed while under repair"
    twin.tick()
    twin.tick()
    assert task.status == TaskStatus.BLOCKED     # auto-retry leaves an operator's change alone
    assert twin.state.tracks["T23"].status == TrackStatus.CLOSED
    assert twin.state.tracks["T23"].health == 0.0

    twin.retry_task(wo.id)                       # an explicit retry resumes the rebuild
    twin.tick()
    assert task.status == TaskStatus.IN_PROGRESS
    assert twin.state.tracks["T23"].status == TrackStatus.UNDER_REPAIR
    assert 0.0 < twin.state.tracks["T23"].health < 0.95   # rebuilt from what is there, not from before
    twin.advance_ticks(3)
    assert task.status == TaskStatus.COMPLETED
    assert twin.state.tracks["T23"].health == pytest.approx(0.95)


def test_implicit_crew_dependency_takes_part_in_cycle_detection(twin):
    with pytest.raises(ValueError, match="cycle"):
        twin.register_work_order({"target": "T23", "tasks": [
            {"id": "fix", "action": "REPAIR_TRACK", "target": "T23", "ticks_required": 2},
            {"id": "crew", "action": "DISPATCH_CREW", "target": "T23", "ticks_required": 2,
             "depends_on": ["fix"]},
        ]})
    with pytest.raises(ValueError, match="at most"):
        twin.register_work_order({"target": "T23", "tasks": [
            _task("CLOSE_TRACK", "T23") for _ in range(101)]})
    with pytest.raises(ValueError):
        _order(twin, _task("REPAIR_TRACK", "T23", ticks=10_001))


def test_external_closure_during_a_repair_stops_the_work(twin):
    twin.close_track("T23")
    wo = _order(twin, _task("REPAIR_TRACK", "T23", ticks=6))
    task = wo.tasks[0]
    twin.tick()
    twin.tick()
    assert twin.state.tracks["T23"].status == TrackStatus.UNDER_REPAIR

    # An operator (or the agent service) fails the section again under the crew
    twin.close_track("T23")
    twin.tick()
    assert task.status == TaskStatus.BLOCKED
    assert task.blocking_reason == "T23 is CLOSED: section changed while under repair"
    assert task.ticks_remaining == 4
    assert twin.state.tracks["T23"].status == TrackStatus.CLOSED        # not silently undone
    assert twin.state.tracks["T23"].health == 0.0

    twin.retry_task(wo.id)
    for _ in range(4):
        twin.tick()
    assert task.status == TaskStatus.COMPLETED
    assert twin.state.tracks["T23"].status == TrackStatus.OPEN


def test_second_order_on_the_same_asset_is_blocked_by_the_first(twin):
    twin.close_track("T23")
    first = _order(twin, _task("REPAIR_TRACK", "T23", ticks=5))
    second = _order(twin, _task("REPAIR_TRACK", "T23", ticks=5))
    twin.tick()
    assert first.tasks[0].status == TaskStatus.IN_PROGRESS
    assert second.tasks[0].status == TaskStatus.BLOCKED
    assert second.tasks[0].blocking_reason.startswith("Another active operation (WO-001/task_1)")


# ---------------------------------------------------------------------------
# The other actions
# ---------------------------------------------------------------------------

def test_reroute_plans_a_route_around_the_closure_and_verifies_it():
    twin = _triangle_twin()
    twin.close_track("T1")
    twin.tick()
    train = twin.state.trains["TR00"]
    assert train.held is True

    wo = _order(twin, _task("REROUTE_TRAIN", "TR00", ticks=2))
    task = wo.tasks[0]
    twin.tick()
    assert task.status == TaskStatus.IN_PROGRESS
    assert "via A > C > B" in task.detail
    assert "resolved_route" not in task.params
    assert train.route == ["A", "B"]          # nothing has landed yet
    twin.tick()
    assert task.status == TaskStatus.COMPLETED
    assert task.params["resolved_route"] == ["A", "C", "B"]
    assert train.route == ["A", "C", "B"]
    assert "open corridors" in task.detail
    twin.tick()
    assert train.held is False


def _line_twin():
    # A-B-C-D, 50 km sections: a train covers one whole section per tick.
    stations = [_station(s, float(i), 0.0) for i, s in enumerate("ABCD")]
    tracks = [_track(f"T_{u}{v}", u, v, length_km=50.0) for u, v in zip("ABC", "BCD")]
    t = DigitalTwin(build_network_from_data(stations, tracks))
    t.ambient_weather = False
    t.state.trains["TR00"] = TrainState(
        train_id="TR00", current_station="A", route=["A", "B", "C", "D"], speed_kmh=80.0,
        passengers=300, delayed_minutes=0.0,
    )
    return t


def test_reroute_is_planned_from_where_the_train_is_when_it_lands():
    twin = _line_twin()
    train = twin.state.trains["TR00"]
    wo = _order(twin, _task("REROUTE_TRAIN", "TR00", ticks=2, destination="D"))
    twin.tick()
    assert train.current_station == "B"
    assert "via B > C > D" in wo.tasks[0].detail
    twin.tick()
    # The train kept moving; the order lands from C, it is not pulled back to B
    assert wo.tasks[0].status == TaskStatus.COMPLETED
    assert train.current_station == "C"
    assert train.route == ["C", "D"]
    assert wo.tasks[0].params["resolved_route"] == ["C", "D"]


def test_reroute_completes_as_a_no_op_when_the_train_arrives_first():
    twin = _line_twin()
    train = twin.state.trains["TR00"]
    wo = _order(twin, _task("REROUTE_TRAIN", "TR00", ticks=3, destination="D"))
    for _ in range(3):
        twin.tick()
    assert train.current_station == "D"
    assert wo.tasks[0].status == TaskStatus.COMPLETED
    assert wo.tasks[0].detail == "TR00 already reached D; no reroute needed"
    assert wo.tasks[0].params["arrived"] is True
    assert wo.status == WorkOrderStatus.COMPLETE


def test_reroute_to_where_a_held_train_already_is_completes_honestly():
    twin = _triangle_twin()
    train = twin.state.trains["TR00"]
    train.current_station, train.route = "B", ["B", "A"]
    twin.close_track("T1")
    twin.tick()
    assert train.held
    wo = _order(twin, _task("REROUTE_TRAIN", "TR00", ticks=1, destination="B"))
    twin.tick()
    assert wo.tasks[0].status == TaskStatus.COMPLETED
    assert wo.tasks[0].detail == "TR00 already at B; no reroute needed (held short of its next section)"
    with pytest.raises(ValueError, match="unknown destination"):
        _order(twin, _task("REROUTE_TRAIN", "TR00", destination=["B"]))


def test_explicit_route_follows_the_train_and_cannot_teleport_it():
    twin = _line_twin()
    train = twin.state.trains["TR00"]
    wo = _order(twin, _task("REROUTE_TRAIN", "TR00", ticks=2, route=["A", "B", "C", "D"]))
    twin.tick()
    twin.tick()
    assert train.current_station == "C"
    assert wo.tasks[0].status == TaskStatus.COMPLETED
    assert train.route == ["C", "D"]

    # A route the train is no longer on cannot be applied by snapping it back
    twin.state.trains["TR01"] = TrainState(
        train_id="TR01", current_station="D", route=["D", "C"], speed_kmh=1.0,
        passengers=10, delayed_minutes=0.0,
    )
    stale = _order(twin, _task("REROUTE_TRAIN", "TR01", ticks=1, route=["A", "B"]))
    twin.tick()
    assert stale.tasks[0].status == TaskStatus.BLOCKED
    assert stale.tasks[0].blocking_reason == "TR01 is at D, which is not on the planned route"


def test_reroute_is_blocked_when_no_alternate_route_exists():
    twin = _two_station_twin()
    twin.seed_trains(1)
    twin.close_track("TX")
    wo = _order(twin, _task("REROUTE_TRAIN", "TR00"))
    twin.tick()
    assert wo.tasks[0].status == TaskStatus.BLOCKED
    assert wo.tasks[0].blocking_reason.startswith("No alternate route for TR00")
    assert wo.status == WorkOrderStatus.BLOCKED


def test_reroute_with_an_explicit_route_must_be_passable():
    twin = _triangle_twin()
    twin.close_track("T1")
    bad = _order(twin, _task("REROUTE_TRAIN", "TR00", route=["A", "B"]))
    twin.tick()
    assert bad.tasks[0].status == TaskStatus.BLOCKED
    assert bad.tasks[0].blocking_reason == "Planned route crosses T1 (CLOSED)"

    good = _order(twin, _task("REROUTE_TRAIN", "TR00", ticks=1, route=["A", "C", "B"]))
    twin.tick()
    assert good.tasks[0].status == TaskStatus.COMPLETED
    assert twin.state.trains["TR00"].route == ["A", "C", "B"]

    with pytest.raises(ValueError, match="unknown station"):
        _order(twin, _task("REROUTE_TRAIN", "TR00", route=["A", "Z"]))


def test_speed_restriction_is_physical_and_slows_trains():
    free = _two_station_twin(length_km=1000.0)
    free.seed_trains(1)
    restricted = free.copy()
    wo = _order(restricted, _task("SPEED_RESTRICT", "TX", speed_kmh=20))

    free.tick()
    restricted.tick()
    assert wo.tasks[0].status == TaskStatus.COMPLETED
    assert restricted.state.tracks["TX"].speed_restriction_kmh == 20.0
    assert "20 km/h" in wo.tasks[0].detail
    # Same RNG stream on both clones, so the only difference is the limit
    free.tick()
    restricted.tick()
    assert restricted.state.trains["TR00"].progress < free.state.trains["TR00"].progress


def test_restore_signal_protects_with_red_then_clears_to_green(twin):
    wo = _order(twin, _task("RESTORE_SIGNAL", "T23", ticks=2))
    track = twin.state.tracks["T23"]
    ends = (track.source, track.destination)
    twin.tick()
    assert wo.tasks[0].status == TaskStatus.IN_PROGRESS
    assert all(twin.state.stations[s].active_signals["T23"] == SignalStatus.RED for s in ends)
    twin.tick()
    assert wo.tasks[0].status == TaskStatus.COMPLETED
    assert all(twin.state.stations[s].active_signals["T23"] == SignalStatus.GREEN for s in ends)
    assert twin.get_state()["stations"][ends[0]]["active_signals"]["T23"] == "GREEN"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_cancel_aborts_work_in_flight_and_puts_assets_back(twin):
    twin.close_track("T23")
    wo = twin.create_incident_response("T23")
    twin.tick()                                  # closed; crew en route
    assert "CREW-001" in twin.crews

    twin.cancel_work_order(wo.id, reason="Incident downgraded")
    close, dispatch, repair = wo.tasks
    assert wo.status == WorkOrderStatus.CANCELLED
    assert wo.cancelled and wo.cancel_reason == "Incident downgraded"
    assert close.status == TaskStatus.COMPLETED          # finished work stays finished
    assert dispatch.status == TaskStatus.CANCELLED
    assert repair.status == TaskStatus.CANCELLED
    assert twin.crews == {}                              # recalled
    assert wo.estimated_ticks_remaining() == 0

    before = wo.payload()
    for _ in range(5):
        twin.tick()
    after = wo.payload()
    assert after["tasks"] == before["tasks"]             # nothing advances
    with pytest.raises(ValueError, match="already cancelled"):
        twin.cancel_work_order(wo.id)
    with pytest.raises(ValueError, match="is cancelled"):
        twin.retry_task(wo.id)


def test_cancel_reopens_a_track_that_was_only_closing(twin):
    wo = _order(twin, _task("CLOSE_TRACK", "T23", ticks=3))
    twin.tick()
    assert twin.state.tracks["T23"].status == TrackStatus.CLOSING
    twin.cancel_work_order(wo.id)
    assert twin.state.tracks["T23"].status == TrackStatus.OPEN
    assert len(twin.find_route("NAGPUR_JUNCT", "RAIPUR_JUNCT")) == 2


def test_cancel_mid_repair_lifts_the_crew_but_keeps_the_work_done(twin):
    twin.close_track("T23")
    wo = _order(twin, _task("REPAIR_TRACK", "T23", ticks=4))
    twin.tick()
    twin.tick()
    partial = twin.state.tracks["T23"].health
    assert partial > 0.0
    twin.cancel_work_order(wo.id)
    track = twin.state.tracks["T23"]
    assert track.status == TrackStatus.CLOSED
    assert track.health == partial


def test_cancel_after_a_mid_flight_block_still_puts_the_assets_back(twin):
    twin.close_track("T23")
    order = _order(twin, _task("REPAIR_TRACK", "T23", ticks=10), _task("RESTORE_SIGNAL", "T05", ticks=5))
    repair, signal = order.tasks
    for _ in range(3):
        twin.tick()
    assert twin.state.tracks["T23"].status == TrackStatus.UNDER_REPAIR
    ends = (twin.state.tracks["T05"].source, twin.state.tracks["T05"].destination)
    assert all(twin.state.stations[s].active_signals["T05"] == SignalStatus.RED for s in ends)

    twin.set_weather("T23", "STORM")
    twin.set_weather("T05", "STORM")
    twin.tick()
    assert repair.status == TaskStatus.BLOCKED and signal.status == TaskStatus.BLOCKED
    twin.set_weather("T23", "CLEAR")
    twin.set_weather("T05", "CLEAR")
    twin.retry_task(order.id, "task_2")               # one retried and waiting, one still blocked
    assert signal.status == TaskStatus.PENDING

    twin.cancel_work_order(order.id, reason="Stood down")
    assert twin.state.tracks["T23"].status == TrackStatus.CLOSED           # crew marker lifted
    assert len(twin.find_route("NAGPUR_JUNCT", "RAIPUR_JUNCT")) > 2       # still closed, not stuck
    assert all("T05" not in twin.state.stations[s].active_signals for s in ends)
    assert [t.status for t in order.tasks] == [TaskStatus.CANCELLED, TaskStatus.CANCELLED]
    for _ in range(5):
        twin.tick()
    assert twin.state.tracks["T23"].status == TrackStatus.CLOSED


def test_crews_stand_down_when_an_order_can_no_longer_progress(twin):
    twin.close_track("T23")
    wo = _order(twin,
                _task("DISPATCH_CREW", "T23", ticks=2),
                _task("REPAIR_TRACK", "T23", ticks=2, depends_on=["task_1"], restored_health=0.3))
    twin.advance_ticks(4)
    assert [t.status for t in wo.tasks] == [TaskStatus.COMPLETED, TaskStatus.UNRESOLVED]
    assert wo.status == WorkOrderStatus.PARTIAL
    assert twin.crews == {}
    events = len(wo.events)
    twin.advance_ticks(5)
    assert len(wo.events) == events                  # a settled order is not swept again


def test_registration_only_takes_the_callers_description_of_the_work(twin):
    with pytest.raises(ValueError, match="Unsupported task field"):
        _order(twin, {"action": "REPAIR_TRACK", "target": "T23", "status": "COMPLETED"})
    with pytest.raises(ValueError, match="Unsupported task field"):
        _order(twin, {"action": "REPAIR_TRACK", "target": "T23", "ticks_required": 20, "ticks_remaining": 0})
    with pytest.raises(ValueError, match="Unsupported task field"):
        _order(twin, {"action": "REPAIR_TRACK", "target": "T23", "rollback": {"health": "x"}})
    with pytest.raises(ValueError, match="set by the twin"):
        _order(twin, _task("DISPATCH_CREW", "T23", crew_id="CREW-999"))
    with pytest.raises(ValueError, match="Unsupported work order field"):
        twin.register_work_order({"target": "T23", "cancelled": True,
                                  "tasks": [_task("CLOSE_TRACK", "T23")]})
    with pytest.raises(ValueError, match="boolean"):
        _order(twin, _task("CLOSE_TRACK", "T23", ticks=True))

    done = FieldTask(id="t", action=TaskAction.REPAIR_TRACK, target="T23", status=TaskStatus.COMPLETED)
    with pytest.raises(ValueError, match="must be registered PENDING"):
        twin.register_work_order(WorkOrder(id="WO-X", target="T23", tasks=[done]))
    assert twin.work_orders == {}
    assert twin.state.tracks["T23"].status == TrackStatus.OPEN


def test_ledger_reaches_complete_only_when_the_twin_reopens_the_track(twin):
    """The escalation ledger grades the incident from the same snapshot the
    console sees: closed -> under repair -> open must read not-COMPLETE
    until the twin has actually restored the corridor, then COMPLETE."""
    ledger = esc.IncidentLedger()
    twin.close_track("T23")
    ledger.observe(twin.get_state(), now=0.0)
    ledger.record_dispatch("A", [{"strategy_id": "T_CLOSE_T23", "track_id": "T23"}],
                           twin.get_state(), now=0.0)
    wo = twin.create_incident_response("T23")

    def incident(payload):
        return next((i for i in payload["incidents"] if i["track_id"] == "T23"), None)

    for tick in range(1, 29):
        twin.tick()
        inc = incident(ledger.observe(twin.get_state(), now=float(tick)))
        assert inc is not None and inc["resolution"] != "COMPLETE", tick
        assert esc.is_blocking(twin.get_state()["tracks"]["T23"])
    assert wo.status != WorkOrderStatus.COMPLETE

    twin.tick()                                      # tick 29: repair verified, T23 OPEN
    assert wo.status == WorkOrderStatus.COMPLETE
    resolutions = []
    for tick in range(29, 33):
        inc = incident(ledger.observe(twin.get_state(), now=float(tick)))
        resolutions.append(inc["resolution"] if inc else "COMPLETE")
        twin.tick()
    assert "COMPLETE" in resolutions
    assert resolutions[-1] == "COMPLETE"


# ---------------------------------------------------------------------------
# Observability and invariants
# ---------------------------------------------------------------------------

def test_payload_exposes_what_the_console_needs(twin):
    twin.close_track("T23")
    wo = twin.create_incident_response("T23", incident_id="INC-007")
    for _ in range(12):
        twin.tick()
    payload = twin.work_order_payload(wo.id)
    assert payload["id"] == "WO-001"
    assert payload["incident_id"] == "INC-007"
    assert payload["status"] == "PARTIAL"
    assert payload["type"] == "CRITICAL_INCIDENT_RESPONSE"
    assert payload["target"] == "T23"
    # Tick 1 closed the section, tick 10 landed the crew and began the
    # repair (19 left), ticks 11 and 12 took it to 17.
    assert payload["completion_percentage"] == round(100 * (1 + 1 + 3 / 20) / 3)
    assert payload["estimated_ticks_remaining"] == 17
    tasks = {t["id"]: t for t in payload["tasks"]}
    assert tasks["task_1"]["status"] == "COMPLETED"
    assert tasks["task_2"]["status"] == "COMPLETED"
    assert tasks["task_3"] == {
        "id": "task_3", "action": "REPAIR_TRACK", "target": "T23", "status": "IN_PROGRESS",
        "ticks_required": 20, "ticks_remaining": 17, "progress": 0.15,
        "depends_on": ["task_1", "task_2"], "blocking_reason": None,
        "detail": tasks["task_3"]["detail"], "started_tick": 10, "completed_tick": None,
        "params": {},
    }
    assert "under repair" in tasks["task_3"]["detail"]
    kinds = [e["kind"] for e in payload["events"]]
    assert kinds[:3] == ["registered", "started", "completed"]
    assert "status" in kinds
    assert twin.work_orders_payload() == [payload]

    state = twin.get_state()
    assert state["sim_tick"] == 12
    assert state["work_orders"][0]["id"] == "WO-001"
    assert state["crews"]["CREW-001"]["status"] == "ON_SITE"
    assert state["tracks"]["T23"]["status"] == "UNDER_REPAIR"


def test_work_orders_never_touch_the_rng_stream(twin):
    # Ambient weather on, so the RNG is busy; a random storm can delay the
    # order (auto_retry absorbs it) but must never desynchronise the clones.
    quiet = twin
    quiet.ambient_weather = True
    busy = twin.copy()
    busy.close_track("T23")
    busy.create_incident_response("T23").auto_retry = True
    for _ in range(60):
        quiet.tick()
        busy.tick()
    assert busy.work_orders["WO-001"].status == WorkOrderStatus.COMPLETE
    assert quiet.state.weather == busy.state.weather
    assert quiet._rng.random() == busy._rng.random()


def test_clones_carry_work_orders_and_advance_independently(twin):
    twin.close_track("T23")
    twin.create_incident_response("T23")
    clone = twin.copy()
    for _ in range(30):
        clone.tick()
    assert clone.work_orders["WO-001"].status == WorkOrderStatus.COMPLETE
    assert clone.state.tracks["T23"].status == TrackStatus.OPEN
    assert twin.work_orders["WO-001"].status == WorkOrderStatus.UNRESOLVED
    assert twin.state.tracks["T23"].status == TrackStatus.CLOSED


def test_advance_ticks_fast_forwards(twin):
    twin.create_incident_response("T23")
    assert twin.advance_ticks(29) == 29
    assert twin.work_orders["WO-001"].status == WorkOrderStatus.COMPLETE
    with pytest.raises(ValueError):
        twin.advance_ticks(0)


def test_ledger_treats_transitional_states_as_blocking():
    assert esc.is_blocking({"status": "UNDER_REPAIR", "health": 0.8})
    assert esc.is_blocking({"status": "CLOSING", "health": 0.9})
    assert not esc.is_blocking({"status": "OPEN", "health": 0.95})


def test_ledger_releases_a_proved_hold_when_the_section_is_restored():
    twin = _two_station_twin(length_km=1000.0)
    twin.seed_trains(1)
    train = twin.state.trains["TR00"]
    twin.close_track("TX")
    twin.tick()
    assert train.held

    ledger = esc.IncidentLedger()
    ledger.observe(twin.get_state(), now=0.0)
    ledger.record_dispatch("A", [
        {"strategy_id": "T_CLOSE_TX", "track_id": "TX"},
        {"strategy_id": "R_HOLD_TR00", "train_id": "TR00", "old_route": list(train.route)},
    ], twin.get_state(), now=0.0)
    inc = ledger.observe(twin.get_state(), now=1.0)["incidents"][0]
    assert {i["kind"]: i["state"] for i in inc["work_items"]} == {"close_track": "DONE", "hold": "DONE"}
    assert inc["resolution"] != "COMPLETE"

    _order(twin, _task("REPAIR_TRACK", "TX", ticks=3))
    twin.advance_ticks(3)
    assert twin.state.tracks["TX"].status == TrackStatus.OPEN
    twin.tick()                                  # the released train moves again
    assert not train.held
    inc = ledger.observe(twin.get_state(), now=2.0)["incidents"][0]
    assert {i["kind"]: i["state"] for i in inc["work_items"]} == {"close_track": "DONE", "hold": "DONE"}
    assert inc["resolution"] == "COMPLETE"
    assert inc["code"] == "L0"


def test_ledger_attaches_field_work_from_the_snapshot(twin):
    ledger = esc.IncidentLedger()
    twin.close_track("T23")
    inc = ledger.observe(twin.get_state(), now=0.0)["incidents"][0]
    assert inc["field_work"] is None

    twin.create_incident_response("T23", incident_id=inc["id"])
    twin.set_weather("T23", "STORM")
    twin.tick()
    inc = ledger.observe(twin.get_state(), now=1.0)["incidents"][0]
    field_work = inc["field_work"]
    assert field_work["id"] == "WO-001"
    assert field_work["status"] == "BLOCKED"
    assert field_work["blocked_reason"] == "Severe storm prevents crew access to T23"
    assert [t["status"] for t in field_work["tasks"]] == ["COMPLETED", "BLOCKED", "PENDING"]
    # Field work is context, not the completion signal: the incident is
    # graded from trains and proved actions exactly as before
    assert inc["resolution"] in ("UNRESOLVED", "PARTIAL", "BLOCKED")

    # A cancelled order gives way to a live one on the same corridor
    twin.cancel_work_order("WO-001")
    twin.set_weather("T23", "CLEAR")
    twin.create_incident_response("T23")
    inc = ledger.observe(twin.get_state(), now=2.0)["incidents"][0]
    assert inc["field_work"]["id"] == "WO-002"


def test_demo_scenario_t23_failure_in_a_storm(twin):
    """The README demo: T23 fails in a storm. The response closes the section
    at once, but the repair cannot start until the storm clears and the
    order is retried — and only then does the twin call T23 operational."""
    twin.close_track("T23")
    twin.set_weather("T23", "STORM")
    wo = twin.create_incident_response("T23", incident_id="INC-001")
    assert wo.status == WorkOrderStatus.UNRESOLVED

    twin.tick()
    assert wo.tasks[0].status == TaskStatus.COMPLETED
    assert twin.state.tracks["T23"].status == TrackStatus.CLOSED
    assert wo.tasks[1].status == TaskStatus.BLOCKED
    assert wo.status == WorkOrderStatus.BLOCKED

    twin.advance_ticks(4)
    assert wo.status == WorkOrderStatus.BLOCKED
    assert wo.tasks[2].status == TaskStatus.PENDING

    twin.set_weather("T23", "CLEAR")
    twin.retry_task(wo.id)
    ticks = 0
    while wo.status != WorkOrderStatus.COMPLETE and ticks < 60:
        twin.tick()
        ticks += 1
    assert wo.status == WorkOrderStatus.COMPLETE
    assert ticks == 29
    track = twin.state.tracks["T23"]
    assert track.status == TrackStatus.OPEN and track.health >= 0.9
    assert twin.find_route("NAGPUR_JUNCT", "RAIPUR_JUNCT") == ["NAGPUR_JUNCT", "RAIPUR_JUNCT"]
    snapshot = twin.get_state()
    assert not esc.is_blocking(snapshot["tracks"]["T23"])
