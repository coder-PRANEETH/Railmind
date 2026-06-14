"""
server.py  —  RailMind FastAPI  (architecture-aligned version)
────────────────────────────────────────────────────────────────
Flow:
  Digital Twin snapshot
    → WeatherAgent   (risk scores per track)
    → TrackAgent     (strategies: close / restrict / monitor)
    → SignalAgent    (strategies: RED / YELLOW / GREEN per section)
    → RoutingAgent   (strategies: reroute options per affected train)
    → PlannerAgent   (combines strategies → Plan_A / Plan_B / Plan_C)
    → SimulationNode (clones twin, applies each plan, scores locally)
    → MasterAgent    (MCDM normalised ranking → best plan)

Agent contract (every specialist):
  Input  : RailState (read-only)
  Output : { agent_name, risk_score, strategies: [{ id, name, confidence, actions[] }] }

Planner contract:
  Input  : strategies from all specialists
  Output : plans: [{ plan_id, strategy_ids, actions[] }]  — 3 plans: aggressive/balanced/conservative

Simulation contract:
  Input  : each plan + cloned twin
  Output : { plan_id, delay, risk, passenger_impact, congestion }

Master contract:
  Input  : simulation_results[]
  Output : { selected_plan, score, ranking, explanation }
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, TypedDict

import networkx as nx
import requests
from fastapi import FastAPI, HTTPException
from langgraph.graph import END, StateGraph

app = FastAPI(title="RailMind")
BASE_URL = "http://127.0.0.1:8000"


# ─────────────────────────────────────────────────────────────────────────────
# TRAIN ROUTES  (unchanged from original)
# ─────────────────────────────────────────────────────────────────────────────

TRAINS: Dict[str, List[str]] = {
    "T001": ["CHENNAI_CHET","SECUNDERABAD","NAGPUR_JUNCT","RAIPUR_JUNCT","RANCHI","HOWRAH_JUNCT"],
    "T002": ["NEW_DELHI","JAIPUR_JUNCT","BHOPAL_JUNCT","NAGPUR_JUNCT"],
    "T003": ["MUMBAI_CENTR","SECUNDERABAD","CHENNAI_CHET","THIRUVANANTH"],
    "T004": ["GUWAHATI","PATNA_JUNCTI","LUCKNOW_JUNC","NEW_DELHI"],
    "T005": ["AHMEDABAD_JU","BHOPAL_JUNCT","RAIPUR_JUNCT","RANCHI","HOWRAH_JUNCT"],
    "T006": ["JAMMU_TAWI","DEHRADUN","NEW_DELHI","LUCKNOW_JUNC","PATNA_JUNCTI"],
    "T007": ["PUNE_JUNCTIO","SECUNDERABAD","NAGPUR_JUNCT","BHOPAL_JUNCT"],
    "T008": ["BENGALURU_EA","SECUNDERABAD","MUMBAI_CENTR","AHMEDABAD_JU"],
    "T009": ["CHANDIGARH_R","JAIPUR_JUNCT","BHOPAL_JUNCT","RAIPUR_JUNCT"],
    "T010": ["BHUBANESWAR","HOWRAH_JUNCT","GUWAHATI"],
    "T011": ["NEW_DELHI","CHANDIGARH_R","JAMMU_TAWI"],
    "T012": ["NEW_DELHI","LUCKNOW_JUNC","PATNA_JUNCTI","GUWAHATI"],
    "T013": ["HOWRAH_JUNCT","PATNA_JUNCTI","LUCKNOW_JUNC","NEW_DELHI"],
    "T014": ["MUMBAI_CENTR","AHMEDABAD_JU","BHOPAL_JUNCT"],
    "T015": ["MUMBAI_CENTR","PUNE_JUNCTIO","SECUNDERABAD"],
    "T016": ["BENGALURU_EA","CHENNAI_CHET","THIRUVANANTH"],
    "T017": ["SECUNDERABAD","NAGPUR_JUNCT","BHOPAL_JUNCT","JAIPUR_JUNCT"],
    "T018": ["RAIPUR_JUNCT","RANCHI","HOWRAH_JUNCT"],
    "T019": ["DEHRADUN","NEW_DELHI","JAIPUR_JUNCT"],
    "T020": ["PATNA_JUNCTI","RANCHI","RAIPUR_JUNCT"],
    "T021": ["AHMEDABAD_JU","PUNE_JUNCTIO","SECUNDERABAD","CHENNAI_CHET"],
    "T022": ["GUWAHATI","HOWRAH_JUNCT","BHUBANESWAR"],
    "T023": ["JAIPUR_JUNCT","BHOPAL_JUNCT","NAGPUR_JUNCT","SECUNDERABAD"],
    "T024": ["NEW_DELHI","DEHRADUN","JAMMU_TAWI"],
    "T025": ["CHENNAI_CHET","BENGALURU_EA","THIRUVANANTH"],
    "T026": ["MUMBAI_CENTR","SECUNDERABAD","NAGPUR_JUNCT"],
    "T027": ["HOWRAH_JUNCT","RANCHI","RAIPUR_JUNCT","BHOPAL_JUNCT"],
    "T028": ["LUCKNOW_JUNC","PATNA_JUNCTI","GUWAHATI"],
    "T029": ["CHANDIGARH_R","DEHRADUN","NEW_DELHI"],
    "T030": ["AHMEDABAD_JU","BHOPAL_JUNCT","NAGPUR_JUNCT"],
    "T031": ["SECUNDERABAD","PUNE_JUNCTIO","MUMBAI_CENTR"],
    "T032": ["GUWAHATI","PATNA_JUNCTI","HOWRAH_JUNCT"],
    "T033": ["BHUBANESWAR","RANCHI","RAIPUR_JUNCT"],
    "T034": ["JAIPUR_JUNCT","NEW_DELHI","DEHRADUN"],
    "T035": ["BENGALURU_EA","SECUNDERABAD","NAGPUR_JUNCT","RAIPUR_JUNCT"],
    "T036": ["CHENNAI_CHET","SECUNDERABAD","PUNE_JUNCTIO"],
    "T037": ["HOWRAH_JUNCT","PATNA_JUNCTI","LUCKNOW_JUNC"],
    "T038": ["AHMEDABAD_JU","MUMBAI_CENTR","SECUNDERABAD"],
    "T039": ["JAMMU_TAWI","CHANDIGARH_R","JAIPUR_JUNCT"],
    "T040": ["RAIPUR_JUNCT","NAGPUR_JUNCT","BHOPAL_JUNCT"],
    "T041": ["NEW_DELHI","JAIPUR_JUNCT","CHANDIGARH_R"],
    "T042": ["PATNA_JUNCTI","RANCHI","HOWRAH_JUNCT"],
    "T043": ["SECUNDERABAD","CHENNAI_CHET","BENGALURU_EA"],
    "T044": ["MUMBAI_CENTR","AHMEDABAD_JU","BHOPAL_JUNCT","RAIPUR_JUNCT"],
    "T045": ["GUWAHATI","HOWRAH_JUNCT","RANCHI"],
    "T046": ["DEHRADUN","LUCKNOW_JUNC","PATNA_JUNCTI"],
    "T047": ["THIRUVANANTH","BENGALURU_EA","SECUNDERABAD"],
    "T048": ["PUNE_JUNCTIO","AHMEDABAD_JU","BHOPAL_JUNCT"],
    "T049": ["NAGPUR_JUNCT","RAIPUR_JUNCT","RANCHI"],
    "T050": ["CHENNAI_CHET","SECUNDERABAD","MUMBAI_CENTR"],
}


# ─────────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────────

class RailState(TypedDict):
    snapshot:               dict            # raw /api/state/ response
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

    log:                    List[str]


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _strategy(sid: str, name: str, confidence: float, actions: List[str]) -> dict:
    """Standard strategy object used by every specialist agent."""
    return {
        "strategy_id": sid,
        "strategy_name": name,
        "confidence": confidence,
        "actions": actions,
    }


def _find_affected_trains(track_id: str, tracks: dict, trains_data: dict) -> List[str]:
    """Return train IDs whose current route passes through this track's edge."""
    track = tracks.get(track_id, {})
    src, dst = track.get("source"), track.get("destination")
    if not src or not dst:
        return []
    affected = []
    for tid, t in trains_data.items():
        route = t.get("route", [])
        for i in range(len(route) - 1):
            if route[i] == src and route[i + 1] == dst:
                affected.append(tid)
                break
    return affected


