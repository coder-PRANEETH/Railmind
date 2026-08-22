"""Task executors and the per-tick work-order engine.

Each TaskAction maps to one executor that says how the twin carries that
work out: whether the field can do it right now (blocking_reason), what the
asset looks like while it is happening (start), what each tick of work does
to it (advance), the final mutation (complete), how the twin proves the
mutation landed (verify), and how to put the asset back if the order is
cancelled mid-flight (abort). run_tick() is the only caller; the tick loop
never special-cases an action.

The engine draws nothing from the twin's RNG: two clones of a twin stay in
lockstep whether or not they carry work orders.
"""

from typing import Dict, List, Optional, Tuple

from .models import IMPASSABLE_TRACK_STATUSES, SignalStatus, TrackStatus, WeatherCondition
from .work_orders import (
    CREW_WORK,
    TRACK_STATUS_WRITERS,
    CrewStatus,
    CrewUnit,
    FieldTask,
    TaskAction,
    TaskStatus,
    WorkOrder,
)

# Health a completed repair restores by default, and the floor below which
# the twin refuses to call a repaired track operational (the same threshold
# build_network_from_data uses to grade a track DEGRADED).
REPAIRED_HEALTH = 0.95
REPAIR_ACCEPTANCE_HEALTH = 0.4
DEFAULT_SPEED_RESTRICTION_KMH = 60.0

# Weather that keeps a crew off the track. RAIN and FOG slow trains but a
# gang can still work; a STORM cannot be worked through.
CREW_ACCESS_BLOCKERS = {
    WeatherCondition.STORM: "Severe storm prevents crew access",
}

# Passes the engine makes over one order within a single tick. Each pass
# can only start tasks or advance each task once, so the loop settles long
# before this; the cap is a safety net, not a tuning knob.
MAX_PASSES_PER_TICK = 64


def holds_asset(task: FieldTask) -> bool:
    """The task's transitional state is on its asset: it is running, or it
    was stopped mid-flight and has not been retried, cancelled or ended."""
    return task.started and task.status in (TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)


class TaskExecutor:
    action: TaskAction
    target_kind = "track"           # "track" | "train"
    needs_crew_access = False       # a STORM at the target keeps the crew away

    def validate(self, twin, task: FieldTask) -> None:
        if self.target_kind == "train":
            if task.target not in twin.state.trains:
                raise ValueError(f"Task {task.id}: unknown train {task.target}")
        elif task.target not in twin.state.tracks:
            raise ValueError(f"Task {task.id}: unknown track {task.target}")

    def blocking_reason(self, twin, wo: WorkOrder, task: FieldTask) -> Optional[str]:
        """Why the field cannot do this right now. Called before a task
        starts and on every tick while it holds its asset (running, or
        blocked mid-flight and considered for auto-retry), so an executor
        can also report that the asset was changed under it. An explicit
        retry moves the task to PENDING first: resuming then re-applies
        the work deliberately."""
        return None

    def start(self, twin, wo: WorkOrder, task: FieldTask) -> None:
        pass

    def advance(self, twin, wo: WorkOrder, task: FieldTask) -> None:
        pass

    def complete(self, twin, wo: WorkOrder, task: FieldTask) -> None:
        pass

    def verify(self, twin, wo: WorkOrder, task: FieldTask) -> Tuple[bool, str]:
        return True, ""

    def abort(self, twin, wo: WorkOrder, task: FieldTask) -> None:
        pass


