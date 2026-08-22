# Work orders: field work inside the Digital Twin

The agents decide what *should* happen. The backend carries the order. The
Digital Twin decides whether it *has* happened — because only the twin runs
the railway the work is done on.

A **work order** is the twin's record of one intervention. It contains
**field tasks** (close a section, send a crew, rebuild the track, …) that
take simulation time, can be held up by the railway itself, and are marked
done only when the twin reads back a physical state that proves it.

```
WorkOrder  ──►  FieldTasks  ──►  executed over tick()  ──►  physical state changes
                                                                   │
                                              verification reads it back
                                                                   │
                                    COMPLETED / BLOCKED / UNRESOLVED per task
                                                                   │
                                     COMPLETE / PARTIAL / BLOCKED / UNRESOLVED per order
```

Code: [`railmind/work_orders.py`](../railmind/work_orders.py) (models and
state machine), [`railmind/execution.py`](../railmind/execution.py)
(per-action executors and the tick engine), the `register_work_order` /
`create_incident_response` / `cancel_work_order` / `retry_task` methods on
[`DigitalTwin`](../railmind/twin.py), and the `/api/workorders/` endpoints in
[`railmind_django/api/views.py`](../railmind_django/api/views.py).

## 1 · Objects

**WorkOrder** — `id` (`WO-001`, …), `incident_id`, `type`, `target`,
`status`, `tasks`, `created_tick`, `cancelled` / `cancel_reason`,
`auto_retry`, and an event log (`registered`, `started`, `completed`,
`blocked`, `unresolved`, `retried`, `cancelled`, `status`).

**FieldTask** — `id`, `action`, `target`, `status`, `ticks_required`,
`ticks_remaining`, `depends_on`, `blocking_reason`, `params`, `detail`,
`started_tick`, `completed_tick`. Progress is
`(ticks_required − ticks_remaining) / ticks_required`.

Tasks point at the twin's own `TrackSegment`, `TrainState` and station
signal objects. There is no second copy of T23 for work orders: "the order
says T23 is repaired" and "the twin says T23 is operational" are the same
object.

A caller *describes* the work — `id`, `incident_id`, `type`, `target`,
`auto_retry`, and per task `id`, `action`, `target`, `ticks_required`,
`depends_on`, `params`. Everything else on the models (`status`,
`ticks_remaining`, `rollback`, `events`, …) is the engine's record of what
the railway actually did; registration rejects it, so no request can
register work as already done.

**CrewUnit** — created by `DISPATCH_CREW`, visible in the snapshot as
`crews`, `EN_ROUTE` then `ON_SITE`, stood down when its order completes or
is cancelled.

## 2 · The task state machine

```
PENDING ──► IN_PROGRESS ──► COMPLETED
   │             │
   │             ├──► BLOCKED ──► PENDING   (retry)
   └──► BLOCKED ─┘
                 └──► UNRESOLVED
any non-terminal ──► CANCELLED           (work order cancelled)
```

| State | Meaning |
|---|---|
| `PENDING` | Exists; waiting on dependencies or on its first tick. |
| `IN_PROGRESS` | The simulated railway is doing the work; `ticks_remaining` counts down. |
| `COMPLETED` | The required physical state was reached **and read back**. |
| `BLOCKED` | The railway cannot perform it right now; `blocking_reason` says why. Stays blocked until retried. |
| `UNRESOLVED` | The work ran its course but the physical state does not satisfy the completion criteria. Terminal. |
| `CANCELLED` | Its order was cancelled before it finished. Terminal. |

Illegal moves raise — the table lives in `TASK_TRANSITIONS`.

## 3 · Time passes inside the twin

Nothing is instantaneous. `DigitalTwin.tick()` first moves the trains and
the weather as before, then runs every live order:

1. Tasks whose dependencies are all `COMPLETED` are checked for blocking
   conditions and started (`PENDING → IN_PROGRESS`). Starting applies the
   task's *transitional* physical state.
