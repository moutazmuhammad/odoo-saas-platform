import * as React from "react";
import { usePolling } from "@/hooks/usePolling";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  Play,
  Square,
  RotateCw,
  Cpu,
  MemoryStick,
  HardDrive,
  ExternalLink,
  ServerCrash,
  CreditCard,
  ArrowUpCircle,
  Clock,
} from "lucide-react";
import { GitBranch } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { ActionButton } from "@/components/ActionButton";
import { AlertBanner } from "@/components/AlertBanner";
import { InfoCard } from "@/components/InfoCard";
import { HelpHint } from "@/components/HelpHint";
import { PerformanceHistory } from "@/components/PerformanceHistory";
import { EmptyState } from "@/components/EmptyState";
import { BillingPanel } from "@/components/BillingPanel";
import { Spinner } from "@/components/Spinner";
import { useInstances } from "@/context/InstancesContext";
import { useToast } from "@/context/ToastContext";
import { api, ApiError, type ApiInstance } from "@/lib/api";
import { formatBytes, formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";

// States that resolve on their own — poll fast while in any of them.
const TRANSITIONAL = new Set(["provisioning", "pending_provision", "paid", "pending_payment"]);
// Subset where work is actually happening on our side. "pending_payment"
// is deliberately NOT here: nothing provisions until the customer pays,
// and showing "Provisioning in progress" next to the unpaid-invoice
// banner reads as a contradiction.
const PROVISIONING = new Set(["provisioning", "pending_provision", "paid"]);

function formatMoney(amount: number, currency = "USD") {
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
      maximumFractionDigits: 2,
    }).format(amount);
  } catch {
    return `${amount} ${currency}`;
  }
}

