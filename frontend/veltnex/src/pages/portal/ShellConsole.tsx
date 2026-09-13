import * as React from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { api, ApiError, terminalOutputUrl } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Odoo.sh-style in-browser shell into the instance's container.
 *
 * Protocol (see saas_core/controllers/ssh_terminal.py):
 *  - create  → POST /saas/terminal/instance/create  → { session_id, initial_output(b64) }
 *  - output  → SSE  /saas/terminal/instance/output/<sid> (base64 chunks, JSON-encoded)
 *  - input   → POST /saas/terminal/instance/input
 *  - resize  → POST /saas/terminal/instance/resize
 *  - close   → POST /saas/terminal/instance/close
 */
function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return arr;
}

type Status = "connecting" | "open" | "closed" | "error";

export default function ShellConsole({ instanceId }: { instanceId: number }) {
  const hostRef = React.useRef<HTMLDivElement>(null);
  const [status, setStatus] = React.useState<Status>("connecting");
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let disposed = false;
    let sid: string | null = null;
    let es: EventSource | null = null;

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      theme: { background: "#0b0f19", foreground: "#e6e6e6" },
      scrollback: 5000,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    if (hostRef.current) term.open(hostRef.current);
    try {
      fit.fit();
    } catch {
      /* element not measured yet — best effort */
    }

    const connectStream = (sessionId: string) => {
      es = new EventSource(terminalOutputUrl(sessionId));
      es.onmessage = (e) => {
        try {
          const b64 = JSON.parse(e.data) as string;
          term.write(b64ToBytes(b64));
        } catch {
          /* ignore malformed frame */
        }
      };
      es.addEventListener("closed", () => {
        if (!disposed) setStatus("closed");
        es?.close();
      });
      es.addEventListener("timeout", () => {
        // The SSE GET caps at 5 min server-side; the shell lives on.
        es?.close();
        if (!disposed) connectStream(sessionId);
      });
      // Transient network errors: EventSource retries on its own.
    };

    const start = async () => {
      try {
        const { session_id, initial_output } = await api.terminalCreate(
          instanceId,
          term.cols,
          term.rows,
        );
        if (disposed) {
          api.terminalClose(session_id).catch(() => {});
          return;
        }
        sid = session_id;
        if (initial_output) term.write(b64ToBytes(initial_output));
        term.onData((d) => {
          if (sid) api.terminalInput(sid, d).catch(() => {});
        });
        term.onResize(({ cols, rows }) => {
          if (sid) api.terminalResize(sid, cols, rows).catch(() => {});
        });
        connectStream(session_id);
        setStatus("open");
        term.focus();
      } catch (e) {
        if (disposed) return;
        setError(e instanceof ApiError ? e.message : "Couldn't open the shell.");
        setStatus("error");
      }
    };
    start();

    const onResize = () => {
      try {
        fit.fit();
      } catch {
        /* best effort */
      }
    };
    window.addEventListener("resize", onResize);
    // Fit again shortly after mount once the flex layout has settled.
    const t = window.setTimeout(onResize, 60);

    return () => {
      disposed = true;
      window.clearTimeout(t);
      window.removeEventListener("resize", onResize);
      es?.close();
      if (sid) api.terminalClose(sid).catch(() => {});
      term.dispose();
    };
  }, [instanceId]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1.5 pb-2 text-xs text-muted">
        <span
          className={cn(
            "size-1.5 rounded-full",
            status === "open"
              ? "bg-success animate-pulse-soft"
              : status === "connecting"
                ? "bg-warning"
                : "bg-danger",
          )}
        />
        {status === "open"
          ? "Connected"
          : status === "connecting"
            ? "Connecting…"
            : status === "error"
              ? "Failed"
              : "Session closed"}
        <span className="text-muted/60">· shell</span>
      </div>
      {error ? (
        <div className="rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
          {error}
        </div>
      ) : (
        <div
          ref={hostRef}
          className="min-h-0 flex-1 overflow-hidden rounded-md border border-border bg-[#0b0f19] p-2"
        />
      )}
    </div>
  );
}
