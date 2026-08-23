// RailMind API client. Talks to the FastAPI agent service when reachable and
// falls back to a fully-featured local mock (same canonical network) so the
// dashboard always works — even with no backend running.

import {
  STATIONS,
  TRACKS,
  TRAINS,
  findPath,
  stationName,
  trackById,
  type Train,
} from "./railmind-data";

export type AgentOutputs = {
  weather: { strategy: string; risk_score: number; high_risk_tracks: string[] };
  track: { actions: string[] };
  signal: { actions: string[] };
  routing: {
    affected_trains: string[];
    reroutes: { train: string; from: string[]; to: string[] }[];
  };
};

export type Plan = {
  id: string;
  name: string;
  delay_min: number;
  risk: number;
  passengers_impacted: number;
  congestion: number;
  score: number; // 0-100, higher is better
  actions: string[];
  // Per-plan map preview data (real backend)
  failed_tracks?: string[];
  rerouted_trains?: { id: string; newRoute: string[] }[];
  held_trains?: string[];
};

export type LogEvent = { t: string; source: string; message: string };

// ── Escalation ladder and completion signal ─────────────────────────────────

/** COMPLETE / PARTIAL / BLOCKED / UNRESOLVED — see the backend's escalation.py. */
export type Resolution = "COMPLETE" | "PARTIAL" | "BLOCKED" | "UNRESOLVED";
export type WorkState = "DONE" | "PENDING" | "FAILED" | "BLOCKED";
export type Disposition = "RECOVERED" | "HELD" | "EXPOSED" | "BLOCKED";

export type WorkItem = {
  id: string;
  kind: "close_track" | "reroute" | "hold" | "weather_closure" | string;
  label: string;
  target: string;
  incident_id: string | null;
  state: WorkState;
  detail: string;
  plan_id?: string | null;
};

export type EscalationDriver = { code: string; level: number; detail: string };

export type IncidentEvent = {
  at: string;
  from: string;
  to: string;
  reason: string;
  kind: "opened" | "escalated" | "de-escalated" | "acknowledged" | "status" | "resolved" | string;
};

export type Incident = {
  id: string;
  kind: "TRACK_FAILURE" | "SEVERE_WEATHER" | string;
  track_id: string;
  corridor: string;
  level: number;
  code: string;
  tier_label: string;
  owner: string;
  posture: string;
  peak_code: string;
  resolution: Resolution;
  resolution_reason: string;
  acknowledged_by: string | null;
  age_seconds: number;
  tier_age_seconds: number;
  sla_seconds: number | null;
  closed: boolean;
  affected_trains: string[];
  passengers_at_risk: number;
  dispositions: Record<string, Disposition>;
  progress: {
    trains_recovered: number;
    trains_total: number;
    actions_done: number;
    actions_total: number;
  };
  drivers: EscalationDriver[];
  /** The twin's work order on this corridor, when one exists (context, not the signal). */
  field_work?: FieldWork | null;
  work_items: WorkItem[];
  events: IncidentEvent[];
};

// ── Field work: the twin's work orders ──────────────────────────────────────
// Executed by the digital twin over simulation ticks; a task is COMPLETED only
// when the twin reads back the physical state that proves it.

export type TaskStatus =
  | "PENDING"
  | "IN_PROGRESS"
  | "COMPLETED"
  | "BLOCKED"
  | "UNRESOLVED"
  | "CANCELLED";
export type WorkOrderStatus = Resolution | "CANCELLED";

export type FieldTask = {
  id: string;
  action: string;
  target: string;
  status: TaskStatus;
  ticks_required: number;
  ticks_remaining: number;
  progress: number; // 0..1
  depends_on: string[];
  blocking_reason: string | null;
  detail: string;
  started_tick: number | null;
  completed_tick: number | null;
  params?: Record<string, unknown>;
};

export type WorkOrderEvent = { tick: number; kind: string; task_id: string | null; detail: string };

export type WorkOrder = {
  id: string;
  incident_id: string | null;
  type: string;
  target: string;
  status: WorkOrderStatus;
  completion_percentage: number;
  estimated_ticks_remaining: number;
  created_tick: number;
  cancelled: boolean;
  cancel_reason: string | null;
  auto_retry: boolean;
  tasks: FieldTask[];
  events: WorkOrderEvent[];
};

