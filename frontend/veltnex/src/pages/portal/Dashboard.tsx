import { usePolling } from "@/hooks/usePolling";
import { getLocale } from "@/i18n";
import { i18nText } from "@/i18n";
import * as React from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  Plus,
  ArrowRight,
  Wallet,
  CheckCircle2,
  AlertTriangle,
  Server,
  Ban,
  Activity,
  GitBranch,
  Archive,
  CalendarClock,
  ExternalLink,
} from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Skeleton, SkeletonCard } from "@/components/ui/skeleton";
import { ProjectStatusBadge } from "@/components/ProjectStatusBadge";
import { EmptyState } from "@/components/EmptyState";
import { AlertBanner } from "@/components/AlertBanner";
import { CustomerFilter } from "@/components/CustomerFilter";
import { useAuth } from "@/context/AuthContext";
import { api, ApiError, type DashboardData, type DashboardEnvStatus, type DashboardSummary } from "@/lib/api";
import { cn } from "@/lib/utils";
import { projectStatus } from "@/lib/projects";

const CREATE_TO = "/hosting";

function money(n: number, c = "USD") {
  return new Intl.NumberFormat(getLocale(), { style: "currency", currency: c }).format(n);
}

function formatDate(iso: string) {
  return new Intl.DateTimeFormat(getLocale(), { dateStyle: "medium" }).format(new Date(iso));
}

// "3 hours ago" style; falls back to a date for anything older than a week.
function timeAgo(iso: string | null) {
  if (!iso) return "—";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  const rtf = new Intl.RelativeTimeFormat(getLocale(), { numeric: "auto" });
  if (seconds < 3600) return rtf.format(-Math.max(1, Math.round(seconds / 60)), "minute");
  if (seconds < 86400) return rtf.format(-Math.round(seconds / 3600), "hour");
  if (seconds < 7 * 86400) return rtf.format(-Math.round(seconds / 86400), "day");
  return formatDate(iso);
}

interface ActionItem {
  id: string;
  text: string;
  cta: string;
  to: string;
  // When set, the item is an awaiting-payment order: show a secondary
  // "Don't complete" button that abandons the order (this instance id).
  cancelId?: number;
  cancelName?: string;
}

const projectLink = (id: number) => `/my/instances/${id}/environments`;

const ENV_STATUS: { key: DashboardEnvStatus; label: string; dot: string }[] = [
  { key: "online", label: i18nText("Online"), dot: "bg-success" },
  { key: "starting", label: i18nText("Starting"), dot: "bg-primary" },
  { key: "stopped", label: i18nText("Stopped"), dot: "bg-muted" },
  { key: "attention", label: i18nText("Needs attention"), dot: "bg-danger" },
  { key: "unknown", label: i18nText("Not checked recently"), dot: "bg-border" },
];

const ENV_LABEL: Record<string, string> = {
  production: i18nText("Production"),
  staging: i18nText("Staging"),
  development: i18nText("Development"),
};

function SectionCard({
  icon: Icon,
  title,
  action,
  children,
}: {
  icon: typeof Server;
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <Card className="flex flex-col">
      <div className="flex items-center justify-between gap-3 border-b border-border p-4 px-5">
        <h2 className="flex items-center gap-2 font-semibold">
          <Icon className="size-4 text-muted" />
          {title}
        </h2>
        {action}
      </div>
      <div className="flex-1 p-4 px-5">{children}</div>
    </Card>
  );
}

