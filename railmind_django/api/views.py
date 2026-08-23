import sys
import threading
import uuid
import hashlib
from datetime import datetime, timezone
from pathlib import Path

# Add the repo root to sys.path so we can import the core railmind engine
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction
from django.db import connection
from django.core.management import call_command
from django.utils import timezone as django_timezone

from railmind.data_loader import load_railway
from railmind.graph import build_network_from_data
from railmind.twin import DigitalTwin
from .models import FieldTask as PersistedFieldTask
from .models import WorkOrder as PersistedWorkOrder
from .models import WorkOrderEvent as PersistedWorkOrderEvent

# In-memory store for twin sessions. The default twin is built lazily on the
# first request (never at import time) so the server always boots instantly,
# even with no network access.
twin_sessions = {}
# Guards twin_sessions AND every read-modify/snapshot of a twin: a tick or
# mutation racing a deepcopy/dump would raise "dictionary changed size during
# iteration" on the live state dicts.
_twin_lock = threading.Lock()
_persistence_lock = threading.Lock()
_persistence_ready = False


def _ensure_work_order_tables():
    """Apply this app's migration lazily for the project's zero-setup mode.

    Deployments should still run ``manage.py migrate`` normally.  The lazy
    guard keeps the existing in-process demo/test client working when it is
    started directly against a fresh SQLite file, while doing nothing once
    the table exists.
    """
    global _persistence_ready
    if _persistence_ready:
        return
    with _persistence_lock:
        if _persistence_ready:
            return
        if PersistedWorkOrder._meta.db_table not in connection.introspection.table_names():
            call_command("migrate", "api", interactive=False, verbosity=0)
        _persistence_ready = True


def _dt_from_epoch(value):
    """Twin timestamps are epoch seconds; Django stores timezone-aware UTC."""
    return datetime.fromtimestamp(float(value or 0), tz=timezone.utc)