export type CrewUnit = {
  id: string;
  target: string;
  status: "EN_ROUTE" | "ON_SITE" | string;
  work_order_id: string;
  task_id: string;
  dispatched_tick: number;
  arrived_tick: number | null;
};

/** Summary the ledger attaches to an incident from the twin's snapshot. */
export type FieldWork = {
  id: string;
  status: WorkOrderStatus;
  completion_percentage: number;
  estimated_ticks_remaining: number;
  blocked_reason: string | null;
  current: string | null;
  tasks: Pick<
    FieldTask,
    "id" | "action" | "status" | "progress" | "ticks_remaining" | "blocking_reason" | "detail"
  >[];
};

export type Escalation = {
  level: number;
  code: string;
  label: string;
  owner: string;
  posture: string;
  resolution: Resolution;
  totals: { open: number; complete: number; partial: number; blocked: number; unresolved: number };
  incidents: Incident[];
  severed_pairs: number;
  generated_at: string;
  twin_source?: string;
};

export type HandoffBrief = {
  incident_id: string;
  resolution: Resolution;
  level: string;
  escalating_to: string;
  text: string;
  source: "llm" | "rules" | string;
};

export type Explanation = { text: string; source: "llm" | "rules" | string };

export type RunResponse = {
  injected_failure?: string;
  twin_source?: string;
  explanation?: Explanation;
  recommended_action: Plan;
  candidate_plans: Plan[];
  agent_outputs: AgentOutputs;
  execution_log: LogEvent[];
  failed_tracks: string[];
  rerouted_trains: { id: string; newRoute: string[] }[];
  held_trains?: string[];
  trains?: Train[];
  executed_plan?: string;
  applied_actions?: string[];
  escalation?: Escalation;
};

export type LiveTrack = {
  id: string;
  health: number;
  status: string;
  source?: string;
  destination?: string;
};

export type LiveState = {
  twin_source: string;
  trains: Train[];
  tracks: LiveTrack[];
  weather: Record<string, string>;
  // Field work rides along with the fleet so one poll covers both
  sim_tick?: number;
  work_orders?: WorkOrder[];
  crews?: Record<string, CrewUnit>;
};

const API_BASE_URL: string =
  (import.meta.env?.VITE_API_BASE_URL as string | undefined) ?? "http://127.0.0.1:8001";

function nowHMS(offset = 0) {
  const d = new Date(Date.now() + offset * 1000);
  return d.toTimeString().slice(0, 8);
}

// ── Mock pipeline (mirrors the FastAPI agent flow on the same network) ──────

