import sys
import threading
import uuid
from pathlib import Path

# Add the repo root to sys.path so we can import the core railmind engine
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from railmind.data_loader import load_railway
from railmind.graph import build_network_from_data
from railmind.twin import DigitalTwin

# In-memory store for twin sessions. The default twin is built lazily on the
# first request (never at import time) so the server always boots instantly,
# even with no network access.
twin_sessions = {}
# Guards twin_sessions AND every read-modify/snapshot of a twin: a tick or
# mutation racing a deepcopy/dump would raise "dictionary changed size during
# iteration" on the live state dicts.
_twin_lock = threading.Lock()

# Sandbox sessions kept at most (oldest evicted first); the default twin is never evicted.
MAX_SANDBOX_SESSIONS = 16


def _build_default_twin():
    stations, tracks = load_railway("India")
    graph = build_network_from_data(stations, tracks)
    twin = DigitalTwin(graph)
    twin.seed_trains(5)
    return twin


def _session_id(request):
    return request.headers.get("X-Session-ID", "default")


def _resolve_twin(session_id):
    """
    Fetch the twin for a session, creating the default twin on first use.

    The caller MUST already hold _twin_lock: keeping lookup and use in one
    critical section stops a concurrent /api/reset/ from swapping
    twin_sessions between lookup and mutation (which would silently mutate
    an orphaned twin). _twin_lock is a plain Lock (not reentrant), so this
    helper must never try to acquire it itself.

    Args:
        session_id: The session id from the X-Session-ID header.

    Returns:
        DigitalTwin: The digital twin session instance if found, otherwise None.
    """
    if session_id == "default" and "default" not in twin_sessions:
        twin_sessions["default"] = _build_default_twin()
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
    # Living twin: state reads advance the simulation (no background thread)
    with _twin_lock:
        twin = _resolve_twin(_session_id(request))
        if not twin:
            return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)

        twin.maybe_tick()
        state_dump = twin.get_state()

        graph_state = {
            "nodes": list(twin.graph.graph.nodes()),
            "edges": list(twin.graph.graph.edges())
        }

    return Response({
        "weather": state_dump.get("weather", {}),
        "tracks": state_dump.get("tracks", {}),
        "trains": state_dump.get("trains", {}),
        "stations": state_dump.get("stations", {}),
        "graph": graph_state
    })


@api_view(['POST'])
def reset_twin(request):
    """
    Rebuild the default twin from the canonical network data.

    Clears all sandbox sessions. Handy between demo runs.
    """
    with _twin_lock:
        twin_sessions.clear()
        twin_sessions["default"] = _build_default_twin()
    return Response({"status": "success", "message": "Digital twin reset to baseline state."})


