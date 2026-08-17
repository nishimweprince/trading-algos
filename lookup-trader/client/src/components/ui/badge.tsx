import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium transition-colors",
  {
    variants: {
      variant: {
        default: "border-transparent bg-[var(--color-primary)] text-[var(--color-primary-foreground)]",
        secondary: "border-transparent bg-[var(--color-secondary)] text-[var(--color-secondary-foreground)]",
        outline: "text-[var(--color-foreground)]",
        win: "border-transparent bg-emerald-600/20 text-emerald-400",
        loss: "border-transparent bg-red-600/20 text-red-400",
        timeout: "border-transparent bg-amber-600/20 text-amber-400",
        ambiguous: "border-transparent bg-zinc-600/20 text-zinc-400",
        long: "border-transparent bg-[var(--color-operator)]/15 text-[var(--color-operator)]",
        short: "border-[var(--color-operator-dim)] bg-[var(--color-operator-dim)]/40 text-zinc-400",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps extends React.HTMLAttributes<HTMLDivElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}