export function buildMockRun(trackId?: string): RunResponse {
  const failed = trackId ?? "T23";
  const t = trackById(failed);
  const fromName = stationName(t?.from ?? "NAGPUR_JUNCT");
  const toName = stationName(t?.to ?? "RAIPUR_JUNCT");

  const crossesFailure = (route: string[]) =>
    route.some(
      (s, i) =>
        i < route.length - 1 &&
        !!t &&
        ((s === t.from && route[i + 1] === t.to) || (s === t.to && route[i + 1] === t.from)),
    );

  const affected = TRAINS.filter((tr) => crossesFailure(tr.route));
  const passengers = affected.reduce((a, b) => a + b.passengers, 0);

  const reroutes = affected.map((tr) => {
    const alt = findPath(tr.route[0], tr.route[tr.route.length - 1], [failed]);
    return { train: tr.id, from: tr.route, to: alt ?? tr.route };
  });

  const monitored = TRACKS.filter((x) => x.status === "monitored" && x.id !== failed).map(
    (x) => x.id,
  );

  const agent_outputs: AgentOutputs = {
    weather: { strategy: "W5", risk_score: 0.05, high_risk_tracks: [] },
    track: {
      actions: [`T_CLOSE_${failed}`, ...monitored.slice(0, 3).map((id) => `T_MONITOR_${id}`)],
    },
    signal: {
      actions: [`S_YELLOW_${failed}`, ...monitored.slice(0, 2).map((id) => `S_YELLOW_${id}`)],
    },
    routing: {
      affected_trains: affected.map((a) => a.id),
      reroutes,
    },
  };

  const plans: Plan[] = [
    {
      id: "A",
      name: "Plan A — Safety First",
      delay_min: 30 + reroutes.length * 15,
      risk: 0.18,
      passengers_impacted: passengers,
      congestion: 0.45,
      score: 84,
      actions: [
        `Close track ${failed}`,
        `Signal RED on ${failed}`,
        ...affected.map((a) => `Reroute ${a.id}`),
      ],
      failed_tracks: [failed],
      rerouted_trains: reroutes.map((r) => ({ id: r.train, newRoute: r.to })),
      held_trains: [],
    },
    {
      id: "B",
      name: "Plan B — Balanced Response",
      delay_min: 15 + reroutes.length * 15,
      risk: 0.24,
      passengers_impacted: passengers,
      congestion: 0.35,
      score: 95,
      actions: [
        `Speed-limit 60 km/h on ${failed}`,
        `Signal YELLOW on ${failed}`,
        ...affected.map((a) => `Reroute ${a.id}`),
      ],
      failed_tracks: [],
      rerouted_trains: reroutes.map((r) => ({ id: r.train, newRoute: r.to })),
      held_trains: [],
    },
    {
      id: "C",
      name: "Plan C — Minimal Intervention",
      delay_min: 60 * Math.max(affected.length, 1),
      risk: 0.85,
      passengers_impacted: passengers,
      congestion: 0.7,
      score: 21,
      actions: [`Monitor ${failed}`, "Keep all signals GREEN", "No reroutes"],
      failed_tracks: [],
      rerouted_trains: [],
      held_trains: [],
    },
  ];

  const recommended = plans.reduce((a, b) => (a.score >= b.score ? a : b));

  const log: LogEvent[] = [
    {
      t: nowHMS(-7),
      source: "Sensor",
      message: `Anomaly detected on track ${failed} (${fromName} ↔ ${toName})`,
    },
    {
      t: nowHMS(-6),
      source: "Weather",
      message: "Nominal conditions — no weather action required",
    },
    { t: nowHMS(-6), source: "Track", message: `Track Agent flagged ${failed} for closure` },
    { t: nowHMS(-5), source: "Signal", message: `Signal Agent set ${failed} to YELLOW` },
    ...affected.map((a, i) => ({
      t: nowHMS(-4 + i * 0.1),
      source: "Routing",
      message: `Routing Agent computed alternate path for ${a.id}`,
    })),
    {
      t: nowHMS(-3),
      source: "Planner",
      message: `Planner generated ${plans.length} candidate plans`,
    },
    {
      t: nowHMS(-2),
      source: "Simulator",
      message: "Simulator scored plans against the incident snapshot",
    },
    {
      t: nowHMS(0),
      source: "Master",
      message: `Master Agent selected ${recommended.name} (score ${recommended.score}/100)`,
    },
  ];
  log.reverse(); // newest first

  return {
    injected_failure: trackId,
    twin_source: "mock (offline)",
    explanation: {
      text:
        `${recommended.name} scored ${recommended.score}/100 — it reroutes ${reroutes.length} ` +
        `affected train(s) around ${failed} (${fromName} ↔ ${toName}) while keeping delay at ` +
        `${recommended.delay_min} min. Minimal intervention would strand passengers on a dead section.`,
      source: "rules",
    },
    recommended_action: recommended,
    candidate_plans: plans,
    agent_outputs,
    execution_log: log,
    failed_tracks: [failed],
    rerouted_trains: reroutes.map((r) => ({ id: r.train, newRoute: r.to })),
    held_trains: [],
    trains: TRAINS,
  };
}

// Mock approximation of a weather incident: storms degrade the track badly
// enough to close it. Shared by injectWeather's offline path and the
// storm-run retry in the dashboard so both report a weather-shaped incident.
export function buildMockWeatherRun(trackId: string, condition = "STORM"): RunResponse {
  const mock = buildMockRun(trackId);
  mock.agent_outputs.weather = { strategy: "W1", risk_score: 0.85, high_risk_tracks: [trackId] };
  mock.execution_log = [
    {
      t: nowHMS(0),
      source: "Weather",
      message: `${condition} reported on ${trackId} — emergency weather protocol`,
    },
    // The base mock's "Nominal conditions" Weather entry would contradict
    // the storm story — drop it.
    ...mock.execution_log.filter(
      (e) => !(e.source === "Weather" && e.message.includes("Nominal conditions")),
    ),
  ];
  return mock;
}

