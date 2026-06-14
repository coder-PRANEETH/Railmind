import { useEffect, useMemo, useState } from "react";
import { Activity, AlertTriangle, CheckCircle2, Cloud, GitBranch, Loader2, Play, Radio, Route, ShieldAlert, Siren, Train, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ScrollArea } from "@/components/ui/scroll-area";
import { NetworkMap } from "./NetworkMap";
import {
  TRACKS,
  incidentSummary,
  injectTrackFailure,
  runSimulation,
  type Plan,
  type RunResponse,
} from "@/lib/railmind-mock";

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
    warning:     { label: "WARNING",     cls: "text-warning", dot: "bg-warning" },
    incident:    { label: "INCIDENT",    cls: "text-danger",  dot: "bg-danger"  },
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
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">{label}</div>
      <div className={`text-lg font-semibold font-mono-mc ${accent ?? "text-foreground"}`}>{value}</div>
    </div>
  );
}

function planColor(score: number, allScores: number[]) {
  const max = Math.max(...allScores);
  const min = Math.min(...allScores);
  if (score === max) return { ring: "glow-green", text: "text-success", bar: "bg-success" };
  if (score === min) return { ring: "glow-red",   text: "text-danger",  bar: "bg-danger"  };
  return { ring: "", text: "text-warning", bar: "bg-warning" };
}

