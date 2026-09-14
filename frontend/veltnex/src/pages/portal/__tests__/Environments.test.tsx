import * as React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/renderWithProviders";
import Environments from "@/pages/portal/Environments";
import { api, ApiError, type ProjectEnvironments, type EnvChild } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      environments: vi.fn(),
      instanceBranches: vi.fn(),
      environmentCreate: vi.fn(),
      // Rendered unconditionally by <DeploymentHistory> on the (default)
      // Overview tab.
      instanceBuilds: vi.fn().mockResolvedValue([]),
    },
  };
});

// Environments.tsx statically imports every tab's page component
// (ShellPage -> ShellConsole -> @xterm/xterm) even though only one tab
// renders at a time, and importing the real xterm module alone probes
// for canvas support — which jsdom doesn't implement. Stub it the same
// way ShellConsole.test.tsx does, purely so the import doesn't crash;
// the Shell tab itself isn't exercised by these tests.
vi.mock("@xterm/xterm", () => ({
  Terminal: vi.fn().mockImplementation(function () {
    return { loadAddon: vi.fn(), open: vi.fn(), write: vi.fn(), dispose: vi.fn(), focus: vi.fn(), onData: vi.fn(), onResize: vi.fn(), cols: 80, rows: 24 };
  }),
}));
vi.mock("@xterm/addon-fit", () => ({
  FitAddon: vi.fn().mockImplementation(function () {
    return { fit: vi.fn() };
  }),
}));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));

const mockedApi = vi.mocked(api, { deep: true });

function makeEnv(overrides: Partial<EnvChild>): EnvChild {
  return {
    id: 1,
    name: "acme-prod",
    domain: "acme.example.com",
    url: "https://acme.example.com",
    version: "18.0",
    environment: "production",
    environment_label: "Production",
    branch: "main",
    state: "running",
    state_label: "Running",
    access_token: "tok",
    is_production: true,
    pending_payment: false,
    pending_invoice_id: false,
    ...overrides,
  } as EnvChild;
}

function mockProject(overrides: Partial<ProjectEnvironments> = {}) {
  const production = makeEnv({ id: 1, is_production: true, environment: "production" as EnvChild["environment"] });
  mockedApi.environments.mockResolvedValue({
    project_id: 1,
    project_name: "Acme",
    production,
    main_branch: "main",
    env_server_price: 10,
    billing_cycle: "monthly",
    has_repo: true,
    repo_url: "https://github.com/acme/acme",
    environments: [],
    production_plan: { plan_name: "Pro", workers: 4, storage_gb: 50, is_trial: false },
    slots: { staging: { used: 0, total: 2 }, development: { used: 0, total: 2 } },
    ...overrides,
  } as ProjectEnvironments);
}

describe("Environments", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a loading state, then the project once it loads", async () => {
    mockProject();
    renderWithProviders(<Environments />, { route: "/i/1", path: "/i/:id" });

    expect(screen.getByText(/loading project/i)).toBeInTheDocument();
    expect(await screen.findByText("Acme")).toBeInTheDocument();
    expect(screen.getAllByText("acme-prod").length).toBeGreaterThan(0);
  });

  it("shows an error banner when the project fails to load", async () => {
    mockedApi.environments.mockRejectedValue(new ApiError("Project not found.", "not_found"));
    renderWithProviders(<Environments />, { route: "/i/1", path: "/i/:id" });

    expect(await screen.findByText("Project not found.")).toBeInTheDocument();
  });

  it("creates a new staging server with the entered name and branch", async () => {
    mockProject();
    mockedApi.instanceBranches.mockResolvedValue({ branches: ["main", "feature-x"] });
    mockedApi.environmentCreate.mockResolvedValue({ auto_provisioned: true, child_id: 99 });
    const user = userEvent.setup();

    renderWithProviders(<Environments />, { route: "/i/1", path: "/i/:id" });
    await screen.findByText("Acme");

    // The "add" trigger next to "Staging" is an icon-only button (no
    // accessible name) — locate it via the section header it sits in.
    const stagingHeading = screen.getByText("Staging");
    const addButton = stagingHeading.parentElement!.querySelector("button")!;
    await user.click(addButton);

    await waitFor(() => expect(mockedApi.instanceBranches).toHaveBeenCalledWith(1));

    const dialog = screen.getByRole("dialog", { name: /new staging server/i });
    await user.type(within(dialog).getByLabelText(/server name/i), "qa-server");
    await user.click(within(dialog).getByRole("button", { name: /create server/i }));

    await waitFor(() =>
      expect(mockedApi.environmentCreate).toHaveBeenCalledWith(1, "staging", "qa-server", undefined),
    );
    // A second load() call follows a successful create.
    await waitFor(() => expect(mockedApi.environments).toHaveBeenCalledTimes(2));
  });
});