export default function InstanceDetail() {
  const { id = "" } = useParams();
  const instanceId = Number(id);
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const toast = useToast();
  const { runAction, patch } = useInstances();

  const [instance, setInstance] = React.useState<ApiInstance | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [pending, setPending] = React.useState<"start" | "stop" | "restart" | null>(null);
  const [confirmCancel, setConfirmCancel] = React.useState(false);
  const [cancelling, setCancelling] = React.useState(false);
  const [live, setLive] = React.useState<{ cpu: number; ram: number } | null>(null);
  const [cpuHist, setCpuHist] = React.useState<number[]>([]);
  const [ramHist, setRamHist] = React.useState<number[]>([]);

  const load = React.useCallback(async () => {
    try {
      const data = await api.instance(instanceId, params.get("access_token") || undefined);
      setInstance(data);
      patch(data);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Instance not found.");
    }
  }, [instanceId, params, patch]);

  React.useEffect(() => {
    load();
  }, [load]);

  // Notify on payment return.
  React.useEffect(() => {
    if (params.get("payment") === "success") {
      toast.success("Payment received", "Your instance is being provisioned.");
    }
  }, [params, toast]);

  // Live status polling (state + usage) — resilient (backoff / pause-hidden /
  // stop-on-auth) via usePolling.
  const _fast = instance ? TRANSITIONAL.has(instance.state) : false;
  const _pollStatus = !!instance && (_fast || instance.state === "running");
  usePolling(
    async () => {
      const s = await api.instanceStatus(instanceId);
      setInstance((prev) =>
        prev ? { ...prev, state: s.state, state_label: s.state_label, url: s.url || prev.url, usage: s.usage || prev.usage } : prev
      );
    },
    { interval: _fast ? 4000 : 10000, enabled: _pollStatus }
  );

  // Near-real-time CPU/RAM by polling the cheap cached endpoint (which
  // also marks the instance "watched" so the backend sampler measures
  // it). No SSH per viewer — measurement is decoupled from viewing.
  const running = instance?.state === "running";
  React.useEffect(() => {
    if (!running) setLive(null);
  }, [running]);
  const _liveToken = params.get("access_token") || undefined;
  usePolling(
    async () => {
      const m = await api.instanceMetrics(instanceId, _liveToken);
      setLive({ cpu: m.cpu, ram: m.ram });
      setCpuHist((h) => [...h.slice(-39), m.cpu]);
      setRamHist((h) => [...h.slice(-39), m.ram]);
    },
    { interval: 3000, enabled: running, immediate: true }
  );

  if (error) {
    return (
      <EmptyState
        className="mt-10"
        icon={ServerCrash}
        title="Instance not found"
        description="This instance may have been removed or you don't have access."
        action={<Button onClick={() => navigate("/my/instances")}>Back to instances</Button>}
      />
    );
  }

  if (!instance) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center">
        <Spinner size="lg" label="Loading instance…" />
      </div>
    );
  }

  const isRunning = instance.state === "running";
  const isBusy = PROVISIONING.has(instance.state);
  const awaitingPayment = instance.state === "pending_payment";
  const isSuspended = instance.state === "suspended";

  const run = async (action: "start" | "stop" | "restart", label: string) => {
    setPending(action);
    try {
      await runAction(instanceId, action);
      await load();
      toast.success(label, instance.name);
    } catch (e) {
      toast.error("Action failed", e instanceof ApiError ? e.message : "Please try again.");
    } finally {
      setPending(null);
    }
  };

  const cancelInvoice = async () => {
    setCancelling(true);
    try {
      const { result } = await api.invoiceCancel(instanceId);
      setConfirmCancel(false);
      toast.success("Invoice cancelled", "The pending charge was removed.");
      if (result === "instance_cancelled") {
        navigate("/my/instances");
      } else {
        await load();
      }
    } catch (e) {
      toast.error("Couldn't cancel", e instanceof ApiError ? e.message : "Please try again.");
    } finally {
      setCancelling(false);
    }
  };

  return (
    <div className="animate-fade-in">
      {/* Identity (name/status/url) lives in the shared InstanceLayout header.
          Here we keep only the lifecycle actions, right-aligned. */}
      <div className="mb-6 flex flex-wrap justify-end gap-2">
          {isRunning ? (
            <>
              <ActionButton
                variant="secondary"
                icon={RotateCw}
                loading={pending === "restart"}
                loadingText="Restarting…"
                disabled={!!pending}
                onClick={() => run("restart", "Instance restarted")}
              >
                Restart
              </ActionButton>
              <ActionButton
                variant="danger"
                icon={Square}
                loading={pending === "stop"}
                loadingText="Stopping…"
                disabled={!!pending}
                onClick={() => run("stop", "Instance stopped")}
              >
                Stop
              </ActionButton>
            </>
          ) : (
            <ActionButton
              icon={Play}
              loading={pending === "start" || isBusy}
              loadingText={isBusy ? "Provisioning…" : "Starting…"}
              disabled={!!pending || isBusy || isSuspended || awaitingPayment}
              onClick={() => run("start", "Instance started")}
            >
              Start
            </ActionButton>
          )}
      </div>

      {isBusy && (
        <AlertBanner
          className="mt-6"
          variant="info"
          title="Provisioning in progress"
          description="Your instance is being prepared. Resources come online automatically — this page updates itself."
        />
      )}
      {isSuspended && (
        <AlertBanner
          className="mt-6"
          variant="warning"
          title="This instance is suspended"
          description="Suspended instances can't be started until billing is resolved."
          action={
            <Button size="sm" variant="secondary" onClick={() => navigate("/my/billing")}>
              View invoices
            </Button>
          }
        />
      )}
      {instance.last_error && (instance.state === "stopped" || instance.state === "failed") && (
        <AlertBanner
          className="mt-6"
          variant="danger"
          title="Your instance was stopped"
          description={instance.last_error}
          action={
            instance.is_hosting ? (
              <Button size="sm" variant="secondary" onClick={() => navigate(`/my/instances/${id}/code`)}>
                <GitBranch className="size-4" />
                Review code &amp; packages
              </Button>
            ) : undefined
          }
        />
      )}
      {!instance.has_unpaid_invoice && instance.is_cancelled && (
        <AlertBanner
          className="mt-6"
          variant="warning"
          title={
            instance.has_retained_snapshot
              ? "This instance is cancelled — your last snapshot is kept"
              : "This instance is cancelled"
          }
          description={
            instance.has_retained_snapshot
              ? `We've kept your most recent full snapshot${
                  instance.retained_snapshot_date
                    ? ` (${formatDate(instance.retained_snapshot_date)})` : ""
                }. Reactivate the instance to restore it. Because the instance was deleted, a one-time data-restoration fee${
                  instance.restoration_fee
                    ? ` of ${formatMoney(instance.restoration_fee, instance.currency)}`
                    : ""
                } applies on top of the new plan.`
              : "Reactivate to provision a fresh instance. No snapshot was retained, so it will start empty."
          }
          action={
            <Button
              size="sm"
              onClick={() => (window.location.href = instance.reactivate_url || `/my/instances/${id}/reactivate`)}
            >
              <RotateCw className="size-4" />
              Reactivate
            </Button>
          }
        />
      )}
      {instance.has_unpaid_invoice && (
        <AlertBanner
          className="mt-6"
          variant="warning"
          title="You have an unpaid invoice for this instance"
          description={
            instance.cancellable_invoice_id
              ? "Complete the payment to activate this change — or decline it to remove the invoice."
              : "Complete payment to keep your instance active."
          }
          action={
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={() => (window.location.href = instance.checkout_url || "/my/billing")}>
                <CreditCard className="size-4" />
                Pay now
              </Button>
              {instance.cancellable_invoice_id && (
                <Button size="sm" variant="secondary" onClick={() => setConfirmCancel(true)}>
                  Decline
                </Button>
              )}
            </div>
          }
        />
      )}

      <div className="mt-6 grid gap-4 lg:grid-cols-3">
        <UsageCard icon={Cpu} label="CPU" helpAnchor="cpu-usage" value={live?.cpu ?? instance.usage.cpu} active={isRunning} live={isRunning && !!live} history={cpuHist} />
        <UsageCard icon={MemoryStick} label="Memory" helpAnchor="ram-usage" value={live?.ram ?? instance.usage.ram} active={isRunning} live={isRunning && !!live} history={ramHist} />
        <UsageCard icon={HardDrive} label="Storage" helpAnchor="storage-usage" value={instance.usage.storage} active />
      </div>

      <div className="mt-4">
        <PerformanceHistory instanceId={instanceId} accessToken={params.get("access_token") || undefined} />
      </div>

      <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <InfoCard label="Region" value={<span className="text-base">{(instance.region || "—").split(" · ").pop()}</span>} />
        <InfoCard label="Version" value={<span className="text-base">{instance.version || "—"}</span>} />
        <InfoCard label="Plan" value={<span className="text-base">{instance.plan_name || `${instance.workers}W`}</span>} hint={`${instance.workers} workers`} />
        <InfoCard label="Storage" value={formatBytes(instance.storage_gb)} hint={instance.created ? `Since ${formatDate(instance.created)}` : undefined} />
      </div>

      <Card className="mt-4 flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-sm text-muted">Current plan</p>
          <p className="mt-0.5 text-base font-semibold">{instance.plan_name || `${instance.workers} workers`}</p>
          <p className="mt-1 text-xs text-muted">
            {instance.workers} workers · {formatBytes(instance.storage_gb)} · billed {instance.billing_cycle}
          </p>
          {instance.pending_plan && (
            <p className="mt-2 inline-flex items-center gap-1.5 text-xs text-warning">
              <Clock className="size-3.5" /> Change to “{instance.pending_plan}” pending payment
            </p>
          )}
          {instance.scheduled_plan && (
            <p className="mt-2 inline-flex items-center gap-1.5 text-xs text-info">
              <Clock className="size-3.5" /> Downgrade to “{instance.scheduled_plan}” scheduled for next billing cycle
            </p>
          )}
        </div>
        <Button
          variant="secondary"
          className="shrink-0"
          onClick={() => {
            window.location.href = `/my/instances/${instance.id}/${instance.is_trial ? "upgrade" : "change-plan"}`;
          }}
        >
          <ArrowUpCircle className="size-4" />
          {instance.is_trial ? "Upgrade plan" : "Change plan"}
        </Button>
      </Card>

      {!instance.is_trial && <BillingPanel instance={instance} onChange={load} />}

      <Dialog
        open={confirmCancel}
        onClose={() => !cancelling && setConfirmCancel(false)}
        title="Decline this invoice?"
        description={
          instance.state === "draft" || instance.state === "pending_payment"
            ? "This will cancel the pending charge and the new instance order — the subdomain will be released. This can't be undone."
            : "This will cancel the pending charge and undo the change it was for. Your instance keeps running on its current plan."
        }
      >
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setConfirmCancel(false)} disabled={cancelling}>
            Keep it
          </Button>
          <Button variant="danger" onClick={cancelInvoice} disabled={cancelling}>
            {cancelling ? "Cancelling…" : "Yes, decline"}
          </Button>
        </div>
      </Dialog>
    </div>
  );
}

