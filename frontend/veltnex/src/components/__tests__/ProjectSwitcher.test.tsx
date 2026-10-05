import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/renderWithProviders";
import { ProjectSwitcher } from "../ProjectSwitcher";
import type { ApiInstance } from "@/lib/api";

vi.mock("@/context/InstancesContext", () => ({ useInstances: () => ({
  instances: [
    { id: 1, name: "Owned project", is_owned_project: true, is_hosting: true },
    { id: 2, name: "Shared project", is_owned_project: false, is_hosting: true },
    { id: 3, name: "Staging environment", parent_id: 1, is_owned_project: true },
    { id: 1, name: "Owned project", is_owned_project: true, is_hosting: true },
  ] as ApiInstance[], loading: false, error: null,
}) }));

describe("Project selector categories", () => {
  it("groups root projects by ownership, without environments or duplicates", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ProjectSwitcher />);
    await user.click(screen.getByRole("button", { name: "Select a project" }));
    const own = screen.getByRole("region", { name: "My projects" });
    const shared = screen.getByRole("region", { name: "Shared projects" });
    expect(within(own).getAllByRole("button", { name: /Owned project/ })).toHaveLength(1);
    expect(within(shared).getAllByRole("button", { name: /Shared project/ })).toHaveLength(1);
    expect(within(own).queryByText("Shared project")).not.toBeInTheDocument();
    expect(within(shared).queryByText("Owned project")).not.toBeInTheDocument();
    expect(screen.queryByText("Staging environment")).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("Search projects"), "Shared");
    expect(screen.queryByText("Owned project")).not.toBeInTheDocument();
    expect(screen.getByText("Shared project")).toBeInTheDocument();
  });
  it("does not add the active project's environments to the selector", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ProjectSwitcher />, { route: "/my/instances/1/environments" });
    await user.click(screen.getByRole("button", { name: "Owned project" }));
    expect(screen.queryByText(/· environments/)).not.toBeInTheDocument();
    expect(screen.queryByText("Staging environment")).not.toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "My projects" })).getAllByRole("button", { name: /Owned project/ })).toHaveLength(1);
  });
});