def _alternate_route(G: nx.Graph, src: str, dst: str, blocked_edges: List[tuple]) -> List[str] | None:
    """Dijkstra on a copy of G with blocked_edges removed."""
    H = G.copy()
    for u, v in blocked_edges:
        if H.has_edge(u, v):
            H.remove_edge(u, v)
    try:
        return nx.shortest_path(H, source=src, target=dst)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 1 — WEATHER
# Reads  : snapshot.weather[]
# Outputs: weather_risk (sensor data), weather_strategies[]
# Rule   : weighted formula — no ML, no API
# ─────────────────────────────────────────────────────────────────────────────

def weather_node(state: RailState) -> RailState:
    weather_list = state["snapshot"].get("weather", [])
    risk_map: Dict[str, float] = {}

    for w in weather_list:
        track_id    = w["track_id"]
        rainfall    = w.get("rainfall", 0)
        temperature = w.get("temperature", 25)

        # Weighted scoring — no training needed
        risk = 100 * (
            0.7 * min(rainfall / 50, 1.0) +
            0.3 * max(min((temperature - 25) / 25, 1.0), 0.0)
        )
        risk_map[track_id] = round(risk, 1)

    state["weather_risk"] = risk_map

    # ── generate strategies ordered by severity ──
    high_tracks    = [t for t, r in risk_map.items() if r > 70]
    medium_tracks  = [t for t, r in risk_map.items() if 40 < r <= 70]

    strategies = []

    if high_tracks:
        strategies.append(_strategy(
            "W1", "emergency_weather_protocol",
            confidence=0.92,
            actions=[
                f"close_track_{t}+reroute_all_trains" for t in high_tracks
            ],
        ))
        strategies.append(_strategy(
            "W2", "speed_restriction_high_risk_tracks",
            confidence=0.75,
            actions=[
                f"reduce_speed_40kmh_{t}+intensive_monitoring" for t in high_tracks
            ],
        ))

    if medium_tracks:
        strategies.append(_strategy(
            "W3", "caution_medium_risk_tracks",
            confidence=0.60,
            actions=[
                f"reduce_speed_60kmh_{t}+30min_sensor_poll" for t in medium_tracks
            ],
        ))

    # Always-available conservative strategy
    strategies.append(_strategy(
        "W4", "monitor_and_reassess",
        confidence=0.40,
        actions=["continuous_weather_monitoring+alert_crew"],
    ))

    if not strategies or (not high_tracks and not medium_tracks):
        strategies = [_strategy(
            "W5", "nominal_weather_conditions",
            confidence=0.99,
            actions=["no_weather_action_required"],
        )]

    state["weather_strategies"] = strategies

    high_str = ", ".join(f"{t}({r}%)" for t, r in risk_map.items() if r > 60)
    state["log"].append(
        f"WeatherAgent | risk_score={max(risk_map.values(), default=0):.1f} | "
        f"high-risk: [{high_str or 'none'}] | strategies: {[s['strategy_id'] for s in strategies]}"
    )
    return state


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 2 — TRACK
# Reads  : snapshot.tracks[], weather_risk
# Outputs: track_strategies[]
# Rule   : weighted formula — optional Decision Tree (not needed for MVP)
# ─────────────────────────────────────────────────────────────────────────────
def track_node(state):

    tracks = state["snapshot"]["tracks"]

    strategies = []

    for track_id, track in tracks.items():

        health = track["health"]

        if health <= 0.2:

            strategies.append({
                "strategy_id": f"T_CLOSE_{track_id}",
                "track_id": track_id,
                "action": "close_track"
            })

        elif health <= 0.5:

            strategies.append({
                "strategy_id": f"T_RESTRICT_{track_id}",
                "track_id": track_id,
                "action": "speed_limit_60"
            })

        elif health <= 0.7:

            strategies.append({
                "strategy_id": f"T_MONITOR_{track_id}",
                "track_id": track_id,
                "action": "monitor"
            })

    if not strategies:

        strategies.append({
            "strategy_id": "T_OK",
            "action": "healthy"
        })

    state["track_strategies"] = strategies

    return state