class CloseTrackExecutor(TaskExecutor):
    action = TaskAction.CLOSE_TRACK

    def blocking_reason(self, twin, wo, task):
        track = twin.state.tracks[task.target]
        if holds_asset(task) and track.status not in (TrackStatus.CLOSING, TrackStatus.CLOSED):
            return f"{task.target} is {track.status.value}: section changed while being closed"
        return None

    def start(self, twin, wo, task):
        track = twin.state.tracks[task.target]
        task.rollback.setdefault("status", track.status.value)
        if track.status != TrackStatus.CLOSED:
            twin.set_track_status(task.target, TrackStatus.CLOSING)
        task.detail = f"{task.target} is being taken out of service"

    def complete(self, twin, wo, task):
        twin.set_track_status(task.target, TrackStatus.CLOSED)

    def verify(self, twin, wo, task):
        track = twin.state.tracks[task.target]
        if track.status == TrackStatus.CLOSED:
            return True, f"{task.target} is closed to traffic"
        return False, f"{task.target} reports {track.status.value}, not CLOSED"

    def abort(self, twin, wo, task):
        track = twin.state.tracks[task.target]
        if track.status == TrackStatus.CLOSING and "status" in task.rollback:
            twin.set_track_status(task.target, TrackStatus(task.rollback["status"]))


class RerouteTrainExecutor(TaskExecutor):
    """The route is planned from wherever the train is when the order
    lands, not where it was when the task started: trains keep moving
    while the dispatcher works, and a route is only applied if it leaves
    from the train's current station."""
    action = TaskAction.REROUTE_TRAIN
    target_kind = "train"

    def validate(self, twin, task):
        super().validate(twin, task)
        route = task.params.get("route")
        if route is not None:
            if not isinstance(route, list) or len(route) < 2 or not all(isinstance(s, str) for s in route):
                raise ValueError(f"Task {task.id}: route must be a list of at least two station ids")
            unknown = [s for s in route if s not in twin.state.stations]
            if unknown:
                raise ValueError(f"Task {task.id}: unknown station(s) in route: {unknown}")
        destination = task.params.get("destination")
        if destination is not None and (
                not isinstance(destination, str) or destination not in twin.state.stations):
            raise ValueError(f"Task {task.id}: unknown destination {destination!r}")

    @staticmethod
    def _first_impassable(twin, route) -> Optional[str]:
        for u, v in zip(route, route[1:]):
            track = twin._track_between(u, v)
            if track is None:
                return f"{u}-{v} (no track)"
            if track.status in IMPASSABLE_TRACK_STATUSES:
                return f"{track.track_id} ({track.status.value})"
        return None

    def _destination(self, twin, task) -> Optional[str]:
        explicit = task.params.get("route")
        if explicit:
            return explicit[-1]
        if task.params.get("destination"):
            return task.params["destination"]
        # Pinned at start so a terminus turnaround does not move the goal
        if task.params.get("resolved_destination"):
            return task.params["resolved_destination"]
        train = twin.state.trains[task.target]
        return train.route[-1] if train.route else None

    def _resolve(self, twin, task) -> Tuple[Optional[List[str]], Optional[str], bool]:
        """(route, blocking reason, already there) from the train's current
        station on the live network — which already hides impassable
        sections. An explicit route is used from the train's position on
        it; a train that has left it cannot be teleported back."""
        train = twin.state.trains[task.target]
        here = train.current_station
        explicit = task.params.get("route")
        if explicit:
            if here not in explicit:
                return None, f"{task.target} is at {here}, which is not on the planned route", False
            remaining = list(explicit[explicit.index(here):])
            if len(remaining) < 2:
                return None, None, True
            bad = self._first_impassable(twin, remaining)
            if bad:
                return None, f"Planned route crosses {bad}", False
            return remaining, None, False

        destination = self._destination(twin, task)
        if not destination:
            return None, f"{task.target} has no destination to reroute towards", False
        if destination == here:
            return None, None, True
        try:
            route = twin.find_route(here, destination)
        except ValueError as exc:
            return None, f"No alternate route for {task.target}: {exc}", False
        if len(route) < 2:
            return None, f"No alternate route for {task.target}", False
        return route, None, False

    def blocking_reason(self, twin, wo, task):
        _, reason, _ = self._resolve(twin, task)
        return reason

    def start(self, twin, wo, task):
        if not task.params.get("destination") and not task.params.get("route"):
            train = twin.state.trains[task.target]
            task.params.setdefault("resolved_destination", train.route[-1] if train.route else None)
        route, _, arrived = self._resolve(twin, task)
        if arrived:
            task.detail = f"{task.target} is already at {self._destination(twin, task)}"
        else:
            task.detail = f"Dispatcher preparing a new route for {task.target} via {' > '.join(route)}"

    def complete(self, twin, wo, task):
        train = twin.state.trains[task.target]
        route, _, arrived = self._resolve(twin, task)
        task.params["arrived"] = arrived
        if arrived or route is None:
            task.params["resolved_route"] = None
            return
        # A new route leaving by the edge the train is already on keeps the
        # train's progress along it; reroute_train alone would restart it.
        old_next = train.route[train.route_index + 1] if train.route_index + 1 < len(train.route) else None
        progress = train.progress
        twin.reroute_train(task.target, list(route))
        if old_next == route[1]:
            train.progress = progress
        task.params["resolved_route"] = list(route)

    def verify(self, twin, wo, task):
        train = twin.state.trains[task.target]
        if task.params.get("arrived"):
            destination = self._destination(twin, task)
            if train.current_station != destination:
                return False, f"{task.target} is at {train.current_station}, not {destination}"
            if train.held:
                # Already where this order wanted it; its onward working is
                # another order's business, but say so rather than hide it.
                return True, (f"{task.target} already at {destination}; no reroute needed "
                              f"(held short of its next section)")
            return True, f"{task.target} already reached {destination}; no reroute needed"
        route = task.params.get("resolved_route")
        if not route or train.route != route:
            return False, f"{task.target} is not running the issued route"
        bad = self._first_impassable(twin, train.route)
        if bad:
            return False, f"{task.target}'s route crosses {bad}"
        return True, f"{task.target} running {route[0]} > {route[-1]} on open corridors"


