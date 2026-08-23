"""Deterministic, twin-compatible WorkOrder construction.

The planner may reason in strategy IDs, but the object crossing the
Agent → Backend boundary must be accepted unchanged by
``DigitalTwin.register_work_order``.  This module intentionally contains no
execution status: the twin is the only component allowed to add progress or
claim that a task completed.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Iterable, List, Optional


ACTION_DURATION = {
    "CLOSE_TRACK": 1,
    "REROUTE_TRAIN": 2,
    "HOLD_TRAIN": 1,
    "SPEED_RESTRICT": 1,
    "MONITOR": 1,
    "DISPATCH_CREW": 10,
    "REPAIR_TRACK": 20,
    "RESTORE_SIGNAL": 8,
}


def _task(task_id: str, action: str, target: str, *, ticks_required: Optional[int] = None,
          depends_on: Optional[Iterable[str]] = None, params: Optional[Dict[str, Any]] = None,
          crew_type: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> dict:
    """Return the public DigitalTwin task schema.

    ``depends_on`` remains the canonical API spelling to preserve the live
    twin API. The twin also accepts ``dependencies`` on input and exposes
    both spellings on read, allowing integrations to migrate gradually.
    """
    return {
        "id": task_id,
        "action": action,
        "target": target,
        "ticks_required": int(ticks_required or ACTION_DURATION[action]),
        "depends_on": list(depends_on or []),
        "params": dict(params or {}),
        "crew_type": crew_type,
        "metadata": dict(metadata or {}),
    }


def _append(tasks: List[dict], action: str, target: str, **kwargs: Any) -> str:
    task_id = f"task_{len(tasks) + 1}"
    tasks.append(_task(task_id, action, target, **kwargs))
    return task_id


def _add_unique(tasks: List[dict], action: str, target: str, **kwargs: Any) -> Optional[str]:
    """Add one operational task per action/target pair and return its id."""
    for task in tasks:
        if task["action"] == action and task["target"] == target:
            return task["id"]
    return _append(tasks, action, target, **kwargs)


def build_work_order(plan: dict, *, incident_id: Optional[str] = None,
                     field_requirements: Optional[List[dict]] = None,
                     resources: Optional[dict] = None) -> dict:
    """Convert a selected A/B/C plan into one executable WorkOrder proposal.

    No legacy ``estimated_ticks``, status, or free-form action is emitted:
    every task is a valid ``TaskAction`` and can be posted directly to the
    Django twin endpoint. Resource information remains as audit metadata;
    the live twin enforces allocation when a dispatch task starts.
    """
    resources = dict(resources or {"repair_crews": 2, "signal_crews": 1})
    tasks: List[dict] = []
    close_tasks: Dict[str, str] = {}

    for strategy in plan.get("actions", []):
        if not isinstance(strategy, dict):
            continue
        sid = str(strategy.get("strategy_id", ""))
        target = strategy.get("track_id")
        if sid.startswith("T_CLOSE_"):
            target = target or sid.removeprefix("T_CLOSE_")
            if target:
                close_tasks[target] = _add_unique(tasks, "CLOSE_TRACK", target) or ""
        elif sid.startswith("T_RESTRICT_") and target:
            _add_unique(tasks, "SPEED_RESTRICT", target, params={"speed_kmh": 60})
        elif sid.startswith("T_MONITOR_") and target:
            _add_unique(tasks, "MONITOR", target)
        elif sid.startswith("R_REROUTE_") and strategy.get("train_id"):
            _add_unique(tasks, "REROUTE_TRAIN", strategy["train_id"],
                        params={"route": list(strategy.get("new_route") or [])})
        elif sid.startswith("R_HOLD_") and strategy.get("train_id"):
            _add_unique(tasks, "HOLD_TRAIN", strategy["train_id"])
        elif sid.startswith("W"):
            for operational_action in strategy.get("actions", []):
                if operational_action.startswith("close_track_"):
                    weather_target = operational_action.removeprefix("close_track_").split("+")[0]
                    if weather_target:
                        close_tasks[weather_target] = _add_unique(
                            tasks, "CLOSE_TRACK", weather_target
                        ) or ""
                elif operational_action.startswith("reduce_speed_40kmh_"):
                    weather_target = operational_action.removeprefix("reduce_speed_40kmh_")
                    _add_unique(tasks, "SPEED_RESTRICT", weather_target, params={"speed_kmh": 40})
                elif operational_action.startswith("reduce_speed_60kmh_"):
                    weather_target = operational_action.removeprefix("reduce_speed_60kmh_")
                    _add_unique(tasks, "SPEED_RESTRICT", weather_target, params={"speed_kmh": 60})

    # Required physical interventions get a capacity-accounted crew dispatch
    # and an explicit dependency chain. This is not inferred from prose.
    for requirement in field_requirements or []:
        if not requirement.get("required"):
            continue
        target = requirement.get("target") or requirement.get("track") or requirement.get("signal")
        action = str(requirement.get("action", "")).upper()
        if not target or action not in {"REPAIR_TRACK", "RESTORE_SIGNAL"}:
            continue
        crew_type = "repair" if action == "REPAIR_TRACK" else "signal"
        pool = f"{crew_type}_crews"
        prior = [close_tasks[target]] if close_tasks.get(target) else []
        dispatch_id = _append(
            tasks, "DISPATCH_CREW", target, depends_on=prior, crew_type=crew_type,
            metadata={"resource_pool": pool},
        )
        _append(
            tasks, action, target,
            ticks_required=int(requirement.get("estimated_ticks") or ACTION_DURATION[action]),
            depends_on=[dispatch_id], crew_type=crew_type,
            metadata={"resource_pool": pool, "requirement": dict(requirement)},
        )

    # Reroutes and holds wait for planned closures, maintaining a safe order
    # without serialising unrelated field work.
    close_ids = [task_id for task_id in close_tasks.values() if task_id]
    for task in tasks:
        if task["action"] in {"REROUTE_TRAIN", "HOLD_TRAIN"}:
            task["depends_on"] = list(dict.fromkeys(task["depends_on"] + close_ids))

    work_order_id = f"WO-{uuid.uuid4().hex[:8].upper()}"
    return {
        # ``id`` is authoritative for the DigitalTwin / Django API;
        # ``work_order_id`` remains for the current console response shape.
        "id": work_order_id,
        "work_order_id": work_order_id,
        "incident_id": incident_id,
        "type": "CRITICAL_INCIDENT_RESPONSE",
        "target": next((t["target"] for t in tasks if t["action"] in {"CLOSE_TRACK", "REPAIR_TRACK"}),
                       (tasks[0]["target"] if tasks else "NETWORK")),
        "tasks": tasks,
        "auto_retry": False,
        "priority": plan.get("priority", "HIGH"),
        "plan_id": plan.get("plan_id"),
        "resources": resources,
        "ettr_ticks": int(plan.get("ettr_ticks", 0)),
        "created_at": time.time(),
        "source": "RailMind Master Agent",
        "status": "PROPOSED",
    }