// ── Real backend adapter ────────────────────────────────────────────────────

function isFiniteNumber(x: unknown): x is number {
  return typeof x === "number" && Number.isFinite(x);
}

// Every metric the console renders must be a real number: normalizePlan's 0s
// exist to survive *optional* gaps, but a plan missing its core metrics would
// otherwise render "Risk 0.00" — a confident lie on an ops console. Reject it.
function isValidPlan(x: unknown): x is Plan {
  if (!x || typeof x !== "object") return false;
  const p = x as Record<string, unknown>;
  return (
    typeof p.id === "string" &&
    isFiniteNumber(p.delay_min) &&
    isFiniteNumber(p.risk) &&
    isFiniteNumber(p.passengers_impacted) &&
    isFiniteNumber(p.congestion) &&
    isFiniteNumber(p.score) &&
    Array.isArray(p.actions)
  );
}

export function isValidRunResponse(x: unknown): x is RunResponse {
  if (!x || typeof x !== "object") return false;
  const r = x as Record<string, unknown>;
  return (
    isValidPlan(r.recommended_action) &&
    Array.isArray(r.candidate_plans) &&
    r.candidate_plans.every(isValidPlan) &&
    Array.isArray(r.execution_log)
  );
}

// Second belt behind isValidRunResponse: defaults the *optional* fields the
// render path dereferences (preview arrays etc.) so they can never crash it.
function normalizePlan(p: Plan): Plan {
  return {
    ...p,
    delay_min: p.delay_min ?? 0,
    risk: p.risk ?? 0,
    passengers_impacted: p.passengers_impacted ?? 0,
    congestion: p.congestion ?? 0,
    score: p.score ?? 0,
    actions: p.actions ?? [],
  };
}

export function normalize(r: RunResponse): RunResponse {
  return {
    ...r,
    recommended_action: normalizePlan(r.recommended_action),
    candidate_plans: (r.candidate_plans ?? []).map(normalizePlan),
    execution_log: r.execution_log ?? [],
    failed_tracks: r.failed_tracks ?? [],
    rerouted_trains: r.rerouted_trains ?? [],
    held_trains: r.held_trains ?? [],
    trains: r.trains && r.trains.length > 0 ? r.trains : TRAINS,
    agent_outputs: {
      weather: {
        strategy: r.agent_outputs?.weather?.strategy ?? "—",
        risk_score: r.agent_outputs?.weather?.risk_score ?? 0,
        high_risk_tracks: r.agent_outputs?.weather?.high_risk_tracks ?? [],
      },
      track: { actions: r.agent_outputs?.track?.actions ?? [] },
      signal: { actions: r.agent_outputs?.signal?.actions ?? [] },
      routing: {
        affected_trains: r.agent_outputs?.routing?.affected_trains ?? [],
        reroutes: r.agent_outputs?.routing?.reroutes ?? [],
      },
    },
  };
}

async function tryFetch(
  url: string,
  init?: RequestInit,
  timeoutMs = 8000,
): Promise<RunResponse | null> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const res = await fetch(url, { ...init, signal: controller.signal });
    clearTimeout(timer);
    if (!res.ok) return null;
    const json: unknown = await res.json();
    return isValidRunResponse(json) ? normalize(json) : null;
  } catch {
    return null;
  }
}

/**
 * Run the pipeline. `mockTrackId`/`mockKind` only shape the offline mock: pass
 * the track (and incident kind) of an injection that already reached the
 * backend so a fallback after a mid-stream failure reports the right incident
 * — weather-shaped for storms — instead of the T23 track-failure default.
 */
export async function runSimulation(
  mockTrackId?: string,
  mockKind: "track" | "weather" = "track",
): Promise<RunResponse> {
  const real = await tryFetch(`${API_BASE_URL}/run`, { method: "POST" }, 20000);
  if (real) return real;
  await new Promise((r) => setTimeout(r, 600));
  return mockKind === "weather" && mockTrackId
    ? buildMockWeatherRun(mockTrackId)
    : buildMockRun(mockTrackId);
}

