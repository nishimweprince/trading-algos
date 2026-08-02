import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

interface StatCardProps {
  title: string;
  value: string;
  subtitle?: string;
  className?: string;
}

export function StatCard({ title, value, subtitle, className }: StatCardProps) {
  return (
    <Card className={cn("bg-zinc-900/50", className)}>
      <CardHeader className="p-3 pb-1">
        <CardTitle className="text-xs text-zinc-400 font-normal">{title}</CardTitle>
      </CardHeader>
      <CardContent className="p-3 pt-0">
        <div className="font-mono text-lg font-semibold">{value}</div>
        {subtitle && <div className="text-xs text-zinc-500 mt-1">{subtitle}</div>}
      </CardContent>
    </Card>
  );
}
