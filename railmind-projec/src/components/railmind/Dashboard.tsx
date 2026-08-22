import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  Cloud,
  CloudLightning,
  GitBranch,
  Loader2,
  Play,
  Radio,
  RotateCcw,
  Route,
  ShieldAlert,
  Siren,
  Sparkles,
  Train,
  Zap,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ScrollArea } from "@/components/ui/scroll-area";
import { NetworkMap } from "./NetworkMap";
import { TrainsPanel, getTrainRoute } from "./TrainsPanel";
import { EscalationRail, IncidentTracker } from "./EscalationPanel";
import { FieldWorkPanel } from "./FieldWorkPanel";
import { TRACKS, TRAINS, stationName, type Train as TrainType } from "@/lib/railmind-data";
import {
  acknowledgeIncident,
  advanceTwin,
  applyPlan,
  buildMockBrief,
  buildMockEscalation,
  cancelWorkOrder,
  dispatchFieldWork,
  draftBrief,
  fetchEscalation,
  fetchLiveState,
  incidentSummary,
  injectTrackFailure,
  injectWeather,
  resetTwin,
  retryWorkOrder,
  runSimulation,
  runSimulationStream,
  STEADY_STATE,
  type Escalation,
  type HandoffBrief,
  type LiveState,
  type LogEvent,
  type Plan,
  type RunResponse,
  type StreamOutcome,
  type StreamParams,
} from "@/lib/railmind-api";

function useClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

function StatusPill({ status }: { status: "operational" | "warning" | "incident" }) {
  const map = {
    operational: { label: "OPERATIONAL", cls: "text-success", dot: "bg-success" },
    warning: { label: "WARNING", cls: "text-warning", dot: "bg-warning" },
    incident: { label: "INCIDENT", cls: "text-danger", dot: "bg-danger" },
  } as const;
  const s = map[status];
  return (
    <div className="flex items-center gap-2 font-mono-mc text-xs">
      <span className={`h-2 w-2 rounded-full ${s.dot} pulse-dot`} />
      <span className={s.cls}>{s.label}</span>
    </div>
  );
}

function Metric({ label, value, accent }: { label: string; value: string; accent?: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
        {label}
      </div>
      <div className={`text-lg font-semibold font-mono-mc ${accent ?? "text-foreground"}`}>
        {value}
      </div>
    </div>
  );
}

function planColor(score: number, allScores: number[]) {
  const max = Math.max(...allScores);
  const min = Math.min(...allScores);
  if (score === max) return { ring: "glow-green", text: "text-success", bar: "bg-success" };
  if (score === min) return { ring: "glow-red", text: "text-danger", bar: "bg-danger" };
  return { ring: "", text: "text-warning", bar: "bg-warning" };
}

type RunRecord = {
  run: number;
  score: number;
  delay: number;
  risk: number;
  plan: string;
};

