import { describe, expect, it } from "vitest";
import { projectStatus } from "./projects";

describe("projectStatus", () => {
  it("shows a cancelled subscription as cancelled", () => {
    expect(projectStatus({ state: "cancelled" })).toBe("cancelled");
    expect(projectStatus({ state: "cancelled_by_client" })).toBe("cancelled");
  });

  it("never reflects environment availability", () => {
    for (const state of ["running", "provisioning", "stopped", "suspended", "failed", "pending_payment"] as const) {
      expect(projectStatus({ state })).toBe("active");
    }
  });
});
