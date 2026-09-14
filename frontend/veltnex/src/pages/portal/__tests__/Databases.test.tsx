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
const RUNNING_HOSTING_INSTANCE = { is_hosting: true } as unknown as ApiInstance;

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
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a loading spinner, then the database list", async () => {
    mockLoaded();
    renderWithProviders(<Databases embedId={1} />);

    expect(screen.getByText(/loading databases/i)).toBeInTheDocument();
    expect(await screen.findByText("production")).toBeInTheDocument();
  });

  it("shows an error banner when the list fails to load", async () => {
    mockedApi.databases.mockRejectedValue(new ApiError("Instance is unreachable.", "error"));
    mockedApi.backups.mockResolvedValue({ backups: [], ready: true, state: "running" });
    mockedApi.instance.mockResolvedValue(RUNNING_HOSTING_INSTANCE);

    renderWithProviders(<Databases embedId={1} />);

    expect(await screen.findByText("Instance is unreachable.")).toBeInTheDocument();
  });

  it("creates a database with the values entered in the dialog", async () => {
    mockLoaded();
    mockedApi.dbCreate.mockResolvedValue(undefined);
    const user = userEvent.setup();

    renderWithProviders(<Databases embedId={1} />);
    await screen.findByText("production");

    await user.click(screen.getByRole("button", { name: /create database/i }));
    const dialog = screen.getByRole("dialog");

    await user.type(within(dialog).getByLabelText(/database name/i), "staging");
    await user.type(within(dialog).getByLabelText(/admin password/i), "s3cret!");
    await user.click(within(dialog).getByRole("button", { name: /create database/i }));

    await waitFor(() =>
      expect(mockedApi.dbCreate).toHaveBeenCalledWith(1, "staging", "admin", "s3cret!"),
    );
    // The create flow re-loads the list rather than showing a toast.
    await waitFor(() => expect(mockedApi.databases).toHaveBeenCalledTimes(2));
  });

  it("rejects a database name that fails the naming pattern before calling the API", async () => {
    mockLoaded();
    const user = userEvent.setup();

    renderWithProviders(<Databases embedId={1} />);
    await screen.findByText("production");

    await user.click(screen.getByRole("button", { name: /create database/i }));
    const dialog = screen.getByRole("dialog");

    await user.type(within(dialog).getByLabelText(/database name/i), "Bad Name!");
    await user.type(within(dialog).getByLabelText(/admin password/i), "s3cret!");
    await user.click(within(dialog).getByRole("button", { name: /create database/i }));

    expect(
      await within(dialog).findByText(/lowercase letters, numbers, or underscores/i),
    ).toBeInTheDocument();
    expect(mockedApi.dbCreate).not.toHaveBeenCalled();
  });

  it("drops a database only after its name is typed to confirm", async () => {
    mockLoaded();
    mockedApi.dbDrop.mockResolvedValue(undefined);
    const user = userEvent.setup();

    renderWithProviders(<Databases embedId={1} />);
    await screen.findByText("production");

    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(screen.getByRole("button", { name: /delete/i }));

    const dialog = screen.getByRole("dialog", { name: /delete database/i });
    const deleteButton = within(dialog).getByRole("button", { name: /delete database/i });
    expect(deleteButton).toBeDisabled();

    await user.type(within(dialog).getByLabelText(/type production to confirm/i), "production");
    expect(deleteButton).toBeEnabled();

    await user.click(deleteButton);

    await waitFor(() => expect(mockedApi.dbDrop).toHaveBeenCalledWith(1, "production"));
  });
});
