const quickActions = [
  "Train delays today",
  "Track 7 health",
  "Weather impact",
  "Best scenario",
  "Active incidents",
  "Maintenance schedule"
];

export default function QuickActions({ onPick, disabled }) {
  return (
    <div className="grid gap-2">
      {quickActions.map((action) => (
        <button
          key={action}
          type="button"
          disabled={disabled}
          onClick={() => onPick(action)}
          className="group rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3 text-left text-sm font-semibold text-slate-200 transition hover:border-cyan-300/40 hover:bg-cyan-300/10 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
        >
          <span className="mr-2 text-cyan-300 transition group-hover:mr-3">/</span>
          {action}
        </button>
      ))}
    </div>
  );
}