/** Live twin snapshot for the polling loop. Null when the backend is offline. */
export async function fetchLiveState(): Promise<LiveState | null> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    const res = await fetch(`${API_BASE_URL}/state`, { signal: controller.signal });
    clearTimeout(timer);
    if (!res.ok) return null;
    const json = (await res.json()) as LiveState;
    return Array.isArray(json.trains) && Array.isArray(json.tracks) ? json : null;
  } catch {
    return null;
  }
}

export async function applyPlan(planId?: string): Promise<RunResponse | null> {
  const url = planId
    ? `${API_BASE_URL}/apply-plan?plan_id=${encodeURIComponent(planId)}`
    : `${API_BASE_URL}/apply-plan`;
  // Applying reroutes to a (possibly remote) twin is the slowest call — give
  // it real headroom so a successful execution is never mislabeled as offline.
  return tryFetch(url, { method: "POST" }, 25000);
}

export async function resetTwin(): Promise<boolean> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    const res = await fetch(`${API_BASE_URL}/reset`, {
      method: "POST",
      signal: controller.signal,
    });
    clearTimeout(timer);
    return res.ok;
  } catch {
    return false;
  }
}

// ── Escalation ──────────────────────────────────────────────────────────────

const TIER_OWNERS: Record<number, { code: string; label: string; owner: string; posture: string }> =
  {
    0: {
      code: "L0",
      label: "Steady State",
      owner: "Section Controller",
      posture: "Routine monitoring — no active response.",
    },
    1: {
      code: "L1",
      label: "Advisory",
      owner: "Section Controller",
      posture: "Assets degraded. Watch and restrict; no train is impacted yet.",
    },
    2: {
      code: "L2",
      label: "Elevated",
      owner: "Divisional Controller",
      posture: "Trains are impacted. Recovery is owned and in progress.",
    },
    3: {
      code: "L3",
      label: "Critical",
      owner: "Divisional Emergency Cell",
      posture: "Passengers are stranded or failures are compounding.",
    },
    4: {
      code: "L4",
      label: "Emergency",
      owner: "Zonal Emergency Control Room",
      posture: "The network cannot recover unaided.",
    },
  };

export const STEADY_STATE: Escalation = {
  level: 0,
  ...TIER_OWNERS[0],
  resolution: "COMPLETE",
  totals: { open: 0, complete: 0, partial: 0, blocked: 0, unresolved: 0 },
  incidents: [],
  severed_pairs: 0,
  generated_at: "",
};

/**
 * Offline escalation, derived from a mock run. The backend ledger grades the
 * real twin; with no backend the console still has to answer "how urgent is
 * this, and is the work done?" — so the same shape is synthesised from what
 * the mock pipeline already knows. `executed` flips the committed work to
 * verified, mirroring what the ledger would observe after an apply.
 */
