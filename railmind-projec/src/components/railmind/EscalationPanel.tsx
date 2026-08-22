import { useCallback, useEffect, useRef, useState } from "react";
import {
  Ban,
  BellRing,
  CheckCircle2,
  CircleDashed,
  ClipboardList,
  Clock,
  Loader2,
  ShieldCheck,
  TriangleAlert,
  UserCheck,
  XCircle,
} from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import type {
  Disposition,
  Escalation,
  HandoffBrief,
  Incident,
  Resolution,
  WorkItem,
  WorkState,
} from "@/lib/railmind-api";

// Tier colour is the only place urgency is encoded twice (position on the
// ladder and hue), which is deliberate: the rail must read at a glance from
// across a control room, and the code text carries it for anyone who cannot
// separate the hues.
const TIER_TONE: Record<number, { text: string; bg: string; ring: string }> = {
  0: { text: "text-success", bg: "bg-success", ring: "glow-green" },
  1: { text: "text-info", bg: "bg-info", ring: "glow-blue" },
  2: { text: "text-warning", bg: "bg-warning", ring: "" },
  3: { text: "text-danger", bg: "bg-danger", ring: "glow-red" },
  4: { text: "text-danger", bg: "bg-danger", ring: "glow-red" },
};

const RESOLUTION_TONE: Record<
  Resolution,
  { text: string; border: string; bg: string; icon: typeof CheckCircle2; blurb: string }
> = {
  COMPLETE: {
    text: "text-success",
    border: "border-success/40",
    bg: "bg-success/15",
    icon: ShieldCheck,
    blurb: "Every committed action verified on the twin.",
  },
  PARTIAL: {
    text: "text-warning",
    border: "border-warning/40",
    bg: "bg-warning/15",
    icon: Clock,
    blurb: "Some work is verified; the rest is outstanding.",
  },
  BLOCKED: {
    text: "text-danger",
    border: "border-danger/40",
    bg: "bg-danger/15",
    icon: Ban,
    blurb: "Automated recovery is exhausted — physical intervention required.",
  },
  UNRESOLVED: {
    text: "text-muted-foreground",
    border: "border-border",
    bg: "bg-muted/40",
    icon: CircleDashed,
    blurb: "Nothing has landed on the twin yet.",
  },
};

const WORK_TONE: Record<WorkState, { icon: typeof CheckCircle2; cls: string }> = {
  DONE: { icon: CheckCircle2, cls: "text-success" },
  PENDING: { icon: CircleDashed, cls: "text-muted-foreground" },
  FAILED: { icon: XCircle, cls: "text-danger" },
  BLOCKED: { icon: Ban, cls: "text-danger" },
};

const DISPOSITION_TONE: Record<Disposition, string> = {
  RECOVERED: "text-success",
  EXPOSED: "text-warning",
  HELD: "text-danger",
  BLOCKED: "text-danger",
};

function minutes(seconds: number) {
  return `${(seconds / 60).toFixed(1)}m`;
}

