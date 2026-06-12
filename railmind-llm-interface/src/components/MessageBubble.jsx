import { useState } from "react";
import { Bot, ChevronDown, ChevronRight, User } from "lucide-react";

export default function MessageBubble({ message }) {
  const isUser = message.role === "user";

  return (
    <div className={`flex gap-3 ${isUser ? "justify-end" : "justify-start"}`}>
      {!isUser ? <Avatar icon={Bot} tone="assistant" /> : null}

      <div className={`max-w-[88%] space-y-2 sm:max-w-[76%] ${isUser ? "items-end" : ""}`}>
        <div
          className={
            isUser
              ? "rounded-[1.35rem] rounded-br-md bg-cyan-300 px-4 py-3 text-sm leading-6 text-slate-950 shadow-lg shadow-cyan-900/20"
              : "rounded-[1.35rem] rounded-bl-md border border-white/10 bg-white/[0.06] px-4 py-3 text-sm leading-6 text-slate-100"
          }
        >
          {message.content}
        </div>

        {!isUser && message.tool ? <ToolBadge tool={message.tool} demoMode={message.demoMode} /> : null}
      </div>

      {isUser ? <Avatar icon={User} tone="user" /> : null}
    </div>
  );
}

function Avatar({ icon: Icon, tone }) {
  const className =
    tone === "user"
      ? "grid size-9 shrink-0 place-items-center rounded-2xl bg-cyan-300 text-slate-950"
      : "grid size-9 shrink-0 place-items-center rounded-2xl border border-cyan-300/20 bg-cyan-300/10 text-cyan-100";

  return (
    <div className={className}>
      <Icon className="size-4" />
    </div>
  );
}

function ToolBadge({ tool, demoMode }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="overflow-hidden rounded-2xl border border-cyan-300/15 bg-slate-900/80">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-xs text-cyan-100"
      >
        <span className="flex items-center gap-2">
          {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
          <span className="font-bold uppercase tracking-[0.2em]">Tool Called</span>
          <span className="rounded-full bg-cyan-300/10 px-2 py-1 font-mono text-cyan-200">
            {tool.name}
          </span>
          {tool.endpoint ? (
            <span className="hidden rounded-full bg-white/5 px-2 py-1 font-mono text-slate-300 sm:inline">
              {tool.endpoint}
            </span>
          ) : null}
        </span>
        {demoMode ? (
          <span className="rounded-full bg-amber-300/10 px-2 py-1 font-semibold text-amber-200">
            demo mode
          </span>
        ) : null}
      </button>

      {open ? (
        <pre className="max-h-72 overflow-auto border-t border-white/10 bg-black/30 p-3 text-xs leading-5 text-slate-200">
          {JSON.stringify(tool, null, 2)}
        </pre>
      ) : null}
    </div>
  );
}
