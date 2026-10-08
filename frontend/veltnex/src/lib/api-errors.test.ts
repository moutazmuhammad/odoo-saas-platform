import { describe, it, expect, vi, afterEach } from "vitest";
import { api, ApiError } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("JSON-RPC error messages", () => {
  it("drops the HTTP status prefix werkzeug adds to Forbidden errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ error: { message: "Odoo Server Error", data: { name: "werkzeug.exceptions.Forbidden",
        message: "403 Forbidden: The shell is available while the server is running. Start the server and try again." } } }),
    }));
    await expect(api.terminalCreate(1, 80, 24)).rejects.toMatchObject({
      message: "The shell is available while the server is running. Start the server and try again.",
    });
    await expect(api.terminalCreate(1, 80, 24)).rejects.toBeInstanceOf(ApiError);
  });
});
