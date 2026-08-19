import { faMoon, faSun } from "@fortawesome/free-solid-svg-icons";
import { Icon } from "@/lib/icon";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const dark = theme !== "light";

  return (
    <button
      type="button"
      onClick={toggle}
      className="inline-flex cursor-pointer items-center gap-2.5 text-[11px] uppercase leading-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      aria-pressed={dark}
      aria-label={dark ? "Switch to light" : "Switch to dark"}
    >
      <span
        className={cn(
          "flex h-5 w-9 shrink-0 items-center rounded-full border border-foreground p-0.5",
          dark ? "justify-end bg-foreground" : "justify-start",
        )}
      >
        <span
          className={cn("block h-3.5 w-3.5 rounded-full", dark ? "bg-background" : "bg-foreground")}
        />
      </span>
      <span className="inline-flex items-center gap-1.5">
        <Icon icon={dark ? faMoon : faSun} className="h-3 w-3" />
        {dark ? "Dark" : "Light"}
      </span>
    </button>
  );
}