export function buildMockEscalation(r: RunResponse, executed = false): Escalation {
  const failed = r.failed_tracks?.[0];
  if (!failed) return { ...STEADY_STATE, generated_at: nowHMS() };

  const track = trackById(failed);
  const held = r.held_trains ?? [];
  const rerouted = r.rerouted_trains ?? [];
  const affected = [...new Set([...held, ...rerouted.map((x) => x.id)])].sort();
  const passengers = TRAINS.filter((t) => affected.includes(t.id)).reduce(
    (a, b) => a + b.passengers,
    0,
  );

  const dispositions: Record<string, Disposition> = {};
  for (const id of affected) {
    dispositions[id] = executed ? "RECOVERED" : held.includes(id) ? "HELD" : "EXPOSED";
  }

  const work: WorkItem[] = [
    {
      id: "WI-001",
      kind: "close_track",
      label: `Close ${failed}`,
      target: failed,
      incident_id: "INC-001",
      state: executed ? "DONE" : "PENDING",
      detail: executed
        ? `Track ${failed} confirmed out of service on the twin.`
        : "Not yet executed.",
    },
    ...rerouted.map((x, i) => ({
      id: `WI-${String(i + 2).padStart(3, "0")}`,
      kind: "reroute" as const,
      label: `Reroute ${x.id}`,
      target: x.id,
      incident_id: "INC-001",
      state: (executed ? "DONE" : "PENDING") as WorkState,
      detail: executed
        ? `${x.id} is running the alternate corridor.`
        : `${x.id} has not taken the alternate corridor yet.`,
    })),
  ];

  const recovered = Object.values(dispositions).filter((d) => d === "RECOVERED").length;
  const done = work.filter((w) => w.state === "DONE").length;
  const resolution: Resolution = executed
    ? "COMPLETE"
    : done > 0 || recovered > 0
      ? "PARTIAL"
      : "UNRESOLVED";
  const level = executed ? 0 : held.length > 0 ? 3 : 2;
  const tier = TIER_OWNERS[level];

  const incident: Incident = {
    id: "INC-001",
    kind: "TRACK_FAILURE",
    track_id: failed,
    corridor: `${failed} · ${stationName(track?.from ?? "")} ↔ ${stationName(track?.to ?? "")}`,
    level,
    ...tier,
    tier_label: tier.label,
    peak_code: TIER_OWNERS[Math.max(level, 2)].code,
    resolution,
    resolution_reason: executed
      ? `All ${affected.length} affected train(s) recovered and every committed action verified on the twin.`
      : `No committed action has landed on the twin yet; ${affected.length} train(s) impacted.`,
    acknowledged_by: null,
    age_seconds: 0,
    tier_age_seconds: 0,
    sla_seconds: level === 3 ? 120 : 180,
    closed: false,
    affected_trains: affected,
    passengers_at_risk: passengers,
    dispositions,
    progress: {
      trains_recovered: recovered,
      trains_total: affected.length,
      actions_done: done,
      actions_total: work.length,
    },
    drivers: [
      { code: "ASSET", level: 1, detail: `${failed} is out of service.` },
      ...(held.length
        ? [
            {
              code: "STOPPED",
              level: 3,
              detail: `${held.length} train(s) brought to a stand: ${held.join(", ")}.`,
            },
          ]
        : []),
    ],
    work_items: work,
    events: [
      {
        at: nowHMS(-4),
        from: "L0",
        to: "L1",
        reason: `Track failure detected on ${failed}.`,
        kind: "opened",
      },
      {
        at: nowHMS(-2),
        from: "L1",
        to: tier.code,
        reason: held.length
          ? `${held.length} train(s) brought to a stand.`
          : `${affected.length} train(s) routed into the failure.`,
        kind: "escalated",
      },
    ],
  };

  return {
    level,
    ...tier,
    resolution,
    totals: {
      open: 1,
      complete: resolution === "COMPLETE" ? 1 : 0,
      partial: resolution === "PARTIAL" ? 1 : 0,
      blocked: 0,
      unresolved: resolution === "UNRESOLVED" ? 1 : 0,
    },
    incidents: [incident],
    severed_pairs: 0,
    generated_at: nowHMS(),
  };
}

function isEscalation(x: unknown): x is Escalation {
  if (!x || typeof x !== "object") return false;
  const e = x as Record<string, unknown>;
  return (
    typeof e.code === "string" &&
    typeof e.resolution === "string" &&
    Array.isArray(e.incidents) &&
    !!e.totals
  );
}

/** Current handling level and completion signal. Null when the backend is offline. */
export async function fetchEscalation(): Promise<Escalation | null> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    const res = await fetch(`${API_BASE_URL}/escalation`, { signal: controller.signal });
    clearTimeout(timer);
    if (!res.ok) return null;
    const json: unknown = await res.json();
    return isEscalation(json) ? json : null;
  } catch {
    return null;
  }
}

/** Take ownership at the current tier. Returns the re-graded state, or null offline. */
export async function acknowledgeIncident(
  incidentId: string,
  owner = "Console Operator",
): Promise<Escalation | null> {
  try {
    const res = await fetch(
      `${API_BASE_URL}/incidents/${encodeURIComponent(incidentId)}/acknowledge?owner=${encodeURIComponent(owner)}`,
      { method: "POST" },
    );
    if (!res.ok) return null;
    const json: unknown = await res.json();
    return isEscalation(json) ? json : null;
  } catch {
    return null;
  }
}

export async function draftBrief(incidentId: string): Promise<HandoffBrief | null> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    const res = await fetch(`${API_BASE_URL}/incidents/${encodeURIComponent(incidentId)}/brief`, {
      method: "POST",
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!res.ok) return null;
    const json = (await res.json()) as HandoffBrief;
    return typeof json?.text === "string" ? json : null;
  } catch {
    return null;
  }
}

