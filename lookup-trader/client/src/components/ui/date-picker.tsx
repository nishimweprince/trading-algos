import * as React from "react";
import { CalendarDays } from "lucide-react";
import type { Matcher } from "react-day-picker";
import { format } from "date-fns";
import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

/** Earliest year offered in the caption dropdown. */
const EARLIEST_YEAR = 2000;

export interface DatePickerProps {
  value?: Date;
  onChange: (date: Date | undefined) => void;
  disabled?: boolean;
  /** Days the operator cannot pick — used to keep the two ends of a range in order. */
  disabledDays?: Matcher | Matcher[];
  /** Blinded sessions hide the date on screen; the selection itself stays intact. */
  masked?: boolean;
  placeholder?: string;
  className?: string;
  id?: string;
}

export function DatePicker({
  value,
  onChange,
  disabled,
  disabledDays,
  masked = false,
  placeholder = "Pick a date",
  className,
  id,
}: DatePickerProps) {
  const [open, setOpen] = React.useState(false);

  const handleSelect = (date: Date | undefined) => {
    onChange(date);
    if (date) setOpen(false);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          id={id}
          type="button"
          variant="outline"
          disabled={disabled}
          className={cn("h-9 w-full justify-start gap-2 px-3 font-normal", !value && "text-muted-foreground", className)}
        >
          <CalendarDays className="h-4 w-4 shrink-0 opacity-70" aria-hidden="true" />
          <span className="tnum truncate">
            {masked ? "•••" : value ? format(value, "d MMM yyyy") : placeholder}
          </span>
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-auto p-2">
        <Calendar
          mode="single"
          selected={value}
          onSelect={handleSelect}
          defaultMonth={value}
          disabled={disabledDays}
          // Month and year dropdowns, so any window in the archive is two clicks away.
          captionLayout="dropdown"
          startMonth={new Date(EARLIEST_YEAR, 0)}
          endMonth={new Date()}
          autoFocus
        />
      </PopoverContent>
    </Popover>
  );
}
