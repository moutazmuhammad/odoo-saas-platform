import * as React from "react";
import { AlertTriangle, CheckCircle2, Info, XCircle, X } from "lucide-react";
import { cn } from "@/lib/utils";

export type AlertVariant = "success" | "warning" | "danger" | "info";

interface AlertBannerProps {
  variant?: AlertVariant;
  title: string;
  description?: React.ReactNode;
  onDismiss?: () => void;
  className?: string;
  action?: React.ReactNode;
}

const CONFIG: Record<
  AlertVariant,
  { icon: typeof Info; ring: string; text: string; iconColor: string }
> = {
  success: { icon: CheckCircle2, ring: "border-success/30 bg-success/10", text: "text-success", iconColor: "text-success" },
  warning: { icon: AlertTriangle, ring: "border-warning/30 bg-warning/10", text: "text-warning", iconColor: "text-warning" },
  danger: { icon: XCircle, ring: "border-danger/30 bg-danger/10", text: "text-danger", iconColor: "text-danger" },
  info: { icon: Info, ring: "border-info/30 bg-info/10", text: "text-info", iconColor: "text-info" },
};

/** Friendly, contextual messaging — never raw errors. */
export function AlertBanner({
  variant = "info",
  title,
  description,
  onDismiss,
  className,
  action,
}: AlertBannerProps) {
  const c = CONFIG[variant];
  const Icon = c.icon;
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-3 rounded-lg border p-4 animate-fade-in",
        c.ring,
        className
      )}
    >
      <Icon className={cn("mt-0.5 size-5 shrink-0", c.iconColor)} />
      <div className="min-w-0 flex-1">
        <p className={cn("text-sm font-medium", c.text)}>{title}</p>
        {description && (
          <div className="mt-1 text-sm text-foreground/80">{description}</div>
        )}
        {action && <div className="mt-3">{action}</div>}
      </div>
      {onDismiss && (
        <button
          onClick={onDismiss}
          className="rounded p-0.5 text-muted transition-colors hover:text-foreground"
          aria-label="Dismiss"
        >
          <X className="size-4" />
        </button>
      )}
    </div>
  );
}