class SpeedRestrictExecutor(TaskExecutor):
    action = TaskAction.SPEED_RESTRICT

    @staticmethod
    def _speed(task) -> float:
        return float(task.params.get("speed_kmh", DEFAULT_SPEED_RESTRICTION_KMH))

    def validate(self, twin, task):
        super().validate(twin, task)
        try:
            speed = self._speed(task)
        except (TypeError, ValueError):
            raise ValueError(f"Task {task.id}: speed_kmh must be a number")
        if speed <= 0:
            raise ValueError(f"Task {task.id}: speed_kmh must be positive")

    def start(self, twin, wo, task):
        track = twin.state.tracks[task.target]
        task.rollback.setdefault("speed_restriction_kmh", track.speed_restriction_kmh)
        task.detail = f"Posting {self._speed(task):g} km/h restriction on {task.target}"

    def complete(self, twin, wo, task):
        twin.state.tracks[task.target].speed_restriction_kmh = self._speed(task)

    def verify(self, twin, wo, task):
        track = twin.state.tracks[task.target]
        speed = self._speed(task)
        if track.speed_restriction_kmh == speed:
            return True, f"{task.target} restricted to {speed:g} km/h"
        return False, f"{task.target} reports no {speed:g} km/h restriction"


class DispatchCrewExecutor(TaskExecutor):
    action = TaskAction.DISPATCH_CREW
    needs_crew_access = True

    def start(self, twin, wo, task):
        crew_id = task.rollback.get("crew_id")
        if crew_id not in twin.crews:
            crew_id = twin._next_crew_id()
            twin.crews[crew_id] = CrewUnit(
                crew_id=crew_id,
                target=task.target,
                work_order_id=wo.id,
                task_id=task.id,
                dispatched_tick=twin.sim_tick,
            )
            task.rollback["crew_id"] = crew_id
        task.params["crew_id"] = crew_id
        task.detail = f"{crew_id} en route to {task.target}"

    def advance(self, twin, wo, task):
        task.detail = f"{task.params.get('crew_id')} en route to {task.target}, {task.ticks_remaining} tick(s) out"

    def complete(self, twin, wo, task):
        crew = twin.crews.get(task.params.get("crew_id"))
        if crew is not None:
            crew.status = CrewStatus.ON_SITE
            crew.arrived_tick = twin.sim_tick

    def verify(self, twin, wo, task):
        crew = twin.crews.get(task.params.get("crew_id"))
        if crew is not None and crew.status == CrewStatus.ON_SITE and crew.target == task.target:
            return True, f"{crew.crew_id} on site at {task.target}"
        return False, f"No crew on site at {task.target}"

    def abort(self, twin, wo, task):
        twin.crews.pop(task.params.get("crew_id"), None)