# ─────────────────────────────────────────────────────────────────────────────
# AGENT 3 — SIGNAL
# Reads  : track_strategies (to know which tracks are at risk)
# Outputs: signal_strategies[]
# Rule   : rule table — risk level → signal state (no dataset needed)
# ─────────────────────────────────────────────────────────────────────────────

def signal_node(state: RailState) -> RailState:
    tracks      = state["snapshot"].get("tracks", {})
    weather_risk = state["weather_risk"]
    strategies  = []
    max_signal_risk = 0.0

    for track_id, track in tracks.items():
        health      = track.get("health", 1.0)
        w_risk_norm = weather_risk.get(track_id, 0) / 100.0
        risk_score  = round(0.6 * (1 - health) + 0.4 * w_risk_norm, 3)
        max_signal_risk = max(max_signal_risk, risk_score)
        sid_base    = track_id.replace(" ", "_")

        # Rule table — derived from Indian Railways IRGSR speed restriction rules
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
                actions=[
                    f"signal_{track_id}_GREEN",
                ],
            ))

    if not strategies:
        strategies.append(_strategy(
            "S_OK", "all_signals_nominal",
            confidence=0.99,
            actions=["routine_signal_monitoring"],
        ))

    state["signal_strategies"] = strategies
    state["log"].append(
        f"SignalAgent | risk_score={max_signal_risk:.2f} | "
        f"strategies: {[s['strategy_id'] for s in strategies]}"
    )
    return state


