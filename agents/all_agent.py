"""
all_agent.py  —  RailMind agent orchestrator (FastAPI + LangGraph)
────────────────────────────────────────────────────────────────
Flow:
  Digital Twin snapshot
    → WeatherAgent   (risk scores per track)
    → TrackAgent     (strategies: close / restrict / monitor)
    → SignalAgent    (strategies: RED / YELLOW / GREEN per section)
    → RoutingAgent   (strategies: reroute options per affected train)
    → PlannerAgent   (combines strategies → Plan A / Plan B / Plan C)
    → SimulationNode (clones twin, applies each plan, scores locally)
    → MasterAgent    (MCDM normalised ranking → best plan)

Twin sources (first reachable wins):
  1. TWIN_BASE_URL env var
  2. Local Django twin  (http://127.0.0.1:8000)
  3. Hosted twin        (https://railmind-backend-4pep.onrender.com)
  4. Embedded twin      (railmind package, fully offline)

Agent contract (every specialist):
  Input  : RailState (read-only)
  Output : strategies: [{ strategy_id, ... }] appended to state

Master contract:
  Input  : simulation_results[]
  Output : { selected_plan, score, ranking } via min-max normalised MCDM
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, TypedDict

import networkx as nx
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.graph import END, StateGraph

try:
    import anthropic
except ImportError:
    anthropic = None

# Make the repo root importable so the embedded twin fallback works offline,
# and this directory importable so sibling modules resolve however uvicorn was
# launched (from agents/ or from the repo root).
REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = Path(__file__).resolve().parent
for _path in (REPO_ROOT, AGENTS_DIR):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))

import escalation as esc                      # noqa: E402  (needs sys.path above)
from briefing import handoff_brief            # noqa: E402

app = FastAPI(
    title="RailMind Agents",
    description="Multi-agent decision pipeline for railway operations",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TWIN_CANDIDATES = [
    os.environ.get("TWIN_BASE_URL"),
    "http://127.0.0.1:8000",
    "https://railmind-backend-4pep.onrender.com",
]

PLAN_NAMES = {
    "A": "Plan A — Safety First",
    "B": "Plan B — Balanced Response",
    "C": "Plan C — Minimal Intervention",
}

WEATHER_CONDITION_RISK = {"CLEAR": 5.0, "RAIN": 55.0, "FOG": 65.0, "STORM": 85.0}

MCDM_WEIGHTS = {
    "delay":            0.35,
    "risk":             0.40,
    "passenger_impact": 0.15,
    "congestion":       0.10,
}

# ─────────────────────────────────────────────────────────────────────────────
# ESCALATION LEDGER
# One ledger per process, guarded by its own lock: it is read by the console's
# polling loop and written by the pipeline and by /apply-plan, all of which
# FastAPI may run concurrently on its threadpool. The lock is never held across
# network I/O — snapshots are fetched first, then folded in.
# ─────────────────────────────────────────────────────────────────────────────

_LEDGER = esc.IncidentLedger()
_ledger_lock = threading.RLock()

# The role a plan executed from the console is attributed to. Real deployments
# would take this from the authenticated operator; the demo has one desk.
CONSOLE_OPERATOR = "Console Operator"


def _observe(snapshot: dict) -> dict:
    with _ledger_lock:
        return _LEDGER.observe(snapshot)


def _find_incident(incident_id: str) -> dict:
    """Return one incident's payload, 404ing if the ledger has never seen it."""
    with _ledger_lock:
        payload = _LEDGER.incident_payload(incident_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found.")
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# TWIN ACCESS
# The orchestrator talks to the first reachable twin API; when none is
# reachable it falls back to an embedded in-process twin, so every endpoint
# (including injections, apply-plan and reset) works fully offline.
# ─────────────────────────────────────────────────────────────────────────────

_embedded_twin = None
# Reentrant: serializes ALL embedded-twin access (construction, tick+dump,
# mutation) — FastAPI's threadpool runs sync endpoints truly concurrently.
_embedded_lock = threading.RLock()

# Cache of the reachable remote base (None = use embedded), refreshed
# periodically. "snapshot" holds the probe's parsed body so fetch_snapshot()
# can consume it once instead of re-GETting the same URL.
_twin_base_cache: dict = {"base": None, "snapshot": None, "checked_at": 0.0}
_TWIN_BASE_TTL = 20.0


def _get_embedded_twin():
    global _embedded_twin
    from railmind.data_loader import load_railway
    from railmind.graph import build_network_from_data
    from railmind.twin import DigitalTwin

    with _embedded_lock:
        if _embedded_twin is None:
            stations, tracks = load_railway("India")
            graph = build_network_from_data(stations, tracks)
            twin = DigitalTwin(graph)
            twin.seed_trains(5)
            _embedded_twin = twin
        return _embedded_twin


def _reset_embedded_twin():
    global _embedded_twin
    with _embedded_lock:
        _embedded_twin = None


def _embedded_snapshot() -> dict:
    """Snapshot from the in-process twin — no network needed. Ticks the twin
    so trains move between reads even with no Django server running."""
    with _embedded_lock:
        twin = _get_embedded_twin()
        twin.maybe_tick()
        dump = twin.get_state()
        dump["graph"] = {
            "nodes": list(twin.graph.graph.nodes()),
            "edges": list(twin.graph.graph.edges()),
        }
    return dump


def _twin_base() -> str | None:
    """Return the first reachable remote twin base URL, or None for embedded."""
    now = time.time()
    if now - _twin_base_cache["checked_at"] < _TWIN_BASE_TTL:
        return _twin_base_cache["base"]

    base_found = None
    probe_snapshot = None
    for base in TWIN_CANDIDATES:
        if not base:
            continue
        try:
            # Keep the probe well under the console's /state fetch timeout so a
            # dead candidate can't make live polling fail over to the mock fleet
            resp = requests.get(f"{base}/api/state/", timeout=1.5)
            resp.raise_for_status()
            body = resp.json()
            # A foreign server squatting a candidate port can 200 here; only a
            # real twin snapshot (trains + tracks) qualifies as a base.
            if not isinstance(body, dict) or "trains" not in body or "tracks" not in body:
                continue
            probe_snapshot = body
            base_found = base
            break
        except (requests.RequestException, ValueError):
            continue

    _twin_base_cache["base"] = base_found
    _twin_base_cache["snapshot"] = probe_snapshot
    _twin_base_cache["checked_at"] = now
    return base_found


def _drop_probe_snapshot() -> None:
    # A probe body captured before a mutation must not be served after it.
    _twin_base_cache["snapshot"] = None


def fetch_snapshot() -> tuple[dict, str]:
    """Return (snapshot, source_label) from the live twin (remote or embedded)."""
    base = _twin_base()
    if base:
        # A fresh probe already fetched this state — consume it once instead of
        # re-GETting the same URL (probe + fetch would exceed the console's
        # 5s /state abort and flip the UI to mock).
        snapshot = _twin_base_cache["snapshot"]
        if snapshot is not None:
            _twin_base_cache["snapshot"] = None
            return snapshot, base
        try:
            resp = requests.get(f"{base}/api/state/", timeout=4)
            resp.raise_for_status()
            return resp.json(), base
        except requests.RequestException:
            _twin_base_cache["base"] = None
    return _embedded_snapshot(), "embedded"


def _twin_close_track(track_id: str) -> None:
    base = _twin_base()
    if base:
        requests.post(f"{base}/api/track/close/", json={"track_id": track_id}, timeout=4).raise_for_status()
        _drop_probe_snapshot()
    else:
        with _embedded_lock:
            _get_embedded_twin().close_track(track_id)


def _twin_set_weather(track_id: str, condition: str) -> None:
    base = _twin_base()
    if base:
        requests.post(f"{base}/api/weather/set/", json={"track_id": track_id, "condition": condition}, timeout=4).raise_for_status()
        _drop_probe_snapshot()
    else:
        with _embedded_lock:
            _get_embedded_twin().set_weather(track_id, condition)


def _twin_reroute_train(train_id: str, route: List[str]) -> None:
    base = _twin_base()
    if base:
        requests.post(f"{base}/api/train/reroute/", json={"train_id": train_id, "route": route}, timeout=4).raise_for_status()
        _drop_probe_snapshot()
    else:
        with _embedded_lock:
            _get_embedded_twin().reroute_train(train_id, route)


def _twin_reset() -> None:
    # Always clear the embedded twin too — injections made while remotes were
    # unreachable must not resurface after a later fallback flip.
    _reset_embedded_twin()
    base = _twin_base()
    if base:
        requests.post(f"{base}/api/reset/", timeout=6).raise_for_status()
        _drop_probe_snapshot()


# ─────────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────────

class RailState(TypedDict):
    snapshot:               dict            # /api/state/ response (or embedded)
    graph:                  nx.Graph        # station network

    # ── specialist agent outputs (read-only after each agent) ──
    weather_risk:           Dict[str, float]        # track_id → 0-100
    weather_strategies:     List[dict]
    track_strategies:       List[dict]
    signal_strategies:      List[dict]
    routing_strategies:     List[dict]

    # ── orchestration ──
    plans:                  List[dict]              # Planner output
    simulation_results:     List[dict]              # Simulation output
    best_plan:              dict                    # Master output
    escalation:             dict                    # Escalation output

    log:                    List[dict]              # {t, source, message}


def _log(state: RailState, source: str, message: str) -> None:
    state["log"].append({"t": time.strftime("%H:%M:%S"), "source": source, "message": message})


def _strategy(sid: str, name: str, confidence: float, actions: List[str]) -> dict:
    """Standard strategy object used by every specialist agent."""
    return {
        "strategy_id": sid,
        "strategy_name": name,
        "confidence": confidence,
        "actions": actions,
    }


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 1 — WEATHER
# Reads  : snapshot.weather  (dict track_id→condition, or list of sensor dicts)
# Outputs: weather_risk, weather_strategies[]
# Rule   : weighted formula — no ML, no API
# ─────────────────────────────────────────────────────────────────────────────

def weather_node(state: RailState) -> RailState:
    weather = state["snapshot"].get("weather", {})
    risk_map: Dict[str, float] = {}

    if isinstance(weather, dict):
        # Twin format: {track_id: "STORM" | "RAIN" | "FOG" | "CLEAR"}
        for track_id, condition in weather.items():
            cond = condition.value if hasattr(condition, "value") else str(condition)
            risk_map[track_id] = WEATHER_CONDITION_RISK.get(cond.upper(), 0.0)
    else:
        # Sensor format: [{track_id, rainfall, temperature}, ...]
        for w in weather:
            track_id    = w["track_id"]
            rainfall    = w.get("rainfall", 0)
            temperature = w.get("temperature", 25)
            risk = 100 * (
                0.7 * min(rainfall / 50, 1.0) +
                0.3 * max(min((temperature - 25) / 25, 1.0), 0.0)
            )
            risk_map[track_id] = round(risk, 1)

    state["weather_risk"] = risk_map

    high_tracks   = [t for t, r in risk_map.items() if r > 70]
    medium_tracks = [t for t, r in risk_map.items() if 40 < r <= 70]

    strategies = []

    if high_tracks:
        strategies.append(_strategy(
            "W1", "emergency_weather_protocol",
            confidence=0.92,
            actions=[f"close_track_{t}+reroute_all_trains" for t in high_tracks],
        ))
        strategies.append(_strategy(
            "W2", "speed_restriction_high_risk_tracks",
            confidence=0.75,
            actions=[f"reduce_speed_40kmh_{t}+intensive_monitoring" for t in high_tracks],
        ))

    if medium_tracks:
        strategies.append(_strategy(
            "W3", "caution_medium_risk_tracks",
            confidence=0.60,
            actions=[f"reduce_speed_60kmh_{t}+30min_sensor_poll" for t in medium_tracks],
        ))

    if high_tracks or medium_tracks:
        strategies.append(_strategy(
            "W4", "monitor_and_reassess",
            confidence=0.40,
            actions=["continuous_weather_monitoring+alert_crew"],
        ))
    else:
        strategies = [_strategy(
            "W5", "nominal_weather_conditions",
            confidence=0.99,
            actions=["no_weather_action_required"],
        )]

    state["weather_strategies"] = strategies

    high_str = ", ".join(f"{t} ({r:.0f}%)" for t, r in risk_map.items() if r > 60)
    _log(state, "Weather",
         f"max risk {max(risk_map.values(), default=0):.0f}% | high-risk: {high_str or 'none'} | "
         f"strategy {strategies[0]['strategy_id']}")
    return state


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 2 — TRACK
# Reads  : snapshot.tracks[], weather_risk
# Outputs: track_strategies[]
# Rule   : health thresholds → close / restrict / monitor
# ─────────────────────────────────────────────────────────────────────────────

def track_node(state: RailState) -> RailState:
    tracks = state["snapshot"].get("tracks", {})
    strategies = []

    for track_id, track in tracks.items():
        health = track.get("health", 1.0)

        if health <= 0.2 or track.get("status") == "CLOSED":
            strategies.append({
                "strategy_id": f"T_CLOSE_{track_id}",
                "track_id": track_id,
                "action": "close_track",
            })
        elif health <= 0.5:
            strategies.append({
                "strategy_id": f"T_RESTRICT_{track_id}",
                "track_id": track_id,
                "action": "speed_limit_60",
            })
        elif health <= 0.7:
            strategies.append({
                "strategy_id": f"T_MONITOR_{track_id}",
                "track_id": track_id,
                "action": "monitor",
            })

    if not strategies:
        strategies.append({"strategy_id": "T_OK", "action": "healthy"})

    state["track_strategies"] = strategies
    _log(state, "Track",
         f"{sum(1 for s in strategies if 'CLOSE' in s['strategy_id'])} closures, "
         f"{sum(1 for s in strategies if 'RESTRICT' in s['strategy_id'])} restrictions, "
         f"{sum(1 for s in strategies if 'MONITOR' in s['strategy_id'])} monitored")
    return state


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 3 — SIGNAL
# Reads  : snapshot.tracks[], weather_risk
# Outputs: signal_strategies[]
# Rule   : rule table — risk level → signal aspect (IRGSR-style)
# ─────────────────────────────────────────────────────────────────────────────

def signal_node(state: RailState) -> RailState:
    tracks       = state["snapshot"].get("tracks", {})
    weather_risk = state["weather_risk"]
    strategies   = []
    max_signal_risk = 0.0

    for track_id, track in tracks.items():
        health      = track.get("health", 1.0)
        w_risk_norm = weather_risk.get(track_id, 0) / 100.0
        risk_score  = round(0.6 * (1 - health) + 0.4 * w_risk_norm, 3)
        max_signal_risk = max(max_signal_risk, risk_score)
        sid_base    = track_id.replace(" ", "_")

        if risk_score > 0.65:
            strategies.append(_strategy(
                f"S_RED_{sid_base}",
                f"set_{track_id}_RED+halt_all_trains+dispatch_signal_tech",
                confidence=round(risk_score, 2),
                actions=[
                    f"signal_{track_id}_RED",
                    f"halt_trains_approaching_{track_id}",
                    f"dispatch_signal_tech_{track_id}_ETA_20min",
                ],
            ))
        elif risk_score > 0.35:
            strategies.append(_strategy(
                f"S_YELLOW_{sid_base}",
                f"set_{track_id}_YELLOW+speed_30kmh+caution_adjacent",
                confidence=round(risk_score, 2),
                actions=[
                    f"signal_{track_id}_YELLOW",
                    f"speed_limit_30kmh_{track_id}",
                    f"caution_signal_adjacent_sections_{track_id}",
                ],
            ))
        else:
            strategies.append(_strategy(
                f"S_GREEN_{sid_base}",
                f"keep_{track_id}_GREEN+routine_check",
                confidence=0.95,
                actions=[f"signal_{track_id}_GREEN"],
            ))

    if not strategies:
        strategies.append(_strategy(
            "S_OK", "all_signals_nominal",
            confidence=0.99,
            actions=["routine_signal_monitoring"],
        ))

    state["signal_strategies"] = strategies
    reds    = [s["strategy_id"] for s in strategies if s["strategy_id"].startswith("S_RED")]
    yellows = [s["strategy_id"] for s in strategies if s["strategy_id"].startswith("S_YELLOW")]
    _log(state, "Signal",
         f"max section risk {max_signal_risk:.2f} | {len(reds)} RED, {len(yellows)} YELLOW")
    return state


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 4 — ROUTING
# Reads  : track_strategies, graph, snapshot.trains
# Outputs: routing_strategies[]
# Algorithm: Dijkstra on the network minus closed edges
# ─────────────────────────────────────────────────────────────────────────────

def routing_node(state: RailState) -> RailState:
    G = state["graph"].copy()
    trains = state["snapshot"].get("trains", {})
    tracks = state["snapshot"].get("tracks", {})

    closed_tracks = [
        s["track_id"] for s in state["track_strategies"]
        if s.get("action") == "close_track"
    ]
    # W1's emergency closures promise "reroute_all_trains" — honor it: treat
    # high-weather-risk tracks as closed so affected trains get reroutes
    # instead of being silently stranded by the weather protocol.
    closed_tracks += [
        t for t, r in state.get("weather_risk", {}).items()
        if r > 70 and t not in closed_tracks
    ]

    closed_edges = set()
    for track_id in closed_tracks:
        track = tracks.get(track_id, {})
        src, dst = track.get("source"), track.get("destination")
        if src and dst:
            closed_edges.add(frozenset({src, dst}))
            if G.has_edge(src, dst):
                G.remove_edge(src, dst)

    routing_strategies = []

    for train_id, train in trains.items():
        route = train.get("route", [])
        affected = any(
            frozenset({route[i], route[i + 1]}) in closed_edges
            for i in range(len(route) - 1)
        )
        if not affected:
            continue

        origin = train.get("current_station") or (route[0] if route else None)
        if origin not in G:
            origin = route[0] if route else None

        if origin == route[-1]:
            # Already at the final stop — nothing to reroute; the return
            # service gets planned on a later run once the train turns around.
            continue

        try:
            new_route = nx.shortest_path(G, source=origin, target=route[-1])
            if len(new_route) < 2:
                continue
            routing_strategies.append({
                "strategy_id": f"R_REROUTE_{train_id}",
                "train_id": train_id,
                "old_route": route,
                "new_route": new_route,
            })
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            routing_strategies.append({
                "strategy_id": f"R_HOLD_{train_id}",
                "train_id": train_id,
                "old_route": route,
            })

    if not routing_strategies:
        routing_strategies.append({"strategy_id": "R_NOMINAL"})

    state["routing_strategies"] = routing_strategies
    rerouted = [s["train_id"] for s in routing_strategies if s["strategy_id"].startswith("R_REROUTE")]
    held     = [s["train_id"] for s in routing_strategies if s["strategy_id"].startswith("R_HOLD")]
    _log(state, "Routing",
         f"{len(rerouted)} rerouted {rerouted or ''} | {len(held)} held {held or ''}"
         if rerouted or held else "all routes nominal")
    return state


# ─────────────────────────────────────────────────────────────────────────────
# PLANNER AGENT
# Reads  : all specialist strategies
# Outputs: 3 candidate plans
#   Plan A — Safety First        (closures + RED signals + reroutes)
#   Plan B — Balanced Response   (restrictions + YELLOW signals + reroutes)
#   Plan C — Minimal Intervention(monitoring + GREEN signals, no reroutes)
# ─────────────────────────────────────────────────────────────────────────────

def planner_node(state: RailState) -> RailState:
    weather = state["weather_strategies"]
    track   = state["track_strategies"]
    signal  = state["signal_strategies"]
    routing = state["routing_strategies"]

    # Weather strategies follow each plan's doctrine — bundling all of them
    # into every plan would let an approved "Minimal Intervention" still close
    # the stormy track, and identical W deltas across A/B/C would vanish in
    # min-max normalisation so weather could never re-rank the plans.
    #   A — aggressive: W1 emergency closures + W2 hard speed restrictions
    #   B — restrictions only: W2 (high-risk) / W3 (medium-risk) speed limits
    #   C — monitoring only: W4 reassess / W5 nominal
    # W3 belongs in A too: medium-risk weather (RAIN/FOG) emits only W3/W4,
    # and "Safety First" must degrade to the strongest available mitigation
    # rather than an empty weather set.
    weather_by_plan = {
        "A": [w for w in weather if w["strategy_id"] in ("W1", "W2", "W3")],
        "B": [w for w in weather if w["strategy_id"] in ("W2", "W3")],
        "C": [w for w in weather if w["strategy_id"] in ("W4", "W5")],
    }

    plans = [
        {
            "plan_id": "A",
            "plan_name": PLAN_NAMES["A"],
            "actions":
                weather_by_plan["A"] +
                [x for x in track if "CLOSE" in x["strategy_id"]] +
                [x for x in signal if "RED" in x["strategy_id"]] +
                routing,
        },
        {
            "plan_id": "B",
            "plan_name": PLAN_NAMES["B"],
            "actions":
                weather_by_plan["B"] +
                [x for x in track if "RESTRICT" in x["strategy_id"] or "MONITOR" in x["strategy_id"]] +
                [x for x in signal if "YELLOW" in x["strategy_id"]] +
                routing,
        },
        {
            "plan_id": "C",
            "plan_name": PLAN_NAMES["C"],
            "actions":
                weather_by_plan["C"] +
                [x for x in track if "MONITOR" in x["strategy_id"] or x["strategy_id"] == "T_OK"] +
                [x for x in signal if "GREEN" in x["strategy_id"]] +
                [{"strategy_id": "R_NOMINAL"}],
        },
    ]

    state["plans"] = plans
    _log(state, "Planner", f"generated {len(plans)} candidate plans (A safety-first, B balanced, C minimal)")
    return state


# ─────────────────────────────────────────────────────────────────────────────
# SIMULATION ENGINE  (not an agent — infrastructure)
# Reads  : plans[], twin snapshot
# Process: apply each plan's actions to a scoring model of the twin
# Outputs: { plan_id, delay, risk, passenger_impact, congestion }
# ─────────────────────────────────────────────────────────────────────────────

def _simulate_plan(plan: dict, snapshot: dict) -> dict:
    delay = 0
    risk = 0.25
    passenger_impact = 0
    congestion = 0.0

    trains = snapshot.get("trains", {})
    tracks = snapshot.get("tracks", {})

    # Baseline risk: unhealthy open tracks raise risk before mitigation
    for track in tracks.values():
        health = track.get("health", 1.0)
        if health <= 0.5:
            risk += 0.15
        elif health <= 0.7:
            risk += 0.05

    # Edges of failed tracks (broken or closed) and which trains cross them
    failed_edges = {
        frozenset({t["source"], t["destination"]})
        for t in tracks.values()
        if t.get("health", 1.0) <= 0.2 or t.get("status") == "CLOSED"
    }

    def _crosses(route: List[str], edges) -> bool:
        return any(
            frozenset({route[i], route[i + 1]}) in edges
            for i in range(len(route) - 1)
        )

    handled_trains = {
        a.get("train_id") for a in plan["actions"]
        if isinstance(a, dict) and a.get("strategy_id", "").startswith(("R_REROUTE_", "R_HOLD_"))
    }

    # Tracks this plan's own weather protocol closes (filled in the W branch)
    w_closed_edges: set = set()

    for action in plan["actions"]:
        if not isinstance(action, dict):
            continue

        strategy = action.get("strategy_id", "")

        if strategy.startswith("T_CLOSE_"):
            delay += 30
            risk -= 0.20
            congestion += 0.20

        elif strategy.startswith("T_RESTRICT_"):
            delay += 10
            risk -= 0.10
            congestion += 0.05

        elif strategy.startswith("T_MONITOR_"):
            delay += 5
            risk -= 0.05

        elif strategy.startswith("S_RED_"):
            delay += 15
            risk -= 0.10

        elif strategy.startswith("S_YELLOW_"):
            delay += 5
            risk -= 0.05

        elif strategy.startswith("R_REROUTE_"):
            train_id = action.get("train_id")
            delay += 15
            congestion += 0.10
            if train_id in trains:
                passenger_impact += trains[train_id].get("passengers", 0)

        elif strategy.startswith("R_HOLD_"):
            train_id = action.get("train_id")
            delay += 45
            congestion += 0.30
            if train_id in trains:
                passenger_impact += trains[train_id].get("passengers", 0)

        elif strategy.startswith("W"):
            # Weather protocols: closing the stormy track trades delay and
            # congestion for weather risk; speed limits trade less of each.
            for act in action.get("actions", []):
                if act.startswith("close_track_"):
                    delay += 20
                    risk -= 0.15
                    congestion += 0.10
                    # A weather closure blocks the track for THIS plan just
                    # like a health failure would — trains it strands must
                    # be priced below, not ignored.
                    tid = act[len("close_track_"):].split("+")[0]
                    closed = tracks.get(tid)
                    if closed:
                        w_closed_edges.add(
                            frozenset({closed["source"], closed["destination"]})
                        )
                elif act.startswith("reduce_speed_40kmh_"):
                    delay += 10
                    risk -= 0.08
                elif act.startswith("reduce_speed_60kmh_"):
                    delay += 5
                    risk -= 0.04

    # Trains left running toward a failed (or plan-closed) track with no
    # reroute/hold in this plan are stranded: heavy delay, full passenger
    # impact, elevated risk.
    blocked_edges = failed_edges | w_closed_edges
    if blocked_edges:
        for train_id, train in trains.items():
            if train_id in handled_trains:
                continue
            if _crosses(train.get("route", []), blocked_edges):
                delay += 60
                risk += 0.25
                congestion += 0.15
                passenger_impact += train.get("passengers", 0)

    # Weather a plan leaves unmitigated is a cost, not a free pass: running
    # trains through a storm must weigh on the plan that only monitors.
    w_actions = [
        act
        for a in plan["actions"]
        if isinstance(a, dict) and a.get("strategy_id", "").startswith("W")
        for act in a.get("actions", [])
    ]
    for track_id, condition in snapshot.get("weather", {}).items():
        cond_risk = WEATHER_CONDITION_RISK.get(str(condition), 0.0)
        mitigated = any(track_id in act for act in w_actions)
        if mitigated:
            continue
        if cond_risk > 70:
            risk += 0.20
        elif cond_risk > 40:
            risk += 0.08
        else:
            continue
        # Trains driven through unmitigated bad weather are late and exposed
        # — without this, the do-nothing plan rides its zero delay to the
        # top of the MCDM ranking during a storm.
        track = tracks.get(track_id, {})
        edge = frozenset({track.get("source"), track.get("destination")})
        for train_id, train in trains.items():
            if train_id in handled_trains:
                continue
            if _crosses(train.get("route", []), {edge}):
                delay += 25 if cond_risk > 70 else 10
                passenger_impact += train.get("passengers", 0)

    risk = max(0.0, min(1.0, risk))
    congestion = round(min(congestion, 1.0), 2)

    return {
        "plan_id": plan["plan_id"],
        "plan_name": plan["plan_name"],
        "delay": delay,
        "risk": round(risk, 2),
        "passenger_impact": passenger_impact,
        "congestion": congestion,
    }


def simulation_node(state: RailState) -> RailState:
    results = []
    for plan in state["plans"]:
        result = _simulate_plan(plan, state["snapshot"])
        results.append(result)
        _log(state, "Simulator",
             f"{result['plan_name']}: delay {result['delay']}min, risk {result['risk']:.2f}, "
             f"{result['passenger_impact']} passengers, congestion {result['congestion']:.2f}")
    state["simulation_results"] = results
    return state


# ─────────────────────────────────────────────────────────────────────────────
# MASTER AGENT
# Reads  : simulation_results[]
# Algorithm: min-max normalisation + MCDM weighted scoring
# Weights: delay×0.35, risk×0.40, passengers×0.15, congestion×0.10
# Outputs: best_plan (with 0-100 suitability score; higher = better)
# ─────────────────────────────────────────────────────────────────────────────

def _minmax(values: List[float]) -> List[float]:
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]

