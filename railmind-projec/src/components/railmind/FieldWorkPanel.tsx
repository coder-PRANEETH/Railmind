import { useState } from "react";
import {
  Ban,
  CheckCircle2,
  CircleDashed,
  ClipboardList,
  FastForward,
  HardHat,
  Loader2,
  RotateCw,
  Wrench,
  XCircle,
} from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ResolutionChip } from "./EscalationPanel";
import { stationName, trackById } from "@/lib/railmind-data";
import type { CrewUnit, FieldTask, TaskStatus, WorkOrder } from "@/lib/railmind-api";

// Orders the twin is still working; everything else is history.
const LIVE = new Set(["UNRESOLVED", "PARTIAL", "BLOCKED"]);

const ACTION_LABEL: Record<string, string> = {
  CLOSE_TRACK: "Close section",
  DISPATCH_CREW: "Dispatch crew",
  REPAIR_TRACK: "Repair track",
  RESTORE_SIGNAL: "Restore signal",
  SPEED_RESTRICT: "Speed restriction",
  REROUTE_TRAIN: "Reroute train",
};

const TASK_TONE: Record<TaskStatus, { icon: typeof CheckCircle2; cls: string; spin?: boolean }> = {
  COMPLETED: { icon: CheckCircle2, cls: "text-success" },
  IN_PROGRESS: { icon: Loader2, cls: "text-info", spin: true },
  PENDING: { icon: CircleDashed, cls: "text-muted-foreground" },
  BLOCKED: { icon: Ban, cls: "text-danger" },
  UNRESOLVED: { icon: XCircle, cls: "text-danger" },
  CANCELLED: { icon: XCircle, cls: "text-muted-foreground" },
};

function corridor(target: string): string {
  const track = trackById(target);
  return track ? `${stationName(track.from)} ↔ ${stationName(track.to)}` : target;
}

function TaskRow({ task }: { task: FieldTask }) {
  const tone = TASK_TONE[task.status] ?? TASK_TONE.PENDING;
  const Icon = tone.icon;
  const running = task.status === "IN_PROGRESS";
  return (
    <li className="py-1">
      <div className="flex items-center gap-2 text-[11px]">
        <Icon className={`h-3.5 w-3.5 shrink-0 ${tone.cls} ${tone.spin ? "animate-spin" : ""}`} />
        <span className="font-mono-mc text-[10px] text-muted-foreground w-[52px] shrink-0">
          {task.id}
        </span>
        <span className="font-medium">{ACTION_LABEL[task.action] ?? task.action}</span>
        <span className="font-mono-mc text-[10px] text-muted-foreground">{task.target}</span>
        <span className={`ml-auto font-mono-mc text-[10px] ${tone.cls}`}>
          {task.status === "COMPLETED"
            ? "DONE"
            : task.status === "PENDING"
              ? "WAITING"
              : `${task.status}${task.ticks_remaining > 0 ? ` · ${task.ticks_remaining}t` : ""}`}
        </span>
      </div>
      {(running || task.status === "BLOCKED") && (
        <div
          className="mt-1 ml-[22px] h-1 w-[calc(100%-22px)] rounded-full bg-muted overflow-hidden"
          role="progressbar"
          aria-valuenow={Math.round(task.progress * 100)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`${ACTION_LABEL[task.action] ?? task.action} progress`}
        >
          <span
            className={`block h-full rounded-full transition-all duration-700 ${
              task.status === "BLOCKED" ? "bg-danger" : "bg-info"
            }`}
            style={{ width: `${Math.max(2, task.progress * 100)}%` }}
          />
        </div>
      )}
      {task.blocking_reason ? (
        <div className="ml-[22px] mt-0.5 text-[11px] text-danger">{task.blocking_reason}</div>
      ) : (
        task.detail &&
        task.status !== "COMPLETED" && (
          <div className="ml-[22px] mt-0.5 text-[11px] text-muted-foreground">{task.detail}</div>
        )
      )}
    </li>
  );
}

