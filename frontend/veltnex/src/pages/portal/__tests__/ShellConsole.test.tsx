import * as React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithProviders } from "@/test/renderWithProviders";
import ShellConsole from "@/pages/portal/ShellConsole";
import { api, ApiError } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      terminalCreate: vi.fn(),
      terminalInput: vi.fn().mockResolvedValue(undefined),
      terminalResize: vi.fn().mockResolvedValue(undefined),
      terminalClose: vi.fn().mockResolvedValue(undefined),
    },
  };
});

// jsdom has no real canvas, so xterm can't actually render — stand in
// with a fake that records what the component does with it (cols/rows
// read for terminalCreate, onData/onResize handlers registered for the
// input/resize wiring, write() calls for SSE frames).
const fakeTerm = {
  cols: 80,
  rows: 24,
  open: vi.fn(),
  write: vi.fn(),
  focus: vi.fn(),
  dispose: vi.fn(),
  loadAddon: vi.fn(),
  onData: vi.fn(),
  onResize: vi.fn(),
};

vi.mock("@xterm/xterm", () => ({
  // A constructor function that returns the shared fake overrides `this`
  // for `new Terminal(...)` — vi.fn(() => fakeTerm) can't be `new`-ed.
  Terminal: vi.fn().mockImplementation(function () {
    return fakeTerm;
  }),
}));
vi.mock("@xterm/addon-fit", () => ({
  FitAddon: vi.fn().mockImplementation(function () {
    return { fit: vi.fn() };
  }),
}));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));

// jsdom doesn't implement EventSource — ShellConsole opens one for the
// SSE output stream. This fake lets tests dispatch onmessage/close/timeout
// events and inspect what URL was requested.
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onmessage: ((ev: { data: string }) => void) | null = null;
  private listeners: Record<string, ((ev: unknown) => void)[]> = {};
  closed = false;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, cb: (ev: unknown) => void) {
    (this.listeners[type] ??= []).push(cb);
  }
  close() {
    this.closed = true;
  }
}
vi.stubGlobal("EventSource", FakeEventSource);

const mockedApi = vi.mocked(api, { deep: true });

describe("ShellConsole", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    FakeEventSource.instances = [];
  });

  it("opens a session on mount and shows Connected once the stream is up", async () => {
    mockedApi.terminalCreate.mockResolvedValue({
      session_id: "sid-1",
      initial_output: "",
    });

    renderWithProviders(<ShellConsole instanceId={42} />);

    expect(screen.getByText("Connecting…")).toBeInTheDocument();

    await waitFor(() =>
      expect(mockedApi.terminalCreate).toHaveBeenCalledWith(42, fakeTerm.cols, fakeTerm.rows),
    );
    await waitFor(() => expect(screen.getByText("Connected")).toBeInTheDocument());

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toContain("sid-1");
  });

  it("wires terminal input to api.terminalInput once the session is open", async () => {
    mockedApi.terminalCreate.mockResolvedValue({ session_id: "sid-2", initial_output: "" });

    renderWithProviders(<ShellConsole instanceId={42} />);
    await waitFor(() => expect(screen.getByText("Connected")).toBeInTheDocument());

    const onData = fakeTerm.onData.mock.calls[0][0] as (d: string) => void;
    onData("ls -la\n");

    await waitFor(() =>
      expect(mockedApi.terminalInput).toHaveBeenCalledWith("sid-2", "ls -la\n"),
    );
  });

  it("shows an error state when the session can't be created", async () => {
    mockedApi.terminalCreate.mockRejectedValue(
      new ApiError("Instance is not running.", "instance_not_running"),
    );

    renderWithProviders(<ShellConsole instanceId={42} />);

    expect(await screen.findByText("Instance is not running.")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });
});
