import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/renderWithProviders";
import { ReactivateBanner, canReactivate } from "@/components/ReactivateBanner";
import { api, type ApiInstance } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  api: { instance: vi.fn() },
}));

const cancelled = {
  id: 7, name: "alpha", state: "cancelled", project_status: "cancelled",
  permissions: ["billing.manage"],
} as unknown as ApiInstance;

const assign = vi.fn();

beforeEach(() => {
  vi.mocked(api.instance).mockReset();
  assign.mockReset();
  Object.defineProperty(window, "location", { value: { ...window.location, assign }, writable: true });
});

describe("Reactivate for cancelled projects", () => {
  it("offers Reactivate and explains the kept snapshot and its fee", async () => {
    vi.mocked(api.instance).mockResolvedValue({
      ...cancelled, is_cancelled: true, has_retained_snapshot: true,
      retained_snapshot_date: "2026-10-01T10:00:00Z", restoration_fee: 12, currency: "USD",
      reactivate_url: "/my/instances/7/reactivate",
    } as ApiInstance);
    renderWithProviders(<ReactivateBanner project={cancelled} />);
    expect(await screen.findByText(/your last snapshot is kept/)).toBeInTheDocument();
    expect(screen.getByText(/\$12\.00/)).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: /Reactivate/ }));
    expect(assign).toHaveBeenCalledWith("/my/instances/7/reactivate");
  });

  it("says the project starts empty when no snapshot was kept", async () => {
    vi.mocked(api.instance).mockResolvedValue({ ...cancelled, is_cancelled: true, has_retained_snapshot: false } as ApiInstance);
    renderWithProviders(<ReactivateBanner project={cancelled} />);
    expect(await screen.findByText(/starts empty/)).toBeInTheDocument();
  });

  it("is only for cancelled projects and people who can pay", () => {
    expect(canReactivate(cancelled)).toBe(true);
    expect(canReactivate({ ...cancelled, permissions: ["project.view"] })).toBe(false);
    expect(canReactivate({ ...cancelled, state: "running", project_status: "active" } as ApiInstance)).toBe(false);
    renderWithProviders(<ReactivateBanner project={{ ...cancelled, state: "running", project_status: "active" } as ApiInstance} />);
    expect(screen.queryByRole("button", { name: /Reactivate/ })).not.toBeInTheDocument();
    expect(api.instance).not.toHaveBeenCalled();
  });
});
