import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

// Material / Google-console style buttons: 4px radius, restrained shadows,
// clear contained / outlined / text hierarchy.
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded text-sm font-medium transition-all focus-ring disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        // Contained primary
        default:
          "bg-primary text-primary-foreground shadow-sm hover:bg-primary-glow hover:shadow",
        // Outlined (secondary action)
        secondary:
          "border border-border bg-card text-primary hover:bg-primary/[0.06]",
        outline:
          "border border-border bg-transparent text-foreground hover:bg-foreground/[0.04]",
        // Text button
        ghost: "text-muted hover:bg-foreground/[0.06] hover:text-foreground",
        danger: "bg-danger/10 text-danger border border-danger/30 hover:bg-danger/20",
        success:
          "bg-success/15 text-success border border-success/30 hover:bg-success/25",
        link: "text-primary underline-offset-4 hover:underline p-0 h-auto",
      },
      size: {
        default: "h-9 px-4 py-2",
        sm: "h-8 px-3 text-xs",
        lg: "h-11 px-6 text-base",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button
      ref={ref}
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  )
);
Button.displayName = "Button";

export { buttonVariants };
