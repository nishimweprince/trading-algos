import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

export const buttonVariants = cva(
  "inline-flex cursor-pointer items-center justify-center gap-2 whitespace-nowrap text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "rounded-full bg-foreground text-background hover:opacity-80",
        outline: "rounded-none border border-border bg-transparent hover:bg-accent",
        ghost: "rounded-none hover:bg-accent",
        pill: "rounded-full border border-foreground bg-transparent hover:bg-foreground hover:text-background",
        destructive: "rounded-full bg-destructive text-white hover:opacity-90",
      },
      size: {
        default: "h-8 px-4",
        sm: "h-7 px-3 text-[11px]",
        lg: "h-9 px-5",
        icon: "h-8 w-8",
        chip: "h-6 px-2 text-[11px] font-normal",
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
    return (
      <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />
    );
  },
);
Button.displayName = "Button";
