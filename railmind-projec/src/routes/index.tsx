import { createFileRoute } from "@tanstack/react-router";
import { Dashboard } from "@/components/railmind/Dashboard";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "RailMind — Autonomous Railway OS" },
      { name: "description", content: "Mission-control dashboard for AI-driven railway operations: live digital twin, agent decisions, plan generation, simulation, and recommended actions." },
      { property: "og:title", content: "RailMind — Autonomous Railway OS" },
      { property: "og:description", content: "AI-powered railway command center with live digital twin, agent decisions, and recommended actions." },
    ],
  }),
  component: Index,
});

function Index() {
  return <Dashboard />;
}
