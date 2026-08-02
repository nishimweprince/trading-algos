import * as React from "react";
import { Check, ChevronsUpDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

export interface ComboboxOption {
  value: string;
  label: string;
  /** Optional heading this option is filed under. */
  group?: string;
  /** Extra terms the search should match, beyond the label. */
  keywords?: string[];
}

interface ComboboxProps {
  options: ComboboxOption[];
  value?: string;
  onChange: (value: string) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  emptyText?: string;
  disabled?: boolean;
  className?: string;
  id?: string;
}

/** Groups in first-seen order; ungrouped options stay loose at the top. */
function partition(options: ComboboxOption[]) {
  const loose: ComboboxOption[] = [];
  const groups = new Map<string, ComboboxOption[]>();
  for (const option of options) {
    if (!option.group) {
      loose.push(option);
      continue;
    }
    const bucket = groups.get(option.group);
    if (bucket) bucket.push(option);
    else groups.set(option.group, [option]);
  }
  return { loose, groups: [...groups.entries()] };
}

/**
 * Searchable single-select. A plain Select stops being usable once the list runs
 * to a few dozen entries — the operator should be able to type "head" and land on
 * the pattern rather than scroll for it.
 */
export function Combobox({
  options,
  value,
  onChange,
  placeholder = "Select…",
  searchPlaceholder = "Search…",
  emptyText = "No match.",
  disabled,
  className,
  id,
}: ComboboxProps) {
  const [open, setOpen] = React.useState(false);
  const selected = options.find((o) => o.value === value);
  const { loose, groups } = React.useMemo(() => partition(options), [options]);

  const renderItem = (option: ComboboxOption) => (
    <CommandItem
      key={option.value}
      // cmdk matches on this string, so it must be the human label, not the id.
      value={option.label}
      keywords={option.keywords ?? [option.value]}
      onSelect={() => {
        onChange(option.value);
        setOpen(false);
      }}
    >
      <Check
        className={cn("h-4 w-4 shrink-0", option.value === value ? "opacity-100" : "opacity-0")}
        aria-hidden="true"
      />
      <span className="truncate">{option.label}</span>
    </CommandItem>
  );

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          id={id}
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          disabled={disabled}
          className={cn(
            "h-9 w-full justify-between gap-2 px-3 font-normal",
            !selected && "text-zinc-500",
            className,
          )}
        >
          <span className="truncate">{selected ? selected.label : placeholder}</span>
          <ChevronsUpDown className="h-4 w-4 shrink-0 opacity-50" aria-hidden="true" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-[var(--radix-popover-trigger-width)] p-0">
        <Command>
          <CommandInput placeholder={searchPlaceholder} />
          <CommandList>
            <CommandEmpty>{emptyText}</CommandEmpty>
            {loose.map(renderItem)}
            {groups.map(([heading, items]) => (
              <CommandGroup key={heading} heading={heading}>
                {items.map(renderItem)}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
