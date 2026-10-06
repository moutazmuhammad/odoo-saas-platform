import type { RuntimeHealth } from "./api";

export type RuntimeInstance = RuntimeHealth & { state: string; state_label?: string };
const OBSERVED_STATES = new Set(["running", "provisioning", "failed", "stopped", "suspended"]);

/** How long an observation stays valid after the browser received it. */
export const RUNTIME_OBSERVATION_TTL_MS = 120_000;

/**
 * Stamp every runtime observation in an API payload with the time the browser
 * RECEIVED it. Freshness is measured on the client clock from this stamp, never
 * by comparing the server's `runtime_checked_at` to `Date.now()` (clock skew).
 */
export function stampRuntimeReceipt<T>(value: T, receivedAt = Date.now()): T {
  if (Array.isArray(value)) {
    value.forEach((v) => stampRuntimeReceipt(v, receivedAt));
  } else if (value && typeof value === "object") {
    const obj = value as Record<string, unknown>;
    if ("runtime_checked_at" in obj || "runtime_state" in obj) obj.runtime_received_at = receivedAt;
    Object.values(obj).forEach((v) => {
      if (v && typeof v === "object") stampRuntimeReceipt(v, receivedAt);
    });
  }
  return value;
}

/**
 * True when the observation can't be trusted: the server flagged it stale,
 * nothing was ever observed, or no fresh response arrived within the TTL
 * (e.g. polling stopped or keeps failing).
 */
export function isRuntimeExpired(instance: RuntimeHealth, now = Date.now()): boolean {
  if (instance.runtime_stale || !instance.runtime_checked_at) return true;
  const received = instance.runtime_received_at;
  return typeof received !== "number" || !Number.isFinite(received) || now - received > RUNTIME_OBSERVATION_TTL_MS;
}

/** Saved lifecycle state is never sufficient to display an online tenant. */
export function displayRuntimeStatus(instance: RuntimeInstance, now = Date.now()): string {
  if (!OBSERVED_STATES.has(instance.state)) return instance.state;
  if (isRuntimeExpired(instance, now)) return "unknown";
  return instance.runtime_state || "unknown";
}

export function isOnline(instance: RuntimeInstance, now = Date.now()): boolean {
  return displayRuntimeStatus(instance, now) === "online";
}