def master_node(state: RailState) -> RailState:
    results = state["simulation_results"]

    norm = {
        "delay":            _minmax([r["delay"] for r in results]),
        "risk":             _minmax([r["risk"] for r in results]),
        "passenger_impact": _minmax([r["passenger_impact"] for r in results]),
        "congestion":       _minmax([r["congestion"] for r in results]),
    }

    for i, result in enumerate(results):
        cost = sum(MCDM_WEIGHTS[k] * norm[k][i] for k in MCDM_WEIGHTS)
        # Suitability score: 0-100, higher is better
        result["score"] = round((1.0 - cost) * 100, 1)

    ranked = sorted(results, key=lambda r: r["score"], reverse=True)
    best_plan = ranked[0]
    state["best_plan"] = best_plan

    _log(state, "Master",
         f"selected {best_plan['plan_name']} (score {best_plan['score']}/100) | "
         f"ranking: {' > '.join(p['plan_id'] for p in ranked)}")
    return state


# ─────────────────────────────────────────────────────────────────────────────
# ESCALATION AGENT
# Reads  : snapshot (via the ledger), plus whatever the run already decided
# Outputs: escalation — tier, owner, and a COMPLETE/PARTIAL/BLOCKED/UNRESOLVED
#          completion signal per open incident
# Runs last on purpose: it grades the state of the world the other agents just
# reasoned about, including work committed by earlier runs of this pipeline.
# ─────────────────────────────────────────────────────────────────────────────

