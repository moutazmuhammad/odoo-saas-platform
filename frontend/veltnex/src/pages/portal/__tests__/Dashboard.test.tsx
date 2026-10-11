import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { renderWithProviders } from "@/test/renderWithProviders";
import Dashboard from "@/pages/portal/Dashboard";
import { HomeOrDashboard } from "@/App";
import { api, type ApiInstance, type ApiUser, type DashboardData, type DashboardSummary } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";

vi.mock("@/context/AuthContext", () => ({ useAuth: vi.fn() }));
vi.mock("@/pages/Home", () => ({ default: () => <h1>Marketing home</h1> }));
vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  api: { dashboard: vi.fn() },
}));

const hourAgo = new Date(Date.now() - 3600_000).toISOString();
const twoDaysAgo = new Date(Date.now() - 2 * 86400_000).toISOString();
const ref = (id: number, name: string, environment: "production" | "staging" = "production") =>
  ({ project_id: id, project_name: name, environment });

const projects = [
  { id: 1, name: "alpha", state: "running" },
  { id: 2, name: "beta", state: "running" },
].map((p) => ({ domain: "example.com", region: "EU", created: "", workers: 2, storage_gb: 10, state_label: p.state, ...p } as ApiInstance));

const summary: DashboardSummary = {
  environments: [
    { ...ref(1, "alpha"), id: 1, name: "alpha", status: "online", message: "" },
    { ...ref(1, "alpha", "staging"), id: 11, name: "alpha-staging", status: "stopped", message: "" },
    { ...ref(2, "beta"), id: 2, name: "beta", status: "attention", message: "Odoo is not responding" },
  ],
  builds: [
    { ...ref(2, "beta"), id: 7, state: "failed", stage: "build", source: "push", branch: "main", commit: "abc1234", started_at: hourAgo },
    { ...ref(1, "alpha"), id: 6, state: "success", stage: "done", source: "push", branch: "main", commit: "def5678", started_at: hourAgo },
  ],
  backups: [
    { ...ref(1, "alpha"), env_id: 1, daily_enabled: true, status: "late", last_backup_at: twoDaysAgo },
    { ...ref(2, "beta"), env_id: 2, daily_enabled: false, status: "none", last_backup_at: null },
  ],
  renewals: [{ project_id: 2, project_name: "beta", date: "2026-11-01" }],
};

function mockDashboard(s: DashboardSummary | undefined) {
  vi.mocked(api.dashboard).mockResolvedValue({
    instances: projects, recent_invoices: [], currency: "USD",
    stats: { instances: 2, running: 2, open_invoices: 0, outstanding: 0, wallet_balance: 0 },
    summary: s,
  } as DashboardData);
}

beforeEach(() => {
  vi.mocked(useAuth).mockReturnValue({ user: { name: "Owner Person", is_staff: false } as ApiUser } as ReturnType<typeof useAuth>);
  mockDashboard(summary);
});

describe("Customer dashboard summary", () => {
  it("rolls up environment health, builds, backups and the next renewal", async () => {
    renderWithProviders(<Dashboard />);
    await screen.findByText("Recent builds");
    const status = screen.getByRole("list", { name: "Environment status" });
    expect(status).toHaveTextContent("Online1");
    expect(status).toHaveTextContent("Stopped1");
    expect(status).toHaveTextContent("Needs attention1");
    expect(screen.getByText("Odoo is not responding")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("Deployed")).toBeInTheDocument();
    expect(screen.getByText("No automatic backups")).toBeInTheDocument();
    expect(screen.getByText(/^Last backup /)).toBeInTheDocument();
    expect(screen.getByText("beta", { selector: "p.truncate.text-xs" })).toBeInTheDocument();
    // A failed build is raised in "Needs your attention" too.
    expect(screen.getByText("A build of beta failed")).toBeInTheDocument();
  });

  it("shows calm empty states when there is nothing to report", async () => {
    mockDashboard({ environments: [], builds: [], backups: [], renewals: [] });
    renderWithProviders(<Dashboard />);
    expect(await screen.findByText("No environments yet.")).toBeInTheDocument();
    expect(screen.getByText(/No builds yet/)).toBeInTheDocument();
    expect(screen.getByText("No Production environments yet.")).toBeInTheDocument();
    expect(screen.getByText("No upcoming renewals.")).toBeInTheDocument();
    expect(screen.queryByText("Needs your attention")).not.toBeInTheDocument();
  });

  it("still renders when an older server sends no summary", async () => {
    mockDashboard(undefined);
    renderWithProviders(<Dashboard />);
    expect(await screen.findByText("No environments yet.")).toBeInTheDocument();
  });

  it("reports all backups healthy when every Production backup is recent", async () => {
    mockDashboard({ ...summary, backups: [{ ...ref(1, "alpha"), env_id: 1, daily_enabled: true, status: "ok", last_backup_at: hourAgo }] });
    renderWithProviders(<Dashboard />);
    expect(await screen.findByText("All Production environments were backed up in the last day.")).toBeInTheDocument();
  });
});

describe("Home after sign-in", () => {
  const renderAt = (route: string) => renderWithProviders(
    <Routes>
      <Route path="/" element={<HomeOrDashboard />} />
      <Route path="/my/dashboard" element={<h1>Dashboard page</h1>} />
    </Routes>,
    { route },
  );

  it("sends a signed-in customer to the dashboard", () => {
    renderAt("/");
    expect(screen.getByText("Dashboard page")).toBeInTheDocument();
  });

  it("keeps the public site reachable from the portal", () => {
    renderAt("/?site=1");
    expect(screen.getByText("Marketing home")).toBeInTheDocument();
  });

  it("paints nothing while the session check is still running", () => {
    vi.mocked(useAuth).mockReturnValue({ user: null, loading: true } as unknown as ReturnType<typeof useAuth>);
    renderAt("/");
    expect(screen.queryByText("Marketing home")).not.toBeInTheDocument();
    expect(screen.queryByText("Dashboard page")).not.toBeInTheDocument();
  });

  it("shows the public site to visitors", () => {
    vi.mocked(useAuth).mockReturnValue({ user: null } as unknown as ReturnType<typeof useAuth>);
    renderAt("/");
    expect(screen.getByText("Marketing home")).toBeInTheDocument();
  });
});
