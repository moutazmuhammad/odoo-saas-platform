import * as React from "react";
import type { LucideIcon } from "lucide-react";
import { Button, type ButtonProps } from "./ui/button";
import { Spinner } from "./Spinner";
import { cn } from "@/lib/utils";

interface ActionButtonProps extends ButtonProps {
  loading?: boolean;
  icon?: LucideIcon;
  loadingText?: string;
}

/**
 * Button that owns its own loading state UI — disables and swaps in a spinner
 * while an async mock action runs. Used everywhere actions touch "the backend".
 */
export const ActionButton = React.forwardRef<HTMLButtonElement, ActionButtonProps>(
  ({ loading, icon: Icon, loadingText, children, disabled, className, ...props }, ref) => (
    <Button
      ref={ref}
      disabled={loading || disabled}
      className={cn(className)}
      {...props}
    >
      {loading ? (
        <>
          <Spinner size="sm" />
          {loadingText ?? children}
        </>
      ) : (
        <>
          {Icon && <Icon />}
          {children}
        </>
      )}
    </Button>
  )
);
ActionButton.displayName = "ActionButton";
