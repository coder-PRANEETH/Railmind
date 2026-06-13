import sys
from pathlib import Path
import uuid

# Add the parent directory to sys.path so we can import the core railmind engine
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(BASE_DIR))

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from railmind.data_loader import load_railway
from railmind.graph import build_network_from_data
from railmind.twin import DigitalTwin

# Initialize Global MVP State
stations, tracks = load_railway("India")
graph = build_network_from_data(stations, tracks)
global_twin = DigitalTwin(graph)
global_twin.seed_trains(5)

# In-memory store for cloned futures
twin_sessions = {
    "default": global_twin
}

def get_twin(request):
    """
    Helper to fetch the twin context for a given session.
    
    Args:
        request: The HTTP request object containing headers.
        
    Returns:
        DigitalTwin: The digital twin session instance if found, otherwise None.
    """
    session_id = request.headers.get("X-Session-ID", "default")
    return twin_sessions.get(session_id)

@api_view(['GET'])
def get_state(request):
    """
    Retrieve the current state of the Digital Twin network.
    
    This includes weather conditions, track statuses, train locations, and the graph structure.
    Used by all agents.
    
    Example usage:
    ```python
    get_state()
    ```
    
    Returns:
    ```json
    {
        "weather": {},
        "tracks": {},
        "trains": {},
        "graph": {}
    }
    ```
    """
    twin = get_twin(request)
    if not twin:
        return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
    
    state_dump = twin.get_state()
    
    graph_state = {
        "nodes": list(twin.graph.graph.nodes()),
        "edges": list(twin.graph.graph.edges())
    }
    
    return Response({
        "weather": state_dump.get("weather", {}),
        "tracks": state_dump.get("tracks", {}),
        "trains": state_dump.get("trains", {}),
        "graph": graph_state
    })

@api_view(['POST'])
def copy_twin(request):
    """
    Create a new isolated sandbox session (a parallel future) based on the current state.
    
    Required for simulating multiple futures.
    
    Example usage:
    ```python
    future = twin.copy()
    ```
    """
    twin = get_twin(request)
    if not twin:
        return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
    
    new_twin = twin.copy()
    new_session_id = str(uuid.uuid4())
    twin_sessions[new_session_id] = new_twin
    
    return Response({
        "status": "success", 
        "session_id": new_session_id,
        "message": "Future state created successfully."
    })

@api_view(['POST'])
def close_track(request):
    """
    Close a specific railway track due to maintenance or emergency.
    
    Used by Track Agent.
    
    Example usage:
    ```python
    close_track("T14")
    ```
    """
    twin = get_twin(request)
    if not twin:
        return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
        
    track_id = request.data.get("track_id")
    if not track_id:
        return Response({"error": "Missing track_id"}, status=status.HTTP_400_BAD_REQUEST)
        
    try:
        # Utilizing existing string-based apply_action for closure
        twin.apply_action(f"close_track_{track_id}")
        return Response({"status": "success", "track_id": track_id})
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
def find_route(request):
    """
    Calculate the optimal route between a source and a destination station.
    
    Returns alternative route while avoiding closed tracks.
    Used by Routing Agent.
    
    Example usage:
    ```python
    find_route("A", "C")
    ```
    """
    twin = get_twin(request)
    if not twin:
        return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
        
    source = request.data.get("source")
    destination = request.data.get("destination")
    
    if not source or not destination:
        return Response({"error": "Missing source or destination"}, status=status.HTTP_400_BAD_REQUEST)
        
    route = twin.graph.find_route(source, destination)
    
    if route:
        return Response({"route": route, "status": "success"})
    else:
        return Response({"route": None, "error": "No viable route found"}, status=status.HTTP_404_NOT_FOUND)

@api_view(['POST'])
def reroute_train(request):
    """
    Manually assign a new route to an existing train.
    
    Assigns a new route.
    Used by Planner/Simulation.
    
    Example usage:
    ```python
    reroute_train("TR01", ["A", "B", "C"])
    ```
    """
    twin = get_twin(request)
    if not twin:
        return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
        
    train_id = request.data.get("train_id")
    route = request.data.get("route")
    
    if not train_id or not route or not isinstance(route, list):
        return Response({"error": "Missing train_id or invalid route array"}, status=status.HTTP_400_BAD_REQUEST)
        
    if train_id not in twin.state.trains:
        return Response({"error": "Train ID not found"}, status=status.HTTP_404_NOT_FOUND)
        
    # Directly manipulate twin state for MVP
    twin.state.trains[train_id].route = route
    return Response({"status": "success", "train_id": train_id, "route": route})

@api_view(['POST'])
def apply_action(request):
    """
    Apply a generic string-based action command to the twin state.
    
    Converts plan actions into state changes.
    
    Example usage:
    ```python
    apply_action("close_track_T14")
    
    apply_action("reroute_via_route_A")
    ```
    """
    twin = get_twin(request)
    if not twin:
        return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
        
    action = request.data.get("action")
    if not action:
        return Response({"error": "Missing action string"}, status=status.HTTP_400_BAD_REQUEST)
        
    try:
        if action.startswith("reroute_"):
            # Mock fallback for string-based rerouting
            return Response({"status": "acknowledged", "action": action, "note": "Use reroute_train endpoint for concrete routing."})
            
        twin.apply_action(action)
        return Response({"status": "success", "action": action})
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

@api_view(['GET'])
def calculate_delay(request):
    """
    Calculate the current total delay of the network based on train speeds and track closures.
    
    Returns:
    ```python
    delay_minutes
    ```
    """
    twin = get_twin(request)
    if not twin:
        return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
        
    delay = twin.calculate_delay()
    return Response({"delay_minutes": delay})

@api_view(['GET'])
def calculate_risk(request):
    """
    Calculate the current operational risk score of the network.
    
    Factors such as track health and bad weather contribute to the risk score.
    
    Returns:
    ```python
    risk_score
    ```
    """
    twin = get_twin(request)
    if not twin:
        return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
        
    risk = twin.calculate_risk()
    return Response({"risk_score": risk})
