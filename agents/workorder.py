"""Deterministic WorkOrder construction for the RailMind agent pipeline.

The agent decides *what* work should be committed. The backend owns execution and
verification. This module only turns a selected plan into a transport-safe,
ordered execution object.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import time
import uuid


ACTION_DURATION = {
    "CLOSE_TRACK": 0,
    "REROUTE_TRAIN": 2,
    "DISPATCH_CREW": 10,
    "REPAIR_TRACK": 20,
    "RESTORE_SIGNAL": 10,
    "SPEED_RESTRICT": 0,
    "MONITOR": 0,
}


def _task(task_id: str, action: str, target: str, *, estimated_ticks: int = 0,
          depends_on: Optional[List[str]] = None, crew_type: Optional[str] = None,
          metadata: Optional[Dict[str, Any]] = None) -> dict:
    return {
        "id": task_id,
        "action": action,
        "target": target,
        "estimated_ticks": int(estimated_ticks),
        "depends_on": list(depends_on or []),
        "crew_type": crew_type,
        "status": "PENDING",
        "metadata": metadata or {},
    }


def build_work_order(plan: dict, *, incident_id: Optional[str] = None,
                     field_requirements: Optional[List[dict]] = None,
                     resources: Optional[dict] = None) -> dict:
    """Convert selected plan actions + deterministic field requirements to a WorkOrder."""
    resources = resources or {"repair_crews": 2, "signal_crews": 1}
    tasks: List[dict] = []
    close_tasks: Dict[str, str] = {}
    dispatch_tasks: Dict[str, str] = {}

    for action in plan.get("actions", []):
        if not isinstance(action, dict):
            continue
        sid = action.get("strategy_id", "")
        if sid.startswith("T_CLOSE_"):
            target = action.get("track_id") or sid.removeprefix("T_CLOSE_")
            tid = f"task_{len(tasks)+1}"
            tasks.append(_task(tid, "CLOSE_TRACK", target))
            close_tasks[target] = tid
        elif sid.startswith("R_REROUTE_"):
            target = action.get("train_id", "")
            tid = f"task_{len(tasks)+1}"
            tasks.append(_task(tid, "REROUTE_TRAIN", target, estimated_ticks=2,
                               metadata={"new_route": action.get("new_route", [])}))
        elif sid.startswith("R_HOLD_"):
            target = action.get("train_id", "")
            tid = f"task_{len(tasks)+1}"
            tasks.append(_task(tid, "HOLD_TRAIN", target, estimated_ticks=1))

    # Field requirements are intentionally separate from operational actions.
    # They are not inferred from LLM text; they come from specialist agents.
    for req in field_requirements or []:
        if not req.get("required"):
            continue
        target = req.get("target") or req.get("track") or req.get("signal")
        action = req.get("action")
        if not target or not action:
            continue
        action = str(action).upper()
        if action == "REPAIR_TRACK":
            close_dep = close_tasks.get(target)
            dispatch_id = f"task_{len(tasks)+1}"
            deps = [close_dep] if close_dep else []
            tasks.append(_task(dispatch_id, "DISPATCH_CREW", target,
                               estimated_ticks=10, depends_on=deps,
                               crew_type="repair",
                               metadata={"resource_pool": "repair_crews"}))
            dispatch_tasks[target] = dispatch_id
            repair_id = f"task_{len(tasks)+1}"
            tasks.append(_task(repair_id, "REPAIR_TRACK", target,
                               estimated_ticks=int(req.get("estimated_ticks", 20)),
                               depends_on=[dispatch_id], crew_type="repair",
                               metadata={"resource_pool": "repair_crews"}))
        elif action == "RESTORE_SIGNAL":
            deps = [close_tasks[target]] if target in close_tasks else []
            tasks.append(_task(f"task_{len(tasks)+1}", "RESTORE_SIGNAL", target,
                               estimated_ticks=int(req.get("estimated_ticks", 10)),
                               depends_on=deps, crew_type="signal",
                               metadata={"resource_pool": "signal_crews"}))

    # Safety ordering: operational reroutes and field work occur only after
    # the plan's closure actions have been committed. This is intentionally
    # conservative; the backend enforces these dependencies.
    close_ids = list(close_tasks.values())
    for task in tasks:
        if task["action"] in ("REROUTE_TRAIN", "RESTORE_SIGNAL"):
            for close_id in close_ids:
                if close_id != task["id"] and close_id not in task["depends_on"]:
                    task["depends_on"].append(close_id)

    work_order_id = f"WO-{uuid.uuid4().hex[:8].upper()}"
    return {
        "work_order_id": work_order_id,
        "type": "CRITICAL_INCIDENT_RESPONSE",
        "incident_id": incident_id,
        "plan_id": plan.get("plan_id"),
        "priority": plan.get("priority", "HIGH"),
        "status": "PENDING",
        "created_at": time.time(),
        "resources": resources,
        "tasks": tasks,
        "ettr_ticks": int(plan.get("ettr_ticks", 0)),
        "source": "RailMind Master Agent",
    }
