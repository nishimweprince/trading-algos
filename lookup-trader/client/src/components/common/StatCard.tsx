import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

interface StatCardProps {
  title: string;
  value: string;
  subtitle?: string;
  className?: string;
  /** White-on-black panel styling for context stats. */
  monochrome?: boolean;
}

export function StatCard({ title, value, subtitle, className, monochrome }: StatCardProps) {
  return (
    <Card
      className={cn(
        monochrome ? "border-white/15 bg-black" : "bg-zinc-900/50",
        className,
      )}
    >
      <CardHeader className="p-3 pb-1">
        <CardTitle
          className={cn(
            "text-xs font-normal",
            monochrome ? "text-white/45" : "text-zinc-500",
          )}
        >
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent className="p-3 pt-0">
        <div
          className={cn(
            "tnum font-mono text-base font-medium",
            monochrome ? "text-white" : undefined,
          )}
        >
          {value}
        </div>
        {subtitle && (
          <div
            className={cn(
              "tnum mt-1 text-xs",
              monochrome ? "text-white/45" : "text-zinc-500",
            )}
          >
            {subtitle}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