class RepairTrackExecutor(TaskExecutor):
    action = TaskAction.REPAIR_TRACK
    needs_crew_access = True

    @staticmethod
    def _restored_health(task) -> float:
        return float(task.params.get("restored_health", REPAIRED_HEALTH))

    def validate(self, twin, task):
        super().validate(twin, task)
        try:
            health = self._restored_health(task)
        except (TypeError, ValueError):
            raise ValueError(f"Task {task.id}: restored_health must be a number")
        if not 0.0 <= health <= 1.0:
            raise ValueError(f"Task {task.id}: restored_health must be between 0 and 1")

    def blocking_reason(self, twin, wo, task):
        track = twin.state.tracks[task.target]
        if holds_asset(task) and track.status != TrackStatus.UNDER_REPAIR:
            # Someone closed or otherwise changed the section under the crew
            return f"{task.target} is {track.status.value}: section changed while under repair"
        return None

    def start(self, twin, wo, task):
        track = twin.state.tracks[task.target]
        task.rollback.setdefault("status", track.status.value)
        twin.set_track_status(task.target, TrackStatus.UNDER_REPAIR)
        task.detail = f"{task.target} under repair"

    def advance(self, twin, wo, task):
        # Each tick closes an equal share of the gap that is left, measured
        # from the section's health now — so a section re-damaged under
        # the crew is rebuilt from what it is, not from what it was.
        track = twin.state.tracks[task.target]
        gap = self._restored_health(task) - track.health
        health = track.health + gap / (task.ticks_remaining + 1)
        twin.set_track_status(task.target, TrackStatus.UNDER_REPAIR, health=round(health, 4))
        task.detail = f"{task.target} under repair — {int(task.progress * 100)}%, health {health:.2f}"

    def complete(self, twin, wo, task):
        health = self._restored_health(task)
        status = TrackStatus.OPEN if health >= REPAIR_ACCEPTANCE_HEALTH else TrackStatus.DEGRADED
        twin.set_track_status(task.target, status, health=health)

    def verify(self, twin, wo, task):
        track = twin.state.tracks[task.target]
        if track.status == TrackStatus.OPEN and track.health >= REPAIR_ACCEPTANCE_HEALTH:
            return True, f"{task.target} operational at health {track.health:.2f}"
        return False, (
            f"{task.target} health {track.health:.2f} still below "
            f"{REPAIR_ACCEPTANCE_HEALTH:.2f} after repair ({track.status.value})"
        )

    def abort(self, twin, wo, task):
        track = twin.state.tracks[task.target]
        if track.status == TrackStatus.UNDER_REPAIR and "status" in task.rollback:
            # Work done so far stays done; only the "crew on the track"
            # marker is lifted.
            twin.set_track_status(task.target, TrackStatus(task.rollback["status"]))


