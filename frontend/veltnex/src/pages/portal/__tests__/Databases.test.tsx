import * as React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/renderWithProviders";
import Databases from "@/pages/portal/Databases";
import { api, ApiError, type ApiInstance, type DbListData } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      databases: vi.fn(),
      backups: vi.fn(),
      instance: vi.fn(),
      dbCreate: vi.fn(),
      dbDrop: vi.fn(),
    },
  };
});

const mockedApi = vi.mocked(api, { deep: true });

// Only the fields Databases.tsx actually reads.
const RUNNING_HOSTING_INSTANCE = { name: "acme", is_hosting: true, permissions: ["db.view", "db.create", "db.delete", "db.restore", "backup.create", "backup.download"] } as unknown as ApiInstance;

function mockLoaded(overrides: Partial<DbListData> = {}) {
  mockedApi.databases.mockResolvedValue({
    databases: [{ name: "production", login: "admin" }],
    ready: true,
    pending_ops: [],
    ...overrides,
  });
  mockedApi.backups.mockResolvedValue({ backups: [], ready: true, state: "running" });
  mockedApi.instance.mockResolvedValue(RUNNING_HOSTING_INSTANCE);
}

describe("Databases", () => {
  it("offers no create, duplicate or delete: the database is managed like Odoo.sh", async () => {
    mockLoaded({database_limit: 1});
    const user = userEvent.setup();
    renderWithProviders(<Databases embedId={1} />);
    await screen.findByText(/production database/i);
    expect(screen.queryByRole("button", {name: /create database/i})).not.toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /database manager/i})).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", {name: /more actions/i}));
    expect(screen.getByRole("button", {name: /download backup/i})).toBeInTheDocument();
    expect(screen.getByRole("button", {name: /reset password/i})).toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /^delete$/i})).not.toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /duplicate/i})).not.toBeInTheDocument();
    expect(screen.queryByRole("button", {name: /upgrade modules/i})).not.toBeInTheDocument();
  });

  it("restores into the existing database only after its name is typed", async () => {
    mockLoaded({database_limit: 1});
    const user = userEvent.setup();
    renderWithProviders(<Databases embedId={1} />);
    await screen.findByText(/production database/i);
    await user.click(screen.getByRole("button", {name: /restore database/i}));
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/new database name/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/type acme to confirm/i)).toBeInTheDocument();
    expect(screen.queryByText("production")).not.toBeInTheDocument();
    expect(screen.getByRole("button", {name: /upload.*restore/i})).toBeDisabled();
  });

  it("explains that the first database is prepared automatically", async () => {
    mockLoaded({database_limit: 1, databases: []});
    renderWithProviders(<Databases embedId={1} />);
    await screen.findByText(/being prepared/i);
    expect(screen.queryByRole("button", {name: /create database/i})).not.toBeInTheDocument();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a loading spinner, then the database list", async () => {
    mockLoaded();
    renderWithProviders(<Databases embedId={1} />);

    expect(screen.getByText(/loading databases/i)).toBeInTheDocument();
    expect(await screen.findByText(/production database/i)).toBeInTheDocument();
  });

  it("shows an error banner when the list fails to load", async () => {
    mockedApi.databases.mockRejectedValue(new ApiError("Instance is unreachable.", "error"));
    mockedApi.backups.mockResolvedValue({ backups: [], ready: true, state: "running" });
    mockedApi.instance.mockResolvedValue(RUNNING_HOSTING_INSTANCE);

    renderWithProviders(<Databases embedId={1} />);

    expect(await screen.findByText("Instance is unreachable.")).toBeInTheDocument();
  });
});
