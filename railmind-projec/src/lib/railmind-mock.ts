// Mock data + API helpers for RailMind. Tries to hit a real backend if present,
// otherwise generates realistic mock responses so the dashboard fully works.

export type Station = { id: string; name: string; x: number; y: number };
export type Track = {
  id: string;
  from: string;
  to: string;
  length_km: number;
  health: number; // 0..1
  status: "healthy" | "monitored" | "closed";
};
export type Train = {
  id: string;
  route: string[];
  passengers: number;
  rerouted?: boolean;
  newRoute?: string[];
};

export const STATIONS: Station[] = [
  { id: "DEL", name: "Delhi",      x: 120,  y: 90  },
  { id: "JAI", name: "Jaipur",     x: 70,   y: 230 },
  { id: "AGR", name: "Agra",       x: 240,  y: 170 },
  { id: "LKO", name: "Lucknow",    x: 420,  y: 130 },
  { id: "BHO", name: "Bhopal",     x: 300,  y: 330 },
  { id: "NAG", name: "Nagpur",     x: 470,  y: 420 },
  { id: "MUM", name: "Mumbai",     x: 110,  y: 470 },
  { id: "HYD", name: "Hyderabad",  x: 410,  y: 560 },
  { id: "KOL", name: "Kolkata",    x: 700,  y: 320 },
  { id: "CHN", name: "Chennai",    x: 520,  y: 660 },
];

export const TRACKS: Track[] = [
  { id: "T01", from: "DEL", to: "JAI", length_km: 280, health: 0.96, status: "healthy" },
  { id: "T02", from: "DEL", to: "AGR", length_km: 220, health: 0.93, status: "healthy" },
  { id: "T03", from: "DEL", to: "LKO", length_km: 540, health: 0.88, status: "healthy" },
  { id: "T04", from: "JAI", to: "AGR", length_km: 240, health: 0.91, status: "healthy" },
  { id: "T05", from: "AGR", to: "LKO", length_km: 320, health: 0.85, status: "monitored" },
  { id: "T06", from: "AGR", to: "BHO", length_km: 470, health: 0.9,  status: "healthy" },
  { id: "T07", from: "JAI", to: "MUM", length_km: 1150, health: 0.82, status: "healthy" },
  { id: "T08", from: "BHO", to: "MUM", length_km: 840, health: 0.87, status: "healthy" },
  { id: "T13", from: "BHO", to: "NAG", length_km: 340, health: 0.78, status: "monitored" },
  { id: "T14", from: "NAG", to: "HYD", length_km: 500, health: 0.9,  status: "healthy" },
  { id: "T15", from: "MUM", to: "HYD", length_km: 710, health: 0.86, status: "healthy" },
  { id: "T16", from: "LKO", to: "KOL", length_km: 980, health: 0.84, status: "healthy" },
  { id: "T17", from: "NAG", to: "KOL", length_km: 1110, health: 0.83, status: "healthy" },
  { id: "T18", from: "HYD", to: "CHN", length_km: 630, health: 0.92, status: "healthy" },
  { id: "T19", from: "KOL", to: "CHN", length_km: 1660, health: 0.81, status: "healthy" },
  { id: "T23", from: "BHO", to: "NAG", length_km: 350, health: 0.42, status: "monitored" },
];

export const TRAINS: Train[] = [
  { id: "TR00", route: ["DEL", "AGR", "BHO", "NAG", "HYD"], passengers: 312 },
  { id: "TR01", route: ["DEL", "JAI", "MUM"], passengers: 410 },
  { id: "TR02", route: ["DEL", "LKO", "KOL"], passengers: 520 },
  { id: "TR03", route: ["MUM", "HYD", "CHN"], passengers: 280 },
  { id: "TR04", route: ["AGR", "BHO", "NAG", "KOL"], passengers: 273 },
  { id: "TR05", route: ["NAG", "HYD", "CHN"], passengers: 198 },
];

export type AgentOutputs = {
  weather: { strategy: string; risk_score: number; high_risk_tracks: string[] };
  track:   { actions: string[] };
  signal:  { actions: string[] };
  routing: { affected_trains: string[]; reroutes: { train: string; from: string[]; to: string[] }[] };
};

export type Plan = {
  id: string;
  name: string;
  delay_min: number;
  risk: number;
  passengers_impacted: number;
  congestion: number;
  score: number;
  actions: string[];
};

export type LogEvent = { t: string; source: string; message: string };

export type RunResponse = {
  injected_failure?: string;
  recommended_action: Plan;
  candidate_plans: Plan[];
  agent_outputs: AgentOutputs;
  execution_log: LogEvent[];
  failed_tracks: string[];
  rerouted_trains: { id: string; newRoute: string[] }[];
};

const API_BASE_URL = "https://railmind-okf6.onrender.com";

function nowHMS(offset = 0) {
  const d = new Date(Date.now() + offset * 1000);
  return d.toTimeString().slice(0, 8);
}

