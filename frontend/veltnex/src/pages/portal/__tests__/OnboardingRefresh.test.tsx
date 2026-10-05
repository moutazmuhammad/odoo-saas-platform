import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { renderWithProviders } from "@/test/renderWithProviders";
import { AuthProvider } from "@/context/AuthContext";
import { ProtectedRoute } from "@/components/layout/ProtectedRoute";
import PasswordSetup from "../PasswordSetup";
import { api } from "@/lib/api";

vi.mock("@/lib/api", async original => ({ ...await original<typeof import("@/lib/api")>(),
  api: { me: vi.fn(), iamPhoneSetup: vi.fn(), iamPhoneSend: vi.fn(), iamPhoneVerify: vi.fn() } }));
const mocked = vi.mocked(api, { deep: true });

function renderSession(route: string) {
  return renderWithProviders(<AuthProvider><Routes><Route element={<ProtectedRoute />}>
    <Route path="/my/change-password" element={<PasswordSetup />} />
    <Route path="/my/instances" element={<h1>Projects</h1>} />
  </Route></Routes></AuthProvider>, { route });
}

describe("Mobile verification after a page refresh", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.me.mockResolvedValue({ id: 4, name: "Teammate", email: "teammate@example.com", phone: "+201012345678", company: "", initials: "T", must_change_password: false, must_verify_phone: true });
    mocked.iamPhoneSetup.mockResolvedValue({ phone: "+201012345678", phone_country_id: 65, phone_countries: [{ id: 65, name: "Egypt", phone_code: 20 }] });
  });
  it("resumes mobile verification after reloading the setup page", async () => {
    const first = renderSession("/my/change-password");
    expect(await screen.findByRole("heading", { name: "Verify your mobile number" })).toBeInTheDocument();
    first.unmount();
    renderSession("/my/change-password");
    expect(await screen.findByRole("heading", { name: "Verify your mobile number" })).toBeInTheDocument();
    expect(mocked.me).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("heading", { name: "Projects" })).not.toBeInTheDocument();
    expect(mocked.iamPhoneVerify).not.toHaveBeenCalled();
  });
  it("redirects a refreshed projects page to mobile verification after the password has changed", async () => {
    renderSession("/my/instances");
    expect(await screen.findByRole("heading", { name: "Verify your mobile number" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Projects" })).not.toBeInTheDocument();
    expect(await screen.findByLabelText("Mobile number")).toHaveValue("+201012345678");
  });
});
