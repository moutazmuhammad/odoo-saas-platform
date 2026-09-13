import * as React from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Plus,
  ArrowRight,
  Wallet,
  CheckCircle2,
  AlertTriangle,
  Server,
  Ban,
} from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Skeleton, SkeletonCard } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState } from "@/components/EmptyState";
import { AlertBanner } from "@/components/AlertBanner";
import { useAuth } from "@/context/AuthContext";
import { useSections } from "@/lib/useSections";
import { api, ApiError, type DashboardData, type ApiInstance } from "@/lib/api";
import { cn } from "@/lib/utils";

function money(n: number, c = "USD") {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: c }).format(n);
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

function projectLink(i: ApiInstance) {
  return i.is_hosting ? `/my/instances/${i.id}/environments` : `/my/instances/${i.id}`;
}

export default function Dashboard() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const sections = useSections();
  const createTo = sections.hosting ? "/hosting" : "/services";
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
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load your dashboard."));
  }, []);

  React.useEffect(() => {
    load();
  }, [load]);

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
      setCancelError(e instanceof ApiError ? e.message : "Couldn't cancel the order. Please try again.");
    } finally {
      setCancelling(false);
    }
  };

  const currency = data?.currency || data?.wallet?.currency || "USD";
  const outstanding = data?.stats.outstanding ?? 0;
  const openInvoices = data?.stats.open_invoices ?? 0;
  const total = data?.stats.instances ?? 0;
  const running = data?.stats.running ?? 0;
  const wallet = data?.wallet?.total ?? data?.stats.wallet_balance ?? 0;
  // The projects grid excludes awaiting-payment orders (they're surfaced in
  // "Needs your attention" instead) so the customer's project list stays clean.
  const visibleProjects = (data?.instances ?? []).filter((i) => i.state !== "pending_payment");

  const actions: ActionItem[] = React.useMemo(() => {
    if (!data) return [];
    const items: ActionItem[] = [];
    for (const inv of data.recent_invoices) {
      if (inv.status === "open" || inv.status === "overdue") {
        items.push({
          id: `inv-${inv.id}`,
          text: `Invoice ${inv.number} ${inv.status}`,
          cta: "Pay",
          to: `/my/billing/${inv.id}`,
        });
      }
    }
    for (const i of data.instances) {
      if (i.state === "suspended")
        items.push({ id: `s-${i.id}`, text: `${i.name} is suspended`, cta: "Resolve", to: projectLink(i) });
      else if (i.state === "failed")
        items.push({ id: `f-${i.id}`, text: `${i.name} failed to deploy`, cta: "View", to: projectLink(i) });
      else if (i.state === "pending_payment")
        items.push({ id: `p-${i.id}`, text: `${i.name} is awaiting payment`, cta: "Checkout", to: `/my/instances/${i.id}/checkout`, cancelId: i.id, cancelName: i.name });
    }
    return items.slice(0, 6);
  }, [data]);

  return (
    <div className="animate-fade-in">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">
            Welcome back{user ? `, ${user.name.split(" ")[0]}` : ""}
          </h1>
          <p className="mt-1 text-sm text-muted">Everything across your account at a glance.</p>
        </div>
        <Button onClick={() => navigate(createTo)}>
          <Plus className="size-4" />
          New project
        </Button>
      </div>

      {error && <AlertBanner className="mt-6" variant="danger" title="Couldn't load dashboard" description={error} />}

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
          {/* Hero: needs attention (balance owed) */}
          {outstanding > 0 ? (
            <Card className="mt-6 flex flex-col gap-4 border-warning/40 bg-warning/5 p-5 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex items-start gap-3">
                <span className="flex size-11 shrink-0 items-center justify-center rounded-lg bg-warning/15 text-warning">
                  <AlertTriangle className="size-5" />
                </span>
                <div>
                  <p className="text-lg font-semibold">{money(outstanding, currency)} due</p>
                  <p className="text-sm text-muted">
                    {openInvoices} open invoice{openInvoices === 1 ? "" : "s"} awaiting payment.
                  </p>
                </div>
              </div>
              <Button className="shrink-0" onClick={() => navigate("/my/billing")}>
                Pay now
              </Button>
            </Card>
          ) : (
            <Card className="mt-6 flex items-center gap-3 border-success/30 bg-success/5 p-5">
              <span className="flex size-11 shrink-0 items-center justify-center rounded-lg bg-success/15 text-success">
                <CheckCircle2 className="size-5" />
              </span>
              <div>
                <p className="font-semibold">You're all paid up</p>
                <p className="text-sm text-muted">No outstanding invoices.</p>
              </div>
            </Card>
          )}

          {/* Fleet + wallet */}
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <Card className="p-5">
              <div className="flex items-center justify-between">
                <p className="text-sm font-medium text-muted">Fleet health</p>
                <Link to="/my/instances" className="text-xs text-primary hover:underline">
                  View projects
                </Link>
              </div>
              <p className="mt-2 text-2xl font-bold tracking-tight">
                {running}
                <span className="text-base font-medium text-muted"> / {total} running</span>
              </p>
              <div className="mt-3 flex gap-1">
                {Array.from({ length: Math.max(total, 1) }).map((_, i) => (
                  <span
                    key={i}
                    className={cn(
                      "h-2 flex-1 rounded-full",
                      total === 0 ? "bg-border" : i < running ? "bg-success" : "bg-border",
                    )}
                  />
                ))}
              </div>
            </Card>

            <Card className="flex items-center justify-between p-5">
              <div>
                <p className="text-sm font-medium text-muted">Wallet balance</p>
                <p className="mt-2 text-2xl font-bold tracking-tight">{money(wallet, currency)}</p>
              </div>
              <Button variant="secondary" onClick={() => navigate("/my/billing")}>
                <Wallet className="size-4" />
                Top up
              </Button>
            </Card>
          </div>

          {/* Needs attention list */}
          {actions.length > 0 && (
            <Card className="mt-6">
              <div className="border-b border-border p-5">
                <h2 className="font-semibold">Needs your attention</h2>
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
                          onClick={() => setCancelTarget({ id: a.cancelId!, name: a.cancelName || "this order" })}
                        >
                          <Ban className="size-4" />
                          Don't complete
                        </Button>
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

          {/* Projects */}
          <div className="mt-8 flex items-center justify-between">
            <h2 className="font-semibold">Your projects</h2>
            <Link to="/my/instances" className="inline-flex items-center gap-1 text-sm text-primary hover:underline">
              View all <ArrowRight className="size-3.5" />
            </Link>
          </div>
          {visibleProjects.length === 0 ? (
            <Card className="mt-3 p-5">
              <EmptyState
                icon={Server}
                title="No projects yet"
                description="Create your first project to deploy an Odoo environment."
                action={<Button onClick={() => navigate(createTo)}>Create project</Button>}
              />
            </Card>
          ) : (
            <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {visibleProjects.slice(0, 6).map((i) => (
                <Link key={i.id} to={projectLink(i)}>
                  <Card className="group p-5 transition-all hover:-translate-y-0.5 hover:border-primary/40">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="truncate font-semibold">{i.name}</p>
                        <p className="truncate text-xs text-muted">{i.region || i.domain}</p>
                      </div>
                      <StatusBadge status={i.state} label={i.state_label} />
                    </div>
                    <p className="mt-4 text-xs text-muted">{i.is_hosting ? "Hosting project" : "Managed service"}</p>
                  </Card>
                </Link>
              ))}
              <button
                onClick={() => navigate(createTo)}
                className="flex min-h-[7rem] items-center justify-center rounded-xl border border-dashed border-border text-sm text-muted transition-colors hover:border-primary/40 hover:text-foreground"
              >
                <Plus className="mr-2 size-4" />
                New project
              </button>
            </div>
          )}
        </>
      ) : null}

      {/* Abandon (don't complete) an awaiting-payment order. */}
      <Dialog
        open={!!cancelTarget}
        onClose={() => { if (!cancelling) { setCancelTarget(null); setCancelError(null); } }}
        title="Don't complete this order?"
        description={cancelTarget ? `${cancelTarget.name} will be cancelled and its subdomain released. This can't be undone.` : ""}
      >
        {cancelError && <AlertBanner className="mb-4" variant="danger" title="Couldn't cancel" description={cancelError} />}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" disabled={cancelling} onClick={() => { setCancelTarget(null); setCancelError(null); }}>
            Keep order
          </Button>
          <Button variant="danger" disabled={cancelling} onClick={confirmCancel}>
            {cancelling ? "Cancelling…" : "Yes, don't complete"}
          </Button>
        </div>
      </Dialog>
    </div>
  );
}
