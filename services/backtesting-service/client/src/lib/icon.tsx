import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import type { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import { cn } from "@/lib/utils";

export function Icon({
  icon,
  className,
  title,
}: {
  icon: IconDefinition;
  className?: string;
  title?: string;
}) {
  return <FontAwesomeIcon icon={icon} title={title} className={cn("h-3.5 w-3.5", className)} />;
}
