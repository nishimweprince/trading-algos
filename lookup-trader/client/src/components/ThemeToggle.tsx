import { Moon, Sun } from "lucide-react";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const dark = theme !== "light";
  const Icon = dark ? Moon : Sun;

  return (
    <button
      type="button"
      onClick={toggle}
      className="inline-flex cursor-pointer items-center gap-2.5 text-[11px] uppercase leading-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]"
      aria-pressed={dark}
      aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
    >
      <span
        className={cn(
          "flex h-5 w-9 shrink-0 items-center rounded-full border border-[var(--color-foreground)] p-0.5",
          dark ? "justify-end bg-[var(--color-foreground)]" : "justify-start",
        )}
      >
        <span
          className={cn(
            "block h-3.5 w-3.5 rounded-full",
            dark ? "bg-[var(--color-background)]" : "bg-[var(--color-foreground)]",
          )}
        />
      </span>
      <span className="inline-flex items-center gap-1.5">
        <Icon className="h-3 w-3" aria-hidden="true" />
        {dark ? "Dark" : "Light"}
      </span>
    </button>
  );
}
