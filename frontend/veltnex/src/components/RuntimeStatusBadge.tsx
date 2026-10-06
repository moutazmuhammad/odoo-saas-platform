import { useRuntimeClock } from "@/hooks/useRuntimeClock";
import { i18nText, getLocale, translateMessage } from "@/i18n";
import { displayRuntimeStatus, isRuntimeExpired, type RuntimeInstance } from "@/lib/runtime-status";
import { StatusBadge } from "./StatusBadge";

export function RuntimeStatusBadge({ instance, showDetail = false }: { instance: RuntimeInstance; showDetail?: boolean }) {
  // Expire a green observation even when subsequent polling requests fail.
  const now = useRuntimeClock();
  const status = displayRuntimeStatus(instance, now);
  const expired = isRuntimeExpired(instance, now);
  const message = status === "unknown" && (expired || instance.runtime_state !== "unknown")
    ? i18nText("Availability has not been checked recently.")
    : translateMessage(instance.runtime_message || "");
  const checked = instance.runtime_checked_at && Number.isFinite(Date.parse(instance.runtime_checked_at))
    ? `${i18nText("Last checked")}: ${new Date(instance.runtime_checked_at).toLocaleString(getLocale())}` : "";
  const detail = [message, checked].filter(Boolean).join(" · ");
  return (
    <span className="inline-flex flex-col gap-1" title={detail}>
      <StatusBadge status={status} label={status === instance.state ? instance.state_label : undefined} />
      {showDetail && detail && <span className="max-w-lg text-xs text-muted">{detail}</span>}
    </span>
  );
}
