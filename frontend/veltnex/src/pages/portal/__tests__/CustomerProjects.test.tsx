import * as React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/renderWithProviders";
import Instances from "@/pages/portal/Instances";
import Dashboard from "@/pages/portal/Dashboard";
import { api, type ApiInstance, type ApiUser, type DashboardData } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";

vi.mock("@/context/AuthContext", () => ({ useAuth: vi.fn() }));
vi.mock("@/context/InstancesContext", () => ({ useInstances: () => ({ instances: projects, loading: false, error: null }) }));
vi.mock("@/lib/useSections", () => ({ useSections: () => ({ hosting: true, services: true }) }));
vi.mock("@/components/PageHeader", () => ({ PageHeader: ({ title, actions }: { title: string; actions?: React.ReactNode }) => <><h1>{title}</h1>{actions}</> }));
vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  api: { dashboard: vi.fn() },
}));

const projects = [
  { id: 1, name: "alpha", customer: { id: 10, name: "Acme" }, state: "running" },
  { id: 2, name: "beta", customer: { id: 20, name: "Zen" }, state: "stopped" },
  { id: 3, name: "gamma", customer: { id: 10, name: "Acme" }, state: "stopped" },
].map((project) => ({ domain: "example.com", region: "EU", created: "", workers: 2, storage_gb: 10, state_label: project.state, is_owned_project: project.customer.id === 10, ...project } as ApiInstance));

beforeEach(() => {
  vi.mocked(useAuth).mockReturnValue({ user: { name: "Support", is_staff: true } as ApiUser } as ReturnType<typeof useAuth>);
  vi.mocked(api.dashboard).mockResolvedValue({
    instances: projects, recent_invoices: [], currency: "USD",
    stats: { instances: 3, running: 1, open_invoices: 0, outstanding: 0, wallet_balance: 0 },
  } as DashboardData);
});

describe("Staff customer project filtering", () => {
  it("shows all customers by default, filters projects, and combines search and running filters", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Instances />);
    expect(screen.getByText("alpha")).toBeInTheDocument();
    expect(screen.getByText("beta")).toBeInTheDocument();
    expect(screen.getAllByRole("option", { name: "Acme" })).toHaveLength(1);
    await user.selectOptions(screen.getByLabelText("Customer"), "10");
    expect(screen.queryByText("beta")).not.toBeInTheDocument();
    expect(screen.getByText("gamma")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Running (1)" }));
    expect(screen.queryByText("gamma")).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("Search projects…"), "missing");
    expect(screen.getByText("No matching projects")).toBeInTheDocument();
  });

  it("restores the customer filter from the page URL", () => {
    renderWithProviders(<Instances />, { route: "/my/instances?customer=20" });
    expect(screen.getByLabelText("Customer")).toHaveValue("20");
    expect(screen.getByText("beta")).toBeInTheDocument();
    expect(screen.queryByText("alpha")).not.toBeInTheDocument();
  });

  it("keeps customer controls hidden for regular customers", () => {
    vi.mocked(useAuth).mockReturnValue({ user: { name: "Owner", is_staff: false } as ApiUser } as ReturnType<typeof useAuth>);
    renderWithProviders(<Instances />);
    expect(screen.queryByLabelText("Customer")).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Customer" })).not.toBeInTheDocument();
  });

  it("also filters the dashboard project cards and fleet totals", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Dashboard />);
    await screen.findByText("alpha");
    await user.selectOptions(screen.getByLabelText("Customer"), "20");
    expect(screen.getByText("beta")).toBeInTheDocument();
    expect(screen.queryByText("alpha")).not.toBeInTheDocument();
    expect(screen.queryByText("gamma")).not.toBeInTheDocument();
    expect(screen.getByText("/ 1 running", { exact: false })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Customer"), "");
    expect(screen.getByText("alpha")).toBeInTheDocument();
    expect(screen.getByText("/ 3 running", { exact: false })).toBeInTheDocument();
  });
});


describe("My and shared project categories", () => {
  beforeEach(() => {
    vi.mocked(useAuth).mockReturnValue({ user: { id: 10, name: "Teammate and owner", is_staff: false, is_managed_teammate: true, can_create_projects: true } as ApiUser } as ReturnType<typeof useAuth>);
  });
  it("lists each project once in its own category and allows creating owned projects", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Instances />);
    expect(screen.getByRole("tab", { name: "My projects (2)" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Shared projects (1)" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByText("alpha")).toBeInTheDocument();
    expect(screen.getByText("gamma")).toBeInTheDocument();
    expect(screen.queryByText("beta")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create project" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Shared projects (1)" }));
    expect(screen.getByText("beta")).toBeInTheDocument();
    expect(screen.queryByText("alpha")).not.toBeInTheDocument();
    expect(screen.queryByText("gamma")).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("Search projects…"), "alpha");
    expect(screen.getByText("No matching projects")).toBeInTheDocument();
  });
  it("restores the shared category from the URL on refresh", () => {
    renderWithProviders(<Instances />, { route: "/my/instances?category=shared" });
    expect(screen.getByRole("tab", { name: "Shared projects (1)" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("beta")).toBeInTheDocument();
    expect(screen.queryByText("alpha")).not.toBeInTheDocument();
  });
});
