import * as React from "react";
import { usePolling } from "@/hooks/usePolling";
import {
  CheckCircle2,
  XCircle,
  Loader2,
  GitCommit,
  GitBranch,
  Rocket,
  GitMerge,
  RotateCw,
  ChevronDown,
  ExternalLink,
} from "lucide-react";
import { api, type Build } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatDateTime } from "@/lib/format";

const SOURCE_LABEL: Record<Build["source"], string> = {
  initial: "Initial deployment",
  push: "Git push",
  redeploy: "Manual re-deploy",
  merge: "Branch merge",
};
const SOURCE_ICON: Record<Build["source"], typeof Rocket> = {
  initial: Rocket,
  push: GitCommit,
  redeploy: RotateCw,
  merge: GitMerge,
};

function dur(s: number | null): string {
  if (s == null) return "";
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

/** Odoo.sh-style deployment history: the chronological list of builds for an
 *  instance, each with its status, commit/branch, timing, and — for failures —
 *  an expandable log so the customer sees exactly WHY a deploy failed. */
export function DeploymentHistory({
  instanceId,
  accessToken,
}: {
  instanceId: number;
  accessToken?: string;
}) {
  const [builds, setBuilds] = React.useState<Build[] | null>(null);
  const [open, setOpen] = React.useState<number | null>(null);

  const load = React.useCallback(() => {
    api.instanceBuilds(instanceId, accessToken).then(setBuilds).catch(() => setBuilds([]));
  }, [instanceId, accessToken]);

  React.useEffect(() => {
    load();
  }, [load]);
  // Refresh while a build is running so the status flips live (resilient:
  // backs off on error, pauses on a hidden tab, stops on auth-expiry).
  const hasRunningBuild = !!builds?.some((b) => b.state === "running");
  usePolling(load, { interval: 6000, enabled: hasRunningBuild });

  return (
    <div className="mt-6">
      <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted">
        Deployment history
      </p>
      <div className="overflow-hidden rounded-lg border border-border">
        {builds == null ? (
          <div className="space-y-px">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-14 animate-pulse bg-background" />
            ))}
          </div>
        ) : builds.length === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-muted">
            No deployments yet.
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {/* Keep the timeline focused: only the 10 most recent deployments. */}
            {builds.slice(0, 10).map((b) => {
              const Icon = SOURCE_ICON[b.source] ?? Rocket;
              const failed = b.state === "failed";
              const running = b.state === "running";
              const expandable = failed && !!b.log;
              return (
                <li key={b.id}>
                  <div
                    role={expandable ? "button" : undefined}
                    tabIndex={expandable ? 0 : undefined}
                    onClick={() => expandable && setOpen(open === b.id ? null : b.id)}
                    className={cn(
                      "flex w-full items-center gap-3 px-4 py-3 text-left transition-colors",
                      expandable && "cursor-pointer hover:bg-foreground/[0.03]",
                    )}
                  >
                    {/* status */}
                    <span className="shrink-0">
                      {running ? (
                        <Loader2 className="size-5 animate-spin text-primary" />
                      ) : failed ? (
                        <XCircle className="size-5 text-danger" />
                      ) : (
                        <CheckCircle2 className="size-5 text-success" />
                      )}
                    </span>
                    {/* main */}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <Icon className="size-3.5 shrink-0 text-muted" />
                        <span className="truncate text-sm font-medium">
                          {b.commit_message || SOURCE_LABEL[b.source] || "Deployment"}
                        </span>
                        {failed && (
                          <span className="rounded-full bg-danger/10 px-2 py-0.5 text-[11px] font-medium text-danger">
                            Failed
                          </span>
                        )}
                        {running && (
                          <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">
                            Building…
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted">
                        <span>{SOURCE_LABEL[b.source]}</span>
                        {b.branch && (
                          <span className="inline-flex items-center gap-1">
                            <GitBranch className="size-3" /> {b.branch}
                          </span>
                        )}
                        {b.commit &&
                          (b.commit_url ? (
                            <a
                              href={b.commit_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              onClick={(e) => e.stopPropagation()}
                              title="View commit on GitHub"
                              className="inline-flex items-center gap-1 font-mono text-primary underline-offset-2 hover:underline"
                            >
                              {b.commit}
                              <ExternalLink className="size-3" />
                            </a>
                          ) : (
                            <span className="font-mono">{b.commit}</span>
                          ))}
                        {b.author && <span>by {b.author}</span>}
                        {b.at && <span>{formatDateTime(b.at)}</span>}
                        {b.duration_s != null && <span>· {dur(b.duration_s)}</span>}
                      </div>
                    </div>
                    {expandable && (
                      <ChevronDown
                        className={cn(
                          "size-4 shrink-0 text-muted transition-transform",
                          open === b.id && "rotate-180",
                        )}
                      />
                    )}
                  </div>
                  {/* failure reason */}
                  {expandable && open === b.id && (
                    <div className="border-t border-border bg-background/60 px-4 py-3">
                      <p className="mb-1.5 text-xs font-medium text-danger">Why it failed</p>
                      <pre className="max-h-72 overflow-auto rounded-md border border-border bg-background p-3 font-mono text-[11px] leading-relaxed text-muted">
                        {b.log.trimEnd()}
                      </pre>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