export function ResolutionChip({
  resolution,
  size = "sm",
}: {
  resolution: Resolution;
  size?: "sm" | "xs";
}) {
  const tone = RESOLUTION_TONE[resolution];
  const Icon = tone.icon;
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 font-mono-mc ${
        size === "xs" ? "text-[10px]" : "text-[11px]"
      } ${tone.text} ${tone.border} ${tone.bg}`}
      title={tone.blurb}
    >
      <Icon className={size === "xs" ? "h-3 w-3" : "h-3.5 w-3.5"} />
      {resolution}
    </span>
  );
}

/** The ladder itself: five rungs, the live one filled. */
function TierLadder({ level }: { level: number }) {
  return (
    <div className="flex items-center gap-1" aria-hidden="true">
      {[0, 1, 2, 3, 4].map((rung) => (
        <span
          key={rung}
          className={`h-1.5 rounded-full transition-all duration-500 ${
            rung === level
              ? `w-7 ${TIER_TONE[level].bg}`
              : rung < level
                ? `w-3 ${TIER_TONE[level].bg} opacity-40`
                : "w-3 bg-muted"
          }`}
        />
      ))}
    </div>
  );
}

/**
 * The always-on strip under the header: what handling level the network is at,
 * who owns it, and whether the response is finished.
 */
export function EscalationRail({
  escalation,
  offline,
}: {
  escalation: Escalation | null;
  offline: boolean;
}) {
  const level = escalation?.level ?? 0;
  const tone = TIER_TONE[level];
  const prevLevel = useRef(level);
  const [raised, setRaised] = useState(false);

  // One authored moment: the rail marks the instant urgency moves up, then
  // settles. De-escalation is not announced — relief needs no alarm.
  useEffect(() => {
    if (level > prevLevel.current) {
      setRaised(true);
      const id = setTimeout(() => setRaised(false), 1400);
      prevLevel.current = level;
      return () => clearTimeout(id);
    }
    prevLevel.current = level;
  }, [level]);

  const totals = escalation?.totals;
  return (
    <div
      className={`col-span-12 flex flex-wrap items-center gap-x-6 gap-y-3 rounded-xl glass px-4 py-3 ${
        level >= 3 ? tone.ring : ""
      } ${raised ? "escalation-raised" : ""}`}
      role="status"
      aria-live="polite"
    >
      <div className="flex items-center gap-3">
        <div className={`grid h-9 w-9 place-items-center rounded-lg ${tone.bg}/15`}>
          {level >= 3 ? (
            <TriangleAlert className={`h-4.5 w-4.5 ${tone.text}`} />
          ) : (
            <BellRing className={`h-4.5 w-4.5 ${tone.text}`} />
          )}
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
            Handling Level
          </div>
          <div className={`font-mono-mc text-sm font-semibold ${tone.text}`}>
            {escalation?.code ?? "L0"} · {escalation?.label ?? "Steady State"}
          </div>
        </div>
      </div>

      <TierLadder level={level} />

      <div className="min-w-[190px]">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
          Owned by
        </div>
        <div className="font-mono-mc text-sm">{escalation?.owner ?? "Section Controller"}</div>
      </div>

      <div>
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
          Response
        </div>
        <div className="mt-0.5">
          <ResolutionChip resolution={escalation?.resolution ?? "COMPLETE"} />
        </div>
      </div>

      {totals && totals.open > 0 && (
        <div className="font-mono-mc text-[11px] text-muted-foreground">
          {totals.open} open · {totals.blocked} blocked · {totals.partial} partial ·{" "}
          {totals.unresolved} unresolved
        </div>
      )}

      <div className="ml-auto text-[11px] text-muted-foreground font-mono-mc">
        {offline ? "LEDGER: CONSOLE FALLBACK" : `GRADED ${escalation?.generated_at ?? "—"}`}
      </div>
    </div>
  );
}

function DwellMeter({ incident }: { incident: Incident }) {
  if (!incident.sla_seconds) return null;
  const ratio = Math.min(1, incident.tier_age_seconds / incident.sla_seconds);
  const late = ratio >= 1;
  return (
    <div>
      <div className="flex items-baseline justify-between text-[10px] font-mono-mc text-muted-foreground">
        <span className="uppercase tracking-wider">Time at this level</span>
        <span className={late ? "text-danger" : ""}>
          {minutes(incident.tier_age_seconds)} / {minutes(incident.sla_seconds)}
          {late ? " · escalating" : ""}
        </span>
      </div>
      <div
        className="mt-1 h-1 w-full rounded-full bg-muted overflow-hidden"
        role="progressbar"
        aria-valuenow={Math.round(ratio * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Time at this handling level against its limit"
      >
        <span
          className={`block h-full rounded-full transition-all duration-700 ${
            late ? "bg-danger" : ratio > 0.66 ? "bg-warning" : "bg-info"
          }`}
          style={{ width: `${Math.max(2, ratio * 100)}%` }}
        />
      </div>
    </div>
  );
}

function WorkRow({ item }: { item: WorkItem }) {
  const tone = WORK_TONE[item.state] ?? WORK_TONE.PENDING;
  const Icon = tone.icon;
  return (
    <li className="flex items-start gap-2.5 py-1.5">
      <Icon className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${tone.cls}`} />
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-mono-mc text-[12px]">{item.label}</span>
          <span className={`font-mono-mc text-[10px] ${tone.cls}`}>{item.state}</span>
        </div>
        <div className="text-[11px] text-muted-foreground">{item.detail}</div>
      </div>
    </li>
  );
}

