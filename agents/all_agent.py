from typing import List, TypedDict

import networkx as nx
from langgraph.graph import END, StateGraph



import requests

trains  = {
    "T001": [
        "CHENNAI_CHET",
        "SECUNDERABAD",
        "NAGPUR_JUNCT",
        "RAIPUR_JUNCT",
        "RANCHI",
        "HOWRAH_JUNCT"
    ],

    "T002": [
        "NEW_DELHI",
        "JAIPUR_JUNCT",
        "BHOPAL_JUNCT",
        "NAGPUR_JUNCT"
    ],

    "T003": [
        "MUMBAI_CENTR",
        "SECUNDERABAD",
        "CHENNAI_CHET",
        "THIRUVANANTH"
    ],

    "T004": [
        "GUWAHATI",
        "PATNA_JUNCTI",
        "LUCKNOW_JUNC",
        "NEW_DELHI"
    ],

    "T005": [
        "AHMEDABAD_JU",
        "BHOPAL_JUNCT",
        "RAIPUR_JUNCT",
        "RANCHI",
        "HOWRAH_JUNCT"
    ],

    "T006": [
        "JAMMU_TAWI",
        "DEHRADUN",
        "NEW_DELHI",
        "LUCKNOW_JUNC",
        "PATNA_JUNCTI"
    ],

    "T007": [
        "PUNE_JUNCTIO",
        "SECUNDERABAD",
        "NAGPUR_JUNCT",
        "BHOPAL_JUNCT"
    ],

    "T008": [
        "BENGALURU_EA",
        "SECUNDERABAD",
        "MUMBAI_CENTR",
        "AHMEDABAD_JU"
    ],

    "T009": [
        "CHANDIGARH_R",
        "JAIPUR_JUNCT",
        "BHOPAL_JUNCT",
        "RAIPUR_JUNCT"
    ],

    "T010": [
        "BHUBANESWAR",
        "HOWRAH_JUNCT",
        "GUWAHATI"
    ]
}

BASE_URL = "http://127.0.0.1:8000"
response = requests.get(f"{BASE_URL}/api/state/")
response = response.json()


import networkx as nx

def build_graph_from_state(state):
    graph_data = state["graph"]

    G = nx.Graph()

    # Add stations
    G.add_nodes_from(graph_data["nodes"])

    # Add railway connections
    G.add_edges_from(graph_data["edges"])

    return G

graph = build_graph_from_state(response)


# =========================
# STATE
# =========================

class RailState(TypedDict):

    weather_risk: dict
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
    for i in response["weather"]:
        rainfall = i["rainfall"]
        temperature = i["temperature"]

        weather_risk = 100 * (0.7 * (min(rainfall / 50, 1)) + 0.3 * min((temperature - 25) / 25,1))
        state["weather_risk"][response["weather"]["track_id"]] = weather_risk
    return state


# =========================
# TRACK AGENT
# =========================

def track_node(state: RailState):
    for i in response["tracks"]:
        track_id = i["track_id"]
        track_health = i["health"]
        weather_risk = state["weather_risk"][track_id]

        risk_score = 100 * (0.7 * (1 - track_health) + 0.3 * weather_risk)

        if risk_score > 80:
            state["closed_tracks"].append(track_id)
        elif risk_score > 40:
            state["track_actions"] = ["reduce_speed"]
        else:
            state["track_actions"] = ["noop"]

    
    return state

# =========================
# ROUTING AGENT
# =========================

def travels_directly(route, station1, station2):
    for i in range(len(route) - 1):
        if route[i] == station1 and route[i + 1] == station2:
            return True
    return False

def routing_node(state: RailState):
    G = graph.copy()
    rerouted_trains=[]
    for closed in state["closed_tracks"] :
        station_to_close = response["track"][closed]
        s1 = station_to_close["source"]
        s2 = station_to_close["destination"]
        


        if G.has_edge(s1,s2):
            G.remove_edge(s1,s2)

      
        
        for t  in trains:
           route = trains[t]
           if travels_directly(route,s1,s2):
               rerouted_trains.append(t)
               
    for i in rerouted_trains:
        
        s1 = trains[i][0]
        s2 = trains[i][-1]
        path = nx.shortest(G,s1,s2)

        state["routing_actions"].append = [{
        
        "train_id": i,
        "route": path,
    
    }]
    return state


# =========================
# PLANNER AGENT
# =========================

def planner_node(state: RailState):


    for i in state["routing_actions"]:

        payload = {i["train_id"],i["route"]}

        if i["type"] == "set_route":
            response = requests.post(f"{BASE_URL}/api/train/reroute/",json=payload)

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