def _event_key(event):
    raw = "|".join(str(event.get(key, "")) for key in ("tick", "kind", "task_id", "detail"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _persist_work_order(session_id, payload):
    """Upsert the durable record after the twin accepted or advanced work.

    The payload is produced by the twin, rather than trusting request input,
    so durable progress, status and history always describe verified execution.
    """
    _ensure_work_order_tables()
    with transaction.atomic():
        record, _ = PersistedWorkOrder.objects.update_or_create(
            work_order_id=payload["id"],
            defaults={
                "session_id": session_id,
                "incident_id": payload.get("incident_id"),
                "order_type": payload.get("type") or "CRITICAL_INCIDENT_RESPONSE",
                "target": payload.get("target") or "",
                "status": payload.get("status") or "UNRESOLVED",
                "completion_percentage": int(payload.get("completion_percentage") or 0),
                "estimated_ticks_remaining": int(payload.get("estimated_ticks_remaining") or 0),
                "created_tick": int(payload.get("created_tick") or 0),
                "cancelled": bool(payload.get("cancelled")),
                "cancelled_tick": payload.get("cancelled_tick"),
                "cancel_reason": payload.get("cancel_reason"),
                "auto_retry": bool(payload.get("auto_retry")),
                "created_at": _dt_from_epoch(payload.get("created_at")),
                "metadata": {"runtime_payload_version": 1},
            },
        )
        seen_task_ids = []
        for task in payload.get("tasks") or []:
            task_id = str(task.get("id"))
            seen_task_ids.append(task_id)
            PersistedFieldTask.objects.update_or_create(
                work_order=record,
                task_id=task_id,
                defaults={
                    "action": task.get("action") or "",
                    "target": task.get("target") or "",
                    "status": task.get("status") or "PENDING",
                    "ticks_required": int(task.get("ticks_required") or 1),
                    "ticks_remaining": int(task.get("ticks_remaining") or 0),
                    "progress": float(task.get("progress") or 0.0),
                    "dependencies": list(task.get("depends_on") or task.get("dependencies") or []),
                    "blocking_reason": task.get("blocking_reason"),
                    "params": dict(task.get("params") or {}),
                    "task_metadata": dict(task.get("metadata") or {}),
                    "crew_type": task.get("crew_type"),
                    "detail": task.get("detail") or "",
                    "started_tick": task.get("started_tick"),
                    "completed_tick": task.get("completed_tick"),
                },
            )
        record.field_tasks.exclude(task_id__in=seen_task_ids).delete()
        for event in payload.get("events") or []:
            PersistedWorkOrderEvent.objects.get_or_create(
                work_order=record,
                event_key=_event_key(event),
                defaults={
                    "tick": int(event.get("tick") or 0),
                    "kind": event.get("kind") or "status",
                    "task_id": event.get("task_id"),
                    "detail": event.get("detail") or "",
                },
            )
    return record


def _twin_payload(twin, work_order_id):
    """Use the full retained event history for persistence, not the console's
    shortened display slice."""
    return twin.get_work_order(work_order_id).payload(max_events=200)


def _persist_twin_work_orders(session_id, twin):
    _ensure_work_order_tables()
    for work_order_id in list(twin.work_orders):
        _persist_work_order(session_id, _twin_payload(twin, work_order_id))


def _persisted_payload(record):
    """Render a durable record with the same shape as a live twin payload."""
    tasks = list(record.field_tasks.order_by("id"))
    return {
        "id": record.work_order_id,
        "incident_id": record.incident_id,
        "type": record.order_type,
        "target": record.target,
        "status": record.status,
        "completion_percentage": record.completion_percentage,
        "estimated_ticks_remaining": record.estimated_ticks_remaining,
        "created_tick": record.created_tick,
        "created_at": record.created_at.timestamp(),
        "cancelled": record.cancelled,
        "cancel_reason": record.cancel_reason,
        "auto_retry": record.auto_retry,
        "tasks": [{
            "id": task.task_id,
            "action": task.action,
            "target": task.target,
            "status": task.status,
            "ticks_required": task.ticks_required,
            "ticks_remaining": task.ticks_remaining,
            "progress": round(task.progress, 3),
            "depends_on": task.dependencies,
            "dependencies": task.dependencies,
            "blocking_reason": task.blocking_reason,
            "params": task.params,
            "metadata": task.task_metadata,
            "crew_type": task.crew_type,
            "detail": task.detail,
            "started_tick": task.started_tick,
            "completed_tick": task.completed_tick,
        } for task in tasks],
        "events": [{
            "tick": event.tick,
            "kind": event.kind,
            "task_id": event.task_id,
            "detail": event.detail,
        } for event in record.history.all()],
        "persisted": True,
        "updated_at": django_timezone.localtime(record.updated_at).isoformat(),
    }


def _work_order_response(payload, accepted=False):
    """Modern API contract while retaining the old nested work_order body."""
    return {
        "work_order_id": payload["id"],
        "incident_id": payload.get("incident_id"),
        "status": payload.get("status"),
        "created_at": payload.get("created_at"),
        "completion_percentage": payload.get("completion_percentage", 0),
        "estimated_ticks_remaining": payload.get("estimated_ticks_remaining", 0),
        "task_summary": {
            "total": len(payload.get("tasks") or []),
            "by_status": {
                task_status: sum(1 for task in payload.get("tasks") or [] if task.get("status") == task_status)
                for task_status in ("PENDING", "IN_PROGRESS", "COMPLETED", "BLOCKED", "UNRESOLVED", "CANCELLED")
            },
        },
        "work_order": payload,
    }

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
        _persist_twin_work_orders(_session_id(request), twin)
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
    _ensure_work_order_tables()
    with _twin_lock:
        twin_sessions.clear()
        twin_sessions["default"] = _build_default_twin()
    # Reset is an explicit new scenario, so its durable execution history is
    # intentionally cleared too. Ordinary process restarts do not do this.
    PersistedWorkOrder.objects.all().delete()
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
        _persist_twin_work_orders(_session_id(request), twin)
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
        _ensure_work_order_tables()
        with _twin_lock:
            twin = _resolve_twin(_session_id(request))
            if not twin:
                return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
            twin.maybe_tick()
            _persist_twin_work_orders(_session_id(request), twin)
            payload = twin.work_orders_payload()
            sim_tick = twin.sim_tick
        # The live twin wins if present. Orders from before a process restart
        # remain retrievable from the database even though no in-memory twin
        # has yet rehydrated them.
        live_ids = {order["id"] for order in payload}
        persisted = [
            _persisted_payload(record)
            for record in PersistedWorkOrder.objects.filter(session_id=_session_id(request))
            if record.work_order_id not in live_ids
        ]
        payload.extend(persisted)
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
                if body.get("work_order_id") and not body.get("id"):
                    body["id"] = body["work_order_id"]
                wo = twin.register_work_order({
                    "id": body.get("id") or None,
                    "incident_id": incident_id,
                    "type": body.get("type") or "CRITICAL_INCIDENT_RESPONSE",
                    "target": target,
                    "tasks": tasks,
                    "auto_retry": bool(body.get("auto_retry", False)),
                })
            payload = wo.payload()
            _persist_work_order(_session_id(request), _twin_payload(twin, wo.id))
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
    _ensure_work_order_tables()
    with _twin_lock:
        twin = _resolve_twin(_session_id(request))
        if not twin:
            return Response({"error": "Invalid session"}, status=status.HTTP_404_NOT_FOUND)
        try:
            twin.maybe_tick()
            payload = twin.work_order_payload(work_order_id)
            _persist_work_order(_session_id(request), _twin_payload(twin, work_order_id))
        except KeyError:
            try:
                record = PersistedWorkOrder.objects.get(
                    work_order_id=work_order_id, session_id=_session_id(request)
                )
            except PersistedWorkOrder.DoesNotExist:
                return Response({"error": f"Work order '{work_order_id}' not found"}, status=status.HTTP_404_NOT_FOUND)
            payload = _persisted_payload(record)
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
        _persist_work_order(_session_id(request), _twin_payload(twin, wo.id))
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
        _persist_work_order(_session_id(request), _twin_payload(twin, work_order_id))
    return Response({"status": "success", "retried": retried, "work_order": payload})


# ---------------------------------------------------------------------------
# Modern hyphenated API contract.  The original /workorders/ routes above
# remain untouched for the console and existing integrations.  These aliases
# deliberately return 202 on creation: accepting field work is not claiming
# it completed; polling this resource or advancing the controlled tick loop
# reveals the actual execution state.
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
def work_orders_modern(request):
    legacy = work_orders(request)
    if legacy.status_code >= status.HTTP_400_BAD_REQUEST:
        return legacy
    if request.method == 'GET':
        return Response({"work_orders": legacy.data.get("work_orders", []),
                         "sim_tick": legacy.data.get("sim_tick", 0)})
    payload = legacy.data["work_order"]
    return Response(_work_order_response(payload, accepted=True), status=status.HTTP_202_ACCEPTED)


@api_view(['GET'])
def work_order_detail_modern(request, work_order_id):
    legacy = work_order_detail(request, work_order_id)
    if legacy.status_code >= status.HTTP_400_BAD_REQUEST:
        return legacy
    payload = legacy.data["work_order"]
    body = _work_order_response(payload)
    body["sim_tick"] = legacy.data.get("sim_tick", 0)
    return Response(body)


@api_view(['POST'])
def work_order_cancel_modern(request, work_order_id):
    legacy = work_order_cancel(request, work_order_id)
    if legacy.status_code >= status.HTTP_400_BAD_REQUEST:
        return legacy
    body = _work_order_response(legacy.data["work_order"])
    body["status"] = "CANCELLED"
    return Response(body)