// ── Field work ──────────────────────────────────────────────────────────────
// Every verb here is carried out by the twin; the agent service relays its
// answer. Offline, or refused by the twin, reads as null — the console never
// fabricates field progress, because done is proved, not claimed.

function isWorkOrder(x: unknown): x is WorkOrder {
  if (!x || typeof x !== "object") return false;
  const o = x as Record<string, unknown>;
  return (
    typeof o.id === "string" &&
    typeof o.status === "string" &&
    typeof o.target === "string" &&
    Array.isArray(o.tasks)
  );
}

/** Work orders on the live twin. Null when the backend is offline. */
export async function fetchWorkOrders(): Promise<WorkOrder[] | null> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    const res = await fetch(`${API_BASE_URL}/workorders`, { signal: controller.signal });
    clearTimeout(timer);
    if (!res.ok) return null;
    const json = (await res.json()) as { work_orders?: unknown };
    const orders = json?.work_orders;
    return Array.isArray(orders) && orders.every(isWorkOrder) ? orders : null;
  } catch {
    return null;
  }
}

async function postWorkOrderVerb(path: string, timeoutMs = 8000): Promise<WorkOrder | null> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const res = await fetch(`${API_BASE_URL}${path}`, {
      method: "POST",
      signal: controller.signal,
    });
    clearTimeout(timer);
    if (!res.ok) return null;
    const json = (await res.json()) as { work_order?: unknown };
    return isWorkOrder(json?.work_order) ? json.work_order : null;
  } catch {
    return null;
  }
}

/** BLOCKED → PENDING for one task, or every blocked task in the order. */
export function retryWorkOrder(workOrderId: string, taskId?: string): Promise<WorkOrder | null> {
  const query = taskId ? `?task_id=${encodeURIComponent(taskId)}` : "";
  return postWorkOrderVerb(`/workorders/${encodeURIComponent(workOrderId)}/retry${query}`);
}

/** Cancel an order; the twin aborts work in flight and puts assets back. */
export function cancelWorkOrder(
  workOrderId: string,
  reason = "Cancelled by operator",
): Promise<WorkOrder | null> {
  return postWorkOrderVerb(
    `/workorders/${encodeURIComponent(workOrderId)}/cancel?reason=${encodeURIComponent(reason)}`,
  );
}

/** Order the twin's standard response on a failed section: close, crew, repair. */
export function dispatchFieldWork(trackId: string): Promise<WorkOrder | null> {
  return postWorkOrderVerb(`/workorders/incident-response/${encodeURIComponent(trackId)}`);
}

/** Fast-forward the twin so field work plays out on demand. True if it ran. */
export async function advanceTwin(ticks = 5): Promise<boolean> {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    const res = await fetch(`${API_BASE_URL}/workorders/tick?ticks=${ticks}`, {
      method: "POST",
      signal: controller.signal,
    });
    clearTimeout(timer);
    return res.ok;
  } catch {
    return false;
  }
}

/** Deterministic offline brief — same four sections the backend template writes. */
export function buildMockBrief(incident: Incident): HandoffBrief {
  const p = incident.progress;
  const stuck = Object.entries(incident.dispositions)
    .filter(([, d]) => d !== "RECOVERED")
    .map(([id, d]) => `${id} ${d.toLowerCase()}`);
  const done = incident.work_items.filter((w) => w.state === "DONE").map((w) => w.label);
  const pending = incident.work_items.filter((w) => w.state !== "DONE").map((w) => w.label);

  const ask =
    incident.resolution === "BLOCKED"
      ? `ASK: ${incident.owner} to authorise physical restoration — ${incident.resolution_reason}`
      : incident.resolution === "COMPLETE"
        ? `ASK: ${incident.owner} to confirm stand-down; all committed work is verified on the twin.`
        : incident.resolution === "PARTIAL"
          ? `ASK: ${incident.owner} to keep ownership until ${pending.join(", ") || "the outstanding actions"} complete.`
          : `ASK: ${incident.owner} to approve and execute a recovery plan now — no committed action has landed.`;

  return {
    incident_id: incident.id,
    resolution: incident.resolution,
    level: incident.code,
    escalating_to: incident.owner,
    source: "rules",
    text: [
      `SITUATION: ${incident.corridor} is out of service at ${incident.code} ${incident.tier_label}.`,
      `IMPACT: ${p.trains_recovered}/${p.trains_total} affected train(s) recovered, ${incident.passengers_at_risk} passengers at risk${stuck.length ? `; ${stuck.join(", ")}` : ""}.`,
      done.length
        ? `DONE: ${p.actions_done}/${p.actions_total} committed action(s) verified — ${done.join(", ")}.`
        : "DONE: nothing verified complete yet.",
      ask,
    ].join("\n"),
  };
}