# ─────────────────────────────────────────────────────────────────────────────
# AGENT 4 — ROUTING  (= Delay Agent in the architecture)
# Reads  : track_strategies, graph, snapshot.trains
# Outputs: routing_strategies[]
# Algorithm: Dijkstra (K-shortest paths for multiple strategy options)
# ─────────────────────────────────────────────────────────────────────────────

def routing_node(state):

    G = state["graph"].copy()

    trains = state["snapshot"]["trains"]

    routing_strategies = []

    closed_tracks = []

    for s in state["track_strategies"]:

        if s["action"] == "close_track":

            closed_tracks.append(
                s["track_id"]
            )

    tracks = state["snapshot"]["tracks"]

    # remove closed edges

    for track_id in closed_tracks:

        track = tracks[track_id]

        src = track["source"]
        dst = track["destination"]

        if G.has_edge(src, dst):
            G.remove_edge(src, dst)

    # affected trains

    for train_id, train in trains.items():

        route = train["route"]

        affected = False

        for track_id in closed_tracks:

            track = tracks[track_id]

            src = track["source"]
            dst = track["destination"]

            for i in range(len(route)-1):

                a = route[i]
                b = route[i+1]

                if (
                    (a == src and b == dst)
                    or
                    (a == dst and b == src)
                ):
                    affected = True
                    break

        if affected:

            try:

                new_route = nx.shortest_path(
                    G,
                    source=route[0],
                    target=route[-1]
                )

                routing_strategies.append({
                    "strategy_id": f"R_REROUTE_{train_id}",
                    "train_id": train_id,
                    "new_route": new_route
                })

            except nx.NetworkXNoPath:

                routing_strategies.append({
                    "strategy_id": f"R_HOLD_{train_id}",
                    "train_id": train_id
                })

    if not routing_strategies:

        routing_strategies.append({
            "strategy_id": "R_NOMINAL"
        })

    state["routing_strategies"] = routing_strategies

    return state