def escalation_node(state: RailState) -> RailState:
    payload = _observe(state["snapshot"])
    state["escalation"] = payload

    totals = payload["totals"]
    if not totals["open"]:
        _log(state, "Escalation", "no open incidents — handling level L0 Steady State")
        return state

    worst = max(
        payload["incidents"],
        key=lambda i: (esc.RESOLUTION_SEVERITY.get(i["resolution"], 0), i["level"]),
        default=None,
    )
    _log(state, "Escalation",
         f"{payload['code']} {payload['label']} · owner {payload['owner']} | "
         f"{totals['open']} open incident(s) | completion {payload['resolution']}"
         + (f" — {worst['id']} {worst['resolution_reason']}" if worst else ""))
    return state


# ─────────────────────────────────────────────────────────────────────────────
# LANGGRAPH PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def build_nx_graph(state_snapshot: dict) -> nx.Graph:
    G = nx.Graph()
    G.add_nodes_from(state_snapshot.get("graph", {}).get("nodes", []))
    G.add_edges_from(state_snapshot.get("graph", {}).get("edges", []))
    return G


_builder = StateGraph(RailState)

for _name, _fn in [
    ("weather",    weather_node),
    ("track",      track_node),
    ("signal",     signal_node),
    ("routing",    routing_node),
    ("planner",    planner_node),
    ("simulation", simulation_node),
    ("master",     master_node),
    ("escalation", escalation_node),
]:
    _builder.add_node(_name, _fn)