2. Each `IN_PROGRESS` task is re-checked — a storm that begins mid-repair
   stops the work where it stands — and then does one tick of work.
3. When `ticks_remaining` reaches zero the final mutation is applied and
   the result is **verified against the twin**: `COMPLETED` or `UNRESOLVED`.
4. The order's status is recomputed.

A task whose last dependency completes this tick starts this tick, so a
1-tick closure finishes and the crew leaves in the same tick (the ETA
exposed as `estimated_ticks_remaining` is a critical-path calculation that
accounts for this overlap).

The engine never draws from the twin's RNG: two clones stay in lockstep
whether or not they carry work orders, and a forecast clone plays the
repair forward alongside the trains.

## 4 · What each action does

| Action | Target | While executing | Completion is verified when |
|---|---|---|---|
| `CLOSE_TRACK` | track | status `CLOSING` (holds traffic, hidden from routing) | track status is `CLOSED` |
| `REROUTE_TRAIN` | train | dispatcher prepares the route; the train keeps moving | the route — planned on the live network from **where the train is when the order lands** (or the remainder of `params.route` from the train's position on it) — is what the train is running, every hop passable. A train that reaches its destination first completes the task as a no-op |
| `SPEED_RESTRICT` | track | restriction being posted | `speed_restriction_kmh` equals `params.speed_kmh` (default 60) — and trains actually slow down |
| `DISPATCH_CREW` | track | a `CrewUnit` is `EN_ROUTE` | the crew is `ON_SITE` at the target |
| `REPAIR_TRACK` | track | status `UNDER_REPAIR`; each tick closes an equal share of the gap between the section's health *now* and `params.restored_health` (default 0.95) | status `OPEN` and health ≥ 0.4 — otherwise `UNRESOLVED` |
| `RESTORE_SIGNAL` | track | both end signals held at `RED` | both end signals show `GREEN` |

Defaults for `ticks_required`: close 1, reroute 2, restrict 1, dispatch 10,
repair 20, signal 8. `DigitalTwin.create_incident_response(track_id)`
registers the standard order: close (1 tick) → dispatch crew (10) → repair
(20), with the repair depending on both earlier tasks.

## 5 · Blocking — the twin decides

Before a task starts, and on every tick while it runs, the engine asks
whether the railway can actually do it:

- a dependency ended without completing (`UNRESOLVED`, `CANCELLED`);
- the target no longer exists;
- another order is already working — or, stopped mid-flight, still
  holding — the same asset: the same action, or any two of the
  track-status writers (`CLOSE_TRACK`, `REPAIR_TRACK`) on one section. A
  task keeps its section until it is retried, cancelled or ends;
- a `STORM` on the target keeps crews off the track (`DISPATCH_CREW`,
  `REPAIR_TRACK`, `RESTORE_SIGNAL`);
- the section was changed under a running task — an operator closure
  during a repair is not silently undone by the next tick of repair work,
  the repair stops and says so;
- action-specific conditions: no alternate route exists for a reroute, a
  planned route crosses a closed section or the train has left it, a crew
  the same order dispatched for this track ended without arriving.

Inside one order, siblings are sequenced rather than blocked: a repair
waits (`PENDING`, with a "Waiting for task_2 …" detail) for the crew the
order is sending, and for a sibling that is already writing the same
section's status. That implicit crew dependency takes part in cycle
detection at registration, so an order cannot wait on itself.

A blocked task keeps its remaining work and waits. `retry_task()` moves it
back to `PENDING`; an order registered with `auto_retry: true` retries on
its own the tick the condition clears — except a block caused by the
asset being changed under the task (an operator's closure during a
repair) or by a sibling ending badly, which only an explicit retry lifts:
resuming then deliberately re-applies the work to the section as it is.

## 6 · Order status

Same vocabulary and precedence as the escalation ledger's completion signal
(`agents/escalation.py`), so an order and the incident it serves never
disagree about "done":

| Tasks | Order |
|---|---|
| any `BLOCKED` | `BLOCKED` — outranks everything but cancellation; nobody should wait on impossible work |
| all `COMPLETED` | `COMPLETE` |
| some `COMPLETED` or `IN_PROGRESS` | `PARTIAL` |
| nothing has moved (all `PENDING`, or only `UNRESOLVED`) | `UNRESOLVED` |
| order cancelled | `CANCELLED` |

`completion_percentage` averages task progress. An order whose tasks have
all ended — however they ended — stands its crews down and is not swept
again.

The ledger, for its part, treats `CLOSING` and `UNDER_REPAIR` corridors as
blocking (`is_blocking`), so a half-rebuilt track whose health has climbed
past the failure threshold cannot flip an incident to `COMPLETE` before the
twin has actually reopened it — and a closure or hold work item that was
proved earlier stays `DONE` when the repair lifts it (the train is moving
again on a clear route), so the incident *can* reach `COMPLETE` the moment
the corridor is restored. A re-dispatched work item has to be proved
again.

## 7 · Cancellation

`cancel_work_order(id, reason)` aborts every task that has started —
running, blocked mid-flight, or retried and waiting — and puts its asset
back: a `CLOSING` section reopens, an `UNDER_REPAIR` section returns to
its prior status (keeping the health it has gained), a crew en route is
recalled, signals return to their prior aspect. Finished work stays
finished. Nothing in a cancelled order advances again.

## 8 · API (Digital Twin service, port 8000)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/workorders/` | Every order with its tasks, progress and recent events |
| `POST` | `/api/workorders/` | Register an order — `{"template": "CRITICAL_INCIDENT_RESPONSE", "track_id": "T23", "incident_id": "INC-001"}` or an explicit `{"target", "tasks": [...]}` |
| `GET` | `/api/workorders/{id}/` | What is happening to one order right now |
| `POST` | `/api/workorders/{id}/retry/` | `BLOCKED → PENDING` for one task (`task_id`) or all blocked tasks |
| `POST` | `/api/workorders/{id}/cancel/` | Cancel; aborts work in flight |
| `POST` | `/api/tick/` | Fast-forward the simulation `{"ticks": n}` (1–200) |

`GET /api/state/` now also carries `sim_tick`, `work_orders` and `crews`.
All endpoints honour `X-Session-ID`, so a sandbox future carries its own
orders.

### Through the agent orchestrator (port 8001)

The console talks to the agent service, which relays the same verbs to
whichever twin it is using — a remote one over the endpoints above, or its
embedded twin in-process — so field work works fully offline too:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/workorders` | Every order on the live twin |
| `POST` | `/workorders/incident-response/{track_id}` | Order close → crew → repair on a failed section, attributed to its open incident (`409` if one is already live there) |
| `POST` | `/workorders/{id}/retry?task_id=` | Retry blocked work |
| `POST` | `/workorders/{id}/cancel?reason=` | Cancel an order |
| `POST` | `/workorders/tick?ticks=` | Fast-forward the twin (1–200 ticks) |

`GET /state` carries `sim_tick`, `work_orders` and `crews` alongside the
fleet.

**How a plan becomes field work.** The pipeline's specialists attach field
requirements to their findings (a failed section needs `REPAIR_TRACK`, a
red signal needs `RESTORE_SIGNAL`); the `escalation_analysis` stage
aggregates them, the planner prices them into each plan's ETTR (Minimal
Intervention defers them and is penalised for it), and the Master Agent
builds a **proposed WorkOrder** (`agents/workorder.py`) that every run
response carries as `work_order` with status `PROPOSED`. Nothing is
executed by the agent. When the operator applies a plan:

1. the plan's operational actions (closures, reroutes) land on the twin at
   once, as before;
2. the proposal is rebuilt for the plan actually approved and its field
   tasks (`DISPATCH_CREW`, `REPAIR_TRACK`, `RESTORE_SIGNAL`) are handed to
   the twin as a work order per section — unless field work is already live
   there, or the plan defers it (`DEFERRED`);
3. the ledger records every action *and* every field task as a work item,
   proved against the twin: a `dispatch_crew` item is `DONE` when a crew is
   on site (or the twin has completed the dispatch), `repair_track` when
   the section reads `OPEN` at health ≥ 0.4, `restore_signal` when both end
   signals show `GREEN`.

The response's `work_order` then reads `DISPATCHED` (with `twin_orders`),
`DEFERRED`, or `NO_FIELD_WORK`, and the execution log records the hand-off
as `Field` entries. Closing a broken section is only the start of fixing
it; the order is how the incident gets restored rather than merely routed
around.

### In the escalation ledger and the console

Every incident payload from `GET /escalation` carries `field_work` — the
order serving its corridor (`id`, `status`, `completion_percentage`,
`estimated_ticks_remaining`, `blocked_reason`, the running task's `current`
detail, and the task list). It is context for the controller, not the
completion signal: the signal is still graded from trains and proved
actions alone.

The operator console shows a **Field Work** panel (orders, task progress,
blocking reasons, crews, recent events, *Retry blocked work* / *Cancel
order*, *Order repair on {track}* and *+5 ticks*), and each incident card in
the Escalation Tracker carries a one-line field-work summary.

Example detail payload:

```json
{
  "id": "WO-001", "incident_id": "INC-001", "type": "CRITICAL_INCIDENT_RESPONSE",
  "target": "T23", "status": "PARTIAL",
  "completion_percentage": 72, "estimated_ticks_remaining": 17,
  "tasks": [
    {"id": "task_1", "action": "CLOSE_TRACK",   "status": "COMPLETED",   "ticks_remaining": 0},
    {"id": "task_2", "action": "DISPATCH_CREW", "status": "COMPLETED",   "ticks_remaining": 0},
    {"id": "task_3", "action": "REPAIR_TRACK",  "status": "IN_PROGRESS", "ticks_remaining": 17,
     "progress": 0.15, "depends_on": ["task_1", "task_2"], "blocking_reason": null}
  ],
  "events": [{"tick": 10, "kind": "started", "task_id": "task_3", "detail": "T23 under repair"}]
}
```

## 9 · The demo, step by step

T23 fails in a storm. The operator registers the standard response.

| Tick | T23 | Order |
|---|---|---|
| 0 | `CLOSED`, health 0, `STORM` | `UNRESOLVED` — three `PENDING` tasks |
| 1 | `CLOSED` | close `COMPLETED`; dispatch `BLOCKED` — "Severe storm prevents crew access to T23" → order `BLOCKED` |
| 1–n | storm holds | nothing moves; the console shows why |
| retry after `CLEAR` | | dispatch `IN_PROGRESS`, crew `EN_ROUTE` |
| +10 | `UNDER_REPAIR`, health climbing | crew `ON_SITE`; repair `IN_PROGRESS` |
| +29 | `OPEN`, health 0.95 | all `COMPLETED` → `COMPLETE`; crew stood down; the direct Nagpur ↔ Raipur route is back |

The order reads `COMPLETE` only at the last row — when the simulated
railway, not the request, says the work happened.

## 10 · Tests

`tests/test_work_orders.py` (the twin layer: lifecycle, same-tick chaining,
dependencies, every blocking path, retry and auto-retry, unresolved
verification, each action, cancellation rollback, aggregation, payload
shape, RNG lockstep, the demo scenario) and
`tests/test_workorder_endpoints.py` (the Django endpoints through the test
client, including session isolation).

## 11 · Known limits

- Durations are demo-scaled tick counts, not engineering estimates. An
  order carries at most 100 tasks of at most 10 000 ticks each.
- Crews are unlimited: a dispatch always finds a gang. Resource pools are
  the natural next step.
- `UNRESOLVED` is terminal; re-doing failed work means a new order.
- The orchestrator orders repairs only for sections a plan *closes* as
  failed (`T_CLOSE_*`); weather-protocol closures of healthy sections get
  no repair order, since there is nothing to rebuild.
- The console never fabricates field progress offline: the panel reads
  empty until the agent service (and its twin) answers.
