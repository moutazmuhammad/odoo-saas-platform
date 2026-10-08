import * as React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/renderWithProviders";
import Snapshots from "@/pages/portal/Snapshots";
import { api, type ApiBackup, type ApiInstance } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { snapshotsAll: vi.fn(), instance: vi.fn(), snapshotCreate: vi.fn(), snapshotDeleteAny: vi.fn(), snapshotEstimate: vi.fn(), backupRestore: vi.fn() } };
});
const mockedApi = vi.mocked(api, { deep: true });
const INSTANCE = { name: "acme", is_hosting: true, state: "running", permissions: ["backup.view", "backup.create", "db.restore"] } as unknown as ApiInstance;
const ROWS: ApiBackup[] = [
  { id: 2, label: "before-upgrade", type: "automatic", size_mb: 2048, created: "2026-01-02T10:00:00Z", status: "available", download_url: "", is_full_instance: true, source: "snapshot",
    instance_id: 1, instance_state: "running", project_name: "acme", odoo_version: "18.0", billable_gb: 2, monthly_price: 0.8, billing_state: "paid", paid_until: "2026-02-02", currency: "USD",
    permissions: { can_restore: true, can_delete: true, can_new_project: true } },
  { id: 5, label: "old-shop", type: "automatic", size_mb: 1024, created: "2025-12-01T10:00:00Z", status: "available", download_url: "", is_full_instance: true, source: "snapshot",
    instance_id: false, instance_state: "deleted", project_name: "shop", billable_gb: 1, monthly_price: 0.4, billing_state: "overdue", pending_invoice_id: 77, currency: "USD",
    permissions: { can_restore: false, can_delete: true, can_new_project: true } },
];

describe("Snapshots", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedApi.snapshotsAll.mockResolvedValue({ snapshots: ROWS });
    mockedApi.instance.mockResolvedValue(INSTANCE);
    mockedApi.snapshotEstimate.mockResolvedValue({ size_gb: 1.4, limit: 5, count: 1, billable_gb: 2, monthly_price: 0.8, due_now: 0.8, currency: "USD" });
  });

  it("customer-wide: lists every snapshot, including one whose project is gone, with billing actions", async () => {
    renderWithProviders(<Snapshots />, { route: "/my/snapshots", path: "/my/snapshots" });
    expect(await screen.findByText("before-upgrade")).toBeInTheDocument();
    expect(screen.getByText("old-shop")).toBeInTheDocument();
    expect(screen.getByText(/project deleted/i)).toBeInTheDocument();
    expect(screen.getByText(/your snapshot invoice is overdue/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /pay invoice/i })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /new project/i })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: /restore/i })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /take snapshot/i })).not.toBeInTheDocument();
    expect(screen.getByText(/current snapshot charges/i)).toBeInTheDocument();
  });

  it("per-server: only that server's snapshots, with a priced Take snapshot dialog", async () => {
    mockedApi.snapshotCreate.mockResolvedValue({ snapshot_id: 3, limit: 5 });
    const user = userEvent.setup();
    renderWithProviders(<Snapshots embedId={1} />);
    expect(await screen.findByText("before-upgrade")).toBeInTheDocument();
    expect(screen.queryByText("old-shop")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /take snapshot/i }));
    const dialog = screen.getByRole("dialog");
    expect((await within(dialog).findAllByText("$0.80")).length).toBeGreaterThan(0);
    await user.type(within(dialog).getByLabelText(/snapshot name/i), "pre-migration");
    await user.click(within(dialog).getByRole("button", { name: /pay & take snapshot/i }));
    await waitFor(() => expect(mockedApi.snapshotCreate).toHaveBeenCalledWith(1, "pre-migration"));
  });

  it("deletes a snapshot after confirmation", async () => {
    mockedApi.snapshotDeleteAny.mockResolvedValue({ deleted: true });
    const user = userEvent.setup();
    renderWithProviders(<Snapshots embedId={1} />);
    await screen.findByText("before-upgrade");
    await user.click(screen.getByRole("button", { name: /delete snapshot/i }));
    const dialog = screen.getByRole("dialog", { name: /delete snapshot/i });
    await user.click(within(dialog).getByRole("button", { name: /^delete$/i }));
    await waitFor(() => expect(mockedApi.snapshotDeleteAny).toHaveBeenCalledWith(2));
  });
});
