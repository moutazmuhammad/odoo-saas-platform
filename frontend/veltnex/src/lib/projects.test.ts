import { describe, expect, it } from "vitest";
import { projectStatus } from "./projects";

describe("projectStatus", () => {
  it("prefers the status computed by the server", () => {
    // Overdue invoices are only known server-side.
    expect(projectStatus({ state: "running", project_status: "unpaid" })).toBe("unpaid");
  });

  it("maps subscription states when the server sent none", () => {
    expect(projectStatus({ state: "cancelled" })).toBe("cancelled");
    expect(projectStatus({ state: "cancelled_by_client" })).toBe("cancelled");
    expect(projectStatus({ state: "suspended" })).toBe("suspended");
    expect(projectStatus({ state: "pending_payment" })).toBe("unpaid");
  });

  it("never reflects a server's runtime state", () => {
    for (const state of ["running", "provisioning", "stopped", "failed"] as const) {
      expect(projectStatus({ state })).toBe("active");
    }
  });
});
