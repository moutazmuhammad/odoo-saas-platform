import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Home from "../Home";
import { api } from "@/lib/api";

const configuration = vi.hoisted(() => ({ hosting: true, services: true }));
vi.mock("@/lib/useSections", () => ({ useSections: () => configuration }));
vi.mock("@/lib/api", () => ({ api: { services: vi.fn() } }));
// three.js can't render in jsdom; the globe is lazy and wrapped in an error boundary anyway.
vi.mock("@/components/Globe", () => ({ Globe: () => <div data-testid="globe" /> }));

beforeEach(() => {
  vi.clearAllMocks();
  configuration.hosting = true;
  configuration.services = true;
  vi.mocked(api.services).mockResolvedValue([]);
});

const renderHome = () => render(<MemoryRouter><Home /></MemoryRouter>);

describe("Home", () => {
  it("leads with the globe hero and both product paths", async () => {
    renderHome();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Run Odoo the way");
    expect(await screen.findByTestId("globe")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /start hosting/i })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /browse ready-made apps/i }).length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: /one project\. every environment in view/i })).toBeInTheDocument();
    await waitFor(() => expect(api.services).toHaveBeenCalled());
  });

  it("hides hosting-only content when hosting is off", () => {
    configuration.hosting = false;
    renderHome();
    expect(screen.queryByRole("button", { name: /start hosting/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/git-based deploys/i)).not.toBeInTheDocument();
    expect(screen.getByText(/alerts before your customers notice/i)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /browse ready-made apps/i }).length).toBeGreaterThan(0);
  });

  it("does not request the catalog when services are off", () => {
    configuration.services = false;
    renderHome();
    expect(screen.queryByRole("button", { name: /browse ready-made apps/i })).not.toBeInTheDocument();
    expect(api.services).not.toHaveBeenCalled();
  });

  it("lists ready-made apps from the catalog", async () => {
    vi.mocked(api.services).mockResolvedValue([{ id: 7, name: "Pharmacy", icon: "pill", description: "Stock and sales." }] as never);
    renderHome();
    expect(await screen.findByRole("link", { name: /pharmacy/i })).toHaveAttribute("href", "/services/7");
  });
});