_builder.set_entry_point("weather")

for _a, _b in [
    ("weather",    "track"),
    ("track",      "signal"),    # signal reads track health + weather risk
    ("signal",     "routing"),
    ("routing",    "planner"),
    ("planner",    "simulation"),
    ("simulation", "master"),
    ("master",     "escalation"),
    ("escalation", END),
]:
    _builder.add_edge(_a, _b)

pipeline = _builder.compile()


# ─────────────────────────────────────────────────────────────────────────────
# INITIAL STATE FACTORY
# ─────────────────────────────────────────────────────────────────────────────

def _initial_state(snapshot: dict, extra_log: List[dict] | None = None) -> RailState:
    return RailState(
        snapshot=snapshot,
        graph=build_nx_graph(snapshot),
        weather_risk={},
        weather_strategies=[],
        track_strategies=[],
        signal_strategies=[],
        routing_strategies=[],
        plans=[],
        simulation_results=[],
        best_plan={},
        escalation={},
        log=extra_log or [],
    )


# ─────────────────────────────────────────────────────────────────────────────
# RESPONSE FORMATTING  (matches the operator console contract)
# ─────────────────────────────────────────────────────────────────────────────

def _plan_payload(result: dict, plan: dict) -> dict:
    action_labels = []
    for a in plan["actions"]:
        sid = a.get("strategy_id", "")
        if sid.startswith("T_CLOSE_"):
            action_labels.append(f"Close track {a.get('track_id', sid.removeprefix('T_CLOSE_'))}")
        elif sid.startswith("T_RESTRICT_"):
            action_labels.append(f"Speed-limit 60 km/h on {a.get('track_id')}")
        elif sid.startswith("T_MONITOR_"):
            action_labels.append(f"Monitor {a.get('track_id')}")
        elif sid.startswith("S_RED_"):
            action_labels.append(f"Signal RED on {sid.removeprefix('S_RED_')}")
        elif sid.startswith("S_YELLOW_"):
            action_labels.append(f"Signal YELLOW on {sid.removeprefix('S_YELLOW_')}")
        elif sid.startswith("R_REROUTE_"):
            action_labels.append(f"Reroute {a.get('train_id')}")
        elif sid.startswith("R_HOLD_"):
            action_labels.append(f"Hold {a.get('train_id')} at station")
        elif sid.startswith("W") and a.get("strategy_name") not in (None, "nominal_weather_conditions"):
            action_labels.append(f"Weather protocol {sid}")
    if not action_labels:
        action_labels = ["Maintain nominal operations"]

    # Per-plan map data so the console can preview any candidate plan
    plan_failed = sorted({
        a["track_id"] for a in plan["actions"]
        if isinstance(a, dict) and a.get("strategy_id", "").startswith("T_CLOSE_")
    })
    plan_rerouted = [
        {"id": a["train_id"], "newRoute": a["new_route"]}
        for a in plan["actions"]
        if isinstance(a, dict) and a.get("strategy_id", "").startswith("R_REROUTE_")
    ]
    plan_held = [
        a["train_id"] for a in plan["actions"]
        if isinstance(a, dict) and a.get("strategy_id", "").startswith("R_HOLD_")
    ]

    return {
        "id": result["plan_id"],
        "name": result["plan_name"],
        "delay_min": result["delay"],
        "risk": result["risk"],
        "passengers_impacted": result["passenger_impact"],
        "congestion": result["congestion"],
        "score": result["score"],
        "actions": action_labels,
        "failed_tracks": plan_failed,
        "rerouted_trains": plan_rerouted,
        "held_trains": plan_held,
    }


