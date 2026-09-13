import * as React from "react";
import { cn } from "@/lib/utils";

interface BreakdownRowProps {
  label: React.ReactNode;
  value: React.ReactNode;
  sub?: React.ReactNode;
  emphasis?: boolean;
  className?: string;
}

/**
 * A label/value row for configuration summaries and invoices.
 * Note: only ever shows quantities + a final value — no per-unit rates.
 */
export function BreakdownRow({
  label,
  value,
  sub,
  emphasis,
  className,
}: BreakdownRowProps) {
  return (
    <div
      className={cn(
        "flex items-center justify-between gap-4 py-2.5",
        emphasis && "border-t border-border pt-4 mt-1",
        className
      )}
    >
      <div className="min-w-0">
        <p
          className={cn(
            "truncate text-sm",
            emphasis ? "font-semibold text-foreground" : "text-muted"
          )}
        >
          {label}
        </p>
        {sub && <p className="text-xs text-muted/70">{sub}</p>}
      </div>
      <span
        className={cn(
          "shrink-0 tabular-nums",
          emphasis ? "text-lg font-semibold text-foreground" : "text-sm font-medium text-foreground/90"
        )}
      >
        {value}
      </span>
    </div>
  );
}
