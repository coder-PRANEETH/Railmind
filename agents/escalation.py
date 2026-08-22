"""escalation.py — RailMind escalation ladder and completion signal.

Two questions this module answers, and nothing else:

  1. HOW URGENT IS THIS?   An incident nobody resolves must not sit at the same
     handling level forever. Every open incident carries a tier (L0..L4); the
     tier rises when the network deteriorates *or* when the clock runs out on
     the current handling level, and falls again when the situation genuinely
     recovers. Each movement is recorded as an escalation event with the reason
     that caused it, so the ladder is auditable rather than a mood.

  2. IS THE RESPONSE ACTUALLY DONE?   Every incident reports one of four
     states — COMPLETE, PARTIAL, BLOCKED, UNRESOLVED — derived from the live
     twin, never from the fact that a button was pressed. An executed plan
     whose reroutes silently failed reads PARTIAL, not COMPLETE.

Design constraints inherited from the rest of RailMind:
  * Pure functions plus one in-memory ledger; no dependency beyond networkx,
    which the orchestrator already requires.
  * Deterministic: every time-dependent input arrives as an explicit `now`
    argument so tests never race the wall clock.
  * Offline-first: this module performs no I/O. Callers hand it snapshots.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

import networkx as nx

# ─────────────────────────────────────────────────────────────────────────────
# TIERS
# The ladder is the product decision: who is handling this, and how hard.
# `sla_seconds` is how long an incident may sit at that tier before dwell alone
# escalates it (demo-scaled minutes, not railway regulation minutes).
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Tier:
    level: int
    code: str
    label: str
    owner: str
    posture: str
    sla_seconds: Optional[float]


TIERS: tuple = (
    Tier(0, "L0", "Steady State", "Section Controller",
         "Routine monitoring — no active response.", None),
    Tier(1, "L1", "Advisory", "Section Controller",
         "Assets degraded. Watch and restrict; no train is impacted yet.", 240.0),
    Tier(2, "L2", "Elevated", "Divisional Controller",
         "Trains are impacted. Recovery is owned and in progress.", 180.0),
    Tier(3, "L3", "Critical", "Divisional Emergency Cell",
         "Passengers are stranded or failures are compounding. Recovery is the priority task.", 120.0),
    Tier(4, "L4", "Emergency", "Zonal Emergency Control Room",
         "The network cannot recover unaided — corridors are severed or no alternate path exists.", None),
)

TIER_BY_LEVEL: Dict[int, Tier] = {t.level: t for t in TIERS}

# Resolution states — the completion signal.
COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
UNRESOLVED = "UNRESOLVED"
BLOCKED = "BLOCKED"

RESOLUTION_SEVERITY = {COMPLETE: 0, PARTIAL: 1, UNRESOLVED: 2, BLOCKED: 3}

# Work-item states.
WORK_DONE = "DONE"
WORK_PENDING = "PENDING"
WORK_FAILED = "FAILED"
WORK_BLOCKED = "BLOCKED"

# Train dispositions relative to the incident that affected them.
TRAIN_RECOVERED = "RECOVERED"   # moving, and its route avoids every blocked edge
TRAIN_HELD = "HELD"             # physically stopped at a closed section
TRAIN_EXPOSED = "EXPOSED"       # still routed across a blocked edge, not yet stopped
TRAIN_BLOCKED = "BLOCKED"       # no alternate path exists on the live network

SEVERE_WEATHER = ("STORM",)


# ─────────────────────────────────────────────────────────────────────────────
# SNAPSHOT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _condition(value) -> str:
    return (value.value if hasattr(value, "value") else str(value)).upper()


# Track statuses the twin holds traffic at. CLOSING and UNDER_REPAIR are the
# twin's transitional work-order states: a section being rebuilt reads as
# blocking even as its health climbs, so the ledger cannot call an incident
# COMPLETE before the twin has actually reopened the corridor.
IMPASSABLE_STATUSES = ("CLOSED", "CLOSING", "UNDER_REPAIR")


def is_blocking(track: dict) -> bool:
    """A corridor traffic cannot cross: closed by an operator, being worked
    on by a crew, or failed outright."""
    return track.get("status") in IMPASSABLE_STATUSES or track.get("health", 1.0) <= 0.2


def blocked_edges(snapshot: dict) -> set:
    return {
        frozenset({t["source"], t["destination"]})
        for t in snapshot.get("tracks", {}).values()
        if is_blocking(t) and t.get("source") and t.get("destination")
    }


def route_crosses(route: Sequence[str], edges: Iterable) -> bool:
    edge_set = set(edges)
    return any(
        frozenset({route[i], route[i + 1]}) in edge_set
        for i in range(len(route) - 1)
    )


def open_graph(snapshot: dict) -> nx.Graph:
    """The network as trains can actually use it — blocked corridors removed."""
    graph = nx.Graph()
    raw = snapshot.get("graph", {})
    graph.add_nodes_from(raw.get("nodes", []))
    graph.add_edges_from(tuple(e) for e in raw.get("edges", []))
    # A snapshot without a graph block still yields a usable network: tracks
    # carry the same topology, and escalation must never report "no path"
    # merely because the caller trimmed the graph key.
    if graph.number_of_edges() == 0:
        for track in snapshot.get("tracks", {}).values():
            if track.get("source") and track.get("destination"):
                graph.add_edge(track["source"], track["destination"])
    for edge in blocked_edges(snapshot):
        u, v = tuple(edge)
        if graph.has_edge(u, v):
            graph.remove_edge(u, v)
    return graph


def severed_pairs(graph: nx.Graph) -> int:
    """Station pairs with no route between them. Zero on a healthy network."""
    if graph.number_of_nodes() < 2:
        return 0
    total = graph.number_of_nodes()
    reachable = sum(len(c) * (len(c) - 1) // 2 for c in nx.connected_components(graph))
    return total * (total - 1) // 2 - reachable


# Health at which the twin calls a rebuilt section operational (mirrors
# railmind/execution.py REPAIR_ACCEPTANCE_HEALTH; the ledger imports nothing).
REPAIR_ACCEPTANCE_HEALTH = 0.4


def _signals_green(track_id: str, snapshot: dict) -> bool:
    """Both end signals of a section show GREEN on the twin."""
    track = snapshot.get("tracks", {}).get(track_id) or {}
    stations = snapshot.get("stations") or {}
    ends = [s for s in (track.get("source"), track.get("destination")) if s in stations]
    if not ends:
        return _condition(snapshot.get("signals", {}).get(track_id, "")) == "GREEN"
    return all(
        _condition((stations[s].get("active_signals") or {}).get(track_id, "")) == "GREEN"
        for s in ends
    )


def _crew_on_site(track_id: str, snapshot: dict) -> bool:
    crews = snapshot.get("crews") or {}
    if any(isinstance(c, dict) and c.get("target") == track_id and c.get("status") == "ON_SITE"
           for c in crews.values()):
        return True
    # Crews stand down once their order ends; the twin's completed dispatch
    # task is the record that they were there.
    for order in snapshot.get("work_orders") or []:
        for task in (order.get("tasks") or []) if isinstance(order, dict) else []:
            if (isinstance(task, dict) and task.get("action") == "DISPATCH_CREW"
                    and task.get("target") == track_id and task.get("status") == "COMPLETED"):
                return True
    return False


def _path_exists(graph: nx.Graph, train: dict, route: Sequence[str]) -> bool:
    origin = train.get("current_station") or (route[0] if route else None)
    target = route[-1] if route else None
    if not origin or not target or origin not in graph or target not in graph:
        return False
    if origin == target:
        return True
    return nx.has_path(graph, origin, target)


# ─────────────────────────────────────────────────────────────────────────────
# WORK ITEMS
# One row per action the approved plan committed to. Completion is *verified*
# against the twin on every observe() — an apply call that returned 200 but
# whose reroute never landed reads PENDING, and its incident stays PARTIAL.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WorkItem:
    item_id: str
    kind: str                      # close_track | reroute | hold | weather_closure
    label: str
    target: str                    # track id or train id
    incident_id: Optional[str]
    expected_route: List[str] = field(default_factory=list)
    state: str = WORK_PENDING
    detail: str = "Not yet executed."
    plan_id: Optional[str] = None
    dispatched_at: Optional[float] = None
    completed_at: Optional[float] = None

    def payload(self) -> dict:
        return {
            "id": self.item_id,
            "kind": self.kind,
            "label": self.label,
            "target": self.target,
            "incident_id": self.incident_id,
            "state": self.state,
            "detail": self.detail,
            "plan_id": self.plan_id,
        }


def verify_work_item(item: WorkItem, snapshot: dict, graph: nx.Graph, now: float) -> None:
    """Prove or disprove one committed action against the live twin."""
    tracks = snapshot.get("tracks", {})
    trains = snapshot.get("trains", {})

    def settle(state: str, detail: str) -> None:
        item.state = state
        item.detail = detail
        if state == WORK_DONE:
            if item.completed_at is None:
                item.completed_at = now
        else:
            item.completed_at = None

    if item.kind in ("close_track", "weather_closure"):
        track = tracks.get(item.target)
        if track is None:
            settle(WORK_FAILED, f"Track {item.target} is no longer present in the twin.")
        elif is_blocking(track):
            settle(WORK_DONE, f"Track {item.target} confirmed out of service on the twin.")
        elif item.completed_at is not None:
            # The closure was proved earlier and the twin has since restored
            # the corridor (a repair work order reopened it). Restoration is
            # the outcome the closure was for, not evidence it never happened.
            settle(WORK_DONE, f"Track {item.target} closure lifted after restoration.")
        else:
            settle(WORK_PENDING, f"Track {item.target} is still carrying traffic.")
        return

    if item.kind == "reroute":
        train = trains.get(item.target)
        if train is None:
            settle(WORK_FAILED, f"Train {item.target} is no longer present in the twin.")
            return
        route = train.get("route", [])
        blocked = blocked_edges(snapshot)
        if item.expected_route and route == item.expected_route:
            settle(WORK_DONE, f"{item.target} is running the alternate corridor.")
        elif route and not route_crosses(route, blocked) and not train.get("held"):
            # The twin may have turned the train around since dispatch; what
            # matters operationally is that its live route clears the failure.
            settle(WORK_DONE, f"{item.target} is clear of the failed section.")
        elif not _path_exists(graph, train, item.expected_route or route):
            settle(WORK_BLOCKED, f"No open corridor remains for {item.target}.")
        else:
            settle(WORK_PENDING, f"{item.target} has not taken the alternate corridor yet.")
        return

    if item.kind == "repair_track":
        track = tracks.get(item.target)
        if track is None:
            settle(WORK_FAILED, f"Track {item.target} is no longer present in the twin.")
        elif track.get("status") == "OPEN" and track.get("health", 0.0) >= REPAIR_ACCEPTANCE_HEALTH:
            # The twin's own acceptance criterion for a rebuilt section
            settle(WORK_DONE, f"Track {item.target} repair verified on the twin "
                              f"(health {track.get('health', 0.0):.2f}).")
        else:
            settle(WORK_PENDING, f"Track {item.target} repair is not yet verified.")
        return

    if item.kind == "restore_signal":
        if _signals_green(item.target, snapshot):
            settle(WORK_DONE, f"Signals on {item.target} verified GREEN on the twin.")
        else:
            settle(WORK_PENDING, f"Signals on {item.target} are not yet restored.")
        return

    if item.kind == "dispatch_crew":
        # A dispatch is proved by a crew on the section, or by the twin
        # having completed the dispatch task — never by its acceptance.
        if _crew_on_site(item.target, snapshot):
            settle(WORK_DONE, f"Crew on site at {item.target}.")
        else:
            settle(WORK_PENDING, f"Crew for {item.target} is not yet on site.")
        return

    if item.kind == "hold":
        train = trains.get(item.target)
        if train is None:
            settle(WORK_FAILED, f"Train {item.target} is no longer present in the twin.")
        elif train.get("held"):
            settle(WORK_DONE, f"{item.target} is held short of the failed section.")
        elif item.completed_at is not None and not route_crosses(
                train.get("route", []), blocked_edges(snapshot)):
            # The hold was proved earlier and the corridor has since been
            # restored: the train is moving again on a clear route, which
            # is what the hold was protecting it for.
            settle(WORK_DONE, f"{item.target} released after restoration.")
        else:
            settle(WORK_PENDING, f"{item.target} has not been brought to a stand.")
        return

    settle(item.state, item.detail)


# ─────────────────────────────────────────────────────────────────────────────
# FIELD WORK
# The twin reports its work orders inside the snapshot. The one answering an
# incident's corridor is summarised onto the incident so the console and the
# handoff brief can say what is physically happening — without the ledger
# performing any I/O of its own.
# ─────────────────────────────────────────────────────────────────────────────

FIELD_WORK_LIVE = ("UNRESOLVED", "PARTIAL", "BLOCKED")


def field_work_for(incident, work_orders: Sequence[dict]) -> Optional[dict]:
    """Summarise the work order serving this incident, preferring a live one.

    An order matches by incident id, or by targeting the incident's track
    (orders registered before the ledger opened the incident carry no id)."""
    matches = [
        o for o in work_orders
        if isinstance(o, dict) and (
            o.get("incident_id") == incident.incident_id or o.get("target") == incident.track_id
        )
    ]
    if not matches:
        return None
    live = [o for o in matches if o.get("status") in FIELD_WORK_LIVE]
    order = live[-1] if live else matches[-1]
    tasks = [t for t in (order.get("tasks") or []) if isinstance(t, dict)]
    blocked = next((t for t in tasks if t.get("status") == "BLOCKED"), None)
    running = next((t for t in tasks if t.get("status") == "IN_PROGRESS"), None)
    return {
        "id": order.get("id"),
        "status": order.get("status"),
        "completion_percentage": order.get("completion_percentage", 0),
        "estimated_ticks_remaining": order.get("estimated_ticks_remaining", 0),
        "blocked_reason": blocked.get("blocking_reason") if blocked else None,
        "current": running.get("detail") if running else None,
        "tasks": [
            {
                "id": t.get("id"),
                "action": t.get("action"),
                "status": t.get("status"),
                "progress": t.get("progress", 0.0),
                "ticks_remaining": t.get("ticks_remaining"),
                "blocking_reason": t.get("blocking_reason"),
                "detail": t.get("detail"),
            }
            for t in tasks
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# INCIDENTS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EscalationEvent:
    at: float
    from_level: int
    to_level: int
    reason: str
    kind: str          # opened | escalated | de-escalated | acknowledged | status | resolved

    def payload(self) -> dict:
        return {
            "at": time.strftime("%H:%M:%S", time.localtime(self.at)),
            "from": TIER_BY_LEVEL[self.from_level].code,
            "to": TIER_BY_LEVEL[self.to_level].code,
            "reason": self.reason,
            "kind": self.kind,
        }


@dataclass
class Incident:
    incident_id: str
    kind: str                       # TRACK_FAILURE | SEVERE_WEATHER
    track_id: str
    corridor: str
    opened_at: float
    level: int = 1
    peak_level: int = 1
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[float] = None
    tier_since: float = 0.0
    affected_trains: List[str] = field(default_factory=list)
    passengers_at_risk: int = 0
    events: List[EscalationEvent] = field(default_factory=list)
    resolution: str = UNRESOLVED
    resolution_reason: str = ""
    resolved_at: Optional[float] = None
    closed: bool = False
    # Recomputed on every observe(); never trusted across calls.
    dispositions: Dict[str, str] = field(default_factory=dict)
    drivers: List[dict] = field(default_factory=list)
    work_order_id: Optional[str] = None
    # The twin's work order for this corridor, when one exists — field work
    # in progress is context for the controller, not a completion signal:
    # the signal still comes from trains and proved actions alone.
    field_work: Optional[dict] = None

    def move_to(self, level: int, reason: str, now: float) -> None:
        if level == self.level:
            return
        kind = "escalated" if level > self.level else "de-escalated"
        self.events.append(EscalationEvent(now, self.level, level, reason, kind))
        self.level = level
        self.peak_level = max(self.peak_level, level)
        self.tier_since = now
        # An escalation is new information: the previous acknowledgement was
        # given for a smaller problem, so the ladder restarts unacknowledged.
        if kind == "escalated":
            self.acknowledged_by = None
            self.acknowledged_at = None


# ─────────────────────────────────────────────────────────────────────────────
# RESOLUTION — the completion signal
# ─────────────────────────────────────────────────────────────────────────────

def classify_resolution(dispositions: Dict[str, str], items: Sequence[WorkItem],
                        condition_cleared: bool) -> tuple:
    """Return (state, one-line reason) for a single incident.

    Precedence is deliberate:
      BLOCKED    — what remains cannot be fixed by this system. Physical
                   intervention is required, so it outranks PARTIAL: a
                   dispatcher must not read "in progress" and wait.
      COMPLETE   — every affected train recovered, every committed action
                   verified on the twin, and the failure itself contained.
      UNRESOLVED — nothing has moved: no action landed, no train recovered.
      PARTIAL    — otherwise; some of the work is real, the rest is not done.
    """
    total = len(dispositions)
    recovered = [t for t, d in dispositions.items() if d == TRAIN_RECOVERED]
    stuck = [t for t, d in dispositions.items() if d != TRAIN_RECOVERED]
    blocked_trains = [t for t, d in dispositions.items() if d == TRAIN_BLOCKED]
    done_items = [i for i in items if i.state == WORK_DONE]
    blocked_items = [i for i in items if i.state == WORK_BLOCKED]
    failed_items = [i for i in items if i.state == WORK_FAILED]
    all_items_done = not items or len(done_items) == len(items)

    if blocked_items or (stuck and len(blocked_trains) == len(stuck)):
        who = ", ".join(sorted(blocked_trains)) or ", ".join(sorted(i.target for i in blocked_items))
        return BLOCKED, (
            f"No open corridor remains for {who} — recovery needs track restoration, "
            "not another plan."
        )

    if not stuck and all_items_done and condition_cleared:
        if total:
            return COMPLETE, (
                f"All {total} affected train(s) recovered and every committed action "
                "verified on the twin."
            )
        return COMPLETE, "The failure cleared with no train left impacted."

    if not done_items and not recovered:
        detail = f"{len(stuck)} train(s) impacted" if stuck else "the failure is still open"
        return UNRESOLVED, f"No committed action has landed on the twin yet; {detail}."

    parts = []
    if total:
        parts.append(f"{len(recovered)}/{total} train(s) recovered")
    if items:
        parts.append(f"{len(done_items)}/{len(items)} action(s) verified")
    if failed_items:
        parts.append(f"{len(failed_items)} action(s) failed")
    if not condition_cleared:
        parts.append("the failed section is still out of service")
    return PARTIAL, "Recovery is under way — " + ", ".join(parts) + "."


# ─────────────────────────────────────────────────────────────────────────────
# TIER COMPUTATION
# Each driver is named and carries the tier it argues for; an incident sits at
# the highest tier any driver justifies. Naming the drivers is what makes an
# escalation defensible after the fact.
# ─────────────────────────────────────────────────────────────────────────────

def incident_drivers(incident: Incident, dispositions: Dict[str, str],
                     resolution: str, snapshot: dict, network_severed: int) -> List[dict]:
    drivers: List[dict] = []
    held = sorted(t for t, d in dispositions.items() if d == TRAIN_HELD)
    exposed = sorted(t for t, d in dispositions.items() if d == TRAIN_EXPOSED)
    no_path = sorted(t for t, d in dispositions.items() if d == TRAIN_BLOCKED)
    trains = snapshot.get("trains", {})
    passengers_stuck = sum(
        trains.get(t, {}).get("passengers", 0)
        for t, d in dispositions.items() if d != TRAIN_RECOVERED
    )

    if incident.kind == "TRACK_FAILURE":
        drivers.append({"code": "ASSET", "level": 1,
                        "detail": f"{incident.corridor} is out of service."})
    else:
        drivers.append({"code": "WEATHER", "level": 1,
                        "detail": f"Severe weather on {incident.corridor}."})

    if exposed:
        drivers.append({"code": "TRAFFIC", "level": 2,
                        "detail": f"{len(exposed)} train(s) still routed into the failure: "
                                  + ", ".join(exposed) + "."})
    if held:
        drivers.append({"code": "STOPPED", "level": 3,
                        "detail": f"{len(held)} train(s) brought to a stand: "
                                  + ", ".join(held) + "."})
    if passengers_stuck >= 500:
        drivers.append({"code": "PASSENGERS", "level": 3,
                        "detail": f"{passengers_stuck} passengers on trains that have not recovered."})
    if no_path:
        drivers.append({"code": "NO_PATH", "level": 4,
                        "detail": "No alternate corridor exists for " + ", ".join(no_path) + "."})
    if network_severed:
        drivers.append({"code": "SEVERED", "level": 4,
                        "detail": f"{network_severed} station pair(s) have no route between them."})
    if resolution == BLOCKED:
        drivers.append({"code": "BLOCKED", "level": 4,
                        "detail": "Automated recovery is exhausted; physical intervention required."})
    return drivers


def dwell_pressure(incident: Incident, now: float) -> Optional[dict]:
    """Time-based escalation: unhandled work climbs the ladder on its own.

    Acknowledging restarts the clock at the current tier — that is the whole
    point of an acknowledgement — but it cannot hold a tier the network itself
    has outgrown, because the condition drivers are evaluated alongside this.
    """
    tier = TIER_BY_LEVEL[incident.level]
    if tier.sla_seconds is None:
        return None
    baseline = max(incident.tier_since, incident.acknowledged_at or 0.0)
    dwell = now - baseline
    if dwell < tier.sla_seconds:
        return None
    state = "unacknowledged" if incident.acknowledged_by is None else "acknowledged but unresolved"
    return {
        "code": "DWELL",
        "level": min(incident.level + 1, 4),
        "detail": f"{tier.code} {state} for {dwell / 60.0:.1f} min "
                  f"(limit {tier.sla_seconds / 60:.0f} min) — handling level raised.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# LEDGER
# ─────────────────────────────────────────────────────────────────────────────

class IncidentLedger:
    """Tracks open incidents, their tier history, and their completion state.

    Thread-safety is the caller's job — the orchestrator already serializes
    twin access under a lock and calls observe() from inside it.
    """

    def __init__(self) -> None:
        self._incidents: Dict[str, Incident] = {}
        self._items: Dict[str, WorkItem] = {}
        self._seq = 0
        self._item_seq = 0

    # ── lifecycle ──────────────────────────────────────────────────────────

    def reset(self) -> None:
        self._incidents.clear()
        self._items.clear()
        self._seq = 0
        self._item_seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"INC-{self._seq:03d}"

    def _corridor(self, snapshot: dict, track_id: str) -> str:
        track = snapshot.get("tracks", {}).get(track_id, {})
        src, dst = track.get("source", "?"), track.get("destination", "?")
        return f"{track_id} · {pretty_station(src)} ↔ {pretty_station(dst)}"

    # ── observation ────────────────────────────────────────────────────────

    def observe(self, snapshot: dict, now: Optional[float] = None) -> dict:
        """Fold a twin snapshot into the ledger and return the escalation state.

        Opens incidents for newly blocking corridors and severe weather, closes
        those whose corridor vanished, re-verifies every work item against the
        twin, recomputes each incident's completion state, then moves tiers.
        """
        now = time.time() if now is None else now
        graph = open_graph(snapshot)
        blocked = blocked_edges(snapshot)
        severed = severed_pairs(graph)
        tracks = snapshot.get("tracks", {})
        trains = snapshot.get("trains", {})
        weather = {k: _condition(v) for k, v in (snapshot.get("weather") or {}).items()}

        self._open_new_incidents(snapshot, tracks, weather, now)
        self._close_vanished_incidents(tracks, now)

        for item in self._items.values():
            verify_work_item(item, snapshot, graph, now)

        work_orders = snapshot.get("work_orders") or []
        for incident in self._incidents.values():
            if incident.closed:
                continue
            incident.field_work = field_work_for(incident, work_orders)
            incident.dispositions = self._dispositions(incident, trains, blocked, graph)
            items = self.items_for(incident.incident_id)
            cleared = self._condition_cleared(incident, tracks, weather)
            resolution, reason = classify_resolution(incident.dispositions, items, cleared)
            self._apply_resolution(incident, resolution, reason, now)

            drivers = incident_drivers(incident, incident.dispositions, resolution,
                                       snapshot, severed)
            dwell = dwell_pressure(incident, now)
            if dwell:
                drivers.append(dwell)
            incident.drivers = drivers

            target = 0 if resolution == COMPLETE else max(
                (d["level"] for d in drivers), default=1)
            if target != incident.level:
                if target > incident.level:
                    top = max(drivers, key=lambda d: d["level"])
                    why = top["detail"]
                else:
                    why = (f"Completion signal {resolution} — {reason}"
                           if resolution == COMPLETE else
                           "Conditions improved; handling level lowered.")
                incident.move_to(target, why, now)

        return self.payload(snapshot, now)

    def _open_new_incidents(self, snapshot: dict, tracks: dict, weather: dict,
                            now: float) -> None:
        open_track_ids = {i.track_id for i in self._incidents.values()
                          if not i.closed and i.kind == "TRACK_FAILURE"}
        open_weather_ids = {i.track_id for i in self._incidents.values()
                            if not i.closed and i.kind == "SEVERE_WEATHER"}
        trains = snapshot.get("trains", {})

        for track_id, track in tracks.items():
            if not is_blocking(track) or track_id in open_track_ids:
                continue
            self._open(snapshot, trains, track_id, "TRACK_FAILURE",
                       {frozenset({track["source"], track["destination"]})}, now,
                       f"Track failure detected on {self._corridor(snapshot, track_id)}.")

        for track_id, condition in weather.items():
            if condition not in SEVERE_WEATHER or track_id in open_weather_ids:
                continue
            track = tracks.get(track_id)
            if not track:
                continue
            self._open(snapshot, trains, track_id, "SEVERE_WEATHER",
                       {frozenset({track["source"], track["destination"]})}, now,
                       f"{condition} reported on {self._corridor(snapshot, track_id)}.")

    def _open(self, snapshot: dict, trains: dict, track_id: str, kind: str,
              edges: set, now: float, reason: str) -> Incident:
        incident = Incident(
            incident_id=self._next_id(),
            kind=kind,
            track_id=track_id,
            corridor=self._corridor(snapshot, track_id),
            opened_at=now,
            tier_since=now,
        )
        affected = [tid for tid, t in trains.items()
                    if route_crosses(t.get("route", []), edges)]
        incident.affected_trains = sorted(affected)
        incident.passengers_at_risk = sum(
            trains.get(t, {}).get("passengers", 0) for t in affected)
        incident.events.append(EscalationEvent(now, 0, 1, reason, "opened"))
        self._incidents[incident.incident_id] = incident
        return incident

    def _close_vanished_incidents(self, tracks: dict, now: float) -> None:
        for incident in self._incidents.values():
            if incident.closed or incident.track_id in tracks:
                continue
            incident.closed = True
            incident.events.append(EscalationEvent(
                now, incident.level, 0,
                "Corridor no longer present in the twin.", "resolved"))

    def _condition_cleared(self, incident: Incident, tracks: dict, weather: dict) -> bool:
        """True when the *cause* is gone, as opposed to merely routed around."""
        if incident.kind == "TRACK_FAILURE":
            track = tracks.get(incident.track_id)
            return track is None or not is_blocking(track)
        return weather.get(incident.track_id) not in SEVERE_WEATHER

    def _dispositions(self, incident: Incident, trains: dict,
                      blocked: set, graph: nx.Graph) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for train_id in incident.affected_trains:
            train = trains.get(train_id)
            if train is None:
                # Gone from the fleet entirely — no longer this incident's problem.
                out[train_id] = TRAIN_RECOVERED
                continue
            route = train.get("route", [])
            reachable = _path_exists(graph, train, route)
            if train.get("held"):
                out[train_id] = TRAIN_HELD if reachable else TRAIN_BLOCKED
            elif route_crosses(route, blocked):
                out[train_id] = TRAIN_EXPOSED if reachable else TRAIN_BLOCKED
            else:
                out[train_id] = TRAIN_RECOVERED
        return out

    def _apply_resolution(self, incident: Incident, resolution: str,
                          reason: str, now: float) -> None:
        if resolution != incident.resolution:
            incident.events.append(EscalationEvent(
                now, incident.level, incident.level,
                f"Completion signal → {resolution}: {reason}", "status"))
        incident.resolution = resolution
        incident.resolution_reason = reason
        incident.resolved_at = now if resolution == COMPLETE else None

    # ── work items ─────────────────────────────────────────────────────────

    def record_dispatch(self, plan_id: str, actions: Sequence[dict],
                        snapshot: dict, now: Optional[float] = None) -> List[WorkItem]:
        """Register the actions an approved plan committed to.

        Called when a plan is executed. Items start PENDING and are proved DONE
        only by a later observe() reading the twin — which is what stops the
        console reporting success for an apply that silently did nothing.
        """
        now = time.time() if now is None else now
        created: List[WorkItem] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            sid = action.get("strategy_id", "")
            if sid.startswith("T_CLOSE_"):
                track_id = action.get("track_id") or sid[len("T_CLOSE_"):]
                created.append(self._add_item(plan_id, "close_track", track_id,
                                              f"Close {track_id}", snapshot, now))
            elif sid.startswith("R_REROUTE_"):
                train_id = action.get("train_id", "")
                item = self._add_item(plan_id, "reroute", train_id,
                                      f"Reroute {train_id}", snapshot, now,
                                      old_route=action.get("old_route", []))
                item.expected_route = list(action.get("new_route", []))
                created.append(item)
            elif sid.startswith("R_HOLD_"):
                train_id = action.get("train_id", "")
                created.append(self._add_item(plan_id, "hold", train_id,
                                              f"Hold {train_id} short of the failure",
                                              snapshot, now,
                                              old_route=action.get("old_route", [])))
            elif sid.startswith("W"):
                for act in action.get("actions", []):
                    if act.startswith("close_track_"):
                        track_id = act[len("close_track_"):].split("+")[0]
                        created.append(self._add_item(
                            plan_id, "weather_closure", track_id,
                            f"Weather protocol: close {track_id}", snapshot, now))
        return created

    def record_work_order(self, work_order: dict, snapshot: dict, now: Optional[float] = None) -> List[WorkItem]:
        now = time.time() if now is None else now
        created = []
        plan_id = work_order.get("plan_id") or "?"
        for task in work_order.get("tasks", []):
            action = task.get("action")
            target = task.get("target", "")
            mapping = {
                "CLOSE_TRACK": ("close_track", f"Close {target}"),
                "REROUTE_TRAIN": ("reroute", f"Reroute {target}"),
                "HOLD_TRAIN": ("hold", f"Hold {target} short of the failure"),
                "DISPATCH_CREW": ("dispatch_crew", f"Dispatch crew to {target}"),
                "REPAIR_TRACK": ("repair_track", f"Repair {target}"),
                "RESTORE_SIGNAL": ("restore_signal", f"Restore signal {target}"),
            }
            if action not in mapping:
                continue
            kind, label = mapping[action]
            item = self._add_item(plan_id, kind, target, label, snapshot, now)
            if action == "REROUTE_TRAIN":
                item.expected_route = list(task.get("metadata", {}).get("new_route", []))
            created.append(item)
        return created

    def _add_item(self, plan_id: str, kind: str, target: str, label: str,
                  snapshot: dict, now: float, old_route: Sequence[str] = ()) -> WorkItem:
        # Keyed by kind+target: re-executing the same plan must update the
        # existing commitment, not stack duplicate rows in the checklist.
        key = f"{kind}:{target}"
        existing = self._items.get(key)
        if existing is not None:
            existing.plan_id = plan_id
            existing.dispatched_at = now
            # A fresh commitment has to be proved again; an earlier
            # verification does not carry over to the new dispatch.
            existing.state = WORK_PENDING
            existing.detail = "Not yet executed."
            existing.completed_at = None
            if existing.incident_id is None:
                existing.incident_id = self._attribute(kind, target, old_route, snapshot)
            return existing
        self._item_seq += 1
        item = WorkItem(
            item_id=f"WI-{self._item_seq:03d}",
            kind=kind,
            label=label,
            target=target,
            incident_id=self._attribute(kind, target, old_route, snapshot),
            plan_id=plan_id,
            dispatched_at=now,
        )
        self._items[key] = item
        return item

    def _attribute(self, kind: str, target: str, old_route: Sequence[str],
                   snapshot: dict) -> Optional[str]:
        """Bind a work item to the incident it answers.

        Track actions bind by track id. Train actions bind to the incident whose
        corridor the train's pre-plan route crossed — that is the failure the
        reroute exists to escape.
        """
        openers = [i for i in self._incidents.values() if not i.closed]
        if kind in ("close_track", "weather_closure"):
            for incident in openers:
                if incident.track_id == target:
                    return incident.incident_id
            return None
        for incident in openers:
            if target in incident.affected_trains:
                return incident.incident_id
        tracks = snapshot.get("tracks", {})
        for incident in openers:
            track = tracks.get(incident.track_id, {})
            edge = frozenset({track.get("source"), track.get("destination")})
            if old_route and route_crosses(old_route, {edge}):
                return incident.incident_id
        return openers[0].incident_id if openers else None

    def items_for(self, incident_id: str) -> List[WorkItem]:
        return [i for i in self._items.values() if i.incident_id == incident_id]

    # ── operator actions ───────────────────────────────────────────────────

    def acknowledge(self, incident_id: str, owner: str,
                    now: Optional[float] = None) -> Incident:
        now = time.time() if now is None else now
        incident = self._incidents.get(incident_id)
        if incident is None:
            raise KeyError(incident_id)
        incident.acknowledged_by = owner
        incident.acknowledged_at = now
        incident.events.append(EscalationEvent(
            now, incident.level, incident.level,
            f"Acknowledged by {owner} at {TIER_BY_LEVEL[incident.level].code}; "
            "dwell timer restarted.", "acknowledged"))
        return incident

    def attach_work_order(self, work_order_id: str, incident_id: Optional[str] = None) -> None:
        """Associate a committed WorkOrder with the incident it resolves."""
        if incident_id and incident_id in self._incidents:
            self._incidents[incident_id].work_order_id = work_order_id
            return
        open_incidents = self.open_incidents()
        if open_incidents:
            target = max(open_incidents, key=lambda i: i.level)
            target.work_order_id = work_order_id

    def get(self, incident_id: str) -> Optional[Incident]:
        return self._incidents.get(incident_id)

    def open_incidents(self) -> List[Incident]:
        return [i for i in self._incidents.values() if not i.closed]

    def incident_payload(self, incident_id: str, now: Optional[float] = None) -> Optional[dict]:
        incident = self._incidents.get(incident_id)
        if incident is None:
            return None
        return self._incident_payload(incident, time.time() if now is None else now)

    # ── payload ────────────────────────────────────────────────────────────

    def payload(self, snapshot: dict, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        open_incidents = self.open_incidents()
        level = max((i.level for i in open_incidents), default=0)
        tier = TIER_BY_LEVEL[level]

        if open_incidents:
            worst = max(open_incidents,
                        key=lambda i: (RESOLUTION_SEVERITY[i.resolution], i.level))
            network_resolution = worst.resolution
        else:
            network_resolution = COMPLETE

        incidents_payload = [
            self._incident_payload(i, now) for i in
            sorted(self._incidents.values(),
                   key=lambda i: (i.closed, -i.level, i.opened_at))
        ]

        totals = {
            "open": len(open_incidents),
            "complete": sum(1 for i in self._incidents.values() if i.resolution == COMPLETE),
            "partial": sum(1 for i in open_incidents if i.resolution == PARTIAL),
            "blocked": sum(1 for i in open_incidents if i.resolution == BLOCKED),
            "unresolved": sum(1 for i in open_incidents if i.resolution == UNRESOLVED),
        }

        return {
            "level": tier.level,
            "code": tier.code,
            "label": tier.label,
            "owner": tier.owner,
            "posture": tier.posture,
            "resolution": network_resolution,
            "totals": totals,
            "incidents": incidents_payload,
            "severed_pairs": severed_pairs(open_graph(snapshot)),
            "generated_at": time.strftime("%H:%M:%S", time.localtime(now)),
        }

    def _incident_payload(self, incident: Incident, now: float) -> dict:
        tier = TIER_BY_LEVEL[incident.level]
        items = self.items_for(incident.incident_id)
        recovered = sum(1 for d in incident.dispositions.values() if d == TRAIN_RECOVERED)
        done = sum(1 for i in items if i.state == WORK_DONE)
        return {
            "id": incident.incident_id,
            "kind": incident.kind,
            "track_id": incident.track_id,
            "corridor": incident.corridor,
            "level": incident.level,
            "code": tier.code,
            "tier_label": tier.label,
            "owner": tier.owner,
            "posture": tier.posture,
            "peak_code": TIER_BY_LEVEL[incident.peak_level].code,
            "resolution": incident.resolution,
            "resolution_reason": incident.resolution_reason,
        "work_order_id": incident.work_order_id,
            "acknowledged_by": incident.acknowledged_by,
            "age_seconds": round(now - incident.opened_at, 1),
            "tier_age_seconds": round(now - max(incident.tier_since,
                                                incident.acknowledged_at or 0.0), 1),
            "sla_seconds": tier.sla_seconds,
            "closed": incident.closed,
            "affected_trains": incident.affected_trains,
            "passengers_at_risk": incident.passengers_at_risk,
            "dispositions": incident.dispositions,
            "progress": {
                "trains_recovered": recovered,
                "trains_total": len(incident.affected_trains),
                "actions_done": done,
                "actions_total": len(items),
            },
            "drivers": incident.drivers,
            "field_work": incident.field_work,
            "work_items": [i.payload() for i in items],
            "events": [e.payload() for e in incident.events][-12:],
        }


def pretty_station(station_id: str) -> str:
    return str(station_id).replace("_", " ").title()
