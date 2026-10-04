import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/renderWithProviders";
import ProjectAccess from "../ProjectAccess";
import { api, type IamData } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(),
  api: { iam: vi.fn(), iamInvite: vi.fn(), iamRevoke: vi.fn() } }));
const mocked = vi.mocked(api, { deep: true });
const fixture = (): IamData => ({
  roles: [{ code: "viewer", name: "Project Viewer", permissions: ["project.view"] },
    { code: "logs", name: "Logs Viewer", permissions: ["logs.view"] },
    { code: "terminal", name: "Terminal Operator", permissions: ["terminal.open"] }],
  projects: [1, 2].map(id => ({ id, name: `Project ${id}`, customer_id: 10, is_owner: false,
    assignable_roles: { staging: ["viewer", "logs"], development: ["viewer", "logs"], production: [] } })),
  groups: [], members: [],
  grants: [{ id: 20, project_id: 1, role: "logs", environment: "staging", user_id: 4, group_id: null,
    name: "Ahmed", email: "ahmed@example.com", pending: false, expired: false, editable: true }],
});
describe("Project Access", () => {
  beforeEach(() => { vi.clearAllMocks(); mocked.iam.mockResolvedValue(fixture()); });
  it("prompts clients without projects to create their first project", async () => {
    mocked.iam.mockResolvedValue({ ...fixture(), projects: [], empty_reason: "no_projects" });
    renderWithProviders(<ProjectAccess />);
    expect(await screen.findByText("No projects yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create a project" })).toHaveAttribute("href", "/hosting");
    expect(screen.queryByText(/Access management hasn’t been granted/)).not.toBeInTheDocument();
  });
  it("explains the required role for existing teammates without access management", async () => {
    mocked.iam.mockResolvedValue({ ...fixture(), projects: [], empty_reason: "access_not_granted" });
    renderWithProviders(<ProjectAccess />);
    expect(await screen.findByText("Access management hasn’t been granted")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Create a project" })).not.toBeInTheDocument();
  });
  it("combines fixed roles across selected projects and keeps production excluded", async () => {
    mocked.iamInvite.mockResolvedValue({ id: 1, invite_url: "/my/access/accept?token=test" });
    const user = userEvent.setup();
    renderWithProviders(<ProjectAccess />);
    await screen.findByText("Grant project access");
    await user.click(screen.getByRole("checkbox", { name: "Project 1" }));
    await user.click(screen.getByRole("checkbox", { name: "Project 2" }));
    await user.click(screen.getByRole("checkbox", { name: "Logs Viewer" }));
    expect(screen.getByRole("checkbox", { name: "Terminal Operator" })).toBeDisabled();
    await user.type(screen.getByPlaceholderText("teammate@example.com"), "ahmed@example.com");
    await user.click(screen.getByRole("button", { name: "Send invitation" }));
    await waitFor(() => expect(mocked.iamInvite).toHaveBeenCalledWith({ email: "ahmed@example.com",
      project_ids: [1, 2], roles: ["viewer", "logs"], environments: ["staging", "development"] }));
  });
  it("prevents granting outside the administrator's scopes and removes a selected grant", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ProjectAccess />);
    await screen.findByText("Grant project access");
    await user.click(screen.getByRole("checkbox", { name: "Project 1" }));
    await user.type(screen.getByPlaceholderText("teammate@example.com"), "ahmed@example.com");
    await user.click(screen.getByRole("checkbox", { name: "production" }));
    expect(screen.getByRole("button", { name: "Send invitation" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Remove logs from Ahmed" }));
    await waitFor(() => expect(mocked.iamRevoke).toHaveBeenCalledWith([20]));
  });
});