function IncidentCard({
  incident,
  onAcknowledge,
  onBrief,
  busy,
}: {
  incident: Incident;
  onAcknowledge: (id: string) => Promise<void>;
  onBrief: (id: string) => Promise<HandoffBrief | null>;
  busy: boolean;
}) {
  const tone = TIER_TONE[incident.level];
  const [brief, setBrief] = useState<HandoffBrief | null>(null);
  const [drafting, setDrafting] = useState(false);
  const [acking, setAcking] = useState(false);
  const p = incident.progress;

  const draft = useCallback(async () => {
    setDrafting(true);
    try {
      setBrief(await onBrief(incident.id));
    } finally {
      setDrafting(false);
    }
  }, [incident.id, onBrief]);

  const ack = useCallback(async () => {
    setAcking(true);
    try {
      await onAcknowledge(incident.id);
    } finally {
      setAcking(false);
    }
  }, [incident.id, onAcknowledge]);

  return (
    <div className="rounded-lg glass p-3.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className={`font-mono-mc text-sm font-semibold ${tone.text}`}>{incident.code}</span>
        <span className="font-mono-mc text-[12px]">{incident.corridor}</span>
        <ResolutionChip resolution={incident.resolution} size="xs" />
        <span className="ml-auto font-mono-mc text-[10px] text-muted-foreground">
          {incident.id} · open {minutes(incident.age_seconds)}
        </span>
      </div>

      <div className="mt-1.5 text-[12px] text-muted-foreground">{incident.resolution_reason}</div>

      <div className="mt-3 grid gap-x-5 gap-y-3 sm:grid-cols-2 xl:grid-cols-3">
        <div className="space-y-2">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
              Recovery
            </div>
            <div className="font-mono-mc text-[12px]">
              {p.trains_recovered}/{p.trains_total} trains recovered · {p.actions_done}/
              {p.actions_total} actions verified
            </div>
          </div>
          <DwellMeter incident={incident} />
          <div className="flex flex-wrap gap-1.5 pt-0.5">
            {Object.entries(incident.dispositions).map(([train, d]) => (
              <span
                key={train}
                className={`font-mono-mc text-[10px] rounded border border-border px-1.5 py-0.5 ${DISPOSITION_TONE[d]}`}
              >
                {train} {d}
              </span>
            ))}
            {incident.affected_trains.length === 0 && (
              <span className="text-[11px] text-muted-foreground">No train impacted.</span>
            )}
          </div>
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
            Committed work
          </div>
          {incident.work_items.length === 0 ? (
            <div className="mt-1 text-[11px] text-muted-foreground">
              Nothing committed yet — execute a plan to open work items.
            </div>
          ) : (
            <ul className="mt-0.5">
              {incident.work_items.map((item) => (
                <WorkRow key={item.id} item={item} />
              ))}
            </ul>
          )}
          {incident.field_work && (
            <div className="mt-2">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
                Field work
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px]">
                <span className="font-mono-mc text-[10px] text-info">{incident.field_work.id}</span>
                {incident.field_work.status === "CANCELLED" ? (
                  <span className="font-mono-mc text-[10px] text-muted-foreground">CANCELLED</span>
                ) : (
                  <ResolutionChip resolution={incident.field_work.status} size="xs" />
                )}
                <span className="font-mono-mc text-[10px] text-muted-foreground">
                  {incident.field_work.completion_percentage}%
                  {incident.field_work.estimated_ticks_remaining > 0
                    ? ` · ≈ ${incident.field_work.estimated_ticks_remaining} ticks left`
                    : ""}
                </span>
              </div>
              {incident.field_work.blocked_reason ? (
                <div className="mt-0.5 text-[11px] text-danger">
                  {incident.field_work.blocked_reason}
                </div>
              ) : (
                incident.field_work.current && (
                  <div className="mt-0.5 text-[11px] text-muted-foreground">
                    {incident.field_work.current}
                  </div>
                )
              )}
            </div>
          )}
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
            Why this level
          </div>
          <ul className="mt-1 space-y-0.5">
            {incident.drivers.map((d) => (
              <li key={d.code + d.detail} className="flex gap-2 text-[11px]">
                <span className="font-mono-mc text-[10px] text-info w-[74px] shrink-0">
                  {d.code}
                </span>
                <span className="text-muted-foreground">{d.detail}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      {incident.events.length > 0 && (
        <div className="mt-3.5">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground font-mono-mc">
            Escalation history
          </div>
          <ul className="mt-1 space-y-0.5">
            {incident.events.slice(-4).map((e, i) => (
              <li key={`${e.at}-${i}`} className="flex gap-2 font-mono-mc text-[11px]">
                <span className="text-muted-foreground w-[68px] shrink-0">{e.at}</span>
                <span className="w-[68px] shrink-0 text-muted-foreground">
                  {e.from === e.to ? e.to : `${e.from} → ${e.to}`}
                </span>
                <span className="text-foreground/80 font-sans">{e.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          className="h-8"
          onClick={ack}
          disabled={busy || acking || !!incident.acknowledged_by}
        >
          {acking ? (
            <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
          ) : (
            <UserCheck className="h-3.5 w-3.5 mr-1.5" />
          )}
          {incident.acknowledged_by ? `Owned by ${incident.acknowledged_by}` : "Acknowledge"}
        </Button>
        <Button size="sm" variant="outline" className="h-8" onClick={draft} disabled={drafting}>
          {drafting ? (
            <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
          ) : (
            <ClipboardList className="h-3.5 w-3.5 mr-1.5" />
          )}
          Draft handoff brief
        </Button>
        {!incident.acknowledged_by && incident.sla_seconds && (
          <span className="font-mono-mc text-[10px] text-muted-foreground">
            Unacknowledged — escalates at {minutes(incident.sla_seconds)}
          </span>
        )}
      </div>

      {brief && (
        <div className="mt-3 rounded-md border border-border bg-background/40 p-3">
          <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider font-mono-mc text-muted-foreground">
            Handoff brief → {brief.escalating_to}
            <Badge variant="outline" className="font-mono-mc text-[9px]">
              {brief.source === "llm" ? "LLM" : "RULE ENGINE"}
            </Badge>
          </div>
          <pre className="mt-2 whitespace-pre-wrap font-sans text-[12px] leading-relaxed">
            {brief.text}
          </pre>
        </div>
      )}
    </div>
  );
}

export function IncidentTracker({
  escalation,
  offline,
  onAcknowledge,
  onBrief,
  busy = false,
}: {
  escalation: Escalation | null;
  offline: boolean;
  onAcknowledge: (id: string) => Promise<void>;
  onBrief: (id: string) => Promise<HandoffBrief | null>;
  busy?: boolean;
}) {
  const incidents = (escalation?.incidents ?? []).filter((i) => !i.closed);

  return (
    <Card className="col-span-12 p-0 glass overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-2">
          <TriangleAlert className="h-4 w-4 text-warning" />
          <div className="text-sm font-semibold">Escalation Tracker</div>
          <Badge variant="outline" className="ml-2 font-mono-mc text-[10px]">
            {offline ? "FALLBACK" : "LEDGER"}
          </Badge>
        </div>
        <div className="text-[11px] text-muted-foreground font-mono-mc">
          {incidents.length} open
        </div>
      </div>
      <ScrollArea className="h-[420px]">
        <div className="p-3 space-y-3">
          {incidents.length === 0 ? (
            <div className="flex flex-col items-center py-10 text-center">
              <ShieldCheck className="h-9 w-9 text-success mb-2" />
              <div className="text-sm font-semibold">Steady state</div>
              <div className="mt-1 text-xs text-muted-foreground max-w-[36ch]">
                No incident is open. Inject a failure and the ladder, the owner and the completion
                signal appear here.
              </div>
            </div>
          ) : (
            incidents.map((incident) => (
              <IncidentCard
                key={incident.id}
                incident={incident}
                onAcknowledge={onAcknowledge}
                onBrief={onBrief}
                busy={busy}
              />
            ))
          )}
        </div>
      </ScrollArea>
    </Card>
  );
}
