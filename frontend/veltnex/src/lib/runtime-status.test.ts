import { describe, expect, it } from "vitest";
import { displayRuntimeStatus, isRuntimeExpired, stampRuntimeReceipt } from "./runtime-status";

const now = Date.parse("2026-10-06T12:00:00Z");
const checked = new Date(now).toISOString();
const fresh = { runtime_checked_at: checked, runtime_received_at: now };

describe("Observed production status", () => {
  it("never treats saved running state as proof of availability", () => {
    expect(displayRuntimeStatus({ state: "running" }, now)).toBe("unknown");
  });
  it("displays an observed endpoint outage even when lifecycle is running", () => {
    expect(displayRuntimeStatus({ state: "running", runtime_state: "unreachable", ...fresh }, now)).toBe("unreachable");
  });
  it("expires old online observations when status polling fails", () => {
    expect(displayRuntimeStatus({ state: "running", runtime_state: "online", ...fresh }, now + 121_000)).toBe("unknown");
  });
  it("reports stopping while suspension has not finished", () => {
    expect(displayRuntimeStatus({ state: "suspended", runtime_state: "stopping", ...fresh }, now)).toBe("stopping");
  });
  it("keeps payment and predeployment workflow labels", () => {
    expect(displayRuntimeStatus({ state: "pending_payment" }, now)).toBe("pending_payment");
  });
  it("uses live availability independently of saved failed deployment state", () => {
    expect(displayRuntimeStatus({ state: "failed", runtime_state: "online", ...fresh }, now)).toBe("online");
  });
  it("trusts the server stale flag", () => {
    expect(displayRuntimeStatus({ state: "running", runtime_state: "online", ...fresh, runtime_stale: true }, now)).toBe("unknown");
  });
});

describe("Client clock skew", () => {
  it("measures freshness from receipt time, not the server's checked_at", () => {
    // Server clock 10 minutes behind (or browser 10 minutes ahead): still fresh.
    const skewed = { runtime_checked_at: new Date(now - 600_000).toISOString(), runtime_received_at: now };
    expect(isRuntimeExpired(skewed, now + 60_000)).toBe(false);
    expect(displayRuntimeStatus({ state: "running", runtime_state: "online", ...skewed }, now)).toBe("online");
  });
  it("expires 120s after receipt even if checked_at looks recent", () => {
    const future = { runtime_checked_at: new Date(now + 3_600_000).toISOString(), runtime_received_at: now };
    expect(isRuntimeExpired(future, now + 120_000)).toBe(false);
    expect(isRuntimeExpired(future, now + 120_001)).toBe(true);
  });
  it("treats observations without a receipt stamp as expired", () => {
    expect(isRuntimeExpired({ runtime_checked_at: checked }, now)).toBe(true);
  });
  it("stamps nested runtime observations in API payloads", () => {
    const payload = { id: 1, runtime_state: "online", children: [{ id: 2, runtime_checked_at: checked }], other: { x: 1 } };
    const out = stampRuntimeReceipt(payload, now) as typeof payload & { runtime_received_at?: number; other: { runtime_received_at?: number } };
    expect(out.runtime_received_at).toBe(now);
    expect((out.children[0] as { runtime_received_at?: number }).runtime_received_at).toBe(now);
    expect(out.other.runtime_received_at).toBeUndefined();
  });
});
