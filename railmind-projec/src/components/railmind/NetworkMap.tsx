import { useMemo, useState } from "react";
import { STATIONS, TRACKS, TRAINS, type Track } from "@/lib/railmind-mock";

type Props = {
  failedTracks?: string[];
  reroutedTrains?: { id: string; newRoute: string[] }[];
  highlightedRoute?: string[] | null;
  highlightedTrainId?: string | null;
};

export function NetworkMap({ failedTracks = [], reroutedTrains = [], highlightedRoute = null, highlightedTrainId = null }: Props) {
  const [hoverStation, setHoverStation] = useState<string | null>(null);
  const [hoverTrack, setHoverTrack] = useState<string | null>(null);

  const stationById = useMemo(
    () => Object.fromEntries(STATIONS.map((s) => [s.id, s])),
    [],
  );

  const tracksDisplay = useMemo(() => {
    return TRACKS.map((t) => {
      const status: Track["status"] = failedTracks.includes(t.id)
        ? "closed"
        : t.status;
      return { ...t, status };
    });
  }, [failedTracks]);

  const reroutePaths = reroutedTrains.map((tr) => {
    const pts = tr.newRoute.map((id) => stationById[id]).filter(Boolean);
    return { id: tr.id, points: pts };
  });

  function strokeFor(status: Track["status"]) {
    if (status === "closed") return "var(--danger)";
    if (status === "monitored") return "var(--warning)";
    return "var(--success)";
  }

  const hoveredStation = hoverStation ? stationById[hoverStation] : null;
  const stationTracks = hoveredStation
    ? TRACKS.filter((t) => t.from === hoveredStation.id || t.to === hoveredStation.id)
    : [];
  const stationTrains = hoveredStation
    ? TRAINS.filter((tr) => tr.route.includes(hoveredStation.id))
    : [];
  const hoveredTrack = hoverTrack ? TRACKS.find((t) => t.id === hoverTrack) : null;

  return (
    <div className="relative h-full w-full overflow-hidden rounded-xl">
      <svg viewBox="0 0 820 740" className="h-full w-full">
        <defs>
          <radialGradient id="bgGrad" cx="50%" cy="50%" r="60%">
            <stop offset="0%" stopColor="oklch(0.28 0.05 250 / 0.6)" />
            <stop offset="100%" stopColor="oklch(0.18 0.03 260 / 0)" />
          </radialGradient>
          <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
            <path d="M 40 0 L 0 0 0 40" fill="none" stroke="oklch(0.4 0.04 260 / 0.18)" strokeWidth="1" />
          </pattern>
        </defs>
        <rect width="820" height="740" fill="url(#grid)" />
        <rect width="820" height="740" fill="url(#bgGrad)" />

        {/* Tracks */}
        {tracksDisplay.map((t) => {
          const a = stationById[t.from];
          const b = stationById[t.to];
          if (!a || !b) return null;
          const stroke = strokeFor(t.status);
          const isFailed = t.status === "closed";
          return (
            <g key={t.id}>
              <line
                x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                stroke={stroke} strokeOpacity={isFailed ? 0.9 : 0.55}
                strokeWidth={isFailed ? 4 : 2.5}
                className={isFailed ? "dash-flow" : undefined}
                style={{ filter: isFailed ? "drop-shadow(0 0 6px var(--danger))" : undefined }}
              />
              <line
                x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                stroke="transparent" strokeWidth={14}
                onMouseEnter={() => setHoverTrack(t.id)}
                onMouseLeave={() => setHoverTrack(null)}
                style={{ cursor: "pointer" }}
              />
            </g>
          );
        })}

        {/* Reroute (blue) paths */}
        {reroutePaths.map((p, idx) => {
          if (p.points.length < 2) return null;
          const d = p.points.map((pt, i) => `${i === 0 ? "M" : "L"} ${pt.x} ${pt.y}`).join(" ");
          return (
            <path
              key={p.id + idx}
              d={d}
              fill="none"
              stroke="var(--info)"
              strokeWidth={3}
              strokeOpacity={0.85}
              className="dash-flow"
              style={{ filter: "drop-shadow(0 0 6px var(--info))" }}
            />
          );
        })}

        {/* Highlighted train route */}
        {highlightedRoute && highlightedRoute.length >= 2 && (() => {
          const pts = highlightedRoute.map((id) => stationById[id]).filter(Boolean);
          if (pts.length < 2) return null;
          const d = pts.map((pt, i) => `${i === 0 ? "M" : "L"} ${pt.x} ${pt.y}`).join(" ");
          return (
            <g>
              <path
                d={d}
                fill="none"
                stroke="var(--info)"
                strokeWidth={8}
                strokeOpacity={0.25}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              <path
                d={d}
                fill="none"
                stroke="var(--info)"
                strokeWidth={3.5}
                strokeLinecap="round"
                strokeLinejoin="round"
                className="dash-flow"
                style={{ filter: "drop-shadow(0 0 8px var(--info))" }}
              />
              {pts.map((pt, i) => (
                <circle key={i} cx={pt.x} cy={pt.y} r={6}
                  fill="var(--info)" stroke="oklch(0.16 0.03 260)" strokeWidth={2}
                  style={{ filter: "drop-shadow(0 0 6px var(--info))" }}
                />
              ))}
              {highlightedTrainId && pts[0] && (
                <text x={pts[0].x + 14} y={pts[0].y - 10}
                  fill="var(--info)" fontSize={11} className="font-mono-mc" fontWeight={700}>
                  {highlightedTrainId}
                </text>
              )}
            </g>
          );
        })()}



        {/* Stations */}
        {STATIONS.map((s) => {
          const active = hoverStation === s.id;
          return (
            <g
              key={s.id}
              onMouseEnter={() => setHoverStation(s.id)}
              onMouseLeave={() => setHoverStation(null)}
              style={{ cursor: "pointer" }}
            >
              <circle cx={s.x} cy={s.y} r={active ? 14 : 10}
                fill="oklch(0.2 0.03 260)"
                stroke="var(--success)" strokeWidth={2}
                style={{ filter: "drop-shadow(0 0 6px oklch(0.72 0.18 145 / 0.6))" }}
              />
              <circle cx={s.x} cy={s.y} r={4} fill="var(--success)" className="pulse-dot" />
              <text x={s.x + 14} y={s.y + 4}
                fill="oklch(0.92 0.01 240)" fontSize={11}
                className="font-mono-mc">
                {s.name}
              </text>
            </g>
          );
        })}
      </svg>

      {/* Legend */}
      <div className="absolute left-3 bottom-3 glass rounded-md px-3 py-2 text-[11px] font-mono-mc flex gap-3 items-center">
        <span className="flex items-center gap-1.5"><i className="inline-block h-2 w-2 rounded-full bg-success" />Healthy</span>
        <span className="flex items-center gap-1.5"><i className="inline-block h-2 w-2 rounded-full bg-warning" />Monitored</span>
        <span className="flex items-center gap-1.5"><i className="inline-block h-2 w-2 rounded-full bg-danger" />Closed</span>
        <span className="flex items-center gap-1.5"><i className="inline-block h-2 w-2 rounded-full bg-info" />Reroute</span>
      </div>

      {/* Hover tooltip */}
      {(hoveredStation || hoveredTrack) && (
        <div className="absolute right-3 top-3 glass rounded-md px-3 py-2 text-xs font-mono-mc max-w-[260px]">
          {hoveredStation && (
            <>
              <div className="text-success font-semibold">{hoveredStation.name} <span className="text-muted-foreground">({hoveredStation.id})</span></div>
              <div className="text-muted-foreground mt-1">Connected tracks: <span className="text-foreground">{stationTracks.map((t) => t.id).join(", ") || "—"}</span></div>
              <div className="text-muted-foreground">Active trains: <span className="text-foreground">{stationTrains.map((t) => t.id).join(", ") || "—"}</span></div>
            </>
          )}
          {hoveredTrack && !hoveredStation && (
            <>
              <div className="text-info font-semibold">Track {hoveredTrack.id}</div>
              <div className="text-muted-foreground">Route: <span className="text-foreground">{hoveredTrack.from} ↔ {hoveredTrack.to}</span></div>
              <div className="text-muted-foreground">Length: <span className="text-foreground">{hoveredTrack.length_km} km</span></div>
              <div className="text-muted-foreground">Health: <span className="text-foreground">{Math.round(hoveredTrack.health * 100)}%</span></div>
              <div className="text-muted-foreground">Status: <span className={
                failedTracks.includes(hoveredTrack.id) ? "text-danger" :
                hoveredTrack.status === "monitored" ? "text-warning" : "text-success"
              }>{failedTracks.includes(hoveredTrack.id) ? "closed" : hoveredTrack.status}</span></div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
