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
      environmentPlanPreview: vi.fn(),
      environmentChangePlan: vi.fn(),
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
    permissions: ["project.view", "logs.view", "deploy", "instance.operate", "db.view", "sql.execute", "terminal.open", "billing.manage", "environment.delete", "environment.create", "iam.manage"],
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

  it("lets a scoped creator create the first environment without exposing production", async () => {
    mockProject({ production: makeEnv({ permissions: [], state: "restricted", domain: "", url: "" }),
      environments: [], can_create: { staging: true, development: false }, is_project_owner: false });
    renderWithProviders(<Environments />, { route: "/i/1", path: "/i/:id" });
    expect(await screen.findByText(/No environments are available/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create staging environment" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Create development environment" })).not.toBeInTheDocument();
    expect(screen.queryByText("acme-prod")).not.toBeInTheDocument();
    expect(screen.queryByText(/loading project/i)).not.toBeInTheDocument();
  });

  it("resizes a staging server and sends the customer to pay the prorated difference", async () => {
    const staging = makeEnv({ id: 2, name: "acme-stg", environment: "staging", environment_label: "Staging", is_production: false, branch: "staging", workers: 2, storage_gb: 5 });
    mockProject({ environments: [staging] });
    const preview = {
      current: { workers: 2, storage_gb: 5, price: 10 },
      new: { workers: 2, storage_gb: 5, price: 10 },
      period: "monthly", charge_now: 0, remaining_days: 0, total_days: 30, currency: "USD",
      limits: { workers: { min: 1, max: 16 }, storage: { min: 5, max: 500 } }, pending_plan: "",
    };
    mockedApi.environmentPlanPreview.mockImplementation(async (_id, _child, workers, storage) =>
      workers == null ? preview : { ...preview, new: { workers, storage_gb: storage, price: 25 }, charge_now: 7.5, remaining_days: 15 });
    mockedApi.environmentChangePlan.mockResolvedValue({ applied: false, charge: 7.5, invoice_id: 9, checkout_url: "/my/instances/2/checkout" });
    const user = userEvent.setup();

    renderWithProviders(<Environments />, { route: "/i/1?env=2", path: "/i/:id" });
    await user.click(await screen.findByRole("button", { name: /change plan/i }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: /apply/i })).toBeDisabled();

    const workersInput = within(dialog).getByLabelText(/workers/i);
    await user.clear(workersInput);
    await user.type(workersInput, "4");
    expect(await within(dialog).findByText("$7.50")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /pay & resize/i }));
    await waitFor(() => expect(mockedApi.environmentChangePlan).toHaveBeenCalledWith(1, 2, 4, 5));
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
