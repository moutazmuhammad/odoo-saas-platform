import { cn } from "@/lib/utils";

const STATUS_STYLES: Record<
  string,
  { label: string; dot: string; text: string; bg: string; pulse?: boolean }
> = {
  // instance
  pending_payment: { label: "Awaiting payment", dot: "bg-warning", text: "text-warning", bg: "bg-warning/10 border-warning/30", pulse: true },
  pending_provision: { label: "Queued", dot: "bg-info", text: "text-info", bg: "bg-info/10 border-info/30", pulse: true },
  provisioning: { label: "Provisioning", dot: "bg-info", text: "text-info", bg: "bg-info/10 border-info/30", pulse: true },
  running: { label: "Running", dot: "bg-success", text: "text-success", bg: "bg-success/10 border-success/30" },
  stopped: { label: "Stopped", dot: "bg-muted", text: "text-muted", bg: "bg-muted/10 border-border" },
  suspended: { label: "Suspended", dot: "bg-warning", text: "text-warning", bg: "bg-warning/10 border-warning/30" },
  cancelled: { label: "Cancelled", dot: "bg-muted", text: "text-muted", bg: "bg-muted/10 border-border" },
  cancelled_by_client: { label: "Cancelled", dot: "bg-muted", text: "text-muted", bg: "bg-muted/10 border-border" },
  failed: { label: "Failed", dot: "bg-danger", text: "text-danger", bg: "bg-danger/10 border-danger/30" },
  // build
  success: { label: "Success", dot: "bg-success", text: "text-success", bg: "bg-success/10 border-success/30" },
  building: { label: "Building", dot: "bg-info", text: "text-info", bg: "bg-info/10 border-info/30", pulse: true },
  // db / backup
  active: { label: "Active", dot: "bg-success", text: "text-success", bg: "bg-success/10 border-success/30" },
  creating: { label: "Creating", dot: "bg-info", text: "text-info", bg: "bg-info/10 border-info/30", pulse: true },
  maintenance: { label: "Maintenance", dot: "bg-warning", text: "text-warning", bg: "bg-warning/10 border-warning/30" },
  available: { label: "Available", dot: "bg-success", text: "text-success", bg: "bg-success/10 border-success/30" },
  in_progress: { label: "In progress", dot: "bg-info", text: "text-info", bg: "bg-info/10 border-info/30", pulse: true },
  // invoice
  paid: { label: "Paid", dot: "bg-success", text: "text-success", bg: "bg-success/10 border-success/30" },
  open: { label: "Open", dot: "bg-info", text: "text-info", bg: "bg-info/10 border-info/30" },
  overdue: { label: "Overdue", dot: "bg-danger", text: "text-danger", bg: "bg-danger/10 border-danger/30" },
  draft: { label: "Draft", dot: "bg-muted", text: "text-muted", bg: "bg-muted/10 border-border" },
};

interface StatusBadgeProps {
  status: string;
  className?: string;
  label?: string;
}

export function StatusBadge({ status, className, label }: StatusBadgeProps) {
  const s = STATUS_STYLES[status] ?? STATUS_STYLES.draft;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
        s.bg,
        s.text,
        className
      )}
    >
      <span className={cn("size-1.5 rounded-full", s.dot, s.pulse && "animate-pulse-soft")} />
      {label ?? s.label}
    </span>
  );
}