def _template_explanation(final: RailState) -> str:
    """Deterministic explanation of the Master Agent's choice — no LLM needed."""
    results = sorted(final["simulation_results"], key=lambda r: r["score"], reverse=True)
    best, worst = results[0], results[-1]
    reroutes = sum(
        1 for s in final["routing_strategies"]
        if s["strategy_id"].startswith("R_REROUTE_")
    )
    parts = [
        f"{best['plan_name']} scored {best['score']}/100 — the best balance of "
        f"delay ({best['delay']} min), residual risk ({best['risk']:.2f}), "
        f"passenger impact ({best['passenger_impact']}) and congestion ({best['congestion']:.2f})."
    ]
    if reroutes:
        parts.append(f"It keeps {reroutes} affected train(s) moving via alternate corridors.")
    if worst["plan_id"] != best["plan_id"]:
        parts.append(
            f"{worst['plan_name']} was rejected (score {worst['score']}/100): "
            f"{worst['delay']} min of delay and risk {worst['risk']:.2f} make it the costliest option."
        )
    return " ".join(parts)


def _llm_explanation(final: RailState) -> str | None:
    """Ask the language model for a dispatcher-style justification. Returns None when the
    API key is absent or the call fails — callers fall back to the template."""
    if anthropic is None or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        # Hard budget: a slow LLM call must fall back to the template well
        # inside the console's request windows.
        client = anthropic.Anthropic(timeout=6.0, max_retries=0)
        summary = {
            "candidate_plans": [
                {k: r[k] for k in ("plan_id", "plan_name", "delay", "risk", "passenger_impact", "congestion", "score")}
                for r in final["simulation_results"]
            ],
            "selected": final["best_plan"]["plan_id"],
            "reroutes": [
                {"train": s["train_id"], "new_route": s["new_route"]}
                for s in final["routing_strategies"]
                if s["strategy_id"].startswith("R_REROUTE_")
            ],
            "closed_tracks": [
                s["track_id"] for s in final["track_strategies"]
                if s["strategy_id"].startswith("T_CLOSE_")
            ],
        }
        response = client.messages.create(
            model="claude-opus-5",
            max_tokens=1024,
            system=(
                "You are the Master Agent of RailMind, a railway operations AI. "
                "Given simulation results for candidate response plans, explain to a human "
                "dispatcher in 2-3 plain sentences why the selected plan is the right call. "
                "Be concrete about the tradeoffs. No preamble, no markdown."
            ),
            messages=[{"role": "user", "content": json.dumps(summary)}],
        )
        if response.stop_reason == "refusal":
            return None
        return next((b.text for b in response.content if b.type == "text"), None)
    except Exception:
        return None


