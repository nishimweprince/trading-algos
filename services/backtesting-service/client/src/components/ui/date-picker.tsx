import * as React from "react";
import { faCalendar } from "@fortawesome/free-solid-svg-icons";
import { format } from "date-fns";
import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Icon } from "@/lib/icon";
import { cn } from "@/lib/utils";

export interface DatePickerProps {
  value?: Date;
  onChange: (date: Date | undefined) => void;
  disabled?: boolean;
  placeholder?: string;
  className?: string;
  id?: string;
}

export function DatePicker({
  value,
  onChange,
  disabled,
  placeholder = "Any day",
  className,
  id,
}: DatePickerProps) {
  const [open, setOpen] = React.useState(false);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          id={id}
          type="button"
          variant="outline"
          disabled={disabled}
          className={cn(
            "h-8 w-full justify-start gap-2 px-3 font-normal",
            !value && "text-muted-foreground",
            className,
          )}
        >
          <Icon icon={faCalendar} className="h-3.5 w-3.5 shrink-0 opacity-70" />
          <span className="truncate">{value ? format(value, "d MMM yyyy") : placeholder}</span>
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-auto rounded-none p-2">
        <Calendar
          mode="single"
          selected={value}
          onSelect={(date) => {
            onChange(date);
            if (date) setOpen(false);
          }}
          defaultMonth={value}
          captionLayout="dropdown"
          startMonth={new Date(2018, 0)}
          endMonth={new Date()}
          autoFocus
        />
        {value ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="mt-2 w-full"
            onClick={() => {
              onChange(undefined);
              setOpen(false);
            }}
          >
            Clear
          </Button>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
