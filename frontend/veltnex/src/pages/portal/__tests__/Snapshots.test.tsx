import * as React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/renderWithProviders";
import Snapshots from "@/pages/portal/Snapshots";
import { api, type ApiInstance } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { backups: vi.fn(), instance: vi.fn(), snapshotCreate: vi.fn(), snapshotDelete: vi.fn(), backupRestore: vi.fn() } };
});
const mockedApi = vi.mocked(api, { deep: true });
const INSTANCE = { name: "acme", is_hosting: true, permissions: ["backup.view", "backup.create", "db.restore"] } as unknown as ApiInstance;

function mockLoaded() {
  mockedApi.backups.mockResolvedValue({ ready: true, state: "running", backups: [
    { id: 1, label: "20260101T020000Z", type: "automatic", size_mb: 100, created: "2026-01-01T02:00:00Z", status: "available", download_url: "", is_full_instance: true, source: "scheduled" },
    { id: 2, label: "before-upgrade", type: "automatic", size_mb: 120, created: "2026-01-02T10:00:00Z", status: "available", download_url: "", is_full_instance: true, source: "snapshot" },
  ] });
  mockedApi.instance.mockResolvedValue(INSTANCE);
}

describe("Snapshots", () => {
  beforeEach(() => vi.clearAllMocks());

  it("lists only on-demand snapshots and offers restore, delete and new project", async () => {
    mockLoaded();
    renderWithProviders(<Snapshots embedId={1} />);
    expect(await screen.findByText("before-upgrade")).toBeInTheDocument();
    expect(screen.queryByText("20260101T020000Z")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /new project/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /restore/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /delete snapshot/i })).toBeEnabled();
  });

  it("takes a snapshot with the entered name", async () => {
    mockLoaded();
    mockedApi.snapshotCreate.mockResolvedValue({ snapshot_id: 3, limit: 5 });
    const user = userEvent.setup();
    renderWithProviders(<Snapshots embedId={1} />);
    await screen.findByText("before-upgrade");
    await user.click(screen.getByRole("button", { name: /take snapshot/i }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/snapshot name/i), "pre-migration");
    await user.click(within(dialog).getByRole("button", { name: /take snapshot/i }));
    await waitFor(() => expect(mockedApi.snapshotCreate).toHaveBeenCalledWith(1, "pre-migration"));
  });

  it("deletes a snapshot after confirmation", async () => {
    mockLoaded();
    mockedApi.snapshotDelete.mockResolvedValue({ deleted: true });
    const user = userEvent.setup();
    renderWithProviders(<Snapshots embedId={1} />);
    await screen.findByText("before-upgrade");
    await user.click(screen.getByRole("button", { name: /delete snapshot/i }));
    const dialog = screen.getByRole("dialog", { name: /delete snapshot/i });
    await user.click(within(dialog).getByRole("button", { name: /^delete$/i }));
    await waitFor(() => expect(mockedApi.snapshotDelete).toHaveBeenCalledWith(1, 2));
  });
});