def _explanation(final: RailState, use_llm: bool = True) -> dict:
    llm_text = _llm_explanation(final) if use_llm else None
    if llm_text:
        return {"text": llm_text.strip(), "source": "llm"}
    return {"text": _template_explanation(final), "source": "rules"}


def _format_response(final: RailState, twin_source: str, use_llm: bool = True) -> dict:
    best = final["best_plan"]
    plans_by_id = {p["plan_id"]: p for p in final["plans"]}

    failed_tracks = sorted({
        s["track_id"] for s in final["track_strategies"]
        if s["strategy_id"].startswith("T_CLOSE_")
    })

    rerouted_trains = [
        {"id": s["train_id"], "newRoute": s["new_route"]}
        for s in final["routing_strategies"]
        if s["strategy_id"].startswith("R_REROUTE_")
    ]

    reroutes = [
        {"train": s["train_id"], "from": s.get("old_route", []), "to": s["new_route"]}
        for s in final["routing_strategies"]
        if s["strategy_id"].startswith("R_REROUTE_")
    ]

    held_trains = [
        s["train_id"] for s in final["routing_strategies"]
        if s["strategy_id"].startswith("R_HOLD_")
    ]

    trains = [
        {
            "id": tid,
            "route": t.get("route", []),
            "passengers": t.get("passengers", 0),
            "current_station": t.get("current_station"),
        }
        for tid, t in final["snapshot"].get("trains", {}).items()
    ]

    weather_risk = final["weather_risk"]

    return {
        "twin_source": twin_source,
        "explanation": _explanation(final, use_llm=use_llm),
        "recommended_action": _plan_payload(best, plans_by_id[best["plan_id"]]),
        "candidate_plans": [
            _plan_payload(r, plans_by_id[r["plan_id"]])
            for r in final["simulation_results"]
        ],
        "agent_outputs": {
            "weather": {
                "strategy": final["weather_strategies"][0]["strategy_id"] if final["weather_strategies"] else "W5",
                "risk_score": round(max(weather_risk.values(), default=0) / 100, 2),
                "high_risk_tracks": [t for t, r in weather_risk.items() if r > 60],
            },
            "track": {
                "actions": [s["strategy_id"] for s in final["track_strategies"]],
            },
            "signal": {
                "actions": [
                    s["strategy_id"] for s in final["signal_strategies"]
                    if not s["strategy_id"].startswith("S_GREEN")
                ] or [s["strategy_id"] for s in final["signal_strategies"]][:6],
            },
            "routing": {
                "affected_trains": [r["train"] for r in reroutes] + held_trains,
                "reroutes": reroutes,
            },
        },
        "execution_log": list(reversed(final["log"])),  # newest first
        "failed_tracks": failed_tracks,
        "rerouted_trains": rerouted_trains,
        "held_trains": held_trains,
        "trains": trains,
        # Always present: /apply-plan re-grades the ledger after executing, and
        # a cached run replayed through this formatter must not serve the
        # escalation state as it stood before the actions landed.
        "escalation": final.get("escalation") or _observe(final["snapshot"]),
    }


