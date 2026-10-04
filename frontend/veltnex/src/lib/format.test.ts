import { describe, expect, it } from "vitest";
import { formatDate, formatDateTime, parseDate } from "./format";

describe("local date and time display", () => {
  it.each(["2026-10-04 12:30:00", "2026-10-04T12:30:00"])(
    "reads legacy Odoo timestamp %s as UTC",
    (value) => {
      expect(parseDate(value).toISOString()).toBe("2026-10-04T12:30:00.000Z");
      expect(formatDateTime(value)).toBe(
        new Date("2026-10-04T12:30:00Z").toLocaleString(undefined, {
          month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
        }),
      );
    },
  );

  it("preserves explicit timezone offsets, including repeated DST hours", () => {
    expect(parseDate("2026-10-04T15:30:00+03:00").toISOString()).toBe("2026-10-04T12:30:00.000Z");
    expect(parseDate("2026-11-01T01:30:00-05:00").getTime()
      - parseDate("2026-11-01T01:30:00-04:00").getTime()).toBe(3600000);
  });

  it("keeps invoice calendar dates on the same day in every timezone", () => {
    const date = parseDate("2026-10-04");
    expect([date.getFullYear(), date.getMonth(), date.getDate()]).toEqual([2026, 9, 4]);
    expect(formatDate("2026-10-04")).toBe(new Date(2026, 9, 4).toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    }));
  });

  it("converts timestamps near midnight to the user's calendar day", () => {
    expect(formatDate("2026-10-04 23:30:00")).toBe(new Date("2026-10-04T23:30:00Z").toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    }));
  });

  it("keeps Date values and fractional UTC timestamps", () => {
    const date = new Date("2026-10-04T12:30:00Z");
    expect(parseDate(date)).toBe(date);
    expect(parseDate("2026-10-04 12:30:00.123456").getUTCMilliseconds()).toBe(123);
  });

  it("renders absent or invalid timestamps safely", () => {
    expect(formatDateTime("")).toBe("—");
    expect(formatDate("invalid")).toBe("—");
  });
});