# ─────────────────────────────────────────────────────────────────────────────
# PLANNER AGENT
# Reads  : all specialist strategies
# Outputs: 3 candidate plans — Plan_A (aggressive), Plan_B (balanced), Plan_C (conservative)
# Algorithm: greedy confidence-ranked combination
# ─────────────────────────────────────────────────────────────────────────────
def planner_node(state):

    plans = []

    weather = state["weather_strategies"]
    track = state["track_strategies"]
    signal = state["signal_strategies"]
    routing = state["routing_strategies"]

    # PLAN A
    # safest

    plans.append({
        "plan_id": "Plan_A",

        "actions":

            weather +

            [
                x for x in track
                if "CLOSE" in x["strategy_id"]
            ] +

            [
                x for x in signal
                if "RED" in x["strategy_id"]
            ] +

            routing
    })

    # PLAN B
    # balanced

    plans.append({
        "plan_id": "Plan_B",

        "actions":

            weather +

            [
                x for x in track
                if (
                    "RESTRICT" in x["strategy_id"]
                    or
                    "MONITOR" in x["strategy_id"]
                )
            ] +

            [
                x for x in signal
                if "YELLOW" in x["strategy_id"]
            ] +

            routing
    })

    # PLAN C
    # aggressive

    plans.append({
        "plan_id": "Plan_C",

        "actions":

            weather +

            [
                x for x in track
                if (
                    "MONITOR" in x["strategy_id"]
                    or
                    x["strategy_id"] == "T_OK"
                )
            ] +

            [
                x for x in signal
                if "GREEN" in x["strategy_id"]
            ] +

            [{
                "strategy_id": "R_NOMINAL"
            }]
    })

    state["plans"] = plans

    return state

# ─────────────────────────────────────────────────────────────────────────────
# SIMULATION ENGINE  (not an agent — infrastructure)
# Reads  : plans[], twin snapshot
# Process: clone twin per plan, apply actions, score locally
# Algorithm: BFS cascade propagation on NetworkX graph
# Outputs : { plan_id, delay, risk, passenger_impact, congestion }
# ─────────────────────────────────────────────────────────────────────────────

def _simulate_plan(plan, snapshot, graph):

    delay = 0
    risk = 0.25
    passenger_impact = 0
    congestion = 0.0

    trains = snapshot["trains"]

    for action in plan["actions"]:

        if not isinstance(action, dict):
            continue

        strategy = action.get("strategy_id", "")

        # -------------------------
        # TRACK ACTIONS
        # -------------------------

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

        # -------------------------
        # SIGNAL ACTIONS
        # -------------------------

        elif strategy.startswith("S_RED_"):

            delay += 15
            risk -= 0.10

        elif strategy.startswith("S_YELLOW_"):

            delay += 5
            risk -= 0.05

        elif strategy.startswith("S_GREEN_"):

            pass

        # -------------------------
        # ROUTING ACTIONS
        # -------------------------

        elif strategy.startswith("R_REROUTE_"):

            train_id = action.get("train_id")

            delay += 15
            congestion += 0.10

            if train_id in trains:
                passenger_impact += trains[train_id]["passengers"]

        elif strategy.startswith("R_HOLD_"):

            train_id = action.get("train_id")

            delay += 45
            congestion += 0.30

            if train_id in trains:
                passenger_impact += trains[train_id]["passengers"]

        elif strategy == "R_NOMINAL":

            pass

    # -------------------------
    # NORMALIZE
    # -------------------------

    risk = max(0.0, min(1.0, risk))
    congestion = round(congestion, 2)

    return {
        "plan_id": plan["plan_id"],
        "delay": delay,
        "risk": round(risk, 2),
        "passenger_impact": passenger_impact,
        "congestion": congestion,
    }

def simulation_node(state: RailState) -> RailState:
    results = []
    for plan in state["plans"]:
        result = _simulate_plan(plan, state["snapshot"], state["graph"])
        results.append(result)
        state["log"].append(
            f"Simulation | {result['plan_id']} → "
            f"delay={result['delay']}min | "
            f"risk={result['risk']:.2f} | "
            f"passengers={result['passenger_impact']} | "
            f"congestion={result['congestion']:.2f}"
        )
    state["simulation_results"] = results
    return state


