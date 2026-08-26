import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
        "inline-flex items-center rounded-none border px-2 py-0.5 text-[11px] uppercase",
  {
    variants: {
      variant: {
        default: "border-border text-foreground",
        outline: "border-border text-foreground",
        win: "border-transparent bg-win/15 text-win",
        be: "border-border text-muted-foreground",
        loss: "border-transparent bg-loss/15 text-loss",
        long: "border-border text-foreground",
        short: "border-border text-muted-foreground",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}
