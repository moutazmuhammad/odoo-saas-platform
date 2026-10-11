import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Home from "../Home";

const configuration = vi.hoisted(() => ({ hosting: true }));
vi.mock("@/lib/useSections", () => ({ useSections: () => configuration }));
// three.js can't render in jsdom; the globe is lazy and wrapped in an error boundary anyway.
vi.mock("@/components/Globe", () => ({ Globe: () => <div data-testid="globe" /> }));

beforeEach(() => {
  configuration.hosting = true;
});

const renderHome = () => render(<MemoryRouter><Home /></MemoryRouter>);

describe("Home", () => {
  it("leads with the globe hero and the hosting path", async () => {
    renderHome();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Run Odoo the way");
    expect(await screen.findByTestId("globe")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /start hosting/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /configure your plan/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /one project\. every environment in view/i })).toBeInTheDocument();
    expect(screen.getByText(/git-based deploys/i)).toBeInTheDocument();
  });

  it("never offers ready-made apps", () => {
    renderHome();
    expect(screen.queryByText(/ready-made/i)).not.toBeInTheDocument();
    expect(document.querySelector('a[href^="/services"]')).toBeNull();
  });

  it("falls back to sign-up when hosting is off", () => {
    configuration.hosting = false;
    renderHome();
    expect(screen.queryByRole("button", { name: /start hosting/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/git-based deploys/i)).not.toBeInTheDocument();
    expect(screen.getByText(/alerts before your customers notice/i)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /create an account/i }).length).toBeGreaterThan(0);
  });
});
