import { useEffect, useRef, useState } from "react";
import { Loader2, Send, ShieldAlert } from "lucide-react";
import MessageBubble from "./MessageBubble.jsx";

export default function ChatWindow({ messages, onSend, isLoading, error }) {
  const [input, setInput] = useState("");
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isLoading]);

  function handleSubmit(event) {
    event.preventDefault();
    onSend(input);
    setInput("");
  }

  return (
    <section className="flex min-h-[680px] flex-col overflow-hidden rounded-[2rem] border border-white/10 bg-slate-950/75 shadow-2xl shadow-black/40 backdrop-blur">
      <div className="border-b border-white/10 bg-white/[0.03] px-5 py-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-bold text-white">Operations Chat</h2>
            <p className="text-sm text-slate-400">
              Ask naturally. RailMind must call a tool before answering.
            </p>
          </div>
          <div className="rounded-full border border-emerald-300/20 bg-emerald-300/10 px-3 py-1 text-xs font-bold uppercase tracking-[0.22em] text-emerald-200">
            Online
          </div>
        </div>
      </div>

      {error ? (
        <div className="mx-5 mt-5 flex items-center gap-3 rounded-2xl border border-red-400/25 bg-red-500/10 px-4 py-3 text-sm text-red-100">
          <ShieldAlert className="size-5 shrink-0" />
          <span>{error}</span>
        </div>
      ) : null}

      <div className="flex-1 space-y-4 overflow-y-auto px-4 py-5 sm:px-6">
        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}

        {isLoading ? (
          <div className="flex justify-start">
            <div className="flex items-center gap-3 rounded-2xl border border-cyan-300/10 bg-cyan-300/[0.06] px-4 py-3 text-sm text-cyan-100">
              <Loader2 className="size-4 animate-spin" />
              RailMind is choosing the right tool...
            </div>
          </div>
        ) : null}

        <div ref={bottomRef} />
      </div>

      <form onSubmit={handleSubmit} className="border-t border-white/10 bg-slate-950/90 p-4">
        <div className="flex items-end gap-3 rounded-3xl border border-cyan-300/15 bg-white/[0.04] p-2 focus-within:border-cyan-300/45">
          <textarea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                handleSubmit(event);
              }
            }}
            rows={1}
            placeholder="Ask: What is the health of Track 7?"
            className="max-h-36 min-h-11 flex-1 resize-none bg-transparent px-4 py-3 text-sm text-white outline-none placeholder:text-slate-500"
            disabled={isLoading}
          />
          <button
            type="submit"
            disabled={isLoading || !input.trim()}
            className="grid size-11 place-items-center rounded-2xl bg-cyan-300 text-slate-950 transition hover:bg-cyan-200 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
            aria-label="Send message"
          >
            <Send className="size-5" />
          </button>
        </div>
      </form>
    </section>
  );
}
