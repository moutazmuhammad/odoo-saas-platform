import * as React from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Play, Pause, Trash2, ArrowDownToLine, ArrowUpRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AlertBanner } from "@/components/AlertBanner";
import { StatusBadge } from "@/components/StatusBadge";
import { api, logStreamUrl, type ApiInstance } from "@/lib/api";
import { cn, makeId } from "@/lib/utils";

interface Line {
  id: string;
  text: string;
}

// Log-level colouring (Google Cloud Logging-style severities) using the
// website's theme tokens, so the pane matches the rest of the site and adapts
// to light/dark: ERROR = red, WARNING = amber, DEBUG = muted, INFO = normal.
function levelClass(text: string): string {
  if (/\bERROR\b|CRITICAL|Traceback/.test(text)) return "text-danger";
  if (/\bWARNING\b/.test(text)) return "text-warning";
  if (/\bDEBUG\b/.test(text)) return "text-muted";
  return "text-foreground";
}

export default function Logs({ embedId }: { embedId?: number } = {}) {
  const routeParams = useParams();
  const id = embedId != null ? String(embedId) : (routeParams.id ?? "");
  const instanceId = Number(id);
  const embedded = embedId != null;
  const navigate = useNavigate();

  const [instance, setInstance] = React.useState<ApiInstance | null>(null);
  const [lines, setLines] = React.useState<Line[]>([]);
  const [paused, setPaused] = React.useState(false);
  const [autoScroll, setAutoScroll] = React.useState(true);
  const [connError, setConnError] = React.useState<string | null>(null);
  const viewport = React.useRef<HTMLDivElement>(null);
  const sourceRef = React.useRef<EventSource | null>(null);

  React.useEffect(() => {
    api.instance(instanceId).then(setInstance).catch(() => {});
  }, [instanceId]);

  // Live logs are a hosting-only (self-managed) feature. Managed services
  // don't expose logs — bounce back to the instance overview.
  React.useEffect(() => {
    if (instance && !instance.is_hosting) {
      navigate(`/my/instances/${id}`, { replace: true });
    }
  }, [instance, id, navigate]);

  // Logs only stream while the instance is running. Stopped / suspended /
  // not-yet-running instances have no live container — the server rejects
  // the stream, so don't even open it; show a clear message instead.
  const logsAvailable = instance ? instance.state === "running" : true;

  // Open / close the SSE stream based on pause state.
  React.useEffect(() => {
    if (paused || (instance && instance.state !== "running")) {
      sourceRef.current?.close();
      sourceRef.current = null;
      return;
    }
    setConnError(null);
    const es = new EventSource(logStreamUrl(instanceId, 200));
    sourceRef.current = es;

    es.onmessage = (e) => {
      let text = e.data;
      try {
        text = JSON.parse(e.data);
      } catch {
        /* already plain text */
      }
      if (typeof text !== "string") text = String(text);
      setLines((prev) => [...prev.slice(-600), { id: makeId("log"), text }]);
    };
    es.addEventListener("done", () => {
      es.close();
    });
    es.onerror = () => {
      setConnError(
        "The log stream was interrupted. It will resume when the instance is running — or press Resume to retry."
      );
      es.close();
      sourceRef.current = null;
    };

    return () => {
      es.close();
      sourceRef.current = null;
    };
  }, [paused, instanceId, instance]);

  // Auto-scroll to bottom on new lines.
  React.useEffect(() => {
    if (autoScroll && viewport.current) {
      viewport.current.scrollTop = viewport.current.scrollHeight;
    }
  }, [lines, autoScroll]);

  return (
    <div className={cn("animate-fade-in", embedded && "flex h-full min-h-0 flex-col")}>

      <div className="flex shrink-0 flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Logs</h1>
          <p className="mt-1 text-sm text-muted">
            Live log stream{instance ? ` · ${instance.name}` : ""}
          </p>
        </div>
        {logsAvailable && (
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" size="sm" onClick={() => setPaused((p) => !p)}>
              {paused ? <Play className="size-4" /> : <Pause className="size-4" />}
              {paused ? "Resume" : "Pause"}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              title="Open the logs in a new browser tab"
              onClick={() =>
                window.open(`/my/instances/${id}/logs`, "_blank", "noopener,noreferrer")
              }
            >
              <ArrowUpRight className="size-4" />
              New tab
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setAutoScroll((a) => !a)}
              className={cn(autoScroll && "border-primary/40 text-primary")}
            >
              <ArrowDownToLine className="size-4" />
              Auto-scroll
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setLines([])}>
              <Trash2 className="size-4" />
              Clear
            </Button>
          </div>
        )}
      </div>

      {!logsAvailable ? (
        <AlertBanner
          className="mt-6"
          variant="warning"
          title="Logs unavailable"
          description={
            instance?.state === "suspended"
              ? "This instance is suspended. Settle the outstanding invoice to view its logs."
              : instance?.state === "stopped"
                ? "This instance is stopped. Start it to view its live logs."
                : "Live logs become available once the instance is running."
          }
        />
      ) : (
      <div className={cn(embedded && "flex min-h-0 flex-1 flex-col")}>
      {connError && (
        <AlertBanner className="mt-6 shrink-0" variant="warning" title="Log stream interrupted" description={connError} onDismiss={() => setConnError(null)} />
      )}

      <div className={cn("mt-6", embedded && "flex min-h-0 flex-1 flex-col")}>
        <div className={cn("rounded-lg border border-border p-4", embedded && "flex min-h-0 flex-1 flex-col")}>
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
            <StatusBadge status={instance?.state || "running"} />
          </div>
          <div className="mt-2 flex shrink-0 items-center gap-1.5 font-mono text-xs text-muted">
            <span className={cn("size-1.5 rounded-full", paused ? "bg-muted" : "bg-success animate-pulse-soft")} />
            {paused ? "Paused" : "Streaming"} · {lines.length} lines · odoo.log
          </div>

          <div
            ref={viewport}
            onScroll={(e) => {
              const el = e.currentTarget;
              const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
              if (!atBottom && autoScroll) setAutoScroll(false);
            }}
            className={cn(
              "mt-3 overflow-auto rounded-md border border-border bg-background/60 p-3 font-mono text-[11px] leading-relaxed",
              embedded ? "min-h-0 flex-1" : "max-h-[calc(100vh-20rem)] min-h-[420px]",
            )}
          >
            {lines.length === 0 ? (
              <p className="text-muted">
                {paused ? "Stream paused." : "Waiting for log output… (logs stream only while the instance is running)"}
              </p>
            ) : (
              lines.map((l) => (
                <div key={l.id} className={cn("whitespace-pre-wrap break-all py-0.5", levelClass(l.text))}>
                  {l.text}
                </div>
              ))
            )}
          </div>
        </div>
      </div>
      </div>
      )}
    </div>
  );
}