function buildMockRun(trackId?: string): RunResponse {
  const failed = trackId ?? "T23";
  const t = TRACKS.find((x) => x.id === failed);
  const fromName = STATIONS.find((s) => s.id === t?.from)?.name ?? "BHOPAL";
  const toName   = STATIONS.find((s) => s.id === t?.to)?.name ?? "NAGPUR";

  const affected = TRAINS.filter((tr) =>
    tr.route.some((s, i) => i < tr.route.length - 1 && t && ((s === t.from && tr.route[i + 1] === t.to) || (s === t.to && tr.route[i + 1] === t.from))),
  );
  const passengers = affected.reduce((a, b) => a + b.passengers, 0);

  const agent_outputs: AgentOutputs = {
    weather: { strategy: "W5", risk_score: 0.18, high_risk_tracks: ["T13", "T05"] },
    track:   { actions: [`T_CLOSE_${failed}`, "T_MONITOR_T13", "T_MONITOR_T05"] },
    signal:  { actions: [`S_YELLOW_${failed}`, "S_GREEN_T01", "S_GREEN_T02", "S_RED_T13"] },
    routing: {
      affected_trains: affected.map((a) => a.id),
      reroutes: affected.map((a) => ({
        train: a.id,
        from: a.route,
        to: a.route.flatMap((s, i) =>
          i < a.route.length - 1 && t && s === t.from && a.route[i + 1] === t.to
            ? [s, "LKO"]
            : [s],
        ),
      })),
    },
  };

  const plans: Plan[] = [
    {
      id: "A", name: "Plan A — Hold & Wait",
      delay_min: 180, risk: 0.22, passengers_impacted: passengers,
      congestion: 0.72, score: 58,
      actions: [`Hold all trains at ${fromName}`, "Wait for track repair", "Notify passengers"],
    },
    {
      id: "B", name: "Plan B — Partial Reroute",
      delay_min: 95, risk: 0.12, passengers_impacted: Math.round(passengers * 0.6),
      congestion: 0.55, score: 78,
      actions: [`Close ${failed}`, "Reroute high-priority trains", "Hold freight"],
    },
    {
      id: "C", name: "Plan C — Full Reroute",
      delay_min: 60, risk: 0.05, passengers_impacted: passengers,
      congestion: 0.40, score: 94,
      actions: [`Close ${failed}`, `Set Signal Yellow on ${failed}`, ...affected.map((a) => `Reroute ${a.id}`)],
    },
  ];

  const recommended = plans.reduce((a, b) => (a.score >= b.score ? a : b));

  const base = Date.now();
  const log: LogEvent[] = [
    { t: nowHMS(-7), source: "Sensor",   message: `Anomaly detected on track ${failed}` },
    { t: nowHMS(-6), source: "Master",   message: `Failure confirmed: ${failed} (${fromName} ↔ ${toName})` },
    { t: nowHMS(-5), source: "Weather",  message: `Weather strategy ${agent_outputs.weather.strategy} applied` },
    { t: nowHMS(-5), source: "Track",    message: `Track Agent closed ${failed}` },
    { t: nowHMS(-4), source: "Signal",   message: `Signal Agent set ${failed} to YELLOW` },
    ...affected.map((a, i) => ({ t: nowHMS(-3 + i * 0.1), source: "Routing", message: `Routing Agent rerouted ${a.id}` })),
    { t: nowHMS(-2), source: "Planner",  message: `Planner generated ${plans.length} candidate plans` },
    { t: nowHMS(-1), source: "Simulator",message: `Simulator evaluated plans` },
    { t: nowHMS(0),  source: "Master",   message: `Master Agent selected ${recommended.name}` },
  ];
  // newest first
  log.reverse();
  void base;

  return {
    injected_failure: trackId,
    recommended_action: recommended,
    candidate_plans: plans,
    agent_outputs,
    execution_log: log,
    failed_tracks: [failed],
    rerouted_trains: affected.map((a) => ({
      id: a.id,
      newRoute: a.route.flatMap((s, i) =>
        i < a.route.length - 1 && t && s === t.from && a.route[i + 1] === t.to ? [s, "LKO"] : [s],
      ),
    })),
  };
}

async function tryFetch(url: string, init?: RequestInit): Promise<unknown | null> {
  try {
    const res = await fetch(url, init);
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export async function runSimulation(): Promise<RunResponse> {
  const real = await tryFetch(`${API_BASE_URL}/run`, { method: "POST" });
  if (real && typeof real === "object") return real as RunResponse;
  await new Promise((r) => setTimeout(r, 600));
  return buildMockRun();
}

export async function injectTrackFailure(trackId: string): Promise<RunResponse> {
  const real = await tryFetch(`${API_BASE_URL}/simulate-track-failure/${trackId}`, { method: "POST" });
  if (real && typeof real === "object") return real as RunResponse;
  await new Promise((r) => setTimeout(r, 600));
  return buildMockRun(trackId);
}

export function incidentSummary(r: RunResponse | null) {
  if (!r || !r.failed_tracks?.length) return null;
  const failed = r.failed_tracks[0];
  const t = TRACKS.find((x) => x.id === failed);
  const from = STATIONS.find((s) => s.id === t?.from)?.name ?? "—";
  const to   = STATIONS.find((s) => s.id === t?.to)?.name ?? "—";
  const affected = r.agent_outputs.routing.affected_trains;
  const passengers = TRAINS
    .filter((tr) => affected.includes(tr.id))
    .reduce((a, b) => a + b.passengers, 0);
  return { trackId: failed, from, to, affectedCount: affected.length, passengers };
}
