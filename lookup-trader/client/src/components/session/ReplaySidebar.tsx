import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { ActiveTradePanel } from "@/components/trade/ActiveTradePanel";
import { ComparePanel } from "@/components/trade/ComparePanel";
import { TradeForm } from "@/components/trade/TradeForm";
import { TradeList } from "@/components/trade/TradeList";
import type { ReplayChartHandle } from "@/components/chart/ReplayChart";
import type { PriceLevelKey } from "@/components/chart/PriceLines";
import { useTrades } from "@/hooks/useTrades";
import { useActiveTradeStore } from "@/stores/activeTradeStore";
import { cn } from "@/lib/utils";
import type { Session } from "@/types";

type SidebarTab = "label" | "history" | "compare";

const TABS: { id: SidebarTab; label: string }[] = [
  { id: "label", label: "Label" },
  { id: "history", label: "History" },
  { id: "compare", label: "Compare" },
];

interface ReplaySidebarProps {
  session: Session | null;
  blinded: boolean;
  armed: PriceLevelKey | null;
  onArm: (field: PriceLevelKey | null) => void;
  chartRef: React.RefObject<ReplayChartHandle | null>;
}

function TabButtons({
  active,
  onChange,
  tradeCount,
  className,
}: {
  active: SidebarTab;
  onChange: (tab: SidebarTab) => void;
  tradeCount: number;
  className?: string;
}) {
  return (
    <div role="tablist" aria-label="Replay sidebar" className={cn("flex gap-1", className)}>
      {TABS.map(({ id, label }) => (
        <Button
          key={id}
          type="button"
          role="tab"
          aria-selected={active === id}
          variant={active === id ? "default" : "outline"}
          size="sm"
          className="h-8 flex-1 text-xs"
          onClick={() => onChange(id)}
        >
          {label}
          {id === "history" && tradeCount > 0 && (
            <span className="tnum ml-1 rounded bg-zinc-800 px-1 py-0.5 text-[10px] text-zinc-400">
              {tradeCount}
            </span>
          )}
        </Button>
      ))}
    </div>
  );
}

function SidebarPanel({
  tab,
  session,
  blinded,
  armed,
  onArm,
  chartRef,
}: ReplaySidebarProps & { tab: SidebarTab }) {
  return (
    <div role="tabpanel" className="flex flex-col gap-3">
      {tab === "label" && (
        <>
          <TradeForm
            session={session}
            blinded={blinded}
            armed={armed}
            onArm={onArm}
            chartRef={chartRef}
          />
        </>
      )}
      {tab === "history" && <TradeList session={session} blinded={blinded} />}
      {tab === "compare" && <ComparePanel session={session} />}
    </div>
  );
}

function LiveTradeBanner() {
  const status = useActiveTradeStore((s) => s.status);
  if (status === "idle") return null;

  return (
    <div className="rounded border border-operator/30 bg-operator/5 px-3 py-2 text-xs text-operator">
      Live trade in progress — levels lock while the trade is open.
    </div>
  );
}

export function ReplaySidebar(props: ReplaySidebarProps) {
  const { session, blinded } = props;
  const [tab, setTab] = useState<SidebarTab>("label");
  const [mobileOpen, setMobileOpen] = useState(false);
  const { data: trades = [] } = useTrades(session?.session_id);

  const handleTabChange = (next: SidebarTab) => {
    setTab(next);
    setMobileOpen(true);
  };

  const desktopContent: ReactNode = (
    <>
      <LiveTradeBanner />
      <ActiveTradePanel session={session} blinded={blinded} />
      <TabButtons active={tab} onChange={setTab} tradeCount={trades.length} />
      <SidebarPanel {...props} tab={tab} />
    </>
  );

  return (
    <>
      {/* Desktop aside */}
      <aside className="hidden min-h-0 w-full shrink-0 flex-col gap-3 overflow-y-auto border-t border-zinc-800 p-3 lg:flex lg:w-[26rem] lg:max-w-[26rem] lg:border-l lg:border-t-0">
        {desktopContent}
      </aside>

      {/* Mobile bottom panel */}
      <div className="lg:hidden">
        {mobileOpen && (
          <div className="fixed inset-x-0 bottom-12 z-20 max-h-[45vh] overflow-y-auto border-t border-zinc-800 bg-zinc-950 p-3">
            <LiveTradeBanner />
            <div className="mt-3">
              <ActiveTradePanel session={session} blinded={blinded} />
            </div>
            <div className="mt-3">
              <SidebarPanel {...props} tab={tab} />
            </div>
          </div>
        )}
        <div className="fixed inset-x-0 bottom-0 z-30 border-t border-zinc-800 bg-zinc-950 p-2">
          <TabButtons
            active={tab}
            onChange={handleTabChange}
            tradeCount={trades.length}
            className="w-full"
          />
        </div>
      </div>
    </>
  );
}
