import { useRuntimeClock } from "@/hooks/useRuntimeClock";
import { displayRuntimeStatus, type RuntimeInstance } from "@/lib/runtime-status";
import { StatusBadge, hasStatusLabel } from "./StatusBadge";

export function RuntimeStatusBadge({ instance }: { instance: RuntimeInstance }) {
  // Expire a green observation even when subsequent polling requests fail.
  const now = useRuntimeClock();
  const status = displayRuntimeStatus(instance, now);
  // Customers see plain wording, never the backend's lifecycle name
  // ("Provisioning"); the server label is only a fallback for unknown states.
  const label = status === instance.state && !hasStatusLabel(status) ? instance.state_label : undefined;
  return <StatusBadge status={status} label={label} />;
}
