import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/renderWithProviders";
import ProjectAccess from "../ProjectAccess";
import { api, type IamData } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(),
  api: { iam: vi.fn(), iamInvite: vi.fn(), iamGrant: vi.fn(), iamRevoke: vi.fn(), iamGroup: vi.fn() } }));
const mocked = vi.mocked(api, { deep: true });
const fixture = (): IamData => ({
  roles: [{ code: "viewer", name: "Project Viewer", permissions: ["project.view"] },
    { code: "logs", name: "Logs Viewer", permissions: ["logs.view"] },
    { code: "terminal", name: "Terminal Operator", permissions: ["terminal.open"] }],
  projects: [1, 2].map(id => ({ id, name: `Project ${id}`, customer_id: 10, is_owner: true,
    assignable_roles: { staging: ["viewer", "logs"], development: ["viewer", "logs"], production: [] } })),
  groups: [], members: [{ id: 4, name: "Ahmed", email: "ahmed@example.com", customer_ids: [10] }],
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
  });
  it("explains the required role for existing teammates without access management", async () => {
    mocked.iam.mockResolvedValue({ ...fixture(), projects: [], empty_reason: "access_not_granted" });
    renderWithProviders(<ProjectAccess />);
    expect(await screen.findByText("Access management hasn’t been granted")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Create a project" })).not.toBeInTheDocument();
  });
  it("grants combined roles across selected projects from the access panel", async () => {
    mocked.iamInvite.mockResolvedValue({ id: 1, invite_url: "/my/access/accept?token=test" });
    const user = userEvent.setup();
    renderWithProviders(<ProjectAccess />);
    await user.click(await screen.findByRole("button", { name: "Grant access" }));
    await user.click(screen.getByRole("checkbox", { name: "Project 1" }));
    await user.click(screen.getByRole("checkbox", { name: "Project 2" }));
    await user.type(screen.getByLabelText("Email address"), "ahmed@example.com");
    await user.click(screen.getByRole("checkbox", { name: /Logs Viewer/ }));
    await user.type(screen.getByLabelText("Find a role"), "terminal");
    expect(screen.getByRole("checkbox", { name: /Terminal Operator/ })).toBeDisabled();
    expect(mocked.iamInvite).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Grant access" })).toHaveTextContent("Billing access is never included.");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocked.iamInvite).toHaveBeenCalledWith({ email: "ahmed@example.com",
      project_ids: [1, 2], roles: ["viewer", "logs"], environments: ["staging", "development"] }));
    expect(await screen.findByRole("dialog", { name: "Invitation ready" })).toHaveTextContent("Copy invitation link");
  });
  it("blocks saving when the selected environment exceeds the administrator's scope", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ProjectAccess />);
    await user.click(await screen.findByRole("button", { name: "Grant access" }));
    await user.click(screen.getByRole("checkbox", { name: "Project 1" }));
    await user.type(screen.getByLabelText("Email address"), "ahmed@example.com");
    await user.click(screen.getByRole("checkbox", { name: "production" }));
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });
  it("groups a teammate's roles into one row and confirms bulk removal", async () => {
    const d = fixture();
    d.grants.push({ ...d.grants[0], id: 21, role: "viewer", project_id: 2 });
    mocked.iam.mockResolvedValue(d);
    const user = userEvent.setup();
    renderWithProviders(<ProjectAccess />);
    expect(await screen.findAllByText("Ahmed")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Manage access for Ahmed" }));
    const details = screen.getByRole("dialog", { name: "Ahmed" });
    expect(details).toHaveTextContent("Project 1");
    expect(details).toHaveTextContent("Project 2");
    await user.click(within(details).getByRole("button", { name: "Remove access" }));
    expect(mocked.iamRevoke).not.toHaveBeenCalled();
    await user.click(within(screen.getByRole("dialog", { name: "Remove access?" })).getByRole("button", { name: "Remove access" }));
    await waitFor(() => expect(mocked.iamRevoke).toHaveBeenCalledWith([20, 21]));
  });
  it("adds access to an accepted teammate through the same guided flow", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ProjectAccess />);
    await user.click(await screen.findByRole("button", { name: "Manage access for Ahmed" }));
    await user.click(screen.getByRole("button", { name: "Add access" }));
    expect(screen.getByLabelText("Who needs access?")).toHaveValue("user:4");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocked.iamGrant).toHaveBeenCalledWith({ project_ids: [1], roles: ["viewer"], environments: ["staging", "development"], user_id: 4 }));
  });
  it("saves group membership once after editing instead of on every checkbox", async () => {
    const d = fixture();
    d.groups.push({ id: 8, name: "Developers", customer_id: 10, user_ids: [] });
    mocked.iam.mockResolvedValue(d);
    const user = userEvent.setup();
    renderWithProviders(<ProjectAccess />);
    await user.click(await screen.findByRole("tab", { name: /Team groups/ }));
    await user.click(screen.getByRole("button", { name: "Manage members" }));
    await user.click(screen.getByRole("checkbox", { name: /Ahmed/ }));
    expect(mocked.iamGroup).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Save group" }));
    await waitFor(() => expect(mocked.iamGroup).toHaveBeenCalledWith({ group_id: 8, name: "Developers", user_ids: [4] }));
  });
});
