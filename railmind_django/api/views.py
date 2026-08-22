import sys
import threading
import uuid
from pathlib import Path

# Add the repo root to sys.path so we can import the core railmind engine
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

from drf_spectacular.utils import extend_schema
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


def _json_object(request):
    """The request body as a dict, or an error response if it is not one
    (DRF hands a JSON array/string/number through as-is)."""
    body = request.data
    if isinstance(body, dict):
        return body, None
    return None, Response({"error": "Request body must be a JSON object"}, status=status.HTTP_400_BAD_REQUEST)


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
        "graph": graph_state,
        "sim_tick": state_dump.get("sim_tick", 0),
        "work_orders": state_dump.get("work_orders", []),
        "crews": state_dump.get("crews", {}),
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


# Upper bound on ticks one /api/tick/ call may play forward; keeps a single
# request from holding the twin lock for long.
MAX_TICKS_PER_REQUEST = 200

WORK_ORDER_TEMPLATES = ("CRITICAL_INCIDENT_RESPONSE", "TRACK_REPAIR")


@api_view(['POST'])
def advance_twin(request):
    """
    Fast-forward the simulation by a number of ticks.

    Field work (work orders) only progresses as simulation time passes;
    this lets the console or an operator play it forward on demand instead
    of waiting on the wall clock behind /api/state/.

    Example usage:
    ```json
    {"ticks": 5}
    ```
    """
    body, error = _json_object(request)
    if error:
        return error
    ticks = body.get("ticks", 1)
    if isinstance(ticks, bool) or not isinstance(ticks, int):
        return Response({"error": "ticks must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
    if ticks < 1 or ticks > MAX_TICKS_PER_REQUEST:
        return Response(
            {"error": f"ticks must be between 1 and {MAX_TICKS_PER_REQUEST}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with _twin_lock:
        twin = _resolve_twin(_session_id(request))
        if not twin:
            return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)

        sim_tick = twin.advance_ticks(ticks)
        work_orders = twin.work_orders_payload()
    return Response({"status": "success", "ticks": ticks, "sim_tick": sim_tick, "work_orders": work_orders})


@extend_schema(methods=["GET"], operation_id="workorders_list")
@extend_schema(methods=["POST"], operation_id="workorders_create")
@api_view(['GET', 'POST'])
def work_orders(request):
    """
    List the twin's work orders, or register a new one.

    A work order is executed by the twin over simulation ticks; its status
    (UNRESOLVED / PARTIAL / BLOCKED / COMPLETE / CANCELLED) is derived from
    the physical state the tasks actually reach, never from this request
    succeeding.

    POST either a template:
    ```json
    {"template": "CRITICAL_INCIDENT_RESPONSE", "track_id": "T23", "incident_id": "INC-001"}
    ```
    or an explicit order:
    ```json
    {"incident_id": "INC-001", "target": "T23",
     "tasks": [
       {"id": "task_1", "action": "CLOSE_TRACK", "target": "T23", "ticks_required": 1},
       {"id": "task_2", "action": "DISPATCH_CREW", "target": "T23", "ticks_required": 10,
        "depends_on": ["task_1"]},
       {"id": "task_3", "action": "REPAIR_TRACK", "target": "T23", "ticks_required": 20,
        "depends_on": ["task_1", "task_2"]}
     ]}
    ```
    Actions: CLOSE_TRACK, REROUTE_TRAIN, SPEED_RESTRICT, DISPATCH_CREW,
    REPAIR_TRACK, RESTORE_SIGNAL. Optional per-task `params`: `route` /
    `destination` (REROUTE_TRAIN), `speed_kmh` (SPEED_RESTRICT),
    `restored_health` (REPAIR_TRACK). Optional `auto_retry` on the order.
    """
    if request.method == 'GET':
        with _twin_lock:
            twin = _resolve_twin(_session_id(request))
            if not twin:
                return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
            payload = twin.work_orders_payload()
            sim_tick = twin.sim_tick
        return Response({"sim_tick": sim_tick, "work_orders": payload})

    body, error = _json_object(request)
    if error:
        return error

    template = body.get("template")
    if template is not None:
        if template not in WORK_ORDER_TEMPLATES:
            return Response(
                {"error": f"Unknown template '{template}'; expected one of {list(WORK_ORDER_TEMPLATES)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        track_id = body.get("track_id") or body.get("target")
        if not track_id or not isinstance(track_id, str):
            return Response({"error": "Missing track_id"}, status=status.HTTP_400_BAD_REQUEST)
    else:
        target = body.get("target")
        if not target or not isinstance(target, str):
            return Response({"error": "Missing target"}, status=status.HTTP_400_BAD_REQUEST)
        tasks = body.get("tasks")
        if not isinstance(tasks, list) or not tasks or not all(isinstance(t, dict) for t in tasks):
            return Response(
                {"error": "tasks must be a non-empty list of task objects"},
                status=status.HTTP_400_BAD_REQUEST,
            )

    incident_id = body.get("incident_id")
    if incident_id is not None and not isinstance(incident_id, str):
        return Response({"error": "incident_id must be a string"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        with _twin_lock:
            twin = _resolve_twin(_session_id(request))
            if not twin:
                return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)

            if template is not None:
                if track_id not in twin.state.tracks:
                    return Response({"error": f"Track '{track_id}' not found"}, status=status.HTTP_404_NOT_FOUND)
                wo = twin.create_incident_response(
                    track_id, incident_id=incident_id, work_order_id=body.get("id") or None
                )
            else:
                wo = twin.register_work_order({
                    "id": body.get("id") or None,
                    "incident_id": incident_id,
                    "type": body.get("type") or "CRITICAL_INCIDENT_RESPONSE",
                    "target": target,
                    "tasks": tasks,
                    "auto_retry": bool(body.get("auto_retry", False)),
                })
            payload = wo.payload()
    except ValueError as e:
        # Covers pydantic validation errors too (a ValueError subclass)
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({"status": "success", "work_order": payload}, status=status.HTTP_201_CREATED)


@extend_schema(operation_id="workorders_detail")
@api_view(['GET'])
def work_order_detail(request, work_order_id):
    """
    What is happening to one work order right now: its aggregate status,
    completion percentage, ETA in ticks, every task with its progress and
    blocking reason, and the most recent execution events.
    """
    with _twin_lock:
        twin = _resolve_twin(_session_id(request))
        if not twin:
            return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
        try:
            payload = twin.work_order_payload(work_order_id)
        except KeyError:
            return Response({"error": f"Work order '{work_order_id}' not found"}, status=status.HTTP_404_NOT_FOUND)
        sim_tick = twin.sim_tick
    return Response({"sim_tick": sim_tick, "work_order": payload})


@api_view(['POST'])
def work_order_cancel(request, work_order_id):
    """
    Cancel a work order. Tasks in flight are aborted and the assets they
    were working on are put back (a CLOSING track reopens, a crew en route
    is recalled); finished work stays as it is.
    """
    body, error = _json_object(request)
    if error:
        return error
    reason = body.get("reason", "Cancelled by operator")
    if not isinstance(reason, str) or not reason.strip():
        return Response({"error": "reason must be a non-empty string"}, status=status.HTTP_400_BAD_REQUEST)

    with _twin_lock:
        twin = _resolve_twin(_session_id(request))
        if not twin:
            return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
        try:
            wo = twin.cancel_work_order(work_order_id, reason=reason.strip())
        except KeyError:
            return Response({"error": f"Work order '{work_order_id}' not found"}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        payload = wo.payload()
    return Response({"status": "success", "work_order": payload})


@api_view(['POST'])
def work_order_retry(request, work_order_id):
    """
    Move BLOCKED tasks back to PENDING so the twin re-evaluates them on the
    next tick. Pass `task_id` to retry one task, or omit it to retry every
    blocked task in the order.
    """
    body, error = _json_object(request)
    if error:
        return error
    task_id = body.get("task_id")
    if task_id is not None and not isinstance(task_id, str):
        return Response({"error": "task_id must be a string"}, status=status.HTTP_400_BAD_REQUEST)

    with _twin_lock:
        twin = _resolve_twin(_session_id(request))
        if not twin:
            return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
        try:
            retried = twin.retry_task(work_order_id, task_id)
            payload = twin.work_order_payload(work_order_id)
        except KeyError:
            return Response({"error": f"Work order '{work_order_id}' not found"}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"status": "success", "retried": retried, "work_order": payload})