export type StreamParams = {
  injectTrack?: string;
  weatherTrack?: string;
  condition?: string;
};

export type StreamOutcome = {
  result: RunResponse | null;
  /**
   * True when the backend received the request (any SSE event arrived). An
   * injection has then already been applied server-side — a caller that wants
   * a fallback must use plain /run, never the injecting endpoints again.
   */
  sawEvents: boolean;
};

/**
 * Run the pipeline with live SSE log streaming. Calls onLog per agent event.
 * Resolves with the final response plus whether the stream ever delivered
 * anything (see StreamOutcome for the re-injection contract).
 */
export function runSimulationStream(
  onLog: (e: LogEvent) => void,
  params: StreamParams = {},
): Promise<StreamOutcome> {
  return new Promise((resolve) => {
    if (typeof EventSource === "undefined") {
      resolve({ result: null, sawEvents: false });
      return;
    }
    const qs = new URLSearchParams();
    if (params.injectTrack) qs.set("inject_track", params.injectTrack);
    if (params.weatherTrack) {
      qs.set("weather_track", params.weatherTrack);
      qs.set("condition", params.condition ?? "STORM");
    }
    const url = `${API_BASE_URL}/run-stream${qs.size ? `?${qs}` : ""}`;

    let settled = false;
    let sawEvents = false;
    let result: RunResponse | null = null;
    const source = new EventSource(url);

    const finish = () => {
      if (settled) return;
      settled = true;
      source.close();
      resolve({ result, sawEvents });
    };

    const guard = setTimeout(finish, 60000);

    source.addEventListener("log", (ev) => {
      sawEvents = true;
      try {
        onLog(JSON.parse((ev as MessageEvent).data) as LogEvent);
      } catch {
        // ignore malformed log frames
      }
    });
    source.addEventListener("result", (ev) => {
      sawEvents = true;
      try {
        const parsed: unknown = JSON.parse((ev as MessageEvent).data);
        if (isValidRunResponse(parsed)) result = normalize(parsed);
      } catch {
        result = null;
      }
    });
    source.addEventListener("done", () => {
      clearTimeout(guard);
      finish();
    });
    source.onerror = () => {
      clearTimeout(guard);
      finish();
    };
  });
}

// Injection endpoints do strictly more work than /run (twin mutation
// round-trips + full pipeline + LLM budget) — give them the same 20s budget.
export async function injectTrackFailure(trackId: string): Promise<RunResponse> {
  const real = await tryFetch(
    `${API_BASE_URL}/simulate-track-failure/${trackId}`,
    { method: "POST" },
    20000,
  );
  if (real) return real;
  await new Promise((r) => setTimeout(r, 600));
  return buildMockRun(trackId);
}

export async function injectWeather(trackId: string, condition = "STORM"): Promise<RunResponse> {
  const real = await tryFetch(
    `${API_BASE_URL}/simulate-weather/${trackId}?condition=${encodeURIComponent(condition)}`,
    { method: "POST" },
    20000,
  );
  if (real) return real;
  await new Promise((r) => setTimeout(r, 600));
  return buildMockWeatherRun(trackId, condition);
}

export function incidentSummary(r: RunResponse | null) {
  if (!r || !r.failed_tracks?.length) return null;
  const failed = r.failed_tracks[0];
  const t = trackById(failed);
  const from = t ? stationName(t.from) : "—";
  const to = t ? stationName(t.to) : "—";
  const affected = r.agent_outputs.routing.affected_trains;
  const fleet = r.trains ?? TRAINS;
  const passengers = fleet
    .filter((tr) => affected.includes(tr.id))
    .reduce((a, b) => a + b.passengers, 0);
  return { trackId: failed, from, to, affectedCount: affected.length, passengers };
}

export { STATIONS, TRACKS, TRAINS };
