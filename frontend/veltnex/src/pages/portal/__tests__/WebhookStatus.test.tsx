import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/test/renderWithProviders";
import { api, type ApiInstance } from "@/lib/api";
import Code from "../Code";

vi.mock("@/hooks/usePolling", () => ({ usePolling: vi.fn() }));
vi.mock("@/lib/permissions", () => ({ hasPermission: () => true }));
vi.mock("@/lib/api", async original => ({
  ...await original<typeof import("@/lib/api")>(),
  api: { instance: vi.fn() },
}));

function renderStatus(repo: Partial<NonNullable<ApiInstance["repo"]>>, child = false) {
  vi.mocked(api.instance).mockResolvedValue({
    id: 1, is_hosting: true, state: "running", environment: child ? "staging" : "production",
    parent_id: child ? 2 : undefined,
    repo: { url: "https://github.com/acme/widgets", branch: "main", has_token: true,
      state: "cloned", webhook_enabled: true, webhook_registered: true, ...repo },
  } as ApiInstance);
  return renderWithProviders(<Code embedId={1} />);
}

describe("Customer webhook confirmation", () => {
  beforeEach(() => vi.clearAllMocks());

  it("does not claim delivery just because GitHub registration succeeded", async () => {
    renderStatus({ webhook_health: "pending" });
    expect(await screen.findByText(/Waiting for webhook delivery confirmation/)).toBeInTheDocument();
    expect(screen.queryByText(/Webhook delivery confirmed/)).not.toBeInTheDocument();
  });

  it("requires current registration verification as well as a received delivery", async () => {
    renderStatus({ webhook_registered: false, webhook_health: "healthy" });
    expect(await screen.findByText(/Webhook registration not confirmed/)).toBeInTheDocument();
    expect(screen.queryByText(/Webhook delivery confirmed/)).not.toBeInTheDocument();
  });

  it("shows confirmed delivery on inherited environment repositories too", async () => {
    renderStatus({ webhook_health: "healthy" }, true);
    expect(await screen.findByText(/Webhook delivery confirmed/)).toBeInTheDocument();
    expect(screen.getByText("Manage on project")).toBeInTheDocument();
  });

  it("shows delivery failures even while the hook remains registered", async () => {
    renderStatus({ webhook_health: "error" });
    expect(await screen.findByText(/Webhook verification failed/)).toBeInTheDocument();
  });
});
