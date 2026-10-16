import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/test/renderWithProviders";
import { UpgradeStatusBanner, upgradeMessage } from "@/components/UpgradeStatusBanner";
import type { ApiUpgradeStatus } from "@/lib/api";

const base: ApiUpgradeStatus = {
  status: "in_progress", target_at: "2026-10-16T14:20:00Z", deadline_at: "2026-10-16T16:05:00Z",
  completed_at: "", charged: false, can_cancel: false,
};
const INFRA = /cluster|node|pod|kubernetes|operator/i;

describe("Upgrade status banner (C6.4)", () => {
  it("says when it is usually done and that nothing is charged yet", () => {
    renderWithProviders(<UpgradeStatusBanner project={{ upgrade: base }} />);
    expect(screen.getByText("Applying your upgrade")).toBeInTheDocument();
    expect(screen.getByText(/Usually done by .*You'll be charged when it's complete\./)).toBeInTheDocument();
  });

  it("is plain about a delay and never mentions infrastructure", () => {
    for (const status of ["in_progress", "delayed", "action_available"] as const) {
      const m = upgradeMessage({ ...base, status })!;
      expect(m.title + " " + m.description).not.toMatch(INFRA);
    }
    expect(upgradeMessage({ ...base, status: "delayed" })!.description)
      .toBe("Our team is already on it; nothing for you to do. You haven't been charged.");
  });

  it("offers cancel only when it exists, and never says 'not charged' after a charge", () => {
    const later = upgradeMessage({ ...base, status: "action_available" })!;
    expect(later.description).not.toMatch(/cancel/);
    expect(upgradeMessage({ ...base, status: "action_available", can_cancel: true })!.description)
      .toMatch(/keep waiting or cancel it/);
    expect(upgradeMessage({ ...base, status: "delayed", charged: true })!.description)
      .not.toMatch(/haven't been charged/);
  });

  it("shows completion for an hour, then nothing", () => {
    const done = { ...base, status: "complete" as const, completed_at: "2026-10-16T14:00:00Z" };
    const at = (iso: string) => new Date(iso).getTime();
    expect(upgradeMessage(done, at("2026-10-16T14:30:00Z"))!.title).toBe("Upgrade complete");
    expect(upgradeMessage(done, at("2026-10-16T15:30:00Z"))).toBeNull();
  });

  it("renders nothing without an upgrade", () => {
    const { container } = renderWithProviders(<UpgradeStatusBanner project={{ upgrade: false }} />);
    expect(container).toBeEmptyDOMElement();
  });
});
