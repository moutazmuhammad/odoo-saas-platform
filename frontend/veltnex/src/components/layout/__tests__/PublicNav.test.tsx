import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { PublicNav } from "../PublicNav";

vi.mock("@/context/AuthContext", () => ({
  useAuth: () => ({ isAuthenticated: false, user: null, logout: vi.fn() }),
}));
vi.mock("@/lib/useSections", () => ({
  useSections: () => ({ hosting: true, services: true }),
}));

function renderNav() {
  render(
    <MemoryRouter>
      <PublicNav />
      <Routes>
        <Route path="/" element={<p>Home content</p>} />
        <Route path="/hosting" element={<p>Hosting content</p>} />
        <Route path="/register" element={<p>Registration content</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Public mobile navigation", () => {
  it("announces menu state and restores focus when Escape closes it", async () => {
    const user = userEvent.setup();
    renderNav();
    const trigger = screen.getByRole("button", { name: "Toggle menu" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const mobile = document.getElementById("public-mobile-menu")!;
    await user.click(within(mobile).getByRole("link", { name: "Hosting" }));
    expect(screen.getByText("Hosting content")).toBeInTheDocument();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    await user.click(trigger);
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    expect(document.getElementById("public-mobile-menu")).toBeNull();
  });

  it("closes the mobile menu when registration is opened", async () => {
    const user = userEvent.setup();
    renderNav();
    await user.click(screen.getByRole("button", { name: "Toggle menu" }));
    const mobile = document.getElementById("public-mobile-menu")!;
    await user.click(within(mobile).getByRole("button", { name: "Get started" }));
    expect(screen.getByText("Registration content")).toBeInTheDocument();
    expect(document.getElementById("public-mobile-menu")).toBeNull();
  });
});
