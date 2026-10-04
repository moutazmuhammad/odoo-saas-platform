import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/renderWithProviders";
import PasswordSetup from "../PasswordSetup";
import { api, ApiError } from "@/lib/api";
const auth = vi.hoisted(() => ({ user: { email: "ahmed@example.com", must_change_password: true }, refresh: vi.fn(), logout: vi.fn() }));
vi.mock("@/context/AuthContext", () => ({ useAuth: () => auth }));
vi.mock("@/lib/api", async original => ({ ...await original<typeof import("@/lib/api")>(), api: { iamPasswordChange: vi.fn() } }));
const mocked = vi.mocked(api, { deep: true });
describe("First-login password change", () => {
  beforeEach(() => { vi.clearAllMocks(); auth.user.must_change_password = true; });
  it("requires matching replacement passwords without email or phone verification", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PasswordSetup />);
    const continueButton = screen.getByRole("button", { name: "Save password and continue" });
    expect(continueButton).toBeDisabled();
    expect(screen.queryByText(/verification code/i)).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("New password (at least 12 characters)"), "MyOwnPassword123!");
    await user.type(screen.getByLabelText("Confirm password"), "WrongPassword123!");
    expect(continueButton).toBeDisabled();
    await user.clear(screen.getByLabelText("Confirm password"));
    await user.type(screen.getByLabelText("Confirm password"), "MyOwnPassword123!");
    await user.click(continueButton);
    await waitFor(() => expect(mocked.iamPasswordChange).toHaveBeenCalledWith("MyOwnPassword123!"));
    expect(auth.refresh).toHaveBeenCalled();
  });
  it("keeps the account gated when the server rejects the new password", async () => {
    mocked.iamPasswordChange.mockRejectedValue(new ApiError("Choose a password different from your temporary password."));
    const user = userEvent.setup();
    renderWithProviders(<PasswordSetup />);
    await user.type(screen.getByLabelText("New password (at least 12 characters)"), "TemporaryPassword123!");
    await user.type(screen.getByLabelText("Confirm password"), "TemporaryPassword123!");
    await user.click(screen.getByRole("button", { name: "Save password and continue" }));
    expect(await screen.findByText(/different from your temporary password/)).toBeInTheDocument();
    expect(auth.refresh).not.toHaveBeenCalled();
  });
});
