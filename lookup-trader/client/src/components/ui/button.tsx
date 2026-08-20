import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

export const buttonVariants = cva(
  "inline-flex cursor-pointer items-center justify-center gap-2 whitespace-nowrap text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "rounded-full bg-[var(--color-primary)] text-[var(--color-primary-foreground)] hover:opacity-80",
        secondary: "rounded-none bg-[var(--color-secondary)] text-[var(--color-secondary-foreground)] hover:opacity-80",
        outline: "rounded-none border border-[var(--color-border)] bg-transparent hover:bg-[var(--color-accent)]",
        ghost: "rounded-none hover:bg-[var(--color-accent)]",
        destructive: "rounded-full bg-[var(--color-destructive)] text-white hover:opacity-90",
        operator:
          "rounded-full bg-[var(--color-operator)] text-[var(--color-operator-foreground)] hover:opacity-80",
      },
      size: {
        default: "h-8 px-4",
        sm: "h-7 px-3 text-[11px]",
        icon: "h-8 w-8",
        "icon-sm": "h-7 w-7",
        // Tag toggles sit in dense wrapping rows; `sm` at h-8 reads as a button.
        chip: "h-6 rounded px-2 text-xs font-normal",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />;
  },
);
Button.displayName = "Button";