class RestoreSignalExecutor(TaskExecutor):
    action = TaskAction.RESTORE_SIGNAL
    needs_crew_access = True

    @staticmethod
    def _endpoints(twin, task):
        track = twin.state.tracks[task.target]
        return [s for s in (track.source, track.destination) if s in twin.state.stations]

    def _aspects(self, twin, task):
        return [twin.state.stations[s].active_signals.get(task.target) for s in self._endpoints(twin, task)]

    def _set_aspect(self, twin, task, aspect: SignalStatus):
        for station_id in self._endpoints(twin, task):
            twin.state.stations[station_id].active_signals[task.target] = aspect

    def blocking_reason(self, twin, wo, task):
        if holds_asset(task) and any(a != SignalStatus.RED for a in self._aspects(twin, task)):
            return f"Signals on {task.target} were changed while under repair"
        return None

    def start(self, twin, wo, task):
        for station_id in self._endpoints(twin, task):
            current = twin.state.stations[station_id].active_signals.get(task.target)
            task.rollback.setdefault(
                f"signal:{station_id}", current.value if current is not None else None
            )
        # Protecting aspect while technicians work the interlocking
        self._set_aspect(twin, task, SignalStatus.RED)
        task.detail = f"Signals on {task.target} held at RED while under repair"

    def complete(self, twin, wo, task):
        self._set_aspect(twin, task, SignalStatus.GREEN)

    def verify(self, twin, wo, task):
        endpoints = self._endpoints(twin, task)
        if endpoints and all(a == SignalStatus.GREEN for a in self._aspects(twin, task)):
            return True, f"Signals on {task.target} showing GREEN at {' and '.join(endpoints)}"
        return False, f"Signals on {task.target} are not all GREEN"

    def abort(self, twin, wo, task):
        for station_id in self._endpoints(twin, task):
            prior = task.rollback.get(f"signal:{station_id}")
            signals = twin.state.stations[station_id].active_signals
            if prior is None:
                signals.pop(task.target, None)
            else:
                signals[task.target] = SignalStatus(prior)


EXECUTORS: Dict[TaskAction, TaskExecutor] = {
    ex.action: ex for ex in (
        CloseTrackExecutor(),
        RerouteTrainExecutor(),
        SpeedRestrictExecutor(),
        DispatchCrewExecutor(),
        RepairTrackExecutor(),
        RestoreSignalExecutor(),
    )
}


def executor_for(action: TaskAction) -> TaskExecutor:
    return EXECUTORS[TaskAction(action)]


def validate_work_order(twin, wo: WorkOrder) -> None:
    """Reject an order whose tasks name assets the twin does not have."""
    for task in wo.tasks:
        executor_for(task.action).validate(twin, task)


def build_incident_response(twin, track_id: str, incident_id: Optional[str] = None,
                            work_order_id: Optional[str] = None) -> WorkOrder:
    """The standard response to a failed section: close it, get a crew there,
    rebuild it. The repair waits on both earlier tasks."""
    tasks = [
        FieldTask(id="task_1", action=TaskAction.CLOSE_TRACK, target=track_id, ticks_required=1),
        FieldTask(id="task_2", action=TaskAction.DISPATCH_CREW, target=track_id,
                  ticks_required=10, depends_on=["task_1"]),
        FieldTask(id="task_3", action=TaskAction.REPAIR_TRACK, target=track_id,
                  ticks_required=20, depends_on=["task_1", "task_2"]),
    ]
    return WorkOrder(
        id=work_order_id or twin._next_work_order_id(),
        incident_id=incident_id,
        type="CRITICAL_INCIDENT_RESPONSE",
        target=track_id,
        tasks=tasks,
    )


# ---------------------------------------------------------------------------
# Per-tick engine
# ---------------------------------------------------------------------------

def _conflicts(a: FieldTask, b: FieldTask) -> bool:
    """Two tasks that cannot work the same asset at the same time."""
    if a.target != b.target:
        return False
    if a.action == b.action:
        return True
    return a.action in TRACK_STATUS_WRITERS and b.action in TRACK_STATUS_WRITERS


