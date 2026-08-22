import { afterEach, describe, expect, it, vi } from "vitest";
import {
  buildMockBrief,
  buildMockEscalation,
  buildMockRun,
  buildMockWeatherRun,
  fetchEscalation,
  isValidRunResponse,
  normalize,
  runSimulation,
  type RunResponse,
} from "./railmind-api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("buildMockRun", () => {
  it("defaults to the T23 demo incident", () => {
    expect(buildMockRun().failed_tracks).toContain("T23");
  });

  it("shapes the incident around an explicit track id", () => {
    const run = buildMockRun("T05");
    expect(run.failed_tracks).toContain("T05");
    expect(run.failed_tracks).not.toContain("T23");
    expect(run.recommended_action.actions.join(" ")).toContain("T05");
  });
});

describe("buildMockWeatherRun", () => {
  it("shapes the mock as a weather incident, not a bare track failure", () => {
    const run = buildMockWeatherRun("T11");
    expect(run.failed_tracks).toContain("T11");
    expect(run.agent_outputs.weather.strategy).toBe("W1");
    expect(run.agent_outputs.weather.high_risk_tracks).toContain("T11");
    expect(run.execution_log[0].source).toBe("Weather");
    expect(run.execution_log[0].message).toContain("STORM reported on T11");
  });
});

describe("isValidRunResponse", () => {
  it("accepts a complete run", () => {
    expect(isValidRunResponse(buildMockRun())).toBe(true);
  });

  it("rejects a response whose plan is missing risk", () => {
    // normalizePlan would otherwise render the gap as a confident "Risk 0.00".
    const run = buildMockRun() as unknown as { candidate_plans: Record<string, unknown>[] };
    delete run.candidate_plans[1].risk;
    expect(isValidRunResponse(run)).toBe(false);
  });

  it("rejects a recommendation with a non-finite score", () => {
    const run = buildMockRun();
    run.recommended_action.score = NaN;
    expect(isValidRunResponse(run)).toBe(false);
  });
});

describe("normalize", () => {
  it("defaults every nested field the render path dereferences", () => {
    // Second belt behind isValidRunResponse (which now rejects a response this
    // sparse): even fed directly, it must come out fully defaulted, not crash.
    const sparse = {
      twin_source: "embedded",
      explanation: { text: "x", source: "rules" },
      recommended_action: { id: "A", name: "Plan A" },
      candidate_plans: [{ id: "B", name: "Plan B" }],
      execution_log: [],
    } as unknown as RunResponse;

    const r = normalize(sparse);
    expect(r.recommended_action.risk).toBe(0);
    expect(r.recommended_action.actions).toEqual([]);
    expect(r.candidate_plans[0].score).toBe(0);
    expect(r.failed_tracks).toEqual([]);
    expect(r.rerouted_trains).toEqual([]);
    expect(r.held_trains).toEqual([]);
    expect(r.agent_outputs.routing.affected_trains).toEqual([]);
    expect(r.agent_outputs.routing.reroutes).toEqual([]);
    expect(r.agent_outputs.weather.high_risk_tracks).toEqual([]);
    expect(r.agent_outputs.track.actions).toEqual([]);
    expect(r.trains?.length).toBeGreaterThan(0);
  });
});

describe("runSimulation", () => {
  it("falls back to a mock shaped by mockTrackId when the backend is offline", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const run = await runSimulation("T11");
    expect(run.failed_tracks).toContain("T11");
    expect(run.twin_source).toBeTruthy();
  });

  it("falls back to the default incident with no hint", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const run = await runSimulation();
    expect(run.failed_tracks).toContain("T23");
  });

  it("falls back to a weather-shaped mock when the injection was a storm", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const run = await runSimulation("T11", "weather");
    expect(run.failed_tracks).toContain("T11");
    expect(run.agent_outputs.weather.strategy).toBe("W1");
    expect(run.execution_log[0].message).toContain("STORM reported on T11");
  });
});

describe("escalation fallback", () => {
  it("grades a fresh incident as UNRESOLVED with nothing verified", () => {
    const run = buildMockRun("T23");
    const esc = buildMockEscalation(run);
    expect(esc.resolution).toBe("UNRESOLVED");
    expect(esc.incidents).toHaveLength(1);
    const incident = esc.incidents[0];
    expect(incident.track_id).toBe("T23");
    expect(incident.progress.actions_done).toBe(0);
    expect(incident.work_items.every((w) => w.state === "PENDING")).toBe(true);
  });

  it("grades an executed plan as COMPLETE and stands the ladder down", () => {
    const esc = buildMockEscalation(buildMockRun("T23"), true);
    expect(esc.resolution).toBe("COMPLETE");
    expect(esc.level).toBe(0);
    const incident = esc.incidents[0];
    expect(incident.progress.actions_done).toBe(incident.progress.actions_total);
    expect(Object.values(incident.dispositions).every((d) => d === "RECOVERED")).toBe(true);
  });

  it("reports steady state when no track has failed", () => {
    const run = { ...buildMockRun("T23"), failed_tracks: [] };
    expect(buildMockEscalation(run).level).toBe(0);
    expect(buildMockEscalation(run).incidents).toEqual([]);
  });
});

describe("fetchEscalation", () => {
  it("returns the backend payload when it is well-formed", async () => {
    const payload = {
      level: 2,
      code: "L2",
      label: "Elevated",
      owner: "Divisional Controller",
      posture: "",
      resolution: "PARTIAL",
      totals: { open: 1, complete: 0, partial: 1, blocked: 0, unresolved: 0 },
      incidents: [],
      severed_pairs: 0,
      generated_at: "10:00:00",
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => payload }));
    await expect(fetchEscalation()).resolves.toMatchObject({ code: "L2", resolution: "PARTIAL" });
  });

  it("returns null rather than a half-built ladder when the shape is wrong", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ level: 3 }) }),
    );
    await expect(fetchEscalation()).resolves.toBeNull();
  });

  it("returns null when the backend is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    await expect(fetchEscalation()).resolves.toBeNull();
  });
});

describe("buildMockBrief", () => {
  it("writes four labelled sections", () => {
    const incident = buildMockEscalation(buildMockRun("T23")).incidents[0];
    const lines = buildMockBrief(incident).text.split("\n");
    expect(lines.map((l) => l.split(":")[0])).toEqual(["SITUATION", "IMPACT", "DONE", "ASK"]);
  });

  it("asks for execution while nothing has landed", () => {
    const incident = buildMockEscalation(buildMockRun("T23")).incidents[0];
    const brief = buildMockBrief(incident);
    expect(brief.source).toBe("rules");
    expect(brief.text).toContain("nothing verified complete yet");
    expect(brief.text).toContain("approve and execute a recovery plan now");
  });

  it("asks for stand-down once the work is verified", () => {
    const incident = buildMockEscalation(buildMockRun("T23"), true).incidents[0];
    expect(buildMockBrief(incident).text).toContain("confirm stand-down");
  });
});
