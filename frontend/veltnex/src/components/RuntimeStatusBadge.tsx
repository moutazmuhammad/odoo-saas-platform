import { useRuntimeClock } from "@/hooks/useRuntimeClock";
import { displayRuntimeStatus, type RuntimeInstance } from "@/lib/runtime-status";
import { StatusBadge } from "./StatusBadge";

export function RuntimeStatusBadge({ instance }: { instance: RuntimeInstance }) {
  // Expire a green observation even when subsequent polling requests fail.
  const now = useRuntimeClock();
  const status = displayRuntimeStatus(instance, now);
  return <StatusBadge status={status} label={status === instance.state ? instance.state_label : undefined} />;
}