def _sibling_gate(wo: WorkOrder, task: FieldTask) -> Tuple[Optional[str], Optional[str]]:
    """Implicit ordering inside one order: (why to keep waiting, why it is
    blocked). A task waits for a sibling that is working the same asset or
    bringing its crew; it is blocked if that sibling ended without doing so."""
    for other in wo.tasks:
        if other.id == task.id:
            continue
        if (task.action in CREW_WORK and other.action == TaskAction.DISPATCH_CREW
                and other.target == task.target):
            if other.status == TaskStatus.COMPLETED:
                continue
            if other.is_terminal:
                return None, f"Crew for {task.target} did not arrive ({other.id} is {other.status.value})"
            return f"Waiting for {other.id} (crew for {task.target})", None
        if _conflicts(task, other) and holds_asset(other):
            return f"Waiting for {other.id} to finish {other.action.value} on {task.target}", None
    return None, None


def _blocking_reason(twin, wo: WorkOrder, task: FieldTask) -> Optional[str]:
    """Why the field cannot perform this task right now, or None.

    The twin decides: dependencies that ended without completing, assets
    that vanished, another order already working the same asset, weather
    that keeps crews away, then whatever the action itself requires."""
    failed = wo.failed_dependency(task)
    if failed is not None:
        return f"Depends on {failed.id} which is {failed.status.value}"

    executor = executor_for(task.action)
    pool = twin.state.trains if executor.target_kind == "train" else twin.state.tracks
    if task.target not in pool:
        return f"Target {task.target} no longer exists in the twin"

    for other_wo in twin.work_orders.values():
        if other_wo.id == wo.id or other_wo.cancelled:
            continue
        for other in other_wo.tasks:
            if holds_asset(other) and _conflicts(task, other):
                verb = "executing" if other.status == TaskStatus.IN_PROGRESS else "holding"
                return (f"Another active operation ({other_wo.id}/{other.id}) is already "
                        f"{verb} {other.action.value} on {task.target}")

    if executor.needs_crew_access:
        condition = twin.state.weather.get(task.target)
        if condition in CREW_ACCESS_BLOCKERS:
            return f"{CREW_ACCESS_BLOCKERS[condition]} to {task.target}"

    return executor.blocking_reason(twin, wo, task)


def _block(wo: WorkOrder, task: FieldTask, reason: str, tick: int) -> None:
    task.transition(TaskStatus.BLOCKED, detail=f"Blocked: {reason}", blocking_reason=reason)
    wo.log(tick, "blocked", reason, task.id)


def _start(twin, wo: WorkOrder, task: FieldTask, tick: int) -> None:
    executor_for(task.action).start(twin, wo, task)
    task.transition(TaskStatus.IN_PROGRESS)
    if task.started_tick is None:
        task.started_tick = tick
    wo.log(tick, "started", task.detail, task.id)


def _advance(twin, wo: WorkOrder, task: FieldTask, tick: int) -> None:
    """One tick of work, then — if the duration has run out — the final
    mutation and the read-back that decides COMPLETED or UNRESOLVED."""
    executor = executor_for(task.action)
    task.ticks_remaining = max(0, task.ticks_remaining - 1)
    executor.advance(twin, wo, task)
    if task.ticks_remaining > 0:
        return
    executor.complete(twin, wo, task)
    ok, detail = executor.verify(twin, wo, task)
    if ok:
        task.transition(TaskStatus.COMPLETED, detail=detail)
        task.completed_tick = tick
        wo.log(tick, "completed", detail, task.id)
    else:
        task.transition(TaskStatus.UNRESOLVED, detail=f"Verification failed: {detail}")
        wo.log(tick, "unresolved", detail, task.id)


def _refresh_status(twin, wo: WorkOrder, tick: int) -> None:
    if wo.is_settled:
        # Nothing in this order can move again, however it ended
        stand_down_crews(twin, wo)
    new_status = wo.recalculate_status()
    if new_status == wo.status:
        return
    wo.log(tick, "status", f"{wo.status.value} -> {new_status.value}")
    wo.status = new_status


