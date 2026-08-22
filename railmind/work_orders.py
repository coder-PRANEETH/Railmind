"""Execution objects for the digital twin: WorkOrders and their FieldTasks.

A WorkOrder is the twin's own record of one intervention. The agents decide
what should happen and the backend carries the order, but only the twin — by
advancing the order over simulation ticks and reading back the physical state
it produced — can say whether the work actually happened. Nothing in this
module mutates railway state; the executors in execution.py do that.
"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class TaskAction(str, Enum):
    CLOSE_TRACK = "CLOSE_TRACK"
    REROUTE_TRAIN = "REROUTE_TRAIN"
    SPEED_RESTRICT = "SPEED_RESTRICT"
    DISPATCH_CREW = "DISPATCH_CREW"
    REPAIR_TRACK = "REPAIR_TRACK"
    RESTORE_SIGNAL = "RESTORE_SIGNAL"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    UNRESOLVED = "UNRESOLVED"
    CANCELLED = "CANCELLED"


# The task state machine. A move absent here is a caller bug, not a
# condition of the railway, so attempting it raises.
TASK_TRANSITIONS = {
    TaskStatus.PENDING: {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.IN_PROGRESS: {
        TaskStatus.COMPLETED, TaskStatus.BLOCKED, TaskStatus.UNRESOLVED, TaskStatus.CANCELLED,
    },
    TaskStatus.BLOCKED: {TaskStatus.PENDING, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.UNRESOLVED: set(),
    TaskStatus.CANCELLED: set(),
}

TERMINAL_TASK_STATUSES = frozenset({
    TaskStatus.COMPLETED, TaskStatus.UNRESOLVED, TaskStatus.CANCELLED,
})


class WorkOrderStatus(str, Enum):
    """Same vocabulary and precedence as the escalation ledger's completion
    signal (agents/escalation.py), so a work order and the incident it serves
    never disagree about what "done" means. BLOCKED outranks PARTIAL: a
    dispatcher must not read "in progress" and wait for work that cannot
    proceed."""
    UNRESOLVED = "UNRESOLVED"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class CrewStatus(str, Enum):
    EN_ROUTE = "EN_ROUTE"
    ON_SITE = "ON_SITE"


# Simulated duration per action when the order does not give one.
DEFAULT_TICKS_REQUIRED: Dict[TaskAction, int] = {
    TaskAction.CLOSE_TRACK: 1,
    TaskAction.REROUTE_TRAIN: 2,
    TaskAction.SPEED_RESTRICT: 1,
    TaskAction.DISPATCH_CREW: 10,
    TaskAction.REPAIR_TRACK: 20,
    TaskAction.RESTORE_SIGNAL: 8,
}

# Actions that rewrite a track's status. Two of them cannot work the same
# section at once, whatever their order.
TRACK_STATUS_WRITERS = frozenset({TaskAction.CLOSE_TRACK, TaskAction.REPAIR_TRACK})

# Field work that needs a gang on site: a DISPATCH_CREW for the same track in
# the same order is an implicit prerequisite even if the planner did not
# declare the dependency.
CREW_WORK = frozenset({TaskAction.REPAIR_TRACK, TaskAction.RESTORE_SIGNAL})

# Bounds on what one order may ask for: every task is visited on every tick
# under the backend's twin lock, and the ETA is a sum of durations.
MAX_TASKS_PER_ORDER = 100
MAX_TICKS_REQUIRED = 10_000

# Events kept per work order; older ones are dropped so a long-lived order
# cannot grow without bound.
MAX_EVENTS = 200

# What a caller may say about an order and its tasks. Everything else on the
# models (status, ticks_remaining, rollback, events, ...) is the engine's
# record of what the railway actually did, and is never taken from input.
ORDER_INPUT_FIELDS = frozenset({"id", "incident_id", "type", "target", "tasks", "auto_retry"})
TASK_INPUT_FIELDS = frozenset({"id", "action", "target", "ticks_required", "depends_on", "params"})
ENGINE_PARAMS = frozenset({"crew_id", "resolved_route", "resolved_destination", "arrived"})


class CrewUnit(BaseModel):
    crew_id: str
    target: str                     # track the crew is heading for / working on
    status: CrewStatus = CrewStatus.EN_ROUTE
    work_order_id: str
    task_id: str
    dispatched_tick: int
    arrived_tick: Optional[int] = None

    def payload(self) -> dict:
        return {
            "id": self.crew_id,
            "target": self.target,
            "status": self.status.value,
            "work_order_id": self.work_order_id,
            "task_id": self.task_id,
            "dispatched_tick": self.dispatched_tick,
            "arrived_tick": self.arrived_tick,
        }


class FieldTask(BaseModel):
    id: str
    action: TaskAction
    target: str                     # track id, or train id for REROUTE_TRAIN
    status: TaskStatus = TaskStatus.PENDING
    ticks_required: Optional[int] = Field(default=None, ge=1, le=MAX_TICKS_REQUIRED)
    ticks_remaining: Optional[int] = Field(default=None, ge=0)
    depends_on: List[str] = Field(default_factory=list)
    blocking_reason: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    detail: str = "Not yet started."
    started_tick: Optional[int] = None
    completed_tick: Optional[int] = None
    # Physical values captured when the task started, so a cancelled task can
    # put the asset back the way it found it. Never part of the public payload.
    rollback: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("ticks_required", "ticks_remaining", mode="before")
    @classmethod
    def _reject_bool(cls, value):
        # pydantic would read True as 1 tick
        if isinstance(value, bool):
            raise ValueError("must be an integer, not a boolean")
        return value

    @model_validator(mode="after")
    def _fill_duration(self):
        if self.ticks_required is None:
            self.ticks_required = DEFAULT_TICKS_REQUIRED[self.action]
        if self.ticks_remaining is None or self.ticks_remaining > self.ticks_required:
            self.ticks_remaining = self.ticks_required
        return self

    @property
    def progress(self) -> float:
        if self.status == TaskStatus.COMPLETED:
            return 1.0
        return (self.ticks_required - self.ticks_remaining) / self.ticks_required

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

    @property
    def started(self) -> bool:
        """The task has applied its transitional state at least once —
        running, blocked mid-flight, or retried and waiting to resume."""
        return self.started_tick is not None

    def is_fresh(self) -> bool:
        return (
            self.status == TaskStatus.PENDING
            and self.ticks_remaining == self.ticks_required
            and self.started_tick is None
            and self.completed_tick is None
            and self.blocking_reason is None
            and not self.rollback
            and not (ENGINE_PARAMS & set(self.params))
        )

    def transition(self, new_status: TaskStatus, detail: Optional[str] = None,
                   blocking_reason: Optional[str] = None) -> None:
        if new_status not in TASK_TRANSITIONS[self.status]:
            raise ValueError(
                f"Task {self.id}: illegal transition {self.status.value} -> {new_status.value}"
            )
        self.status = new_status
        self.blocking_reason = blocking_reason if new_status == TaskStatus.BLOCKED else None
        if detail is not None:
            self.detail = detail

    def payload(self) -> dict:
        return {
            "id": self.id,
            "action": self.action.value,
            "target": self.target,
            "status": self.status.value,
            "ticks_required": self.ticks_required,
            "ticks_remaining": self.ticks_remaining,
            "progress": round(self.progress, 3),
            "depends_on": list(self.depends_on),
            "blocking_reason": self.blocking_reason,
            "detail": self.detail,
            "started_tick": self.started_tick,
            "completed_tick": self.completed_tick,
            "params": dict(self.params),
        }


class WorkOrderEvent(BaseModel):
    tick: int
    kind: str                       # registered | started | completed | blocked | unresolved | retried | cancelled | status
    detail: str
    task_id: Optional[str] = None

    def payload(self) -> dict:
        return {
            "tick": self.tick,
            "kind": self.kind,
            "task_id": self.task_id,
            "detail": self.detail,
        }


class WorkOrder(BaseModel):
    id: str
    incident_id: Optional[str] = None
    type: str = "CRITICAL_INCIDENT_RESPONSE"
    target: str
    status: WorkOrderStatus = WorkOrderStatus.UNRESOLVED
    tasks: List[FieldTask] = Field(default_factory=list)
    created_at: float = 0.0
    created_tick: int = 0
    cancelled: bool = False
    cancelled_tick: Optional[int] = None
    cancel_reason: Optional[str] = None
    # When set, a BLOCKED task goes back to PENDING by itself as soon as the
    # twin finds its blocking condition gone; otherwise it waits for an
    # explicit retry.
    auto_retry: bool = False
    events: List[WorkOrderEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_tasks(self):
        if not self.tasks:
            raise ValueError("A work order needs at least one task")
        if len(self.tasks) > MAX_TASKS_PER_ORDER:
            raise ValueError(f"A work order may carry at most {MAX_TASKS_PER_ORDER} tasks")
        ids = [t.id for t in self.tasks]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"Duplicate task id(s): {dupes}")
        known = set(ids)
        for task in self.tasks:
            for dep in task.depends_on:
                if dep == task.id:
                    raise ValueError(f"Task {task.id} depends on itself")
                if dep not in known:
                    raise ValueError(f"Task {task.id} depends on unknown task {dep}")

        # Declared dependencies plus the implicit one the engine enforces:
        # crew work waits for the crew its own order dispatches to that
        # track. A cycle through either kind would wait forever.
        deps = {t.id: list(t.depends_on) for t in self.tasks}
        for task in self.tasks:
            if task.action in CREW_WORK:
                for other in self.tasks:
                    if (other.action == TaskAction.DISPATCH_CREW and other.target == task.target
                            and other.id not in deps[task.id]):
                        deps[task.id].append(other.id)
        colour: Dict[str, str] = {}

        def visit(tid, path):
            if colour.get(tid) == "done":
                return
            if colour.get(tid) == "active":
                raise ValueError(
                    f"Dependency cycle: {' -> '.join(path + [tid])} "
                    "(crew work implicitly waits for its order's DISPATCH_CREW)"
                )
            colour[tid] = "active"
            for dep in deps[tid]:
                visit(dep, path + [tid])
            colour[tid] = "done"

        for tid in deps:
            visit(tid, [])
        return self

    def task(self, task_id: str) -> FieldTask:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)

    def dependencies_met(self, task: FieldTask) -> bool:
        return all(self.task(d).status == TaskStatus.COMPLETED for d in task.depends_on)

    def failed_dependency(self, task: FieldTask) -> Optional[FieldTask]:
        """A dependency that has ended without completing, so this task can
        never start on its own."""
        for d in task.depends_on:
            dep = self.task(d)
            if dep.is_terminal and dep.status != TaskStatus.COMPLETED:
                return dep
        return None

    @property
    def is_settled(self) -> bool:
        """Every task has ended; nothing in this order can move again."""
        return all(t.is_terminal for t in self.tasks)

    def assert_fresh(self) -> None:
        """An order is registered before any work, never with work already
        claimed: the engine alone moves tasks out of PENDING."""
        if self.cancelled or self.events:
            raise ValueError(f"Work order {self.id} must be registered fresh (not cancelled, no history)")
        for task in self.tasks:
            if not task.is_fresh():
                raise ValueError(
                    f"Task {task.id} must be registered PENDING with no execution state "
                    f"(got {task.status.value})"
                )

    def recalculate_status(self) -> WorkOrderStatus:
        if self.cancelled:
            return WorkOrderStatus.CANCELLED
        statuses = [t.status for t in self.tasks]
        if TaskStatus.BLOCKED in statuses:
            return WorkOrderStatus.BLOCKED
        if all(s == TaskStatus.COMPLETED for s in statuses):
            return WorkOrderStatus.COMPLETE
        if any(s in (TaskStatus.COMPLETED, TaskStatus.IN_PROGRESS) for s in statuses):
            return WorkOrderStatus.PARTIAL
        return WorkOrderStatus.UNRESOLVED

    @property
    def completion_percentage(self) -> int:
        return int(round(100.0 * sum(t.progress for t in self.tasks) / len(self.tasks)))

    def estimated_ticks_remaining(self) -> int:
        """Critical-path estimate of the ticks until the last task completes.

        A task that waits on others starts in the same tick its last
        dependency completes, so each hop along the chain overlaps by one
        tick. Blocked tasks are costed as if retried now."""
        if self.cancelled:
            return 0
        by_id = {t.id: t for t in self.tasks}
        memo: Dict[str, int] = {}

        def finish(tid):
            if tid in memo:
                return memo[tid]
            t = by_id[tid]
            own = 0 if t.is_terminal else t.ticks_remaining
            latest_dep = max((finish(d) for d in t.depends_on), default=0)
            if own == 0:
                memo[tid] = latest_dep
            elif latest_dep == 0:
                memo[tid] = own
            else:
                memo[tid] = latest_dep + own - 1
            return memo[tid]

        return max((finish(t.id) for t in self.tasks), default=0)

    def log(self, tick: int, kind: str, detail: str, task_id: Optional[str] = None) -> None:
        self.events.append(WorkOrderEvent(tick=tick, kind=kind, detail=detail, task_id=task_id))
        if len(self.events) > MAX_EVENTS:
            del self.events[:len(self.events) - MAX_EVENTS]

    def payload(self, max_events: int = 12) -> dict:
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "type": self.type,
            "target": self.target,
            "status": self.status.value,
            "completion_percentage": self.completion_percentage,
            "estimated_ticks_remaining": self.estimated_ticks_remaining(),
            "created_tick": self.created_tick,
            "cancelled": self.cancelled,
            "cancel_reason": self.cancel_reason,
            "auto_retry": self.auto_retry,
            "tasks": [t.payload() for t in self.tasks],
            "events": [e.payload() for e in self.events[-max_events:]],
        }
