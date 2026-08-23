import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense, useState } from "react";
import { ThemeToggle } from "@/components/ThemeToggle";
import { cn } from "@/lib/utils";

const LiveSignalsPage = lazy(() =>
  import("@/pages/LiveSignalsPage").then((module) => ({ default: module.LiveSignalsPage })),
);
const ReplayPage = lazy(() =>
  import("@/pages/ReplayPage").then((module) => ({ default: module.ReplayPage })),
);
const AutomatedEventsPage = lazy(() =>
  import("@/pages/AutomatedEventsPage").then((module) => ({
    default: module.AutomatedEventsPage,
  })),
);

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

type Mode = "replay" | "events" | "live";

const MODES: ReadonlyArray<{ id: Mode; label: string; short: string }> = [
  { id: "live", label: "Live signals", short: "LI" },
  { id: "replay", label: "Replay", short: "RP" },
  { id: "events", label: "Automated events", short: "EV" },
];

export default function App() {
  const [mode, setMode] = useState<Mode>("live");
  return (
    <QueryClientProvider client={queryClient}>
      <div className="flex h-screen overflow-hidden bg-[var(--color-background)] text-[var(--color-foreground)]">
        <aside className="hidden h-screen w-[200px] shrink-0 flex-col border-r border-[var(--color-border)] md:flex">
          <div className="px-6 pt-7">
            <p className="text-sm font-medium">LT.</p>
            <p className="mt-1 text-[10px] uppercase text-[var(--color-muted-foreground)]">Signal research</p>
          </div>
          <nav className="mt-12 flex flex-col gap-1 px-3" aria-label="Application mode">
            {MODES.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setMode(item.id)}
                className={cn(
                  "group flex h-8 w-full cursor-pointer items-center gap-3 rounded-r-[4px] px-3 text-left text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--color-ring)]",
                  mode === item.id
                    ? "border-[var(--color-foreground)] bg-[var(--color-secondary)] text-[var(--color-foreground)]"
                    : "border-transparent text-[var(--color-muted-foreground)] hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)]",
                )}
                aria-current={mode === item.id ? "page" : undefined}
              >
                <span className="w-4 font-mono text-[9px] opacity-60">{item.short}</span>
                <span>{item.label}</span>
              </button>
            ))}
          </nav>
          <div className="mt-auto px-6 pb-7">
            <ThemeToggle />
          </div>
        </aside>

        <main className="flex min-w-0 flex-1 flex-col">
          <header className="flex h-12 shrink-0 items-center justify-between border-b border-[var(--color-border)] px-4 md:hidden">
            <div>
              <span className="text-sm font-medium">LT.</span>
              <span className="ml-2 text-[10px] uppercase text-[var(--color-muted-foreground)]">Signal research</span>
            </div>
            <ThemeToggle />
          </header>
          <nav className="flex shrink-0 border-b border-[var(--color-border)] md:hidden" aria-label="Application mode">
            {MODES.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setMode(item.id)}
                className={cn(
                  "h-9 flex-1 cursor-pointer border-b px-2 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--color-ring)]",
                  mode === item.id
                    ? "border-[var(--color-foreground)] text-[var(--color-foreground)]"
                    : "border-transparent text-[var(--color-muted-foreground)]",
                )}
                aria-current={mode === item.id ? "page" : undefined}
              >
                {item.label}
              </button>
            ))}
          </nav>
          <div className="min-h-0 flex-1">
            <Suspense
              fallback={
                <div className="grid h-full place-items-center text-xs text-[var(--color-muted-foreground)]">
                  Loading workspace…
                </div>
              }
            >
              {mode === "live" && <LiveSignalsPage />}
              {mode === "replay" && <ReplayPage />}
              {mode === "events" && <AutomatedEventsPage />}
            </Suspense>
          </div>
        </main>
      </div>
    </QueryClientProvider>
  );
}
