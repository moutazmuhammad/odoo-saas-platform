import { expect, it } from "vitest";
import { menuPosition } from "./menuPosition";
it("keeps database action menus visible at either edge in English and Arabic", () => {
  const rect = { left: 16, right: 48, bottom: 100 };
  expect(menuPosition(rect, 390, true)).toEqual({ top: 104, left: 16 });
  expect(menuPosition(rect, 390, false)).toEqual({ top: 104, left: 8 });
  expect(menuPosition({ left: 350, right: 382, bottom: 100 }, 390, true).left).toBe(206);
});