function OrderCard({
  order,
  crews,
  busy,
  pending,
  onRetry,
  onCancel,
}: {
  order: WorkOrder;
  crews: CrewUnit[];
  busy: boolean;
  pending: string | null;
  onRetry: (id: string) => Promise<unknown>;
  onCancel: (id: string) => Promise<unknown>;
}) {
  const live = LIVE.has(order.status);
  const blocked = order.tasks.some((t) => t.status === "BLOCKED");
  const acting = pending === order.id;
  const chip =
    order.status === "CANCELLED" ? (
      <span
        className="inline-flex items-center gap-1.5 rounded-md border border-border bg-muted/40 px-2 py-0.5 font-mono-mc text-[11px] text-muted-foreground"
        title="Cancelled by the operator; work in flight was put back."
      >
        <XCircle className="h-3.5 w-3.5" />
        CANCELLED
      </span>
    ) : (
      <ResolutionChip resolution={order.status} />
    );

  return (
    <div
      className={`rounded-lg glass p-3.5 ${order.status === "BLOCKED" ? "border-danger/40" : ""}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono-mc text-[12px] text-info">{order.id}</span>
        <span className="text-sm font-semibold">
          {order.target} · {corridor(order.target)}
        </span>
        {chip}
        <span className="ml-auto font-mono-mc text-[10px] text-muted-foreground">
          {order.incident_id ?? "no incident"} · {order.type.replace(/_/g, " ").toLowerCase()}
        </span>
      </div>

      <div className="mt-2 flex items-baseline justify-between text-[10px] font-mono-mc text-muted-foreground">
        <span>{order.completion_percentage}% complete</span>
        <span>
          {live
            ? order.estimated_ticks_remaining > 0
              ? `≈ ${order.estimated_ticks_remaining} ticks left`
              : "settling"
            : (order.cancel_reason ?? "finished")}
        </span>
      </div>
      <div
        className="mt-1 h-1.5 w-full rounded-full bg-muted overflow-hidden"
        role="progressbar"
        aria-valuenow={order.completion_percentage}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Work order completion"
      >
        <span
          className={`block h-full rounded-full transition-all duration-700 ${
            order.status === "COMPLETE"
              ? "bg-success"
              : order.status === "BLOCKED"
                ? "bg-danger"
                : "bg-info"
          }`}
          style={{ width: `${Math.max(2, order.completion_percentage)}%` }}
        />
      </div>

      <ul className="mt-2 divide-y divide-border/60">
        {order.tasks.map((t) => (
          <TaskRow key={t.id} task={t} />
        ))}
      </ul>

      {crews.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {crews.map((c) => (
            <span
              key={c.id}
              className={`font-mono-mc text-[10px] rounded border px-1.5 py-0.5 ${
                c.status === "ON_SITE"
                  ? "border-success/40 text-success"
                  : "border-info/40 text-info"
              }`}
            >
              {c.id} · {c.status === "ON_SITE" ? "on site" : "en route"} · {c.target}
            </span>
          ))}
        </div>
      )}

      {order.events.length > 0 && (
        <ul className="mt-2 space-y-0.5">
          {order.events.slice(-3).map((e, i) => (
            <li key={`${e.tick}-${i}`} className="flex gap-2 font-mono-mc text-[11px]">
              <span className="text-muted-foreground w-[52px] shrink-0">t{e.tick}</span>
              <span className="w-[72px] shrink-0 text-muted-foreground">{e.kind}</span>
              <span className="text-foreground/80 font-sans">{e.detail}</span>
            </li>
          ))}
        </ul>
      )}

      {live && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {blocked && (
            <Button
              size="sm"
              variant="outline"
              className="h-8"
              onClick={() => void onRetry(order.id)}
              disabled={busy || acting}
            >
              {acting ? (
                <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
              ) : (
                <RotateCw className="h-3.5 w-3.5 mr-1.5" />
              )}
              Retry blocked work
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            className="h-8 text-danger border-danger/40 hover:bg-danger/10"
            onClick={() => void onCancel(order.id)}
            disabled={busy || acting}
          >
            <XCircle className="h-3.5 w-3.5 mr-1.5" />
            Cancel order
          </Button>
          <span className="font-mono-mc text-[10px] text-muted-foreground">
            {blocked
              ? "Blocked work waits for an explicit retry."
              : "The twin reports progress as ticks pass."}
          </span>
        </div>
      )}
    </div>
  );
}

export function FieldWorkPanel({
  workOrders,
  crews,
  simTick,
  offline,
  busy = false,
  dispatchTarget,
  onRetry,
  onCancel,
  onAdvance,
  onDispatch,
}: {
  workOrders: WorkOrder[];
  crews: Record<string, CrewUnit>;
  simTick: number | null;
  offline: boolean;
  busy?: boolean;
  dispatchTarget: string;
  onRetry: (id: string) => Promise<WorkOrder | null>;
  onCancel: (id: string) => Promise<WorkOrder | null>;
  onAdvance: (ticks: number) => Promise<boolean>;
  onDispatch: (trackId: string) => Promise<WorkOrder | null>;
}) {
  const [pending, setPending] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const live = workOrders.filter((o) => LIVE.has(o.status));
  const crewList = Object.values(crews);

  const act = async (key: string, run: () => Promise<unknown>, ok: string, failed: string) => {
    setPending(key);
    try {
      const result = await run();
      setNote(result ? ok : failed);
    } finally {
      setPending(null);
    }
  };

  return (
    <Card className="col-span-12 p-0 glass overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-2">
          <HardHat className="h-4 w-4 text-info" />
          <div className="text-sm font-semibold">Field Work</div>
          <Badge variant="outline" className="ml-2 font-mono-mc text-[10px]">
            {offline ? "OFFLINE" : "TWIN"}
          </Badge>
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            className="h-8 text-info border-info/40 hover:bg-info/10"
            onClick={() =>
              void act(
                "dispatch",
                () => onDispatch(dispatchTarget),
                `Repair ordered on ${dispatchTarget}.`,
                `The twin did not accept a repair on ${dispatchTarget} (already active, or offline).`,
              )
            }
            disabled={busy || offline || pending !== null}
            title="Close the section, send a crew, rebuild it"
          >
            {pending === "dispatch" ? (
              <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
            ) : (
              <Wrench className="h-3.5 w-3.5 mr-1.5" />
            )}
            Order repair on {dispatchTarget}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="h-8"
            onClick={() =>
              void act(
                "tick",
                () => onAdvance(5),
                "Twin advanced 5 ticks.",
                "The twin could not be advanced (offline).",
              )
            }
            disabled={busy || offline || pending !== null}
            title="Play the simulation forward 5 ticks"
          >
            {pending === "tick" ? (
              <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
            ) : (
              <FastForward className="h-3.5 w-3.5 mr-1.5" />
            )}
            +5 ticks
          </Button>
          <div className="text-[11px] text-muted-foreground font-mono-mc">
            {live.length} live · tick {simTick ?? "—"}
          </div>
        </div>
      </div>
      {note && (
        <div className="px-4 py-1.5 border-b border-border font-mono-mc text-[10px] text-muted-foreground">
          {note}
        </div>
      )}
      <ScrollArea className="h-[420px]">
        <div className="p-3 space-y-3">
          {workOrders.length === 0 ? (
            <div className="flex flex-col items-center py-10 text-center">
              <ClipboardList className="h-9 w-9 text-muted-foreground mb-2" />
              <div className="text-sm font-semibold">No field work</div>
              <div className="mt-1 text-xs text-muted-foreground max-w-[40ch]">
                {offline
                  ? "Field work lives on the digital twin — reconnect the agent service to see it."
                  : "Execute a plan that closes a failed section, or order a repair on the selected track. The twin carries the work out over ticks and proves each step."}
              </div>
            </div>
          ) : (
            [...workOrders]
              .reverse()
              .map((order) => (
                <OrderCard
                  key={order.id}
                  order={order}
                  crews={crewList.filter((c) => c.work_order_id === order.id)}
                  busy={busy}
                  pending={pending}
                  onRetry={(id) =>
                    act(
                      id,
                      () => onRetry(id),
                      `${id}: blocked work queued for retry.`,
                      `${id}: nothing could be retried.`,
                    )
                  }
                  onCancel={(id) =>
                    act(id, () => onCancel(id), `${id} cancelled.`, `${id} could not be cancelled.`)
                  }
                />
              ))
          )}
        </div>
      </ScrollArea>
    </Card>
  );
}
