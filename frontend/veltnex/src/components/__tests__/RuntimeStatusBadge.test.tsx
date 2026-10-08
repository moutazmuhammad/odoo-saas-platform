import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RuntimeStatusBadge } from "../RuntimeStatusBadge";

afterEach(() => vi.useRealTimers());

describe("Production availability badge", () => {
  it("shows unreachable production instead of the saved Running label", () => {
    render(<RuntimeStatusBadge instance={{
      state: "running", state_label: "Running", runtime_state: "unreachable",
      runtime_checked_at: new Date().toISOString(), runtime_received_at: Date.now(), runtime_message: "Tenant URL is unreachable.",
    }} />);
    expect(screen.getByText("Offline")).toBeInTheDocument();
    expect(screen.queryByText("Running")).not.toBeInTheDocument();
  });
  it("shows only the status to customers, without technical details", () => {
    const { container } = render(<RuntimeStatusBadge instance={{
      state: "running", runtime_state: "unknown", runtime_checked_at: new Date().toISOString(), runtime_received_at: Date.now(),
      runtime_message: "Cannot check the tenant workload: cluster access failed.",
    }} />);
    expect(screen.getByText("Checking…")).toBeInTheDocument();
    expect(container.textContent).toBe("Checking…");
    expect(container.querySelector("[title]")).toBeNull();
  });
  it("removes green online status when no fresh observation arrives", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-06T12:00:00Z"));
    render(<RuntimeStatusBadge instance={{
      state: "running", runtime_state: "online", runtime_checked_at: new Date().toISOString(), runtime_received_at: Date.now(),
    }} />);
    expect(screen.getByText("Active")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(135_000));
    expect(screen.getByText("Checking…")).toBeInTheDocument();
    expect(screen.queryByText("Active")).not.toBeInTheDocument();
  });
  it("stays online when the server clock is skewed but the response is fresh", () => {
    render(<RuntimeStatusBadge instance={{
      state: "running", runtime_state: "online", runtime_received_at: Date.now(),
      runtime_checked_at: new Date(Date.now() - 30 * 60_000).toISOString(),
    }} />);
    expect(screen.getByText("Active")).toBeInTheDocument();
  });
});