# ─────────────────────────────────────────────────────────────────────────────
# LAST RUN CACHE
# /apply-plan must execute exactly the plan the operator approved — the twin
# keeps ticking after plans are displayed, so recomputing would silently swap
# in a different plan under the same letter.
# ─────────────────────────────────────────────────────────────────────────────

_last_run: dict = {"final": None, "twin_source": None, "ts": 0.0}
_LAST_RUN_TTL = 600.0


def _record_run(final: RailState, twin_source: str) -> None:
    _last_run["final"] = final
    _last_run["twin_source"] = twin_source
    _last_run["ts"] = time.time()


def _clear_last_run() -> None:
    """Invalidate cached plans. Must run whenever the world is wholesale
    replaced (/reset): the cached plans describe a twin that no longer exists,
    and applying them would silently undo the replacement."""
    _last_run["final"] = None
    _last_run["twin_source"] = None
    _last_run["ts"] = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {
        "service": "RailMind Agents",
        "status": "ok",
        "endpoints": [
            "/state", "/run", "/run-stream", "/apply-plan", "/reset",
            "/simulate-track-failure/{track_id}", "/simulate-weather/{track_id}",
            "/escalation", "/incidents/{incident_id}/acknowledge",
            "/incidents/{incident_id}/brief", "/docs",
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# ESCALATION ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/escalation")
def escalation_state():
    """Current handling level and the completion signal for every incident.

    Cheap enough for the console to poll: it folds one twin snapshot into the
    ledger, re-verifies committed work against that snapshot, and returns the
    graded result. No LLM call — briefs are requested explicitly.
    """
    snapshot, source = fetch_snapshot()
    payload = _observe(snapshot)
    return {"twin_source": source, **payload}


@app.post("/incidents/{incident_id}/acknowledge")
def acknowledge_incident(incident_id: str, owner: str = CONSOLE_OPERATOR):
    """Take ownership at the current tier and restart that tier's dwell timer.

    Acknowledging is not resolving: the incident keeps its completion signal,
    and condition-driven escalation still applies. It only stops the clock from
    escalating work somebody is demonstrably handling.
    """
    with _ledger_lock:
        try:
            _LEDGER.acknowledge(incident_id, owner)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found.")

    snapshot, source = fetch_snapshot()
    payload = _observe(snapshot)
    return {
        "status": "success",
        "incident_id": incident_id,
        "acknowledged_by": owner,
        "twin_source": source,
        **payload,
    }


@app.post("/incidents/{incident_id}/brief")
def incident_brief(incident_id: str):
    """Draft the handoff brief that travels with this incident to its owner.

    The language model writes it when a key is configured; otherwise a deterministic
    template produces the same four sections from the same numbers. The
    response says which, so the console can label it honestly.
    """
    snapshot, _ = fetch_snapshot()
    payload = _observe(snapshot)
    incident = next((i for i in payload["incidents"] if i["id"] == incident_id), None)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found.")
    return handoff_brief(payload, incident)


@app.get("/state")
def live_state():
    """Live twin snapshot for the console's polling loop (trains move each tick)."""
    snapshot, source = fetch_snapshot()
    trains = [
        {
            "id": tid,
            "route": t.get("route", []),
            "passengers": t.get("passengers", 0),
            "current_station": t.get("current_station"),
            "route_index": t.get("route_index", 0),
            "progress": t.get("progress", 0.0),
            "held": t.get("held", False),
            "delayed_minutes": round(t.get("delayed_minutes", 0.0), 1),
        }
        for tid, t in snapshot.get("trains", {}).items()
    ]
    tracks = [
        {
            "id": tid,
            "health": t.get("health", 1.0),
            "status": t.get("status", "OPEN"),
            "source": t.get("source"),
            "destination": t.get("destination"),
        }
        for tid, t in snapshot.get("tracks", {}).items()
    ]
    weather = {
        tid: (c.value if hasattr(c, "value") else str(c))
        for tid, c in (snapshot.get("weather") or {}).items()
    } if isinstance(snapshot.get("weather"), dict) else {}
    return {"twin_source": source, "trains": trains, "tracks": tracks, "weather": weather}


@app.post("/reset")
def reset():
    """Reset the twin to its baseline state (clears failures, weather, reroutes)."""
    # Clear cached plans first — even a partially failed reset has already
    # replaced the embedded twin, and /apply-plan must never re-close tracks
    # the reset just reopened. The ledger goes with them: its incidents and
    # work items describe a world that no longer exists, and a stale BLOCKED
    # incident would keep the console at L4 over a healthy network.
    _clear_last_run()
    with _ledger_lock:
        _LEDGER.reset()
    try:
        _twin_reset()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Twin reset failed: {e}")
    return {"status": "success", "message": "Twin reset to baseline."}


@app.post("/run")
def run_pipeline():
    """Fetch the digital twin state and run the full agent pipeline."""
    snapshot, source = fetch_snapshot()
    final = pipeline.invoke(_initial_state(snapshot))
    _record_run(final, source)
    return _format_response(final, source)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/run-stream")
def run_pipeline_stream(inject_track: str | None = None, weather_track: str | None = None,
                        condition: str = "STORM"):
    """Run the pipeline and stream agent log events live over SSE.

    Optional query params apply an injection first (persistently, like the
    POST endpoints): ?inject_track=T23 or ?weather_track=T05&condition=STORM.
    """
    condition = condition.upper()
    if weather_track and condition not in WEATHER_CONDITION_RISK:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown condition '{condition}'. Use one of {list(WEATHER_CONDITION_RISK)}.",
        )

    known_tracks = fetch_snapshot()[0].get("tracks", {})
    for tid in (inject_track, weather_track):
        if tid and tid not in known_tracks:
            raise HTTPException(status_code=404, detail=f"Track '{tid}' not found.")

    extra_log: List[dict] = []
    try:
        if inject_track:
            _twin_close_track(inject_track)
            extra_log.append({"t": time.strftime("%H:%M:%S"), "source": "Sensor",
                              "message": f"[INJECTED] Track {inject_track} failure detected — closed"})
        if weather_track:
            _twin_set_weather(weather_track, condition)
            extra_log.append({"t": time.strftime("%H:%M:%S"), "source": "Sensor",
                              "message": f"[INJECTED] {condition} reported on track {weather_track}"})
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Twin unreachable: {e}")

    snapshot, source = fetch_snapshot()

    def event_stream():
        try:
            state = _initial_state(snapshot, extra_log=list(extra_log))
            emitted = 0
            for entry in extra_log:
                yield _sse("log", entry)
                emitted += 1

            final_state = dict(state)
            for update in pipeline.stream(state):
                for node_state in update.values():
                    if not isinstance(node_state, dict):
                        continue
                    final_state.update(node_state)
                    log = final_state.get("log", [])
                    while emitted < len(log):
                        yield _sse("log", log[emitted])
                        emitted += 1
                        time.sleep(0.15)  # let the timeline animate

            payload = _format_response(final_state, source)
            _record_run(final_state, source)
            yield _sse("result", payload)
            yield _sse("done", {})
        except Exception as e:
            # A mid-stream crash must not end the stream silently — and the
            # browser EventSource routes `event: error` to source.onerror,
            # which carries no payload, so the operator would never see the
            # message. Emit a normal log event the console timeline renders.
            yield _sse("log", {"t": time.strftime("%H:%M:%S"), "source": "System",
                               "message": f"pipeline failed: {e}"})

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/simulate-track-failure/{track_id}")
def simulate_track_failure(track_id: str):
    """Inject a persistent track failure into the twin and run the pipeline.

    Failures accumulate — inject several tracks to build a cascade scenario.
    Use /reset to return to baseline.
    """
    snapshot, _ = fetch_snapshot()
    if track_id not in snapshot.get("tracks", {}):
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found.")

    try:
        _twin_close_track(track_id)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Twin unreachable: {e}")

    snapshot, source = fetch_snapshot()
    injected_log = [{
        "t": time.strftime("%H:%M:%S"),
        "source": "Sensor",
        "message": f"[INJECTED] Track {track_id} failure detected — closed",
    }]
    final = pipeline.invoke(_initial_state(snapshot, extra_log=injected_log))
    _record_run(final, source)
    return {"injected_failure": track_id, **_format_response(final, source)}


@app.post("/simulate-weather/{track_id}")
def simulate_weather(track_id: str, condition: str = "STORM"):
    """Inject persistent severe weather on a track and run the pipeline."""
    condition = condition.upper()
    if condition not in WEATHER_CONDITION_RISK:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown condition '{condition}'. Use one of {list(WEATHER_CONDITION_RISK)}.",
        )

    snapshot, _ = fetch_snapshot()
    if track_id not in snapshot.get("tracks", {}):
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found.")

    try:
        _twin_set_weather(track_id, condition)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Twin unreachable: {e}")

    snapshot, source = fetch_snapshot()
    injected_log = [{
        "t": time.strftime("%H:%M:%S"),
        "source": "Sensor",
        "message": f"[INJECTED] {condition} reported on track {track_id}",
    }]
    final = pipeline.invoke(_initial_state(snapshot, extra_log=injected_log))
    _record_run(final, source)
    return {"injected_weather": {"track_id": track_id, "condition": condition},
            **_format_response(final, source)}


