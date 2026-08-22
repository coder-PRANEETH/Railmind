# Escalation and the completion signal

Two questions a railway dashboard usually leaves unanswered, and how RailMind answers them.

> **How urgent is this?** An incident nobody resolves must not sit at the same handling level forever.
>
> **Is the response actually done?** "Plan executed" is a claim about a button. It is not evidence that anything changed on the network.

Implementation: [`agents/escalation.py`](../agents/escalation.py) (ladder, ledger, classifier), [`agents/briefing.py`](../agents/briefing.py) (handoff brief), [`railmind-projec/src/components/railmind/EscalationPanel.tsx`](../railmind-projec/src/components/railmind/EscalationPanel.tsx) (rail and tracker).

---

## 1 · The ladder

Every open incident carries a handling level. The level names the posture *and* the role that owns it, so an escalation is a handoff, not a colour change.

| Level | Label | Owner | Dwell limit |
|---|---|---|---|
| `L0` | Steady State | Section Controller | — |
| `L1` | Advisory | Section Controller | 4 min |
| `L2` | Elevated | Divisional Controller | 3 min |
| `L3` | Critical | Divisional Emergency Cell | 2 min |
| `L4` | Emergency | Zonal Emergency Control Room | — |

An incident sits at the highest level any **driver** justifies. Drivers are named, so the ladder is auditable after the fact:

| Driver | Argues for | Fires when |
|---|---|---|
| `ASSET` / `WEATHER` | L1 | A corridor is out of service, or severe weather is on it |
| `TRAFFIC` | L2 | Trains are still routed into the failure |
| `STOPPED` | L3 | Trains have been brought to a stand |
| `PASSENGERS` | L3 | ≥500 passengers on trains that have not recovered |
| `NO_PATH` | L4 | A stranded train has no alternate corridor |
| `SEVERED` | L4 | Station pairs exist with no route between them |
| `BLOCKED` | L4 | Automated recovery is exhausted |
| `DWELL` | current + 1 | The level's dwell limit expired |

**Dwell escalation** is the movement from normal to urgent handling that needs no operator at all: an incident left at a level past its limit is raised one rung, with the elapsed time in the reason. Acknowledging restarts that clock — that is what an acknowledgement *is* — but it cannot hold a level the network itself has outgrown, because condition drivers are evaluated on the same pass. An escalation clears a stale acknowledgement: the previous owner accepted a smaller problem.

**De-escalation** is equally automatic. When the completion signal reads `COMPLETE` the incident drops to `L0`, and the drop is recorded like any other movement.

## 2 · The completion signal

Every incident reports exactly one of four states.

| State | Meaning | Precedence rule |
|---|---|---|
| `COMPLETE` | Every affected train recovered, every committed action verified on the twin, failure contained | Requires all three |
| `PARTIAL` | Some work verified, some outstanding | The default when work is genuinely under way |
| `BLOCKED` | No open corridor remains for the trains still stuck | **Outranks `PARTIAL`** — a dispatcher must not read "in progress" and wait for a plan that cannot exist |
| `UNRESOLVED` | No action verified and no train recovered | The honest state before anything lands |

`BLOCKED` outranking `PARTIAL` is the load-bearing decision here. Half-finished work on an unrecoverable incident is not progress; it is a dispatcher waiting for the wrong thing.

### Trains

Each affected train is graded against the live network, not against the plan that was supposed to help it:

- `RECOVERED` — moving, and its route avoids every blocked corridor
- `EXPOSED` — still routed across a blocked corridor, not yet stopped
- `HELD` — stopped at the failure, but a path still exists
- `BLOCKED` — no path exists from where it stands to where it is going

### Work items

Every action an approved plan commits to becomes a work item, re-verified against the twin on every observation:

| State | Proved by |
|---|---|
| `DONE` | The twin confirms it — track reads CLOSED, train is on the alternate corridor or clear of the failure, held train is at a stand |
| `PENDING` | Dispatched, not yet true on the twin |
| `FAILED` | The target no longer exists in the twin |
| `BLOCKED` | No corridor remains for the train the item was meant to move |

This is why the signal is trustworthy: an `/apply-plan` call that returns `200` but whose reroute never lands leaves its work item `PENDING`, and the incident stays `PARTIAL`. The console can only report work it can prove.

## 3 · The handoff brief

When an incident changes hands, a note travels with it. `POST /incidents/{id}/brief` writes four lines — `SITUATION`, `IMPACT`, `DONE`, `ASK` — addressed to the receiving role.

An LLM writes it when `ANTHROPIC_API_KEY` is set, under a prompt that is grounded hard: it may cite only facts present in the payload, must write "not reported" instead of estimating, may only reference actions that already exist as work items, and takes a per-state directive so a `BLOCKED` incident can never be softened into "in progress". A refusal, a timeout (8 s, no retries) or any exception falls back to a deterministic template with the same four sections and the same numbers. The response says which writer produced the text, and the console labels it.

## 4 · API

| Endpoint | Purpose |
|---|---|
| `GET /escalation` | Handling level, owner, completion signal, incidents, work items. No LLM call — cheap enough to poll |
| `POST /incidents/{id}/acknowledge?owner=…` | Take ownership at the current tier; restarts that tier's dwell clock |
| `POST /incidents/{id}/brief` | Draft the handoff brief |

The pipeline gained an eighth stage, `escalation`, which runs after the master agent and logs its verdict to the SSE timeline. `/apply-plan` registers the executed plan's actions as work items and re-grades against a fresh snapshot before responding. `/reset` clears the ledger with the twin — a stale `BLOCKED` incident must not hold a healthy network at L4.

## 5 · Offline behaviour

The ledger follows the same rule as the rest of the stack: it degrades, it does not disappear. With the backend unreachable the console grades the mock run locally, labels the rail `LEDGER: CONSOLE FALLBACK`, and still produces a brief from the deterministic writer. Acknowledging offline is recorded locally and marked as such.

## 6 · Tests

59 tests cover this feature: `tests/test_escalation.py` (34) pins tier movement, dwell escalation, acknowledgement semantics and all four completion states — including `BLOCKED` via a genuinely partitioned network and the silently-failed apply that must not read `COMPLETE`. `tests/test_briefing.py` (16) pins the per-state wording and the LLM fallback paths, and asserts the prompt's grounding clauses still exist. `tests/test_endpoints.py` adds 9 HTTP-contract tests. Console coverage lives in `railmind-api.test.ts`.

## 7 · Known limits

- Tier thresholds and dwell limits are demo-scaled constants, not railway regulation values.
- The ledger is in-memory and single-process, like the twin it grades.
- Acknowledgement records a role string; there is no authentication behind it.
- Incidents open for blocking corridors and severe weather only — degraded-but-open track raises no incident of its own.
