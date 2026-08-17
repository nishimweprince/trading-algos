import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { ReplayPage } from "@/pages/ReplayPage";
import { AutomatedEventsPage } from "@/pages/AutomatedEventsPage";
import { LiveSignalsPage } from "@/pages/LiveSignalsPage";
import { Button } from "@/components/ui/button";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

type Mode = "replay" | "events" | "live";

export default function App() {
  const [mode, setMode] = useState<Mode>("live");
  return (
    <QueryClientProvider client={queryClient}>
      <div className="flex h-screen flex-col overflow-hidden bg-zinc-950">
        <header className="flex h-12 shrink-0 items-center justify-between border-b border-zinc-800 px-4 text-zinc-100">
          <span className="text-sm font-semibold tracking-wide">Lookup Trader</span>
          <nav className="flex gap-2" aria-label="Application mode">
            <Button size="sm" variant={mode === "live" ? "operator" : "ghost"} onClick={() => setMode("live")}>Live signals</Button>
            <Button size="sm" variant={mode === "replay" ? "operator" : "ghost"} onClick={() => setMode("replay")}>Replay</Button>
            <Button size="sm" variant={mode === "events" ? "operator" : "ghost"} onClick={() => setMode("events")}>Automated events</Button>
          </nav>
        </header>
        <div className="min-h-0 flex-1">
          {mode === "live" && <LiveSignalsPage />}
          {mode === "replay" && <ReplayPage />}
          {mode === "events" && <AutomatedEventsPage />}
        </div>
      </div>
    </QueryClientProvider>
  );
}
