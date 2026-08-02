import * as React from "react";
import { CalendarDays } from "lucide-react";
import type { DateRange } from "react-day-picker";
import { format, isSameYear } from "date-fns";
import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

export type { DateRange };

interface DateRangePickerProps {
  value?: DateRange;
  onChange: (range: DateRange | undefined) => void;
  disabled?: boolean;
  /** Blinded sessions hide the dates on screen; the selection itself stays intact. */
  masked?: boolean;
  placeholder?: string;
  className?: string;
  id?: string;
}

/** "Jan 1 – 31, 2024" when both ends share a year, otherwise both years are shown. */
function formatRange(range: DateRange | undefined, placeholder: string): string {
  if (!range?.from) return placeholder;
  if (!range.to) return `${format(range.from, "MMM d, yyyy")} – …`;
  if (isSameYear(range.from, range.to)) {
    return `${format(range.from, "MMM d")} – ${format(range.to, "MMM d, yyyy")}`;
  }
  return `${format(range.from, "MMM d, yyyy")} – ${format(range.to, "MMM d, yyyy")}`;
}

export function DateRangePicker({
  value,
  onChange,
  disabled,
  masked = false,
  placeholder = "Pick a date range",
  className,
  id,
}: DateRangePickerProps) {
  const [open, setOpen] = React.useState(false);

  // Close once a complete range is picked, so the operator isn't left dismissing it.
  const handleSelect = (range: DateRange | undefined) => {
    onChange(range);
    if (range?.from && range.to) setOpen(false);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          id={id}
          type="button"
          variant="outline"
          disabled={disabled}
          className={cn(
            "h-9 w-full justify-start gap-2 px-3 font-normal",
            !value?.from && "text-muted-foreground",
            className,
          )}
        >
          <CalendarDays className="h-4 w-4 shrink-0 opacity-70" aria-hidden="true" />
          <span className="truncate tnum">
            {masked ? "•••" : formatRange(value, placeholder)}
          </span>
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-auto p-2">
        <Calendar
          mode="range"
          numberOfMonths={2}
          defaultMonth={value?.from}
          selected={value}
          onSelect={handleSelect}
          disabled={{ after: new Date() }}
          autoFocus
        />
      </PopoverContent>
    </Popover>
  );
}
