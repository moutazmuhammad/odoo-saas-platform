import { Outlet, useParams, Link } from "react-router-dom";
import { GitBranch, Globe, ExternalLink, ArrowLeft } from "lucide-react";
import { useInstances } from "@/context/InstancesContext";
import { StatusBadge } from "@/components/StatusBadge";
import { CopyButton } from "@/components/CopyButton";
import { Spinner } from "@/components/Spinner";

/** Shared layout for a single instance: a PERSISTENT header (identity + status +
 *  the live URL) that stays put while the section content below swaps in place —
 *  so switching Overview / Metrics / Databases / Logs / … feels like tabs on one
 *  page, not a jump between separate pages. The left rail is the section nav;
 *  the header never carries tabs (no duplication). The instance URL in the header
 *  is a link that opens the live app; per-environment "Open app" buttons live in
 *  the Environments workspace, so the header carries no separate button. */
export default function InstanceLayout() {
  const { id = "" } = useParams();
  const instanceId = Number(id);
  const { getInstance, loading } = useInstances();
  const inst = getInstance(instanceId);

  // Cache still loading and we don't have this one yet → spinner. If it's loaded
  // but absent (guest/token deep-link, or not in the list) we still render the
  // section: it fetches itself and handles access tokens. Never hard-block the
  // Outlet on the cache.
  if (!inst && loading) {
    return (
      <div className="mt-20 flex justify-center">
        <Spinner size="lg" label="Loading…" />
      </div>
    );
  }

  return (
    <div>
      {/* Back to the projects list — a way out of any project, every section. */}
      <Link
        to="/my/instances"
        className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted transition-colors hover:text-foreground"
      >
        <ArrowLeft className="size-4" />
        All projects
      </Link>

      {inst && (
        <div className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="truncate text-2xl font-bold tracking-tight">{inst.name}</h1>
              <StatusBadge status={inst.state} label={inst.state_label} />
              {inst.is_hosting && (
                <span className="inline-flex items-center gap-1.5 rounded-full border border-border px-2.5 py-0.5 text-xs text-muted">
                  {inst.environment_label}
                  <span className="text-border">·</span>
                  <GitBranch className="size-3" />
                  {inst.branch}
                </span>
              )}
            </div>
            <div className="mt-1.5 flex items-center gap-1">
              {inst.url ? (
                <a
                  href={inst.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1.5 font-mono text-sm text-muted transition-colors hover:text-primary"
                >
                  <Globe className="size-3.5" />
                  {inst.domain}
                  <ExternalLink className="size-3" />
                </a>
              ) : (
                <span className="inline-flex items-center gap-1.5 font-mono text-sm text-muted">
                  <Globe className="size-3.5" />
                  {inst.domain}
                </span>
              )}
              {inst.domain && (
                <CopyButton value={inst.url || `https://${inst.domain}`} label="Copy URL" />
              )}
            </div>
          </div>
        </div>
      )}

      <Outlet />
    </div>
  );
}