export function Dashboard() {
  const now = useClock();
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<RunResponse | null>(null);
  const [trackId, setTrackId] = useState<string>("T23");

  async function doRun() {
    setLoading(true);
    try { setData(await runSimulation()); } finally { setLoading(false); }
  }
  async function doInject() {
    setLoading(true);
    try { setData(await injectTrackFailure(trackId)); } finally { setLoading(false); }
  }

  const incident = useMemo(() => incidentSummary(data), [data]);
  const status: "operational" | "warning" | "incident" =
    !data ? "operational" : incident ? "incident" : "warning";

  const plans: Plan[] = data?.candidate_plans ?? [];
  const scores = plans.map((p) => p.score);

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
              <div className="text-[11px] text-muted-foreground font-mono-mc">AUTONOMOUS RAILWAY OS</div>
            </div>
          </div>

          <div className="hidden md:flex items-center gap-6 ml-6 pl-6 border-l border-border">
            <div>
              <div className="text-[10px] text-muted-foreground font-mono-mc uppercase">System Status</div>
              <StatusPill status={status} />
            </div>
            <div>
              <div className="text-[10px] text-muted-foreground font-mono-mc uppercase">Current Time</div>
              <div className="font-mono-mc text-sm">{now.toTimeString().slice(0, 8)} <span className="text-muted-foreground">UTC</span></div>
            </div>
          </div>

          <div className="ml-auto flex items-center gap-2">
            <div className="hidden sm:flex items-center gap-2 glass rounded-md px-2 py-1.5">
              <span className="text-[10px] text-muted-foreground font-mono-mc uppercase pl-1">Track</span>
              <Select value={trackId} onValueChange={setTrackId}>
                <SelectTrigger className="h-8 w-[100px] border-0 bg-transparent font-mono-mc">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TRACKS.map((t) => (
                    <SelectItem key={t.id} value={t.id} className="font-mono-mc">
                      {t.id} · {t.from}↔{t.to}
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
            </div>
            <Button
              size="sm"
              onClick={doRun}
              disabled={loading}
              className="h-9 bg-success text-primary-foreground hover:bg-success/90 glow-green"
            >
              {loading ? <Loader2 className="h-4 w-4 mr-1.5 animate-spin" /> : <Play className="h-4 w-4 mr-1.5" />}
              Run Full Simulation
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1600px] px-5 py-5 grid grid-cols-12 gap-4">
        {/* Incident */}
        <Card className={`col-span-12 lg:col-span-3 p-4 glass ${incident ? "glow-red" : "glow-green"}`}>
          <div className="flex items-center justify-between">
            <div className="text-[11px] uppercase tracking-wider font-mono-mc text-muted-foreground">Incident Panel</div>
            {incident
              ? <Badge className="bg-danger text-white font-mono-mc">ACTIVE</Badge>
              : <Badge className="bg-success text-primary-foreground font-mono-mc">CLEAR</Badge>}
          </div>
          {incident ? (
            <div className="mt-3 space-y-3">
              <div className="flex items-center gap-2">
                <AlertTriangle className="h-5 w-5 text-danger" />
                <div className="font-semibold text-danger font-mono-mc">TRACK FAILURE</div>
              </div>
              <div className="space-y-2 text-sm">
                <div className="flex justify-between"><span className="text-muted-foreground">Track</span><span className="font-mono-mc">{incident.trackId}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">Location</span><span className="font-mono-mc">{incident.from.toUpperCase()} ↔ {incident.to.toUpperCase()}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">Risk</span><span className="text-danger font-mono-mc">HIGH</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">Affected Trains</span><span className="font-mono-mc">{incident.affectedCount}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">Passengers</span><span className="font-mono-mc">{incident.passengers}</span></div>
              </div>
            </div>
          ) : (
            <div className="mt-6 flex flex-col items-center text-center py-4">
              <CheckCircle2 className="h-10 w-10 text-success mb-2" />
              <div className="font-semibold">No Active Incidents</div>
              <div className="text-xs text-muted-foreground mt-1">All systems nominal across the network.</div>
            </div>
          )}
        </Card>

        {/* Map */}
        <Card className="col-span-12 lg:col-span-9 p-0 glass overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3 border-b border-border">
            <div className="flex items-center gap-2">
              <Activity className="h-4 w-4 text-info" />
              <div className="text-sm font-semibold">Live Digital Twin Network</div>
              <Badge variant="outline" className="ml-2 font-mono-mc text-[10px]">REAL-TIME</Badge>
            </div>
            <div className="text-[11px] text-muted-foreground font-mono-mc">
              {data?.failed_tracks?.length ?? 0} closed · {data?.rerouted_trains?.length ?? 0} rerouted
            </div>
          </div>
          <div className="h-[520px]">
            <NetworkMap
              failedTracks={data?.failed_tracks ?? []}
              reroutedTrains={data?.rerouted_trains ?? []}
            />
          </div>
        </Card>

        {/* Agent decisions */}
        <div className="col-span-12 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
          <AgentCard
            icon={<Cloud className="h-4 w-4" />}
            title="Weather Agent"
            color="text-info"
          >
            <Metric label="Strategy" value={data?.agent_outputs.weather.strategy ?? "—"} />
            <Metric label="Risk Score" value={data ? data.agent_outputs.weather.risk_score.toFixed(2) : "—"} accent="text-success" />
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">High Risk</div>
              <div className="flex flex-wrap gap-1 mt-1">
                {(data?.agent_outputs.weather.high_risk_tracks ?? []).map((t) => (
                  <Badge key={t} variant="outline" className="font-mono-mc text-[10px]">{t}</Badge>
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
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">Actions</div>
            <ActionList items={data?.agent_outputs.track.actions} />
          </AgentCard>

          <AgentCard
            icon={<Radio className="h-4 w-4" />}
            title="Signal Agent"
            color="text-success"
          >
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">Actions</div>
            <ActionList items={data?.agent_outputs.signal.actions} />
          </AgentCard>

          <AgentCard
            icon={<Route className="h-4 w-4" />}
            title="Routing Agent"
            color="text-info"
          >
            <Metric label="Affected Trains" value={String(data?.agent_outputs.routing.affected_trains.length ?? 0)} />
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">Reroutes</div>
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
          <div className="flex items-center gap-2 mb-3">
            <GitBranch className="h-4 w-4 text-info" />
            <div className="text-sm font-semibold">Generated Plans</div>
            <Badge variant="outline" className="ml-2 font-mono-mc text-[10px]">CANDIDATES</Badge>
          </div>
          {plans.length === 0 ? (
            <EmptyHint text="Run a simulation to generate candidate plans." />
          ) : (
            <div className="grid sm:grid-cols-3 gap-3">
              {plans.map((p) => {
                const c = planColor(p.score, scores);
                const isBest = p.id === data?.recommended_action.id;
                return (
                  <div key={p.id} className={`rounded-lg p-3 glass ${c.ring}`}>
                    <div className="flex items-center justify-between">
                      <div className="font-semibold font-mono-mc">Plan {p.id}</div>
                      {isBest && <Badge className="bg-success text-primary-foreground font-mono-mc text-[10px]">BEST</Badge>}
                    </div>
                    <div className="text-[11px] text-muted-foreground mt-0.5 truncate">{p.name}</div>
                    <div className="mt-3 space-y-1.5 text-xs font-mono-mc">
                      <Row label="Delay" value={`${p.delay_min} min`} />
                      <Row label="Risk" value={p.risk.toFixed(2)} />
                      <Row label="Passengers" value={String(p.passengers_impacted)} />
                      <Row label="Congestion" value={p.congestion.toFixed(2)} />
                    </div>
                    <div className="mt-3">
                      <div className="flex items-center justify-between text-[10px] uppercase text-muted-foreground font-mono-mc">
                        <span>Score</span><span className={c.text}>{p.score}</span>
                      </div>
                      <div className="h-1.5 mt-1 rounded-full bg-secondary overflow-hidden">
                        <div className={`h-full ${c.bar}`} style={{ width: `${p.score}%` }} />
                      </div>
                    </div>
                  </div>
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
            <Badge className="ml-2 bg-success text-primary-foreground font-mono-mc text-[10px]">MASTER AGENT</Badge>
          </div>
          {!data ? (
            <EmptyHint text="No recommendation yet. Run the simulation pipeline to receive the optimal action plan." />
          ) : (
            <div className="mt-3">
              <div className="flex items-end justify-between">
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">Selected Plan</div>
                  <div className="text-2xl font-bold text-success font-mono-mc">Plan {data.recommended_action.id}</div>
                  <div className="text-xs text-muted-foreground">{data.recommended_action.name}</div>
                </div>
                <div className="text-right">
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">Score</div>
                  <div className="text-3xl font-bold text-success font-mono-mc">{data.recommended_action.score}</div>
                </div>
              </div>

              <div className="grid grid-cols-3 gap-3 mt-4">
                <Metric label="Expected Delay" value={`${data.recommended_action.delay_min}m`} accent="text-warning" />
                <Metric label="Passengers" value={String(data.recommended_action.passengers_impacted)} accent="text-info" />
                <Metric label="Congestion" value={data.recommended_action.congestion.toFixed(2)} accent="text-success" />
              </div>

              <div className="mt-4">
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc mb-2">Actions</div>
                <ul className="space-y-1.5">
                  {data.recommended_action.actions.map((a) => (
                    <li key={a} className="flex items-start gap-2 text-sm font-mono-mc">
                      <CheckCircle2 className="h-4 w-4 text-success mt-0.5 shrink-0" />
                      <span>{a}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}
        </Card>

        {/* Timeline */}
        <Card className="col-span-12 p-0 glass overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3 border-b border-border">
            <div className="flex items-center gap-2">
              <Activity className="h-4 w-4 text-info" />
              <div className="text-sm font-semibold">Agent Timeline</div>
              <Badge variant="outline" className="ml-2 font-mono-mc text-[10px]">EVENT LOG</Badge>
            </div>
            <div className="text-[11px] text-muted-foreground font-mono-mc">{data?.execution_log.length ?? 0} events</div>
          </div>
          <ScrollArea className="h-[240px]">
            <div className="px-4 py-2">
              {!data ? (
                <EmptyHint text="No events yet." />
              ) : (
                <ol className="relative border-l border-border ml-2">
                  {data.execution_log.map((e, i) => (
                    <li key={i} className="ml-4 py-2">
                      <span className="absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full bg-success glow-green" />
                      <div className="flex items-baseline gap-3 font-mono-mc">
                        <span className="text-xs text-muted-foreground w-20 shrink-0">{e.t}</span>
                        <span className="text-[10px] uppercase tracking-wider text-info w-20 shrink-0">{e.source}</span>
                        <span className="text-sm">{e.message}</span>
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </ScrollArea>
        </Card>

        <div className="col-span-12 text-center text-[11px] text-muted-foreground font-mono-mc py-2">
          RAILMIND · AUTONOMOUS RAILWAY OS · MVP DEMO
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

function AgentCard({ icon, title, color, children }: { icon: React.ReactNode; title: string; color: string; children: React.ReactNode }) {
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
  return (
    <div className="text-xs text-muted-foreground py-6 text-center font-mono-mc">{text}</div>
  );
}