@api_view(['POST'])
def copy_twin(request):
    """
    Create a new isolated sandbox session (a parallel future) based on the current state.

    Required for simulating multiple futures. At most MAX_SANDBOX_SESSIONS
    sandbox sessions are kept; creating more evicts the oldest one.

    Example usage:
    ```python
    future = twin.copy()
    ```
    """
    new_session_id = str(uuid.uuid4())
    with _twin_lock:
        twin = _resolve_twin(_session_id(request))
        if not twin:
            return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)

        twin_sessions[new_session_id] = twin.copy()
        sandbox_ids = [sid for sid in twin_sessions if sid != "default"]
        for sid in sandbox_ids[:max(0, len(sandbox_ids) - MAX_SANDBOX_SESSIONS)]:
            del twin_sessions[sid]

    return Response({
        "status": "success",
        "session_id": new_session_id,
        "message": "Future state created successfully.",
        "note": f"At most {MAX_SANDBOX_SESSIONS} sandbox sessions are kept; the oldest is evicted on overflow."
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
    track_id = request.data.get("track_id")
    if not track_id:
        return Response({"error": "Missing track_id"}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(track_id, str):
        return Response({"error": "track_id must be a string"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        with _twin_lock:
            twin = _resolve_twin(_session_id(request))
            if not twin:
                return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)

            if track_id not in twin.state.tracks:
                return Response({"error": f"Track '{track_id}' not found"}, status=status.HTTP_404_NOT_FOUND)
            # Utilizing existing string-based apply_action for closure
            twin.apply_action(f"close_track_{track_id}")
        return Response({"status": "success", "track_id": track_id})
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
def set_weather(request):
    """
    Set the weather condition on a specific track.

    Used by the agent service to inject weather scenarios.

    Example usage:
    ```python
    set_weather("T05", "STORM")
    ```
    """
    track_id = request.data.get("track_id")
    condition = request.data.get("condition", "CLEAR")
    if not track_id:
        return Response({"error": "Missing track_id"}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(track_id, str):
        return Response({"error": "track_id must be a string"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        with _twin_lock:
            twin = _resolve_twin(_session_id(request))
            if not twin:
                return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)

            twin.set_weather(track_id, str(condition).upper())
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({"status": "success", "track_id": track_id, "condition": str(condition).upper()})


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
    source = request.data.get("source")
    destination = request.data.get("destination")

    if not source or not destination:
        return Response({"error": "Missing source or destination"}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(source, str) or not isinstance(destination, str):
        return Response(
            {"error": "source and destination must be station id strings"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        with _twin_lock:
            twin = _resolve_twin(_session_id(request))
            if not twin:
                return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)

            route = twin.graph.find_route(source, destination)
    except ValueError as e:
        return Response({"route": None, "error": str(e)}, status=status.HTTP_404_NOT_FOUND)

    return Response({"route": route, "status": "success"})


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
    train_id = request.data.get("train_id")
    route = request.data.get("route")

    if not train_id or not route:
        return Response({"error": "Missing train_id or invalid route array"}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(train_id, str):
        return Response({"error": "train_id must be a string"}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(route, list) or not all(isinstance(s, str) for s in route):
        return Response(
            {"error": "route must be a list of station id strings"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        with _twin_lock:
            twin = _resolve_twin(_session_id(request))
            if not twin:
                return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)

            if train_id not in twin.state.trains:
                return Response({"error": "Train ID not found"}, status=status.HTTP_404_NOT_FOUND)

            unknown = [s for s in route if s not in twin.state.stations]
            if unknown:
                return Response(
                    {"error": f"Unknown station id(s): {', '.join(unknown)}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            for u, v in zip(route, route[1:]):
                if not twin.graph.graph.has_edge(u, v):
                    return Response(
                        {"error": f"No track connects '{u}' and '{v}'"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            twin.reroute_train(train_id, route)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"status": "success", "train_id": train_id, "route": route})


@api_view(['POST'])
def apply_action(request):
    """
    Apply a generic string-based action command to the twin state.

    Converts plan actions into state changes.

    Example usage:
    ```python
    apply_action("close_track_T14")

    apply_action("reroute_TR00_via_NEW_DELHI_JAIPUR_JUNCT")
    ```
    """
    action = request.data.get("action")
    if not action:
        return Response({"error": "Missing action string"}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(action, str):
        return Response({"error": "action must be a string"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        with _twin_lock:
            twin = _resolve_twin(_session_id(request))
            if not twin:
                return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)

            if action.startswith("close_track_"):
                # The twin raises ValueError (-> 400) on unknown ids;
                # pre-check so unknown tracks 404 instead, consistent with
                # the /api/track/close/ endpoint.
                track_id = action[len("close_track_"):]
                if track_id not in twin.state.tracks:
                    return Response({"error": f"Track '{track_id}' not found"}, status=status.HTTP_404_NOT_FOUND)

            # reroute_* actions run through the core grammar too; the twin
            # raises ValueError on unknown trains/stations or bad routes.
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
    with _twin_lock:
        twin = _resolve_twin(_session_id(request))
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
    with _twin_lock:
        twin = _resolve_twin(_session_id(request))
        if not twin:
            return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)

        risk = twin.calculate_risk()
    return Response({"risk_score": risk})
