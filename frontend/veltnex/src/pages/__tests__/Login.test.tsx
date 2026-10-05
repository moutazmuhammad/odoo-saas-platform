import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { renderWithProviders } from "@/test/renderWithProviders";
import Login from "../Login";
import { PublicLayout } from "@/components/layout/PublicLayout";

const auth = vi.hoisted(() => ({ login: vi.fn(), user: null as null | { must_change_password: boolean; must_verify_phone: boolean }, loading: false }));
vi.mock("@/context/AuthContext", () => ({ useAuth: () => auth }));
vi.mock("@/components/layout/PublicNav", () => ({ PublicNav: () => null }));
vi.mock("@/components/layout/Footer", () => ({ Footer: () => null }));

function renderLogin(route = "/login") {
  return renderWithProviders(<Routes>
    <Route path="/login" element={<Login />} />
    <Route path="/my/change-password" element={<h1>Required account setup</h1>} />
    <Route path="/" element={<h1>Public home</h1>} />
  </Routes>, { route });
}

describe("Immediate first-login onboarding", () => {
  beforeEach(() => { vi.clearAllMocks(); auth.user = null; auth.loading = false; });
  it.each([
    { must_change_password: true, must_verify_phone: true },
    { must_change_password: false, must_verify_phone: true },
  ])("opens setup immediately for a pending account %j", async flags => {
    auth.login.mockResolvedValue(flags);
    const user = userEvent.setup();
    renderLogin();
    await user.type(screen.getByLabelText("Email or username"), "teammate@example.com");
    await user.type(screen.getByLabelText("Password"), "TemporaryPassword123!");
    await user.click(screen.getByRole("button", { name: /Sign in/i }));
    expect(await screen.findByRole("heading", { name: "Required account setup" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Public home" })).not.toBeInTheDocument();
  });
  it("takes precedence over a backend redirect", async () => {
    auth.login.mockResolvedValue({ must_change_password: true });
    const user = userEvent.setup();
    renderLogin("/login?redirect=/odoo");
    await user.type(screen.getByLabelText("Email or username"), "teammate@example.com");
    await user.type(screen.getByLabelText("Password"), "TemporaryPassword123!");
    await user.click(screen.getByRole("button", { name: /Sign in/i }));
    expect(await screen.findByRole("heading", { name: "Required account setup" })).toBeInTheDocument();
  });
  it("keeps the normal landing page for accounts with completed onboarding", async () => {
    auth.login.mockResolvedValue({ must_change_password: false, must_verify_phone: false });
    const user = userEvent.setup();
    renderLogin();
    await user.type(screen.getByLabelText("Email or username"), "owner@example.com");
    await user.type(screen.getByLabelText("Password"), "OwnPassword123!");
    await user.click(screen.getByRole("button", { name: /Sign in/i }));
    expect(await screen.findByRole("heading", { name: "Public home" })).toBeInTheDocument();
  });
  it("resumes required setup when a signed-in teammate opens a public page", async () => {
    auth.user = { must_change_password: false, must_verify_phone: true };
    renderWithProviders(<Routes><Route element={<PublicLayout />}><Route path="/" element={<h1>Public home</h1>} /></Route><Route path="/my/change-password" element={<h1>Required account setup</h1>} /></Routes>);
    expect(await screen.findByRole("heading", { name: "Required account setup" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Public home" })).not.toBeInTheDocument();
  });
});
