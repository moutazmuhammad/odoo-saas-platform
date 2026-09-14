import * as React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/renderWithProviders";
import SqlConsole from "@/pages/portal/SqlConsole";
import { api, ApiError } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      databases: vi.fn(),
      sqlQuery: vi.fn(),
    },
  };
});

const mockedApi = vi.mocked(api, { deep: true });

describe("SqlConsole", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("loads the database list and runs the default query against the first one", async () => {
    mockedApi.databases.mockResolvedValue({
      databases: [{ name: "sales", login: "admin" }, { name: "hr", login: "admin" }],
      ready: true,
    });
    mockedApi.sqlQuery.mockResolvedValue({
      columns: ["id", "name"],
      rows: [[1, "Acme"]],
      rowcount: 1,
      truncated: false,
      error: null,
    });

    renderWithProviders(<SqlConsole instanceId={7} />);

    await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue("sales"));

    await userEvent.click(screen.getByRole("button", { name: /run/i }));

    await waitFor(() =>
      expect(mockedApi.sqlQuery).toHaveBeenCalledWith(
        7,
        "sales",
        "SELECT id, name FROM res_partner ORDER BY id LIMIT 20;",
      ),
    );

    expect(await screen.findByText("Acme")).toBeInTheDocument();
    expect(screen.getByText("1 row")).toBeInTheDocument();
  });

  it("shows the ApiError message inline when the query fails", async () => {
    mockedApi.databases.mockResolvedValue({
      databases: [{ name: "sales", login: "admin" }],
      ready: true,
    });
    mockedApi.sqlQuery.mockRejectedValue(
      new ApiError("syntax error at or near \"SELCT\"", "sql_error"),
    );

    renderWithProviders(<SqlConsole instanceId={7} />);

    await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue("sales"));
    await userEvent.click(screen.getByRole("button", { name: /run/i }));

    expect(await screen.findByText(/syntax error at or near/i)).toBeInTheDocument();
  });

  it("disables Run until a database is loaded", () => {
    mockedApi.databases.mockReturnValue(new Promise(() => {})); // never resolves
    renderWithProviders(<SqlConsole instanceId={7} />);
    expect(screen.getByRole("button", { name: /run/i })).toBeDisabled();
  });
});
