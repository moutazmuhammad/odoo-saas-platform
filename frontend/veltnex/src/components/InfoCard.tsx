import * as React from "react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { Card } from "./ui/card";

interface InfoCardProps {
  label: string;
  value: React.ReactNode;
  icon?: LucideIcon;
  hint?: string;
  trend?: { value: string; positive?: boolean };
  className?: string;
  children?: React.ReactNode;
}

/** Compact metric/stat card used across dashboard and detail pages. */
export function InfoCard({
  label,
  value,
  icon: Icon,
  hint,
  trend,
  className,
  children,
}: InfoCardProps) {
  return (
    <Card className={cn("p-5 transition-colors hover:border-border/60", className)}>
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-medium text-muted">{label}</p>
        {Icon && (
          <span className="flex size-8 items-center justify-center rounded-lg bg-primary/15 text-primary">
            <Icon className="size-4" />
          </span>
        )}
      </div>
      <div className="mt-3 flex items-end gap-2">
        <span className="text-2xl font-semibold tracking-tight">{value}</span>
        {trend && (
          <span
            className={cn(
              "mb-1 text-xs font-medium",
              trend.positive ? "text-success" : "text-danger"
            )}
          >
            {trend.value}
          </span>
        )}
      </div>
      {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
      {children && <div className="mt-3">{children}</div>}
    </Card>
  );
}
