import { describe, expect, it } from "vitest";
import { hasPermission } from "../permissions";
describe("project permissions", () => {
  it("denies missing permissions and grants only listed actions", () => {
    expect(hasPermission(undefined, "db.delete")).toBe(false);
    expect(hasPermission([], "deploy")).toBe(false);
    expect(hasPermission(["project.view"], "db.delete")).toBe(false);
    expect(hasPermission(["project.view"], "project.view")).toBe(true);
  });
});