def stand_down_crews(twin, wo: WorkOrder) -> None:
    for crew_id in [c.crew_id for c in twin.crews.values() if c.work_order_id == wo.id]:
        del twin.crews[crew_id]


def run_tick(twin) -> None:
    """Advance every live work order by one simulation tick.

    Within a tick the engine keeps sweeping an order until nothing changes,
    so a task whose last dependency completes this tick starts this tick
    (a one-tick closure finishes and the crew leaves in the same tick). Each
    task does at most one tick of work per sweep, so the sweep settles."""
    tick = twin.sim_tick
    for wo in list(twin.work_orders.values()):
        if wo.cancelled or wo.is_settled:
            continue
        advanced = set()
        for _ in range(MAX_PASSES_PER_TICK):
            changed = False
            for task in wo.tasks:
                if task.status == TaskStatus.BLOCKED and wo.auto_retry:
                    # A block that came from a sibling ending badly, or from
                    # the asset being changed under the task, does not
                    # clear by itself — only an explicit retry lifts it.
                    _, gate_reason = _sibling_gate(wo, task)
                    if (wo.dependencies_met(task) and gate_reason is None
                            and _blocking_reason(twin, wo, task) is None):
                        task.transition(TaskStatus.PENDING, detail="Blocking condition cleared; retrying")
                        wo.log(tick, "retried", "Blocking condition cleared", task.id)
                        changed = True

                if task.status == TaskStatus.PENDING:
                    failed = wo.failed_dependency(task)
                    if failed is not None:
                        _block(wo, task, f"Depends on {failed.id} which is {failed.status.value}", tick)
                        changed = True
                        continue
                    if not wo.dependencies_met(task):
                        continue
                    wait, reason = _sibling_gate(wo, task)
                    if wait:
                        task.detail = wait
                        continue
                    reason = reason or _blocking_reason(twin, wo, task)
                    if reason:
                        _block(wo, task, reason, tick)
                        changed = True
                        continue
                    _start(twin, wo, task, tick)
                    changed = True

                if task.status == TaskStatus.IN_PROGRESS and task.id not in advanced:
                    advanced.add(task.id)
                    reason = _blocking_reason(twin, wo, task)
                    if reason:
                        # Conditions changed under a running task: it stops
                        # where it is and keeps its remaining work.
                        _block(wo, task, reason, tick)
                    else:
                        _advance(twin, wo, task, tick)
                    changed = True
            if not changed:
                break
        _refresh_status(twin, wo, tick)


def cancel(twin, wo: WorkOrder, reason: str) -> None:
    tick = twin.sim_tick
    for task in wo.tasks:
        if task.is_terminal:
            continue
        if task.started:
            # Running, blocked mid-flight or retried: its transitional
            # state is on the asset either way and must come off.
            executor_for(task.action).abort(twin, wo, task)
        task.transition(TaskStatus.CANCELLED, detail=f"Cancelled: {reason}")
    wo.cancelled = True
    wo.cancelled_tick = tick
    wo.cancel_reason = reason
    stand_down_crews(twin, wo)
    wo.log(tick, "cancelled", reason)
    _refresh_status(twin, wo, tick)


def retry(twin, wo: WorkOrder, task_ids: List[str]) -> List[str]:
    """BLOCKED -> PENDING for the named tasks; the next tick re-evaluates
    whether the field can now do the work."""
    tick = twin.sim_tick
    retried = []
    for task_id in task_ids:
        task = wo.task(task_id)
        if task.status != TaskStatus.BLOCKED:
            raise ValueError(f"Task {task_id} is {task.status.value}, only BLOCKED tasks can be retried")
        task.transition(TaskStatus.PENDING, detail="Retry requested")
        wo.log(tick, "retried", "Retry requested", task.id)
        retried.append(task.id)
    _refresh_status(twin, wo, tick)
    return retried