# ─────────────────────────────────────────────────────────────────────────────
# MASTER AGENT
# Reads  : simulation_results[]
# Algorithm: min-max normalisation + MCDM weighted scoring
# Weights: delay×0.35, risk×0.40, passengers×0.15, congestion×0.10
# Outputs: selected_plan, score, ranking, explanation
# ─────────────────────────────────────────────────────────────────────────────

MCDM_WEIGHTS = {
    "delay":            0.35,
    "risk":             0.40,
    "passenger_impact": 0.15,
    "congestion":       0.10,
}
def master_node(state):

    best_plan = None
    best_score = float("inf")

    ranked = []

    for result in state["simulation_results"]:

        delay = result["delay"]
        risk = result["risk"]
        passenger_impact = result["passenger_impact"]
        congestion = result["congestion"]

        score = (
            delay * 0.5 +
            risk * 100 * 0.3 +
            passenger_impact * 0.1 +
            congestion * 100 * 0.1
        )

        result["score"] = round(score, 2)

        ranked.append(result)

        if score < best_score:
            best_score = score
            best_plan = result

    ranked.sort(key=lambda x: x["score"])

    state["best_plan"] = best_plan

    ranking = [p["plan_id"] for p in ranked]

    state["log"].append(
        f"MasterAgent | Selected {best_plan['plan_id']} "
        f"| Score={best_plan['score']} "
        f"| Ranking={ranking}"
    )

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
]:
    _builder.add_node(_name, _fn)

_builder.set_entry_point("weather")

for _a, _b in [
    ("weather",    "track"),
    ("track",      "signal"),    # signal reads track risk → must come after track
    ("signal",     "routing"),
    ("routing",    "planner"),
    ("planner",    "simulation"),
    ("simulation", "master"),
    ("master",     END),
]:
    _builder.add_edge(_a, _b)

pipeline = _builder.compile()


# ─────────────────────────────────────────────────────────────────────────────
# INITIAL STATE FACTORY
# ─────────────────────────────────────────────────────────────────────────────

def _initial_state(snapshot: dict, extra_log: List[str] | None = None) -> RailState:
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
        log=extra_log or [],
    )


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/run")
def run_pipeline():
    """Fetch live digital twin state and run the full agent pipeline."""
    try:
        snapshot = requests.get(f"{BASE_URL}/api/state/", timeout=5).json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach digital twin: {e}")

    final = pipeline.invoke(_initial_state(snapshot))
    return _format_response(final)


@app.post("/simulate-track-failure/{track_id}")
def simulate_track_failure(track_id: str):
    """Inject a track failure and run the full pipeline against it."""
    try:
        snapshot = requests.get(f"{BASE_URL}/api/state/", timeout=5).json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach digital twin: {e}")

    tracks = snapshot.get("tracks", {})
    if track_id not in tracks:
        raise HTTPException(status_code=404, detail=f"Track '{track_id}' not found.")

    # Inject failure — force health to 0
    snapshot["tracks"][track_id]["health"] = 0.0

    final = pipeline.invoke(
        _initial_state(snapshot, extra_log=[f"[INJECTED FAILURE] {track_id} health → 0.0"])
    )
    return {"injected_failure": track_id, **_format_response(final)}


def _format_response(final):

    best = final["best_plan"]

    return {

        "recommended_action": {
            "selected_plan": best["plan_id"],
            "score": best["score"],
            "delay_minutes": best["delay"],
            "risk": best["risk"],
            "passengers_affected": best["passenger_impact"],
            "congestion": best["congestion"]
        },

        "candidate_plans": final["simulation_results"],

        "agent_outputs": {

            "weather_agent": [
                s["strategy_id"]
                for s in final.get("weather_strategies", [])
            ],

            "track_agent": [
                s["strategy_id"]
                for s in final.get("track_strategies", [])
            ],

            "signal_agent": [
                s["strategy_id"]
                for s in final.get("signal_strategies", [])
            ],

            "routing_agent": [
                s["strategy_id"]
                for s in final.get("routing_strategies", [])
            ]
        },

        "execution_log": final["log"]
    }




    