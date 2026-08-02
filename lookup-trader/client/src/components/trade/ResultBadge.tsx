import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface ResultBadgeProps {
  result?: string | null;
  source?: string | null;
  className?: string;
}

function resultVariant(result?: string | null) {
  switch (result) {
    case "win":
      return "win" as const;
    case "loss":
      return "loss" as const;
    case "timeout":
      return "timeout" as const;
    case "ambiguous":
      return "ambiguous" as const;
    default:
      return "secondary" as const;
  }
}

export function ResultBadge({ result, source, className }: ResultBadgeProps) {
  return (
    <div className={cn("flex items-center gap-1.5", className)}>
      <Badge variant={resultVariant(result)}>{result ?? "pending"}</Badge>
      {source && <Badge variant="outline">{source}</Badge>}
    </div>
  );
}
