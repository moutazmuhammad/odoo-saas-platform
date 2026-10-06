import { i18nText } from "@/i18n";
import { cn } from "@/lib/utils";

const STATUS_STYLES: Record<
  string,
  { label: string; dot: string; text: string; bg: string; pulse?: boolean }
> = {
  // Observed tenant availability.
  online: { label: i18nText("Online"), dot: "bg-success", text: "text-success", bg: "bg-success/10 border-success/30" },
  starting: { label: i18nText("Starting"), dot: "bg-info", text: "text-info", bg: "bg-info/10 border-info/30", pulse: true },
  stopping: { label: i18nText("Stopping"), dot: "bg-warning", text: "text-warning", bg: "bg-warning/10 border-warning/30", pulse: true },
  unavailable: { label: i18nText("Unavailable"), dot: "bg-danger", text: "text-danger", bg: "bg-danger/10 border-danger/30" },
  unreachable: { label: i18nText("Unreachable"), dot: "bg-warning", text: "text-warning", bg: "bg-warning/10 border-warning/30" },
  unknown: { label: i18nText("Unknown"), dot: "bg-muted", text: "text-muted", bg: "bg-muted/10 border-border" },
  // instance
  pending_payment: { label: i18nText("Awaiting payment"), dot: "bg-warning", text: "text-warning", bg: "bg-warning/10 border-warning/30", pulse: true },
  pending_provision: { label: i18nText("Queued"), dot: "bg-info", text: "text-info", bg: "bg-info/10 border-info/30", pulse: true },
  provisioning: { label: i18nText("Provisioning"), dot: "bg-info", text: "text-info", bg: "bg-info/10 border-info/30", pulse: true },
  running: { label: i18nText("Running"), dot: "bg-success", text: "text-success", bg: "bg-success/10 border-success/30" },
  stopped: { label: i18nText("Stopped"), dot: "bg-muted", text: "text-muted", bg: "bg-muted/10 border-border" },
  suspended: { label: i18nText("Suspended"), dot: "bg-warning", text: "text-warning", bg: "bg-warning/10 border-warning/30" },
  cancelled: { label: i18nText("Cancelled"), dot: "bg-muted", text: "text-muted", bg: "bg-muted/10 border-border" },
  cancelled_by_client: { label: i18nText("Cancelled"), dot: "bg-muted", text: "text-muted", bg: "bg-muted/10 border-border" },
  failed: { label: i18nText("Failed"), dot: "bg-danger", text: "text-danger", bg: "bg-danger/10 border-danger/30" },
  // build
  success: { label: i18nText("Success"), dot: "bg-success", text: "text-success", bg: "bg-success/10 border-success/30" },
  building: { label: i18nText("Building"), dot: "bg-info", text: "text-info", bg: "bg-info/10 border-info/30", pulse: true },
  // db / backup
  active: { label: i18nText("Active"), dot: "bg-success", text: "text-success", bg: "bg-success/10 border-success/30" },
  creating: { label: i18nText("Creating"), dot: "bg-info", text: "text-info", bg: "bg-info/10 border-info/30", pulse: true },
  maintenance: { label: i18nText("Maintenance"), dot: "bg-warning", text: "text-warning", bg: "bg-warning/10 border-warning/30" },
  available: { label: i18nText("Available"), dot: "bg-success", text: "text-success", bg: "bg-success/10 border-success/30" },
  in_progress: { label: i18nText("In progress"), dot: "bg-info", text: "text-info", bg: "bg-info/10 border-info/30", pulse: true },
  // invoice
  paid: { label: i18nText("Paid"), dot: "bg-success", text: "text-success", bg: "bg-success/10 border-success/30" },
  open: { label: i18nText("Open"), dot: "bg-info", text: "text-info", bg: "bg-info/10 border-info/30" },
  overdue: { label: i18nText("Overdue"), dot: "bg-danger", text: "text-danger", bg: "bg-danger/10 border-danger/30" },
  draft: { label: i18nText("Draft"), dot: "bg-muted", text: "text-muted", bg: "bg-muted/10 border-border" },
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
      {i18nText(label ?? s.label)}
    </span>
  );
}
