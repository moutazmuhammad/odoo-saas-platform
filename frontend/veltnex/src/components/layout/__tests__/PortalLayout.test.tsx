import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import { renderWithProviders } from "@/test/renderWithProviders";
import { PortalLayout } from "../PortalLayout";

vi.mock("@/context/AuthContext", () => ({ useAuth: () => ({ user: { name: "Client", email: "client@example.com" }, logout: vi.fn() }) }));
vi.mock("@/context/InstancesContext", () => ({ useInstances: () => ({ getInstance: () => ({ id: 13, name: "Client project", is_hosting: true }) }) }));
vi.mock("@/components/ProjectSwitcher", () => ({ ProjectSwitcher: () => null }));
vi.mock("@/components/NotificationsBell", () => ({ NotificationsBell: () => null }));
vi.mock("@/components/CommandPalette", () => ({ CommandPalette: () => null }));
function Location() { return <p data-testid="location">{useLocation().pathname}</p>; }
describe("Portal navigation", () => {
  it.each(["/my/access", "/my/settings"])("has a direct Projects action from %s", async route => {
    renderWithProviders(<><PortalLayout /><Location /></>, { route });
    await userEvent.setup().click(screen.getByTitle("Projects"));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/my/instances"));
  });
  it("shows Project Access once on the project code page", () => {
    renderWithProviders(<PortalLayout />, { route: "/my/instances/13/environments?tab=code" });
    expect(screen.getAllByTitle("Project Access")).toHaveLength(1);
  });
});