export function Dashboard() {
  const now = useClock();
  const [loading, setLoading] = useState(false);
  // True only while an SSE run-stream is open — unlike `loading`, which also
  // covers apply/reset, where the timeline must keep showing the last run's
  // log instead of an empty "connecting" state under a STREAMING badge.
  const [streaming, setStreaming] = useState(false);
  const [data, setData] = useState<RunResponse | null>(null);
  const [trackId, setTrackId] = useState<string>("T23");
  const [selectedTrain, setSelectedTrain] = useState<string | null>(null);
  const [selectedPlan, setSelectedPlan] = useState<string | null>(null);
  const [live, setLive] = useState<LiveState | null>(null);
  const [liveLog, setLiveLog] = useState<LogEvent[]>([]);
  // Escalation ledger. `null` means the backend never answered — the console
  // then grades locally from the mock run so the ladder is never blank.
  const [escalation, setEscalation] = useState<Escalation | null>(null);
  const [escalationOffline, setEscalationOffline] = useState(false);
  const [history, setHistory] = useState<RunRecord[]>([]);
  const [offlineTrains, setOfflineTrains] = useState<TrainType[]>(() =>
    TRAINS.map((t) => ({ ...t, current_station: t.route[0], route_index: 0, progress: 0 })),
  );
  const busyRef = useRef(false);
  // Latest-wins guard: an in-flight interval poll must not overwrite a
  // fresher post-run/post-apply snapshot with stale state.
  const liveSeqRef = useRef({ issued: 0, applied: 0 });
  // Hysteresis: a single dropped /state poll (timeout or blip) must not flap
  // the console to the mock fleet — flip live→mock only after 2 consecutive
  // failures; flip back on the first success.
  const liveFailStreakRef = useRef(0);

  const refreshLive = useCallback(async () => {
    const seq = ++liveSeqRef.current.issued;
    const state = await fetchLiveState();
    // Any success proves the twin is reachable, so it resets the failure
    // streak even when the snapshot loses the latest-wins race below — the
    // streak must only ever count *consecutive* genuine failures.
    if (state) liveFailStreakRef.current = 0;
    if (seq <= liveSeqRef.current.applied) return state;
    liveSeqRef.current.applied = seq;
    if (state) {
      setLive(state);
    } else if (++liveFailStreakRef.current >= 2) {
      setLive(null);
    }
    return state;
  }, []);

  // ── Live twin polling; offline mode animates the fallback fleet locally ──
  useEffect(() => {
    let stopped = false;
    const poll = async () => {
      if (busyRef.current) return;
      const state = await refreshLive();
      if (stopped) return;
      if (!state) {
        setOfflineTrains((prev) =>
          prev.map((t) => {
            const idx = t.route_index ?? 0;
            let progress = (t.progress ?? 0) + 0.18;
            let routeIndex = idx;
            let route = t.route;
            if (progress >= 1) {
              progress = 0;
              routeIndex = idx + 1;
              if (routeIndex >= route.length - 1) {
                route = [...route].reverse();
                routeIndex = 0;
              }
            }
            return {
              ...t,
              route,
              route_index: routeIndex,
              progress,
              current_station: route[routeIndex],
            };
          }),
        );
      }
    };
    void poll();
    const id = setInterval(() => void poll(), 3000);
    return () => {
      stopped = true;
      liveFailStreakRef.current = 0;
      clearInterval(id);
    };
  }, [refreshLive]);

  // ── Escalation ledger polling ────────────────────────────────────────────
  // Separate from /state on purpose: grading re-verifies committed work and
  // runs the dwell clock, so it must keep ticking while nothing else moves —
  // an untouched incident escalates on time even with the operator idle.
  const refreshEscalation = useCallback(async () => {
    const next = await fetchEscalation();
    if (next) {
      setEscalation(next);
      setEscalationOffline(false);
    }
    return next;
  }, []);

  useEffect(() => {
    let stopped = false;
    const poll = async () => {
      if (busyRef.current) return;
      const next = await refreshEscalation();
      if (stopped || next) return;
      // Backend silent: fall back to grading the last run locally, so the
      // ladder degrades with the rest of the console instead of disappearing.
      setEscalationOffline(true);
      setEscalation((prev) => prev ?? STEADY_STATE);
    };
    void poll();
    const id = setInterval(() => void poll(), 4000);
    return () => {
      stopped = true;
      clearInterval(id);
    };
  }, [refreshEscalation]);

  const adoptEscalation = useCallback((r: RunResponse | null, executed = false) => {
    if (r?.escalation) {
      setEscalation(r.escalation);
      setEscalationOffline(false);
      return;
    }
    if (r) {
      setEscalation(buildMockEscalation(r, executed));
      setEscalationOffline(true);
    }
  }, []);

  const onAcknowledge = useCallback(async (incidentId: string) => {
    const next = await acknowledgeIncident(incidentId);
    if (next) {
      setEscalation(next);
      setEscalationOffline(false);
      return;
    }
    // Offline: acknowledging is still a real operator act — record it
    // locally so ownership shows, and label the ledger as fallback.
    setEscalation((prev) =>
      prev
        ? {
            ...prev,
            incidents: prev.incidents.map((i) =>
              i.id === incidentId
                ? {
                    ...i,
                    acknowledged_by: "Console Operator (local)",
                    tier_age_seconds: 0,
                    events: [
                      ...i.events,
                      {
                        at: new Date().toTimeString().slice(0, 8),
                        from: i.code,
                        to: i.code,
                        reason: "Acknowledged on the console while the ledger was unreachable.",
                        kind: "acknowledged",
                      },
                    ],
                  }
                : i,
            ),
          }
        : prev,
    );
    setEscalationOffline(true);
  }, []);

  // ── Field work: every verb is carried out by the twin; after one lands,
  // re-read both the fleet (orders ride on /state) and the ledger.
  const afterFieldWork = useCallback(async () => {
    await Promise.all([refreshLive(), refreshEscalation()]);
  }, [refreshLive, refreshEscalation]);

  const onRetryWorkOrder = useCallback(
    async (id: string) => {
      const order = await retryWorkOrder(id);
      if (order) await afterFieldWork();
      return order;
    },
    [afterFieldWork],
  );

  const onCancelWorkOrder = useCallback(
    async (id: string) => {
      const order = await cancelWorkOrder(id);
      if (order) await afterFieldWork();
      return order;
    },
    [afterFieldWork],
  );

  const onAdvanceTwin = useCallback(
    async (ticks: number) => {
      const ok = await advanceTwin(ticks);
      if (ok) await afterFieldWork();
      return ok;
    },
    [afterFieldWork],
  );

  const onDispatchFieldWork = useCallback(
    async (track: string) => {
      const order = await dispatchFieldWork(track);
      if (order) await afterFieldWork();
      return order;
    },
    [afterFieldWork],
  );

  const onBrief = useCallback(
    async (incidentId: string): Promise<HandoffBrief | null> => {
      const real = await draftBrief(incidentId);
      if (real) return real;
      const incident = escalation?.incidents.find((i) => i.id === incidentId);
      return incident ? buildMockBrief(incident) : null;
    },
    [escalation],
  );

  const recordRun = useCallback((r: RunResponse) => {
    setHistory((prev) => [
      ...prev,
      {
        run: prev.length + 1,
        score: r.recommended_action.score,
        delay: r.recommended_action.delay_min,
        risk: r.recommended_action.risk,
        plan: r.recommended_action.id,
      },
    ]);
  }, []);

  const execute = useCallback(
    async (params: StreamParams, fallback: () => Promise<RunResponse>) => {
      if (busyRef.current) return;
      busyRef.current = true;
      setLoading(true);
      setLiveLog([]);
      setSelectedPlan(null);
      try {
        // Streaming stays true until this run's RESULT lands, not merely
        // until the stream closes — a mid-stream crash falls back to a
        // retry fetch, and during it the timeline must keep showing this
        // run's streamed events (with any crash notice), not the previous
        // run's execution log.
        setStreaming(true);
        const streamed: StreamOutcome = await runSimulationStream(
          (e) => setLiveLog((prev) => [e, ...prev]),
          params,
        );
        // If the stream delivered anything, the backend already applied the
        // injection — a retry must use plain /run, never re-inject. Hand the
        // injected track (and incident kind) to the mock fallback so an
        // offline retry still reports the right incident, storm-shaped when
        // the injection was weather.
        const result =
          streamed.result ??
          (streamed.sawEvents
            ? await runSimulation(
                params.injectTrack ?? params.weatherTrack,
                !params.injectTrack && params.weatherTrack ? "weather" : "track",
              )
            : await fallback());
        setData(result);
        recordRun(result);
        adoptEscalation(result);
        await refreshLive();
      } finally {
        setStreaming(false);
        setLoading(false);
        busyRef.current = false;
      }
    },
    [adoptEscalation, recordRun, refreshLive],
  );

  const doRun = useCallback(() => execute({}, runSimulation), [execute]);
  const doInject = useCallback(
    () => execute({ injectTrack: trackId }, () => injectTrackFailure(trackId)),
    [execute, trackId],
  );
  const doStorm = useCallback(
    () => execute({ weatherTrack: trackId, condition: "STORM" }, () => injectWeather(trackId)),
    [execute, trackId],
  );

  async function doApply() {
    if (busyRef.current) return;
    busyRef.current = true;
    setLoading(true);
    try {
      const result = await applyPlan(selectedPlan ?? undefined);
      if (result) {
        setData(result);
        recordRun(result);
        // The apply response carries the ledger's post-execution grade: what
        // the twin actually confirms, not what the executor reported.
        adoptEscalation(result, true);
        setSelectedPlan(null);
        await refreshLive();
      } else if (data) {
        // Backend offline — mirror the execution on the mock data so the
        // button still gives visible feedback.
        const target = plans.find((p) => p.id === selectedPlan) ?? data.recommended_action;
        setData({
          ...data,
          executed_plan: target.id,
          applied_actions: target.actions,
          execution_log: [
            {
              t: new Date().toTimeString().slice(0, 8),
              source: "Executor",
              message: `Plan ${target.id} executed (offline mock — no live twin connected)`,
            },
            ...data.execution_log,
          ],
        });
        adoptEscalation(data, true);
        setSelectedPlan(null);
      }
    } finally {
      setLoading(false);
      busyRef.current = false;
    }
  }

  async function doReset() {
    if (busyRef.current) return;
    busyRef.current = true;
    setLoading(true);
    try {
      const ok = await resetTwin();
      if (ok) {
        setData(null);
        setLiveLog([]);
        setSelectedPlan(null);
        setSelectedTrain(null);
        // The backend ledger was cleared with the twin; mirror that locally
        // so a resolved incident cannot linger on the rail.
        setEscalation(STEADY_STATE);
        await Promise.all([refreshLive(), refreshEscalation()]);
        return;
      }
      // The render-closure `live` can be seconds stale by now (busyRef pauses
      // polls during the reset call) — re-check liveness before deciding the
      // twin is gone, so a blipped-but-alive twin never gets its console wiped.
      const fresh = await refreshLive();
      if (!fresh) {
        // Fully offline (reset failed AND the confirming probe failed — two
        // independent signals, so the hysteresis intent holds): the incident
        // only exists in the mock — clear locally, mirroring the offline
        // apply, and flip the map to the mock fleet in the same render so
        // status, map and data never disagree.
        setLive(null);
        liveFailStreakRef.current = 0;
        setData(null);
        setLiveLog([]);
        setSelectedPlan(null);
        setSelectedTrain(null);
        setEscalation(STEADY_STATE);
        setEscalationOffline(true);
      } else {
        // Reset never reached the twin — wiping the console here would desync
        // it from the twin's real state, so keep everything and say so.
        const notice = {
          t: new Date().toTimeString().slice(0, 8),
          source: "System",
          message: "Reset failed — twin unreachable (state preserved)",
        };
        if (data) {
          setData({ ...data, execution_log: [notice, ...data.execution_log] });
        } else {
          setLiveLog((prev) => [notice, ...prev]);
        }
      }
    } finally {
      setLoading(false);
      busyRef.current = false;
    }
  }

  // Demo shortcuts: /?autorun runs the pipeline, /?inject=T23 injects a failure.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const inject = params.get("inject");
    if (!params.has("autorun") && !inject) return;
    if (inject) {
      setTrackId(inject);
      void execute({ injectTrack: inject }, () => injectTrackFailure(inject));
    } else {
      void doRun();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const incident = useMemo(() => incidentSummary(data), [data]);
  // An incident the ledger holds but this session has not run a pipeline for.
  const ledgerIncident = (escalation?.incidents ?? []).find((i) => !i.closed) ?? null;
  // Status follows the ledger as well as this session's run: an incident open
  // on the backend must not sit under an OPERATIONAL header just because this
  // browser has not run the pipeline itself.
  const status: "operational" | "warning" | "incident" =
    incident || (ledgerIncident?.level ?? 0) >= 3
      ? "incident"
      : data || ledgerIncident
        ? "warning"
        : "operational";

  const plans: Plan[] = data?.candidate_plans ?? [];
  const scores = plans.map((p) => p.score);
  // Prefer the polling snapshot; a run's train list only counts as "live" when
  // it carries positions — otherwise keep the locally animated fallback fleet.
  const dataTrainsAreLive = !!data?.trains?.some(
    (t) => typeof t.progress === "number" && t.current_station,
  );
  const trains = live?.trains ?? (dataTrainsAreLive && data?.trains ? data.trains : offlineTrains);
  const twinSource = live?.twin_source ?? data?.twin_source;

  // Map layers: live impassable tracks + selected-plan preview override.
  // CLOSING / UNDER_REPAIR are the twin's work-order states; it holds
  // traffic at them exactly like CLOSED (mirrors IMPASSABLE_STATUSES in
  // agents/escalation.py).
  const liveClosed = useMemo(
    () =>
      live
        ? live.tracks
            .filter((t) => ["CLOSED", "CLOSING", "UNDER_REPAIR"].includes(t.status))
            .map((t) => t.id)
        : [],
    [live],
  );
  const previewPlan = selectedPlan != null ? plans.find((p) => p.id === selectedPlan) : undefined;
  const mapFailed = previewPlan
    ? Array.from(new Set([...(previewPlan.failed_tracks ?? []), ...liveClosed]))
    : Array.from(new Set([...(data?.failed_tracks ?? []), ...liveClosed]));
  const mapRerouted = previewPlan
    ? (previewPlan.rerouted_trains ?? [])
    : (data?.rerouted_trains ?? []);
  const heldTrains = trains.filter((t) => t.held).map((t) => t.id);

  // While a stream is open, only ever show the current stream — never the
  // previous run's log under the STREAMING badge. Outside a stream (idle,
  // apply, reset) keep showing the last run's log.
  const timeline = streaming ? liveLog : (data?.execution_log ?? liveLog);

  return (
    <div className="min-h-screen text-foreground">
      {/* Header */}
      <header className="sticky top-0 z-30 glass border-b">
        <div className="mx-auto max-w-[1600px] px-5 py-3 flex items-center gap-4">
          <div className="flex items-center gap-3">
            <div className="grid h-10 w-10 place-items-center rounded-lg bg-success/15 glow-green">
              <Train className="h-5 w-5 text-success" />
            </div>
            <div className="leading-tight">
              <div className="text-base font-semibold tracking-wide">RailMind</div>
              <div className="text-[11px] text-muted-foreground font-mono-mc">
                AUTONOMOUS RAILWAY OS
              </div>
            </div>
          </div>

          <div className="hidden md:flex items-center gap-6 ml-6 pl-6 border-l border-border">
            <div>
              <div className="text-[10px] text-muted-foreground font-mono-mc uppercase">
                System Status
              </div>
              <StatusPill status={status} />
            </div>
            <div>
              <div className="text-[10px] text-muted-foreground font-mono-mc uppercase">
                Current Time
              </div>
              <div className="font-mono-mc text-sm">
                {now.toTimeString().slice(0, 8)} <span className="text-muted-foreground">UTC</span>
              </div>
            </div>
          </div>

          <div className="ml-auto flex items-center gap-2">
            <div className="hidden sm:flex items-center gap-2 glass rounded-md px-2 py-1.5">
              <span className="text-[10px] text-muted-foreground font-mono-mc uppercase pl-1">
                Track
              </span>
              <Select value={trackId} onValueChange={setTrackId}>
                <SelectTrigger className="h-8 w-[210px] border-0 bg-transparent font-mono-mc">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TRACKS.map((t) => (
                    <SelectItem key={t.id} value={t.id} className="font-mono-mc">
                      {t.id} · {stationName(t.from)} ↔ {stationName(t.to)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                size="sm"
                variant="destructive"
                onClick={doInject}
                disabled={loading}
                className="h-8"
              >
                <Siren className="h-4 w-4 mr-1.5" />
                Inject Failure
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={doStorm}
                disabled={loading}
                className="h-8 text-info border-info/40 hover:bg-info/10"
              >
                <CloudLightning className="h-4 w-4 mr-1.5" />
                Storm
              </Button>
            </div>
            <Button
              size="sm"
              variant="ghost"
              onClick={doReset}
              disabled={loading}
              className="h-9 text-muted-foreground"
              title="Reset the twin to baseline"
            >
              <RotateCcw className="h-4 w-4" />
            </Button>
            <Button
              size="sm"
              onClick={doRun}
              disabled={loading}
              className="h-9 bg-success text-primary-foreground hover:bg-success/90 glow-green"
            >
              {loading ? (
                <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
              ) : (
                <Play className="h-4 w-4 mr-1.5" />
              )}
              Run Full Simulation
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1600px] px-5 py-5 grid grid-cols-12 gap-4">
        {/* Escalation rail — handling level, owner, completion signal */}
        <EscalationRail escalation={escalation} offline={escalationOffline} />

        {/* Incident */}
        <Card
          className={`col-span-12 lg:col-span-3 p-4 glass ${incident ? "glow-red" : "glow-green"}`}
        >
          <div className="flex items-center justify-between">
            <div className="text-[11px] uppercase tracking-wider font-mono-mc text-muted-foreground">
              Incident Panel
            </div>
            {incident ? (
              <Badge className="bg-danger text-white font-mono-mc">ACTIVE</Badge>
            ) : (
              <Badge className="bg-success text-primary-foreground font-mono-mc">CLEAR</Badge>
            )}
          </div>
          {incident ? (
            <div className="mt-3 space-y-3">
              <div className="flex items-center gap-2">
                <AlertTriangle className="h-5 w-5 text-danger" />
                <div className="font-semibold text-danger font-mono-mc">TRACK FAILURE</div>
              </div>
              <div className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">
                    Track{mapFailed.length > 1 ? "s" : ""}
                  </span>
                  <span className="font-mono-mc">{(data?.failed_tracks ?? []).join(", ")}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Location</span>
                  <span className="font-mono-mc">
                    {incident.from.toUpperCase()} ↔ {incident.to.toUpperCase()}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Risk</span>
                  <span className="text-danger font-mono-mc">HIGH</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Affected Trains</span>
                  <span className="font-mono-mc">{incident.affectedCount}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Passengers</span>
                  <span className="font-mono-mc">{incident.passengers}</span>
                </div>
                {heldTrains.length > 0 && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Held</span>
                    <span className="text-danger font-mono-mc">{heldTrains.join(", ")}</span>
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div className="mt-6 flex flex-col items-center text-center py-4">
              {ledgerIncident ? (
                <>
                  <ShieldAlert className="h-10 w-10 text-warning mb-2" />
                  <div className="font-semibold">Open on the ledger</div>
                  <div className="text-xs text-muted-foreground mt-1">
                    {ledgerIncident.corridor} is at {ledgerIncident.code}{" "}
                    {ledgerIncident.tier_label}. Run the pipeline to generate response plans.
                  </div>
                </>
              ) : (
                <>
                  <CheckCircle2 className="h-10 w-10 text-success mb-2" />
                  <div className="font-semibold">No Active Incidents</div>
                  <div className="text-xs text-muted-foreground mt-1">
                    All systems nominal across the network.
                  </div>
                </>
              )}
            </div>
          )}
        </Card>

        {/* Map */}
        <Card className="col-span-12 lg:col-span-6 p-0 glass overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3 border-b border-border">
            <div className="flex items-center gap-2">
              <Activity className="h-4 w-4 text-info" />
              <div className="text-sm font-semibold">Live Digital Twin Network</div>
              <Badge variant="outline" className="ml-2 font-mono-mc text-[10px]">
                {twinSource ? `TWIN: ${twinSource.replace(/^https?:\/\//, "")}` : "CONNECTING…"}
              </Badge>
            </div>
            <div className="text-[11px] text-muted-foreground font-mono-mc">
              {mapFailed.length} closed · {mapRerouted.length} rerouted
              {selectedTrain && <span className="text-info"> · {selectedTrain} highlighted</span>}
            </div>
          </div>
          <div className="h-[520px]">
            <NetworkMap
              trains={trains}
              failedTracks={mapFailed}
              reroutedTrains={mapRerouted}
              weather={live?.weather ?? {}}
              previewLabel={previewPlan ? previewPlan.name : null}
              highlightedRoute={getTrainRoute(selectedTrain, trains, mapRerouted)}
              highlightedTrainId={selectedTrain}
            />
          </div>
        </Card>

        {/* Trains */}
        <div className="col-span-12 lg:col-span-3">
          <TrainsPanel
            trains={trains}
            selectedTrain={selectedTrain}
            onSelect={setSelectedTrain}
            reroutedTrains={data?.rerouted_trains ?? []}
            heldTrains={heldTrains}
          />
        </div>

        {/* Agent decisions */}
        <div className="col-span-12 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
          <AgentCard icon={<Cloud className="h-4 w-4" />} title="Weather Agent" color="text-info">
            <Metric label="Strategy" value={data?.agent_outputs.weather.strategy ?? "—"} />
            <Metric
              label="Risk Score"
              value={data ? data.agent_outputs.weather.risk_score.toFixed(2) : "—"}
              accent="text-success"
            />
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
                High Risk
              </div>
              <div className="flex flex-wrap gap-1 mt-1">
                {(data?.agent_outputs.weather.high_risk_tracks ?? []).map((t) => (
                  <Badge key={t} variant="outline" className="font-mono-mc text-[10px]">
                    {t}
                  </Badge>
                ))}
                {!data && <span className="text-xs text-muted-foreground">Awaiting run…</span>}
              </div>
            </div>
          </AgentCard>

          <AgentCard
            icon={<ShieldAlert className="h-4 w-4" />}
            title="Track Agent"
            color="text-warning"
          >
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
              Actions
            </div>
            <ActionList items={data?.agent_outputs.track.actions} />
          </AgentCard>

          <AgentCard icon={<Radio className="h-4 w-4" />} title="Signal Agent" color="text-success">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
              Actions
            </div>
            <ActionList items={data?.agent_outputs.signal.actions} />
          </AgentCard>

          <AgentCard icon={<Route className="h-4 w-4" />} title="Routing Agent" color="text-info">
            <Metric
              label="Affected Trains"
              value={String(data?.agent_outputs.routing.affected_trains.length ?? 0)}
            />
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
                Reroutes
              </div>
              <div className="mt-1 space-y-1">
                {(data?.agent_outputs.routing.reroutes ?? []).map((r) => (
                  <div key={r.train} className="text-xs font-mono-mc flex items-center gap-2">
                    <span className="text-foreground">{r.train}</span>
                    <span className="text-muted-foreground">→</span>
                    <span className="text-info">Rerouted</span>
                  </div>
                ))}
                {!data && <span className="text-xs text-muted-foreground">Awaiting run…</span>}
              </div>
            </div>
          </AgentCard>
        </div>

        {/* Plans */}
        <Card className="col-span-12 xl:col-span-7 p-4 glass">
          <div className="flex items-center gap-2 mb-1">
            <GitBranch className="h-4 w-4 text-info" />
            <div className="text-sm font-semibold">Generated Plans</div>
            <Badge variant="outline" className="ml-2 font-mono-mc text-[10px]">
              CANDIDATES
            </Badge>
          </div>
          {plans.length > 0 && (
            <div className="text-[11px] text-muted-foreground mb-3">
              Click a plan to preview its closures and reroutes on the map.
            </div>
          )}
          {plans.length === 0 ? (
            <EmptyHint text="Run a simulation to generate candidate plans." />
          ) : (
            <div className="grid sm:grid-cols-3 gap-3">
              {plans.map((p) => {
                const c = planColor(p.score, scores);
                const isBest = p.id === data?.recommended_action.id;
                const isSelected = selectedPlan === p.id;
                return (
                  <button
                    key={p.id}
                    onClick={() => setSelectedPlan(isSelected ? null : p.id)}
                    className={`text-left rounded-lg p-3 glass transition-all ${c.ring} ${
                      isSelected ? "border-info glow-blue" : "hover:border-info/50"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <div className="font-semibold font-mono-mc">Plan {p.id}</div>
                      <div className="flex gap-1">
                        {isSelected && (
                          <Badge className="bg-info/20 text-info border-info/40 font-mono-mc text-[10px]">
                            PREVIEW
                          </Badge>
                        )}
                        {isBest && (
                          <Badge className="bg-success text-primary-foreground font-mono-mc text-[10px]">
                            BEST
                          </Badge>
                        )}
                      </div>
                    </div>
                    <div className="text-[11px] text-muted-foreground mt-0.5 truncate">
                      {p.name}
                    </div>
                    <div className="mt-3 space-y-1.5 text-xs font-mono-mc">
                      <Row label="Delay" value={`${p.delay_min} min`} />
                      <Row label="Risk" value={p.risk.toFixed(2)} />
                      <Row label="Passengers" value={String(p.passengers_impacted)} />
                      <Row label="Congestion" value={p.congestion.toFixed(2)} />
                    </div>
                    <div className="mt-3">
                      <div className="flex items-center justify-between text-[10px] uppercase text-muted-foreground font-mono-mc">
                        <span>Score</span>
                        <span className={c.text}>{p.score}</span>
                      </div>
                      <div className="h-1.5 mt-1 rounded-full bg-secondary overflow-hidden">
                        <div
                          className={`h-full ${c.bar}`}
                          style={{ width: `${Math.max(0, Math.min(100, p.score))}%` }}
                        />
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </Card>

        {/* Recommended */}
        <Card className={`col-span-12 xl:col-span-5 p-5 glass ${data ? "glow-green" : ""}`}>
          <div className="flex items-center gap-2">
            <Zap className="h-4 w-4 text-success" />
            <div className="text-sm font-semibold">Recommended Action</div>
            <Badge className="ml-2 bg-success text-primary-foreground font-mono-mc text-[10px]">
              MASTER AGENT
            </Badge>
          </div>
          {!data ? (
            <EmptyHint text="No recommendation yet. Run the simulation pipeline to receive the optimal action plan." />
          ) : (
            <div className="mt-3">
              <div className="flex items-end justify-between">
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
                    Selected Plan
                  </div>
                  <div className="text-2xl font-bold text-success font-mono-mc">
                    Plan {data.recommended_action.id}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {data.recommended_action.name}
                  </div>
                </div>
                <div className="text-right">
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
                    Score
                  </div>
                  <div className="text-3xl font-bold text-success font-mono-mc">
                    {data.recommended_action.score}
                  </div>
                </div>
              </div>

              {data.explanation && (
                <div className="mt-3 rounded-md bg-secondary/40 border border-border px-3 py-2.5">
                  <div className="flex items-center gap-1.5 mb-1">
                    <Sparkles className="h-3.5 w-3.5 text-info" />
                    <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
                      Why this plan
                    </span>
                    <Badge variant="outline" className="ml-auto font-mono-mc text-[9px]">
                      {data.explanation.source === "llm" ? "LLM" : "RULE ENGINE"}
                    </Badge>
                  </div>
                  <p className="text-xs leading-relaxed text-foreground/90">
                    {data.explanation.text}
                  </p>
                </div>
              )}

              <div className="grid grid-cols-3 gap-3 mt-4">
                <Metric
                  label="Expected Delay"
                  value={`${data.recommended_action.delay_min}m`}
                  accent="text-warning"
                />
                <Metric
                  label="Passengers"
                  value={String(data.recommended_action.passengers_impacted)}
                  accent="text-info"
                />
                <Metric
                  label="Congestion"
                  value={data.recommended_action.congestion.toFixed(2)}
                  accent="text-success"
                />
              </div>

              <div className="mt-4">
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc mb-2">
                  Actions
                </div>
                <ul className="space-y-1.5">
                  {data.recommended_action.actions.map((a) => (
                    <li key={a} className="flex items-start gap-2 text-sm font-mono-mc">
                      <CheckCircle2 className="h-4 w-4 text-success mt-0.5 shrink-0" />
                      <span>{a}</span>
                    </li>
                  ))}
                </ul>
              </div>

              <Button
                size="sm"
                onClick={doApply}
                disabled={loading}
                className="mt-4 w-full bg-info text-primary-foreground hover:bg-info/90"
              >
                {loading ? (
                  <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                ) : (
                  <Zap className="h-4 w-4 mr-1.5" />
                )}
                Execute {selectedPlan ? `Plan ${selectedPlan}` : "Recommended Plan"} on Live Twin
              </Button>
              {data.executed_plan && (
                <div className="mt-2 text-[11px] text-info font-mono-mc text-center">
                  Plan {data.executed_plan} executed — {data.applied_actions?.length ?? 0} action(s)
                  applied to the twin.
                </div>
              )}
            </div>
          )}
        </Card>

        {/* Escalation tracker — per-incident completion signal and work */}
        <IncidentTracker
          escalation={escalation}
          offline={escalationOffline}
          onAcknowledge={onAcknowledge}
          onBrief={onBrief}
          busy={loading}
        />

        {/* Field work — the twin's work orders and crews, proved tick by tick */}
        <FieldWorkPanel
          workOrders={live?.work_orders ?? []}
          crews={live?.crews ?? {}}
          simTick={live?.sim_tick ?? null}
          offline={!live}
          busy={loading}
          dispatchTarget={trackId}
          onRetry={onRetryWorkOrder}
          onCancel={onCancelWorkOrder}
          onAdvance={onAdvanceTwin}
          onDispatch={onDispatchFieldWork}
        />

        {/* Timeline */}
        <Card className="col-span-12 xl:col-span-7 p-0 glass overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3 border-b border-border">
            <div className="flex items-center gap-2">
              <Activity className="h-4 w-4 text-info" />
              <div className="text-sm font-semibold">Agent Timeline</div>
              <Badge variant="outline" className="ml-2 font-mono-mc text-[10px]">
                {streaming ? "STREAMING" : "EVENT LOG"}
              </Badge>
            </div>
            <div className="text-[11px] text-muted-foreground font-mono-mc">
              {timeline.length} events
            </div>
          </div>
          <ScrollArea className="h-[260px]">
            <div className="px-4 py-2">
              {timeline.length === 0 ? (
                <EmptyHint text={streaming ? "Connecting to agent stream…" : "No events yet."} />
              ) : (
                <ol className="relative border-l border-border ml-2">
                  {timeline.map((e, i) => (
                    <li key={`${e.t}-${e.source}-${i}`} className="ml-4 py-2">
                      <span className="absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full bg-success glow-green" />
                      <div className="flex items-baseline gap-3 font-mono-mc">
                        <span className="text-xs text-muted-foreground w-20 shrink-0">{e.t}</span>
                        <span className="text-[10px] uppercase tracking-wider text-info w-20 shrink-0">
                          {e.source}
                        </span>
                        <span className="text-sm">{e.message}</span>
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </ScrollArea>
        </Card>

        {/* Analytics */}
        <Card className="col-span-12 xl:col-span-5 p-4 glass">
          <div className="flex items-center gap-2 mb-3">
            <BarChart3 className="h-4 w-4 text-info" />
            <div className="text-sm font-semibold">Run Analytics</div>
            <Badge variant="outline" className="ml-2 font-mono-mc text-[10px]">
              {history.length} RUNS
            </Badge>
          </div>
          {history.length === 0 ? (
            <EmptyHint text="Run the pipeline a few times — score and delay trends appear here." />
          ) : (
            <div className="space-y-4">
              <div>
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc mb-1">
                  Recommended-plan score per run (0–100)
                </div>
                <div className="h-[104px]">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={history} margin={{ top: 6, right: 8, bottom: 0, left: -22 }}>
                      <CartesianGrid stroke="var(--border)" strokeOpacity={0.4} vertical={false} />
                      <XAxis
                        dataKey="run"
                        tick={{ fill: "var(--muted-foreground)", fontSize: 10 }}
                        tickLine={false}
                        axisLine={{ stroke: "var(--border)" }}
                      />
                      <YAxis
                        domain={[0, 100]}
                        tick={{ fill: "var(--muted-foreground)", fontSize: 10 }}
                        tickLine={false}
                        axisLine={false}
                      />
                      <ChartTooltip
                        cursor={{ stroke: "var(--info)", strokeOpacity: 0.4 }}
                        contentStyle={{
                          background: "oklch(0.18 0.03 260)",
                          border: "1px solid var(--border)",
                          borderRadius: 8,
                          fontSize: 11,
                        }}
                        labelFormatter={(l) => `Run ${l}`}
                        formatter={(value, _name, item) => [
                          `${value}/100 (Plan ${(item?.payload as RunRecord | undefined)?.plan ?? "?"})`,
                          "Score",
                        ]}
                      />
                      <Line
                        type="monotone"
                        dataKey="score"
                        stroke="var(--info)"
                        strokeWidth={2}
                        dot={{ r: 3, fill: "var(--info)", strokeWidth: 0 }}
                        activeDot={{ r: 5 }}
                        isAnimationActive={false}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc mb-1">
                  Expected delay per run (minutes)
                </div>
                <div className="h-[104px]">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={history} margin={{ top: 6, right: 8, bottom: 0, left: -22 }}>
                      <CartesianGrid stroke="var(--border)" strokeOpacity={0.4} vertical={false} />
                      <XAxis
                        dataKey="run"
                        tick={{ fill: "var(--muted-foreground)", fontSize: 10 }}
                        tickLine={false}
                        axisLine={{ stroke: "var(--border)" }}
                      />
                      <YAxis
                        tick={{ fill: "var(--muted-foreground)", fontSize: 10 }}
                        tickLine={false}
                        axisLine={false}
                      />
                      <ChartTooltip
                        cursor={{ fill: "var(--info)", fillOpacity: 0.08 }}
                        contentStyle={{
                          background: "oklch(0.18 0.03 260)",
                          border: "1px solid var(--border)",
                          borderRadius: 8,
                          fontSize: 11,
                        }}
                        labelFormatter={(l) => `Run ${l}`}
                        formatter={(value) => [`${value} min`, "Delay"]}
                      />
                      <Bar
                        dataKey="delay"
                        fill="var(--warning)"
                        radius={[4, 4, 0, 0]}
                        maxBarSize={26}
                        isAnimationActive={false}
                      />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>
            </div>
          )}
        </Card>

        <div className="col-span-12 text-center text-[11px] text-muted-foreground font-mono-mc py-2">
          RAILMIND · AUTONOMOUS RAILWAY OS · LIVE DIGITAL TWIN
        </div>
      </main>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-foreground">{value}</span>
    </div>
  );
}

function AgentCard({
  icon,
  title,
  color,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  color: string;
  children: React.ReactNode;
}) {
  return (
    <Card className="p-4 glass">
      <div className="flex items-center gap-2 mb-3">
        <span className={color}>{icon}</span>
        <div className="text-sm font-semibold">{title}</div>
      </div>
      <div className="space-y-3">{children}</div>
    </Card>
  );
}

function ActionList({ items }: { items?: string[] }) {
  if (!items || items.length === 0)
    return <div className="text-xs text-muted-foreground mt-1">Awaiting run…</div>;
  return (
    <div className="mt-1 space-y-1">
      {items.map((a) => (
        <div key={a} className="text-xs font-mono-mc flex items-center gap-2">
          <span className="h-1.5 w-1.5 rounded-full bg-warning" />
          <span>{a}</span>
        </div>
      ))}
    </div>
  );
}

function EmptyHint({ text }: { text: string }) {
  return <div className="text-xs text-muted-foreground py-6 text-center font-mono-mc">{text}</div>;
}