export function EnvironmentsCard({ summary }: { summary: DashboardSummary }) {
  const envs = summary.environments;
  const counts = Object.fromEntries(ENV_STATUS.map((s) => [s.key, envs.filter((e) => e.status === s.key).length]));
  const attention = envs.filter((e) => e.status === "attention").slice(0, 4);
  return (
    <SectionCard icon={Activity} title={i18nText("Environments")}>
      {envs.length === 0 ? (
        <p className="text-sm text-muted">{i18nText("No environments yet.")}</p>
      ) : (
        <>
          <ul className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm" aria-label={i18nText("Environment status")}>
            {ENV_STATUS.filter((s) => counts[s.key] > 0 || s.key === "online").map((s) => (
              <li key={s.key} className="flex items-center gap-2">
                <span className={cn("size-2 rounded-full", s.dot)} />
                <span className="text-muted">{s.label}</span>
                <span className="ms-auto font-semibold tabular-nums">{counts[s.key]}</span>
              </li>
            ))}
          </ul>
          {attention.length > 0 && (
            <ul className="mt-4 space-y-2 border-t border-border pt-3">
              {attention.map((e) => (
                <li key={e.id} className="flex items-start justify-between gap-3 text-sm">
                  <span className="min-w-0">
                    <span className="font-medium">{e.project_name}</span>
                    <span className="text-muted"> · {ENV_LABEL[e.environment] ?? e.environment}</span>
                    {e.message && <span className="block truncate text-xs text-muted">{e.message}</span>}
                  </span>
                  <Link to={projectLink(e.project_id)} className="shrink-0 text-xs text-primary hover:underline">
                    {i18nText("View")}
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </SectionCard>
  );
}

const BUILD_STYLE = {
  success: { label: i18nText("Deployed"), cls: "text-success" },
  failed: { label: i18nText("Failed"), cls: "text-danger" },
  running: { label: i18nText("Building"), cls: "text-primary" },
} as const;

export function BuildsCard({ summary }: { summary: DashboardSummary }) {
  const builds = summary.builds.slice(0, 5);
  return (
    <SectionCard icon={GitBranch} title={i18nText("Recent builds")}>
      {builds.length === 0 ? (
        <p className="text-sm text-muted">{i18nText("No builds yet. Connect a Git repository to deploy on every push.")}</p>
      ) : (
        <ul className="space-y-2.5">
          {builds.map((b) => {
            const style = BUILD_STYLE[b.state] ?? BUILD_STYLE.running;
            return (
              <li key={b.id} className="flex items-start justify-between gap-3 text-sm">
                <span className="min-w-0">
                  <Link to={`${projectLink(b.project_id)}?tab=code`} className="font-medium hover:underline">
                    {b.project_name}
                  </Link>
                  <span className="text-muted"> · {ENV_LABEL[b.environment] ?? b.environment}</span>
                  <span className="block truncate text-xs text-muted">
                    {[b.branch, b.commit].filter(Boolean).join(" @ ") || "—"} · {timeAgo(b.started_at)}
                  </span>
                </span>
                <span className={cn("shrink-0 text-xs font-medium", style.cls)}>{style.label}</span>
              </li>
            );
          })}
        </ul>
      )}
    </SectionCard>
  );
}

export function BackupsCard({ summary }: { summary: DashboardSummary }) {
  const rows = summary.backups;
  const problems = rows.filter((r) => r.status !== "ok");
  return (
    <SectionCard icon={Archive} title={i18nText("Production backups")}>
      {rows.length === 0 ? (
        <p className="text-sm text-muted">{i18nText("No Production environments yet.")}</p>
      ) : problems.length === 0 ? (
        <p className="flex items-center gap-2 text-sm">
          <CheckCircle2 className="size-4 text-success" />
          {i18nText("All Production environments were backed up in the last day.")}
        </p>
      ) : (
        <ul className="space-y-2.5">
          {problems.slice(0, 5).map((r) => (
            <li key={r.env_id} className="flex items-start justify-between gap-3 text-sm">
              <span className="min-w-0">
                <span className="font-medium">{r.project_name}</span>
                <span className={cn("block text-xs", r.status === "none" ? "text-warning" : "text-danger")}>
                  {r.status === "none"
                    ? i18nText("No automatic backups")
                    : i18nText("Last backup {0}", [timeAgo(r.last_backup_at)])}
                </span>
              </span>
              <Link to={`/my/instances/${r.project_id}/backups`} className="shrink-0 text-xs text-primary hover:underline">
                {r.status === "none" ? i18nText("Enable") : i18nText("View")}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </SectionCard>
  );
}

// Keep only the rows that belong to the projects in view (staff customer filter).
function scopeSummary(summary: DashboardSummary | undefined, projectIds: Set<number>): DashboardSummary {
  const keep = <T extends { project_id: number }>(rows: T[] | undefined) => (rows ?? []).filter((r) => projectIds.has(r.project_id));
  return {
    environments: keep(summary?.environments),
    builds: keep(summary?.builds),
    backups: keep(summary?.backups),
    renewals: keep(summary?.renewals),
  };
}

export default function Dashboard() {
  const { user } = useAuth();
  const isStaff = !!(user?.is_staff || user?.is_internal);
  const [searchParams, setSearchParams] = useSearchParams();
  const customerId = searchParams.get("customer") || "";
  const setCustomerId = (id: string) => {
    const next = new URLSearchParams(searchParams);
    if (id) next.set("customer", id);
    else next.delete("customer");
    setSearchParams(next, { replace: true });
  };
  const navigate = useNavigate();
  const [data, setData] = React.useState<DashboardData | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  // Awaiting-payment order pending abandon-confirmation, + in-flight flag.
  const [cancelTarget, setCancelTarget] = React.useState<{ id: number; name: string } | null>(null);
  const [cancelling, setCancelling] = React.useState(false);
  const [cancelError, setCancelError] = React.useState<string | null>(null);

  const load = React.useCallback(() => {
    api
      .dashboard()
      .then(setData)
      .catch((e) => setError(e instanceof ApiError ? e.message : i18nText("Could not load your dashboard.")));
  }, []);

  React.useEffect(() => {
    load();
  }, [load]);

  usePolling(load, { interval: 30000 });

  const confirmCancel = async () => {
    if (!cancelTarget) return;
    setCancelling(true);
    setCancelError(null);
    try {
      await api.invoiceCancel(cancelTarget.id);
      setCancelTarget(null);
      setData(null);
      load();
    } catch (e) {
      setCancelError(e instanceof ApiError ? e.message : i18nText("Couldn't cancel the order. Please try again."));
    } finally {
      setCancelling(false);
    }
  };

  const currency = data?.currency || data?.wallet?.currency || "USD";
  const outstanding = data?.stats.outstanding ?? 0;
  const openInvoices = data?.stats.open_invoices ?? 0;
  const customerProjects = React.useMemo(
    () => (data?.instances ?? []).filter((i) => !isStaff || !customerId || String(i.customer?.id) === customerId),
    [data, isStaff, customerId],
  );
  const summary = React.useMemo(
    () => scopeSummary(data?.summary, new Set(customerProjects.map((i) => i.id))),
    [data, customerProjects],
  );
  const total = customerProjects.length;
  const active = customerProjects.filter((i) => projectStatus(i) === "active").length;
  const wallet = data?.wallet?.total ?? data?.stats.wallet_balance ?? 0;
  const nextRenewal = summary.renewals[0];
  // The projects grid excludes awaiting-payment orders (they're surfaced in
  // "Needs your attention" instead) so the customer's project list stays clean.
  const visibleProjects = customerProjects.filter((i) => i.state !== "pending_payment");
  const projectsTo = isStaff && customerId ? `/my/instances?customer=${encodeURIComponent(customerId)}` : "/my/instances";

  const actions: ActionItem[] = React.useMemo(() => {
    if (!data) return [];
    const items: ActionItem[] = [];
    for (const inv of data.recent_invoices) {
      if (inv.status === "open" || inv.status === "overdue") {
        items.push({
          id: `inv-${inv.id}`,
          text: i18nText("Invoice {0} {1}", [inv.number, inv.status]),
          cta: i18nText("Pay"),
          to: `/my/billing/${inv.id}`,
        });
      }
    }
    for (const i of customerProjects) {
      if (i.state === "suspended")
        items.push({ id: `s-${i.id}`, text: i18nText("{0} is suspended", [i.name]), cta: i18nText("Resolve"), to: projectLink(i.id) });
      else if (i.state === "failed")
        items.push({ id: `f-${i.id}`, text: i18nText("{0} failed to deploy", [i.name]), cta: i18nText("View"), to: projectLink(i.id) });
      else if (i.state === "pending_payment")
        items.push({ id: `p-${i.id}`, text: i18nText("{0} is awaiting payment", [i.name]), cta: i18nText("Checkout"), to: `/my/instances/${i.id}/checkout`, cancelId: i.id, cancelName: i.name });
    }
    for (const b of summary.builds) {
      if (b.state === "failed" && !items.some((x) => x.id === `b-${b.project_id}`))
        items.push({ id: `b-${b.project_id}`, text: i18nText("A build of {0} failed", [b.project_name]), cta: i18nText("View"), to: `${projectLink(b.project_id)}?tab=code` });
    }
    return items.slice(0, 6);
  }, [data, customerProjects, summary]);

  return (
    <div className="animate-fade-in">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{i18nText("Welcome back")}{user ? `, ${user.name.split(" ")[0]}` : ""}
          </h1>
          <p className="mt-1 text-sm text-muted">{i18nText("Everything across your account at a glance.")}</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" onClick={() => window.location.assign("/?site=1")}>
            <ExternalLink className="size-4" />{i18nText("Website")}</Button>
          <Button onClick={() => navigate(CREATE_TO)}>
            <Plus className="size-4" />{i18nText("New project")}</Button>
        </div>
      </div>

      {error && <AlertBanner className="mt-6" variant="danger" title={i18nText("Couldn't load dashboard")} description={error} />}

      {!data && !error ? (
        <div className="mt-8 space-y-6">
          <Skeleton className="h-24 w-full rounded-xl" />
          <div className="grid gap-4 sm:grid-cols-2">
            <SkeletonCard />
            <SkeletonCard />
          </div>
        </div>
      ) : data ? (
        <>
          {isStaff && (
            <div className="mt-6">
              <CustomerFilter projects={data.instances} value={customerId} onChange={setCustomerId} />
            </div>
          )}
          {/* Hero: needs attention (balance owed) */}
          {outstanding > 0 ? (
            <Card className="mt-6 flex flex-col gap-4 border-warning/40 bg-warning/5 p-5 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex items-start gap-3">
                <span className="flex size-11 shrink-0 items-center justify-center rounded-lg bg-warning/15 text-warning">
                  <AlertTriangle className="size-5" />
                </span>
                <div>
                  <p className="text-lg font-semibold">{money(outstanding, currency)}{i18nText(" due")}</p>
                  <p className="text-sm text-muted">
                    {openInvoices}{i18nText(" open invoice")}{openInvoices === 1 ? "" : "s"}{i18nText(" awaiting payment.")}</p>
                </div>
              </div>
              <Button className="shrink-0" onClick={() => navigate("/my/billing")}>{i18nText("Pay now")}</Button>
            </Card>
          ) : (
            <Card className="mt-6 flex items-center gap-3 border-success/30 bg-success/5 p-5">
              <span className="flex size-11 shrink-0 items-center justify-center rounded-lg bg-success/15 text-success">
                <CheckCircle2 className="size-5" />
              </span>
              <div>
                <p className="font-semibold">{i18nText("You're all paid up")}</p>
                <p className="text-sm text-muted">{i18nText("No outstanding invoices.")}</p>
              </div>
            </Card>
          )}

          {/* Key numbers */}
          <div className="mt-4 grid gap-4 sm:grid-cols-3">
            <Card className="p-5">
              <div className="flex items-center justify-between">
                <p className="text-sm font-medium text-muted">{i18nText("Active projects")}</p>
                <Link to={projectsTo} className="text-xs text-primary hover:underline">{i18nText("View projects")}</Link>
              </div>
              <p className="mt-2 text-2xl font-bold tracking-tight">
                {active}
                <span className="text-base font-medium text-muted"> / {total}{i18nText(" active")}</span>
              </p>
              <div className="mt-3 flex gap-1">
                {Array.from({ length: Math.max(total, 1) }).map((_, i) => (
                  <span
                    key={i}
                    className={cn(
                      "h-2 flex-1 rounded-full",
                      total === 0 ? "bg-border" : i < active ? "bg-success" : "bg-border",
                    )}
                  />
                ))}
              </div>
            </Card>

            <Card className="flex items-center justify-between p-5">
              <div>
                <p className="text-sm font-medium text-muted">{i18nText("Wallet balance")}</p>
                <p className="mt-2 text-2xl font-bold tracking-tight">{money(wallet, currency)}</p>
              </div>
              <Button variant="secondary" onClick={() => navigate("/my/billing")}>
                <Wallet className="size-4" />{i18nText("Top up")}</Button>
            </Card>

            <Card className="p-5">
              <p className="flex items-center gap-2 text-sm font-medium text-muted">
                <CalendarClock className="size-4" />{i18nText("Next renewal")}</p>
              {nextRenewal ? (
                <>
                  <p className="mt-2 text-2xl font-bold tracking-tight">{formatDate(nextRenewal.date)}</p>
                  <p className="truncate text-xs text-muted">{nextRenewal.project_name}</p>
                </>
              ) : (
                <p className="mt-2 text-sm text-muted">{i18nText("No upcoming renewals.")}</p>
              )}
            </Card>
          </div>

          {/* Needs attention list */}
          {actions.length > 0 && (
            <Card className="mt-6">
              <div className="border-b border-border p-5">
                <h2 className="font-semibold">{i18nText("Needs your attention")}</h2>
              </div>
              <ul className="divide-y divide-border">
                {actions.map((a) => (
                  <li key={a.id} className="flex items-center justify-between gap-3 p-4 px-5">
                    <span className="flex items-center gap-2.5 text-sm">
                      <span className="size-2 shrink-0 rounded-full bg-warning" />
                      {a.text}
                    </span>
                    <div className="flex shrink-0 items-center gap-2">
                      {a.cancelId !== undefined && (
                        <Button
                          size="sm"
                          variant="ghost"
                          className="text-danger hover:bg-danger/10"
                          onClick={() => setCancelTarget({ id: a.cancelId!, name: a.cancelName || i18nText("this order") })}
                        >
                          <Ban className="size-4" />{i18nText("Don't complete")}</Button>
                      )}
                      <Button size="sm" variant="secondary" onClick={() => navigate(a.to)}>
                        {a.cta}
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          {/* Health, delivery and safety at a glance */}
          <div className="mt-6 grid gap-4 lg:grid-cols-3">
            <EnvironmentsCard summary={summary} />
            <BuildsCard summary={summary} />
            <BackupsCard summary={summary} />
          </div>

          {/* Projects */}
          <div className="mt-8 flex items-center justify-between">
            <h2 className="font-semibold">{isStaff ? i18nText("Customer projects") : i18nText("Your projects")}</h2>
            <Link to={projectsTo} className="inline-flex items-center gap-1 text-sm text-primary hover:underline">{i18nText("View all ")}<ArrowRight className="size-3.5" />
            </Link>
          </div>
          {visibleProjects.length === 0 ? (
            <Card className="mt-3 p-5">
              <EmptyState
                icon={Server}
                title={customerId ? i18nText("No matching projects") : i18nText("No projects yet")}
                description={customerId ? i18nText("This customer has no active projects.") : i18nText("Create your first project to deploy an Odoo environment.")}
                action={<Button onClick={() => navigate(CREATE_TO)}>{i18nText("Create project")}</Button>}
              />
            </Card>
          ) : (
            <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {visibleProjects.slice(0, 6).map((i) => (
                <Link key={i.id} to={projectLink(i.id)}>
                  <Card className="group p-5 transition-all hover:-translate-y-0.5 hover:border-primary/40">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="truncate font-semibold">{i.name}</p>
                        {isStaff && <p className="truncate text-xs text-muted">{i.customer?.name || "—"}</p>}
                        <p className="truncate text-xs text-muted">{i.region || i.domain}</p>
                      </div>
                      <ProjectStatusBadge project={i} />
                    </div>
                  </Card>
                </Link>
              ))}
              <button
                onClick={() => navigate(CREATE_TO)}
                className="flex min-h-28 items-center justify-center rounded-xl border border-dashed border-border text-sm text-muted transition-colors hover:border-primary/40 hover:text-foreground"
              >
                <Plus className="me-2 size-4" />{i18nText("New project")}</button>
            </div>
          )}
        </>
      ) : null}

      {/* Abandon (don't complete) an awaiting-payment order. */}
      <Dialog
        open={!!cancelTarget}
        onClose={() => { if (!cancelling) { setCancelTarget(null); setCancelError(null); } }}
        title={i18nText("Don't complete this order?")}
        description={cancelTarget ? i18nText("{0} will be cancelled and its subdomain released. This can't be undone.", [cancelTarget.name]) : ""}
      >
        {cancelError && <AlertBanner className="mb-4" variant="danger" title={i18nText("Couldn't cancel")} description={cancelError} />}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" disabled={cancelling} onClick={() => { setCancelTarget(null); setCancelError(null); }}>{i18nText("Keep order")}</Button>
          <Button variant="danger" disabled={cancelling} onClick={confirmCancel}>
            {cancelling ? i18nText("Cancelling…") : i18nText("Yes, don't complete")}
          </Button>
        </div>
      </Dialog>
    </div>
  );
}
