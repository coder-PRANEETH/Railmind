# RailMind — Pitch

> **An autonomous multi-agent operating system for railway networks.**
> A living digital twin, a pipeline of specialist AI agents, counterfactual simulation, and a closed decision loop — on one screen.

---

## The problem

Railway incident response is manual, slow, and local. When a track fails, a human dispatcher must work out which trains are affected, what the response options are, and what each option costs the rest of the network — under time pressure, with cascading knock-on effects they can't see.

## What RailMind does

1. **A living digital twin** of a 21-station Indian railway network runs continuously — trains move along real corridors, congestion drifts, weather rolls in and clears. Closed tracks physically hold trains.
2. When something breaks (or a dispatcher injects a scenario), a **LangGraph pipeline of specialist agents** — Weather, Track, Signal, Routing — each analyze the snapshot and emit strategies, streamed live to the console as they think.
3. A **Planner** composes three response doctrines (Safety First / Balanced / Minimal Intervention); a **simulation engine** plays each one against the incident snapshot with a twin-informed cost model, including stranded-train penalties for plans that ignore the failure.
4. The **Master Agent** ranks plans with min-max normalized MCDM scoring (risk 0.40 / delay 0.35 / passengers 0.15 / congestion 0.10) and explains its choice in plain language — via an LLM when an API key is present, via a rule engine otherwise.
5. **One click executes the plan on the live twin** — tracks close, trains take their alternate corridors, and the next pipeline run confirms the network is stable. The loop is closed.
6. **An escalation ledger grades the response.** Every incident carries a handling level (L0 Steady State → L4 Emergency) that rises with the damage *or* with the clock, names the role that owns it at each rung, and reports one of four completion states — COMPLETE, PARTIAL, BLOCKED, UNRESOLVED — recomputed from the twin, never from the fact that a button was pressed.

## Screenshots

**Live operations** — trains (blue dots) moving on real geography, ambient weather, per-track health:

![Live operations](screenshot-live.png)

**Incident response** — T23 failure injected: held train flagged, reroutes drawn, three scored plans, explained recommendation, one-click execution:

![Incident response](screenshot-incident.png)

## What makes it interesting

- **Fully offline-first.** Every layer has a fallback: the agents embed their own twin if the twin API is down; the console ships a mock pipeline if the agents are down. The demo cannot fail because a network did.
- **Real algorithms, not theater.** Dijkstra reroutes on a NetworkX graph, MCDM plan ranking, BFS cascade of failures, deterministic track-health hashing. Every number on screen is computed.
- **Compound scenarios.** Failures and storms stack — inject three failures and watch the network strain, then reset in one click.
- **The loop is closed.** Most dashboards stop at "recommended action." RailMind executes it and the world visibly changes.
- **Done is proved, not claimed.** Every committed action is re-verified against the twin. An apply that silently changed nothing reads PENDING and its incident stays PARTIAL — and when no corridor remains, the state is BLOCKED, which outranks "in progress" so nobody waits on impossible work.
- **Urgency moves on its own.** An incident nobody handles escalates on a dwell timer, reassigns to the next role up, and de-escalates when the network genuinely recovers.

## Architecture

```
Django digital twin (:8000)  ←→  FastAPI + LangGraph agents (:8001)  ←→  React console (:8080)
        lazy-ticking                weather→track→signal→routing            live map, SSE timeline,
        living network              →planner→simulator→master               plan preview, analytics,
                                    →escalation ledger                      escalation tracker
```

One canonical dataset (`railmind/data/india_network.json`) is shared by all three layers, so station and track IDs line up end to end.

## Run it

```bash
docker compose up          # everything, one command
# or: see the Quickstart in the root README for the three-terminal dev setup
```

## Numbers

- 21 stations, 33 corridors, real geography
- 8 pipeline stages, 4 specialist agents, a 5-rung escalation ladder
- 123 pytest tests + 19 console tests + typecheck/lint/test/build CI
- 3 deploy paths: docker compose, Render blueprint, bare `npm run dev` + `uvicorn`
