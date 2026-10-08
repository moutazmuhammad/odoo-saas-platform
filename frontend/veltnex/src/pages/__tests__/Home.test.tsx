import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import Home from "../Home";
import { ProductPreview } from "@/components/home/ProductPreview";
import { findArticle } from "@/lib/docs-content";
import { api, type ApiService } from "@/lib/api";

const configuration = vi.hoisted(() => ({ hosting: true, services: true }));
vi.mock("@/lib/useSections", () => ({ useSections: () => configuration }));
vi.mock("@/lib/api", () => ({ api: { services: vi.fn() } }));

beforeEach(() => {
  vi.clearAllMocks();
  configuration.hosting = true;
  configuration.services = true;
  vi.mocked(api.services).mockResolvedValue([]);
});

function renderHome() {
  return render(<MemoryRouter><Home /></MemoryRouter>);
}

describe("Homepage product presentation", () => {
  it("uses existing routes and documentation, with no placeholder destinations", async () => {
    renderHome();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Your Odoo.Under control.");
    expect(screen.getByText("Illustrative workspace · sample data")).toBeInTheDocument();
    expect(screen.queryByText(/99.99%|All systems operational|24\/7/)).not.toBeInTheDocument();
    await waitFor(() => expect(api.services).toHaveBeenCalled());
    for (const link of screen.getAllByRole("link")) {
      const href = link.getAttribute("href")!;
      expect(href).not.toBe("#");
      if (href.startsWith("/docs/")) expect(findArticle(href.slice(6))).not.toBeNull();
      else expect(["/hosting", "/services"]).toContain(href);
    }
  });

  it("does not market services or request their catalog when services are disabled", () => {
    configuration.services = false;
    renderHome();
    expect(screen.queryByRole("link", { name: "Explore ready-made apps" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Configure your hosting" })).toHaveLength(3);
    expect(api.services).not.toHaveBeenCalled();
  });

  it("keeps hosting-only tools out of the service-only homepage", async () => {
    configuration.hosting = false;
    renderHome();
    expect(screen.queryByRole("link", { name: "Configure your hosting" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "A workspace for the whole lifecycle." })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Databases" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Development/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Read the getting-started guide" })).toHaveAttribute("href", "/docs/managed-services");
    await waitFor(() => expect(api.services).toHaveBeenCalled());
  });

  it("falls back to registration when both product sections are disabled", () => {
    configuration.hosting = false;
    configuration.services = false;
    renderHome();
    expect(screen.getAllByRole("link", { name: "Create an account" })).toHaveLength(2);
    expect(screen.getAllByRole("link").some(link => ["/hosting", "/services"].includes(link.getAttribute("href")!))).toBe(false);
  });

  it("shows catalog entries supplied by the existing API and links to their detail routes", async () => {
    vi.mocked(api.services).mockResolvedValue([{ id: 42, name: "Configured service", tagline: "Catalog description", icon: "server" } as ApiService]);
    renderHome();
    expect(await screen.findByRole("link", { name: /Configured service/ })).toHaveAttribute("href", "/services/42");
    expect(screen.getByText("Catalog description")).toBeInTheDocument();
  });

  it("handles a failed catalog request without hiding the product explanation", async () => {
    vi.mocked(api.services).mockRejectedValue(new Error("offline"));
    renderHome();
    await waitFor(() => expect(api.services).toHaveBeenCalled());
    expect(screen.getByRole("heading", { name: "Your code. Or a ready-made app." })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Find your starting point." })).not.toBeInTheDocument();
  });

  it("updates homepage metadata and restores it when navigating away", () => {
    document.title = "Existing page title";
    const { unmount } = renderHome();
    expect(document.title).toBe("VELTNEX — The Odoo hosting platform");
    unmount();
    expect(document.title).toBe("Existing page title");
  });
});

describe("Read-only workspace preview", () => {
  it("switches environments and supports keyboard navigation through tabs", async () => {
    const user = userEvent.setup();
    render(<ProductPreview />);
    await user.click(screen.getByRole("button", { name: /Staging/ }));
    expect(screen.getByRole("heading", { name: "Staging" })).toBeInTheDocument();
    expect(within(screen.getByRole("tabpanel")).getByText("staging")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Overview" }));
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Databases" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Databases" })).toHaveAttribute("aria-selected", "true");
    expect(within(screen.getByRole("tabpanel")).getByText("veltnex_demo")).toBeInTheDocument();
    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Snapshots" })).toHaveFocus();
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Daily backups are an optional add-on.");
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveFocus();
    expect(api.services).not.toHaveBeenCalled();
  });

  it("remains valid if hosting is disabled after the visitor selects a tab", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ProductPreview />);
    await user.click(screen.getByRole("button", { name: /Development/ }));
    await user.click(screen.getByRole("tab", { name: "Snapshots" }));
    rerender(<ProductPreview hosting={false} />);
    expect(screen.getByRole("heading", { name: "Production" })).toBeInTheDocument();
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Review database snapshots");
    expect(screen.queryByRole("tab", { name: "Databases" })).not.toBeInTheDocument();
  });
});