function UsageCard({
  icon: Icon,
  label,
  value,
  active,
  live,
  history,
  helpAnchor,
}: {
  icon: typeof Cpu;
  label: string;
  value: number;
  active: boolean;
  live?: boolean;
  history?: number[];
  helpAnchor?: string;
}) {
  const tone = value >= 85 ? "bg-danger" : value >= 65 ? "bg-warning" : "bg-primary-glow";
  const lineTone = value >= 85 ? "text-danger" : value >= 65 ? "text-warning" : "text-primary";
  return (
    <Card className="p-5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm text-muted">
          <Icon className="size-4" />
          {label}
          {helpAnchor && <HelpHint anchor={helpAnchor} />}
          {live && (
            <span className="ml-1 inline-flex items-center gap-1 text-[10px] uppercase tracking-wide text-success">
              <span className="size-1.5 rounded-full bg-success animate-pulse-soft" />
              live
            </span>
          )}
        </div>
        <span className="text-sm font-semibold tabular-nums">{active ? `${value}%` : "—"}</span>
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-background">
        <div
          className={cn("h-full rounded-full transition-all duration-700 ease-out", active ? tone : "bg-border")}
          style={{ width: active ? `${Math.min(value, 100)}%` : "0%" }}
        />
      </div>
      {active && history && history.length > 1 && (
        <Sparkline data={history} className={cn("mt-3", lineTone)} />
      )}
    </Card>
  );
}

function Sparkline({ data, className }: { data: number[]; className?: string }) {
  const w = 100;
  const h = 24;
  const max = Math.max(100, ...data);
  const pts = data
    .map((v, i) => {
      const x = data.length > 1 ? (i / (data.length - 1)) * w : 0;
      const y = h - (Math.min(v, max) / max) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className={cn("h-7 w-full", className)}>
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

