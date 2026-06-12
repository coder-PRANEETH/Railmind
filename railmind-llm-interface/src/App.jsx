import { useState } from "react";
import { Activity, Bot, RadioTower, Sparkles, TrainFront } from "lucide-react";
import ChatWindow from "./components/ChatWindow.jsx";
import QuickActions from "./components/QuickActions.jsx";

const starterMessage = {
  id: crypto.randomUUID(),
  role: "assistant",
  content:
    "I am RailMind. Ask me about train delays, track health, signals, incidents, agent recommendations, or future scenarios.",
  tool: null
};

export default function App() {
  const [messages, setMessages] = useState([starterMessage]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");

  async function sendMessage(text) {
    const cleanText = text.trim();
    if (!cleanText || isLoading) return;

    const userMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: cleanText
    };

    const nextMessages = [...messages, userMessage];
    setMessages(nextMessages);
    setError("");
    setIsLoading(true);

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: nextMessages.map(({ role, content }) => ({ role, content }))
        })
      });

      const payload = await response.json();

      if (!response.ok) {
        throw new Error(payload.error || "RailMind request failed.");
      }

      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: payload.reply,
          tool: payload.tool,
          demoMode: payload.demoMode
        }
      ]);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <main className="min-h-screen overflow-hidden bg-[#050813] text-slate-100">
      <div className="pointer-events-none fixed inset-0 bg-[radial-gradient(circle_at_15%_15%,rgba(0,212,255,0.18),transparent_32%),radial-gradient(circle_at_85%_10%,rgba(255,176,0,0.12),transparent_28%),linear-gradient(135deg,rgba(10,15,30,0.92),rgba(2,6,23,0.98))]" />
      <div className="pointer-events-none fixed inset-0 bg-[linear-gradient(rgba(255,255,255,0.035)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,0.035)_1px,transparent_1px)] bg-[size:42px_42px] opacity-25" />

      <section className="relative mx-auto flex min-h-screen w-full max-w-7xl flex-col gap-5 px-4 py-5 sm:px-6 lg:px-8">
        <header className="flex flex-col gap-4 rounded-[2rem] border border-cyan-300/15 bg-white/[0.04] p-5 shadow-2xl shadow-cyan-950/30 backdrop-blur md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-4">
            <div className="grid size-14 place-items-center rounded-2xl border border-cyan-300/30 bg-cyan-300/10 shadow-lg shadow-cyan-500/10">
              <Bot className="size-7 text-cyan-200" />
            </div>
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.38em] text-cyan-200/80">
                Indian Railways AI Ops
              </p>
              <h1 className="mt-1 text-3xl font-black tracking-tight text-white sm:text-4xl">
                RailMind LLM
              </h1>
            </div>
          </div>

          <div className="grid grid-cols-3 gap-3 text-xs text-slate-300">
            <StatusPill icon={TrainFront} label="Live twin" value="Mocked" />
            <StatusPill icon={RadioTower} label="Tool calls" value="Active" />
            <StatusPill icon={Activity} label="Latency" value="< 2s demo" />
          </div>
        </header>

        <div className="grid flex-1 gap-5 lg:grid-cols-[340px_minmax(0,1fr)]">
          <aside className="flex flex-col gap-5">
            <div className="rounded-[2rem] border border-white/10 bg-slate-950/70 p-5 shadow-2xl shadow-black/30 backdrop-blur">
              <div className="mb-4 flex items-center gap-3">
                <Sparkles className="size-5 text-amber-300" />
                <div>
                  <h2 className="font-bold text-white">Judge-ready prompts</h2>
                  <p className="text-sm text-slate-400">Click one to prove tool calling live.</p>
                </div>
              </div>
              <QuickActions onPick={sendMessage} disabled={isLoading} />
            </div>

            <div className="rounded-[2rem] border border-cyan-300/10 bg-cyan-300/[0.06] p-5">
              <p className="text-sm font-semibold uppercase tracking-[0.28em] text-cyan-200">
                Demo story
              </p>
              <p className="mt-3 text-sm leading-6 text-slate-300">
                The LLM reads the operator question, chooses a railway tool, receives structured
                JSON, then explains the action in plain English. When your teammates finish their
                backend, swap the mock tool functions in <span className="font-mono">server.js</span>.
              </p>
            </div>
          </aside>

          <ChatWindow
            messages={messages}
            onSend={sendMessage}
            isLoading={isLoading}
            error={error}
          />
        </div>
      </section>
    </main>
  );
}

function StatusPill({ icon: Icon, label, value }) {
  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.04] px-3 py-2">
      <div className="flex items-center gap-2 text-cyan-200">
        <Icon className="size-4" />
        <span className="font-semibold">{label}</span>
      </div>
      <p className="mt-1 font-mono text-slate-200">{value}</p>
    </div>
  );
}