@app.post("/apply-plan")
def apply_plan(plan_id: str | None = None):
    """Close the loop: execute the chosen plan on the twin.

    Uses the plans from the last pipeline run shown to the console, so the
    operator executes exactly what they approved even though the twin has
    ticked since; recomputes only when no recent run exists. Closes flagged
    tracks and applies the plan's reroutes so the living twin reflects the
    decision — trains follow their new routes on subsequent ticks.
    Defaults to the Master Agent's recommended plan.
    """
    # Read the cached run into locals ONCE: a concurrent /reset nulls the
    # shared dict, so a check-then-reread would crash (or worse, mix runs)
    # mid-request.
    cached = _last_run["final"]
    cached_source = _last_run["twin_source"]
    if cached is not None and time.time() - _last_run["ts"] < _LAST_RUN_TTL:
        source = cached_source
        # Own log copy: repeated applies must not pile Executor entries into
        # the cached run.
        final = dict(cached)
        final["log"] = list(final["log"])
    else:
        snapshot, source = fetch_snapshot()
        final = pipeline.invoke(_initial_state(snapshot))

    target_id = (plan_id or final["best_plan"]["plan_id"]).upper()
    plan = next((p for p in final["plans"] if p["plan_id"] == target_id), None)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan '{target_id}' not found.")

    applied = []
    apply_error = None
    try:
        for action in plan["actions"]:
            if not isinstance(action, dict):
                continue
            sid = action.get("strategy_id", "")
            if sid.startswith("T_CLOSE_"):
                _twin_close_track(action["track_id"])
                applied.append(f"Closed track {action['track_id']}")
            elif sid.startswith("R_REROUTE_"):
                _twin_reroute_train(action["train_id"], action["new_route"])
                applied.append(f"Rerouted {action['train_id']}")
            elif sid.startswith("W"):
                # Weather protocols advertise closures inside action strings
                # like "close_track_T05+reroute_all_trains" — execute them.
                for act in action.get("actions", []):
                    if act.startswith("close_track_"):
                        track_id = act.removeprefix("close_track_").split("+")[0]
                        _twin_close_track(track_id)
                        applied.append(f"Closed track {track_id} (weather protocol {sid})")
    except requests.RequestException as e:
        # Actions already applied changed the twin — a 502 here would hide
        # them, so report the partial execution instead.
        apply_error = e

    if apply_error is None:
        message = f"Plan {target_id} executed on the live twin: {len(applied)} action(s)"
    else:
        message = (f"Plan {target_id} partially executed: {len(applied)} action(s) "
                   f"applied before the twin became unreachable: {apply_error}")
    final["log"].append({
        "t": time.strftime("%H:%M:%S"),
        "source": "Executor",
        "message": message,
    })

    # Register what this plan committed to, then re-grade against a FRESH
    # snapshot. Registering every action — not just the ones the apply loop
    # reported — is the point: an action that never landed must show as
    # outstanding work, not vanish from the checklist.
    post_snapshot, source = fetch_snapshot()
    with _ledger_lock:
        _LEDGER.record_dispatch(target_id, plan["actions"], post_snapshot)
        graded = _LEDGER.observe(post_snapshot)
    final["escalation"] = graded
    final["snapshot"] = post_snapshot
    worst = max(
        graded["incidents"],
        key=lambda i: (esc.RESOLUTION_SEVERITY.get(i["resolution"], 0), i["level"]),
        default=None,
    )
    final["log"].append({
        "t": time.strftime("%H:%M:%S"),
        "source": "Escalation",
        "message": (f"post-execution completion signal: {graded['resolution']}"
                    + (f" — {worst['id']} {worst['resolution_reason']}" if worst else "")),
    })
    # Template-only explanation: the apply path must answer fast — actions are
    # already applied to the twin, so a slow LLM call would make the console
    # time out and mislabel a successful execution as offline.
    return {"executed_plan": target_id, "applied_actions": applied,
            **_format_response(final, source, use_llm=False)}
