import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { DeploymentHistory } from "../DeploymentHistory";
import { api, type Build } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  api: { instanceBuilds: vi.fn() },
}));

beforeEach(() => vi.clearAllMocks());

describe("Deployment progress", () => {
  it.each([
    ["queued", "Queued…", "Waiting for a build slot."],
    ["building", "Building image…", "Preparing your code and dependencies."],
    ["deploying", "Deploying…", "Starting the new version and checking it is ready."],
  ] as const)("shows the %s phase and elapsed time", async (stage, label, message) => {
    vi.mocked(api.instanceBuilds).mockResolvedValue([{
      id: 1, source: "redeploy", state: "running", stage,
      duration_s: 95, commit_message: "", branch: "stage", commit: "",
      author: "", at: "", log: "", commit_url: "",
    } as Build]);
    render(<DeploymentHistory instanceId={12} />);
    expect(await screen.findByText(label)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(message);
    expect(screen.getByText("· 1m 35s")).toBeInTheDocument();
  });
});
