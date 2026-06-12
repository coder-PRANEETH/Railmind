from typing import List, TypedDict

import networkx as nx
from langgraph.graph import END, StateGraph

from digital_twin import DigitalTwin
from track_model import demo_track_model


# =========================
# STATE
# =========================

class RailState(TypedDict):
    twin: DigitalTwin
    track_data: dict
    weather_risk: float
    closed_tracks: List[str]
    
    track_actions: List[str]
    routing_actions: List[dict]
    plans: List[dict]
    simulation_results: List[dict]
    best_plan: dict


# =========================
# WEATHER AGENT
# =========================

def weather_node(state: RailState):
    rainfall = state["twin"].weather["rainfall"]
    actions = []

    if rainfall > 80:
        actions.append("recommend_track_closure")
        actions.append("reduce_speed")
    else:
        actions.append("weather_alert")

    state["weather_risk"] = min(rainfall / 100, 1)
    return state


# =========================
# TRACK AGENT
# =========================

def track_node(state: RailState):
    features = {
        "track_id": state["track_data"]["track_id"],
        "track_health": state["track_data"]["track_health"],
        "track_age": state["track_data"]["track_age"],
        "maintenance_days": state["track_data"]["maintenance_days"],
        "weather_risk": state["weather_risk"],
    }

    track_action = demo_track_model.predict(features)

    state["track_actions"] = [track_action]
    state["closed_tracks"] = []

    if track_action == "close":
        state["closed_tracks"] = [
            state["track_data"]["track_id"]
        ]

    return state


# =========================
# ROUTING AGENT
# =========================

def routing_node(state: RailState):

    
    twin = state["twin"].copy()
    
    twin_state = twin.get_state()
    
    train = twin_state["trains"]["T1"]
    
    tracks = twin_state["tracks"]

    for track_id, track in tracks.items():
        if track["closed"] or track_id in state["closed_tracks"]:
            twin.close_track(track_id)

    route = twin.find_route(
        train["source"],
        train["destination"],
    )

    state["routing_actions"] = [{
        "type": "set_route",
        "train_id": "T1",
        "route": route,
    
    }]
    return state


# =========================
# PLANNER AGENT
# =========================

def planner_node(state: RailState):
    plans = []

    for track_action in state["track_actions"]:
            for routing_action in state["routing_actions"]:
                actions = [
                 
                    track_action,
                    routing_action,
                ]

                plans.append({"actions": actions})

    state["plans"] = plans
    return state


# =========================
# SIMULATION ENGINE
# =========================

def simulate_plan(twin: DigitalTwin, plan):
    future = twin.copy()

    for action in plan["actions"]:
        future.apply_action(action)

    return {
        "plan": plan["actions"],
        "delay": future.calculate_delay(),
        "risk": future.calculate_risk(),
        "future_state": future.get_state(),
    }


def simulation_node(state: RailState):
    results = []

    for plan in state["plans"]:
        results.append(simulate_plan(state["twin"], plan))

    state["simulation_results"] = results
    return state


# =========================
# MASTER AGENT
# =========================

def master_node(state: RailState):
    best_score = -999
    best_plan = {}

    for result in state["simulation_results"]:
        score = (1 - result["risk"]) * 100 - result["delay"]
        result["score"] = score

        if score > best_score:
            best_score = score
            best_plan = result

    state["best_plan"] = best_plan
    return state


# =========================
# GRAPH
# =========================

builder = StateGraph(RailState)

builder.add_node("weather", weather_node)
builder.add_node("track", track_node)
builder.add_node("routing", routing_node)
builder.add_node("planner", planner_node)
builder.add_node("simulation", simulation_node)
builder.add_node("master", master_node)

builder.set_entry_point("weather")
builder.add_edge("weather", "track")
builder.add_edge("track", "routing")
builder.add_edge("routing", "planner")
builder.add_edge("planner", "simulation")
builder.add_edge("simulation", "master")
builder.add_edge("master", END)

graph = builder.compile()

