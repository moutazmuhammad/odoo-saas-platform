import { i18nText } from "@/i18n";
import * as React from "react";
import { usePermissions } from "@/lib/permissions";
import { usePolling } from "@/hooks/usePolling";
import { useNavigate, useParams } from "react-router-dom";
import { Camera, RotateCcw, Trash2, PlusCircle, Rocket, Receipt } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Dialog } from "@/components/ui/dialog";
import { ActionButton } from "@/components/ActionButton";
import { AlertBanner } from "@/components/AlertBanner";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState } from "@/components/EmptyState";
import { Spinner } from "@/components/Spinner";
import { useToast } from "@/context/ToastContext";
import { api, ApiError, type ApiBackup, type ApiInstance, type SnapshotEstimate } from "@/lib/api";
import { formatDate, formatDateTime } from "@/lib/format";
import { RestoreSnapshotDialog } from "@/pages/portal/Backups";

function money(amount: number, currency = "USD") {
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 2 }).format(amount);
  } catch {
    return `${amount.toFixed(2)} ${currency}`;
  }
}

/** DigitalOcean-style snapshots: taken on demand, kept until deleted,
 *  billed per GB-month in advance. A customer-wide page (every project,
 *  including projects that are gone) and a per-server tab (`embedId`). */
export default function Snapshots({ embedId }: { embedId?: number } = {}) {
  const routeParams = useParams();
  const scopedId = embedId != null ? embedId : routeParams.id ? Number(routeParams.id) : null;
  const can = usePermissions(scopedId ?? 0);
  const navigate = useNavigate();
  const toast = useToast();
  const [rows, setRows] = React.useState<ApiBackup[] | null>(null);
  const [instance, setInstance] = React.useState<ApiInstance | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [takeOpen, setTakeOpen] = React.useState(false);
  const [restoreTarget, setRestoreTarget] = React.useState<ApiBackup | null>(null);
  const [deleteTarget, setDeleteTarget] = React.useState<ApiBackup | null>(null);

  const load = React.useCallback(async () => {
    try {
      const [all, inst] = await Promise.all([
        api.snapshotsAll(),
        scopedId ? api.instance(scopedId).catch(() => null) : Promise.resolve(null),
      ]);
      setRows(scopedId ? all.snapshots.filter((s) => s.instance_id === scopedId) : all.snapshots);
      setInstance(inst);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : i18nText("Could not load snapshots."));
    }
  }, [scopedId]);

  React.useEffect(() => {
    load();
  }, [load]);

  const taking = !!rows?.some((b) => b.status === "in_progress");
  usePolling(load, { interval: 5000, enabled: taking });

  const canTake = !!scopedId && can("backup.create") && instance?.state === "running";

  const handleRestore = async (backup: ApiBackup, confirm: string) => {
    if (!backup.instance_id) return;
    await api.backupRestore(backup.instance_id, backup.id, confirm);
    navigate(`/my/instances/${backup.instance_id}`);
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      await api.snapshotDeleteAny(deleteTarget.id);
      toast.success(i18nText("Snapshot deleted"));
      setDeleteTarget(null);
      await load();
    } catch (e) {
      toast.error(i18nText("Couldn't delete the snapshot"), e instanceof ApiError ? e.message : i18nText("Please try again."));
    }
  };

  const startProject = (b: ApiBackup) => {
    const qs = new URLSearchParams({ from_snapshot: String(b.id), snapshot_name: b.label });
    navigate(`/hosting?${qs.toString()}`);
  };

  const pendingRow = (rows || []).find((b) => b.pending_invoice_id && (b.billing_state === "pending" || b.billing_state === "overdue"));
  const openInvoice = pendingRow && pendingRow.pending_invoice_id ? { id: pendingRow.pending_invoice_id, overdue: pendingRow.billing_state === "overdue" } : null;
  const currency = rows?.find((b) => b.currency)?.currency || "USD";

  return (
    <div className="animate-fade-in">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{i18nText("Snapshots")}</h1>
          <p className="mt-1 text-sm text-muted">
            {scopedId
              ? i18nText("A snapshot captures this server, database and files, whenever you want. Keep it as long as you like, restore it here, or start a new project from it.")
              : i18nText("All your snapshots, across every project, including projects you have since deleted. Each has a fixed monthly price, paid in advance, until you delete it.")}
          </p>
        </div>
        {canTake && (
          <Button onClick={() => setTakeOpen(true)} disabled={taking}>
            <Camera className="size-4" />{i18nText("Take snapshot")}</Button>
        )}
      </div>

      {openInvoice && (
        <AlertBanner
          className="mt-4"
          variant={openInvoice.overdue ? "danger" : "warning"}
          title={openInvoice.overdue ? i18nText("Your snapshot invoice is overdue") : i18nText("Your snapshot invoice is ready")}
          description={openInvoice.overdue
            ? i18nText("Pay it now to keep your snapshots; unpaid snapshots are deleted after the grace period.")
            : i18nText("One invoice covers all your snapshots for the coming month. Once you have paid with a card, it is charged automatically.")}
          action={<Button size="sm" onClick={() => window.location.assign(`/my/pay/${openInvoice.id}`)}><Receipt className="size-4" />{i18nText("Pay invoice")}</Button>}
        />
      )}

      {error ? (
        <AlertBanner className="mt-6" variant="danger" title={i18nText("Snapshots")} description={error} />
      ) : !rows ? (
        <div className="mt-20 flex justify-center"><Spinner size="lg" label={i18nText("Loading snapshots…")} /></div>
      ) : rows.length === 0 ? (
        <EmptyState
          className="mt-8"
          icon={Camera}
          title={i18nText("No snapshots yet")}
          description={scopedId
            ? i18nText("Take one before a big change, or to clone this server into a new project.")
            : i18nText("Take a snapshot from any project's Snapshots tab; it will show up here.")}
          action={canTake ? <Button onClick={() => setTakeOpen(true)}><PlusCircle className="size-4" />{i18nText("Take snapshot")}</Button> : undefined}
        />
      ) : (
        <Card className="mt-6 divide-y divide-border">
          {rows.map((b) => {
            const perms = b.permissions || {};
            return (
              <div key={b.id} className="flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex items-start gap-3">
                  <span className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border bg-card text-muted">
                    <Camera className="size-4" />
                  </span>
                  <div>
                    <p className="font-medium">{b.label}</p>
                    <p className="mt-0.5 text-xs text-muted">
                      {!scopedId && (b.project_name ? `${b.project_name}${b.instance_state === "deleted" ? ` (${i18nText("project deleted")})` : ""} · ` : "")}
                      {formatDateTime(b.created)}{b.odoo_version ? ` · Odoo ${b.odoo_version}` : ""}
                    </p>
                    {b.monthly_price != null && b.monthly_price > 0 && (
                      <p className="mt-0.5 text-xs text-muted">
                        {i18nText("{0} per month", [money(b.monthly_price, b.currency || currency)])}
                        {b.billing_state === "paid" && b.paid_until ? ` · ${i18nText("paid until {0}", [formatDate(b.paid_until)])}` : ""}
                      </p>
                    )}
                    {b.status === "failed" && b.error && <p className="mt-1 text-xs text-danger">{b.error}</p>}
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2 sm:justify-end">
                  {b.billing_state === "pending" || b.billing_state === "overdue" ? (
                    <span className={b.billing_state === "overdue" ? "text-xs font-medium text-danger" : "text-xs text-muted"}>
                      {b.billing_state === "overdue" ? i18nText("Overdue") : i18nText("Awaiting payment")}
                    </span>
                  ) : (
                    <StatusBadge status={b.status} />
                  )}
                  {perms.can_new_project && (
                    <Button size="sm" variant="secondary" disabled={b.status !== "available"} onClick={() => startProject(b)}>
                      <Rocket className="size-4" /><span className="hidden sm:inline">{i18nText("New project")}</span>
                    </Button>
                  )}
                  {perms.can_restore && (
                    <Button size="sm" variant="secondary" disabled={b.status !== "available"} onClick={() => setRestoreTarget(b)}>
                      <RotateCcw className="size-4" /><span className="hidden sm:inline">{i18nText("Restore")}</span>
                    </Button>
                  )}
                  {perms.can_delete && (
                    <Button size="sm" variant="ghost" aria-label={i18nText("Delete snapshot")} disabled={b.status === "in_progress"} onClick={() => setDeleteTarget(b)}>
                      <Trash2 className="size-4" />
                    </Button>
                  )}
                </div>
              </div>
            );
          })}
        </Card>
      )}

      {scopedId && (
        <TakeSnapshotDialog
          open={takeOpen}
          instanceId={scopedId}
          onClose={() => setTakeOpen(false)}
          onTake={async (name) => {
            await api.snapshotCreate(scopedId, name);
            setTakeOpen(false);
            toast.success(i18nText("Snapshot started"), i18nText("It appears in the list as soon as it is ready."));
            await load();
          }}
        />
      )}

      <RestoreSnapshotDialog
        backup={restoreTarget}
        instanceName={restoreTarget?.origin_subdomain || instance?.name || ""}
        onClose={() => setRestoreTarget(null)}
        onRestore={handleRestore}
      />

      <Dialog open={!!deleteTarget} onClose={() => setDeleteTarget(null)} title={i18nText("Delete snapshot")}>
        <p className="text-sm text-muted">{i18nText("Delete snapshot {0}? This cannot be undone. Charges stop; the current prepaid period is not refunded.", [deleteTarget?.label || ""])}</p>
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setDeleteTarget(null)}>{i18nText("Cancel")}</Button>
          <Button variant="danger" onClick={handleDelete}><Trash2 className="size-4" />{i18nText("Delete")}</Button>
        </div>
      </Dialog>
    </div>
  );
}

function TakeSnapshotDialog({ open, instanceId, onClose, onTake }: { open: boolean; instanceId: number; onClose: () => void; onTake: (name: string) => Promise<void> }) {
  const [name, setName] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [estimate, setEstimate] = React.useState<SnapshotEstimate | null>(null);
  React.useEffect(() => {
    if (!open) return;
    setName(""); setLoading(false); setError(null); setEstimate(null);
    api.snapshotEstimate(instanceId).then(setEstimate).catch(() => setEstimate(null));
  }, [open, instanceId]);
  const valid = /^[\w .-]{1,60}$/.test(name.trim());
  const atLimit = !!estimate && estimate.count >= estimate.limit;
  const submit = async () => {
    if (!valid || atLimit) return;
    setLoading(true); setError(null);
    try { await onTake(name.trim()); }
    catch (e) { setError(e instanceof ApiError ? e.message : i18nText("Couldn't start the snapshot.")); setLoading(false); }
  };
  return (
    <Dialog open={open} onClose={onClose} title={i18nText("Take snapshot")} description={i18nText("Your server keeps running. The snapshot includes the database and all files.")}>
      {error && <AlertBanner className="mb-4" variant="danger" title={i18nText("Snapshot")} description={error} />}
      <div className="space-y-2">
        <Label htmlFor="snapshot-name">{i18nText("Snapshot name")}</Label>
        <Input id="snapshot-name" autoFocus placeholder={i18nText("e.g. before-upgrade")} value={name} disabled={loading}
          onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && valid && submit()} />
        {name && !valid && <p className="text-xs text-danger">{i18nText("Use letters, numbers, spaces, dots or dashes (max 60).")}</p>}
      </div>
      {estimate && (
        <div className="mt-4 rounded-lg border border-border bg-card/60 p-3 text-sm">
          {estimate.monthly_price != null ? (
            <>
              <div className="flex justify-between"><span className="text-muted">{i18nText("Monthly price")}</span><span className="font-medium">{money(estimate.monthly_price, estimate.currency || "USD")}</span></div>
              {estimate.due_now != null && (
                <div className="mt-2 flex justify-between border-t border-border pt-2"><span className="font-medium">{i18nText("Due now")}</span><span className="font-semibold">{money(estimate.due_now, estimate.currency || "USD")}</span></div>
              )}
              <p className="mt-1 text-xs text-muted">{i18nText("Paid in advance, then included in your monthly snapshot invoice until you delete it.")}</p>
            </>
          ) : (
            <p className="text-xs text-muted">{i18nText("This snapshot is free.")}</p>
          )}
          {atLimit && <p className="mt-2 text-xs text-danger">{i18nText("You have reached the limit of {0} snapshots for this server. Delete one to take another.", [String(estimate.limit)])}</p>}
        </div>
      )}
      <div className="mt-6 flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose} disabled={loading}>{i18nText("Cancel")}</Button>
        <ActionButton loading={loading} loadingText={i18nText("Starting…")} disabled={!valid || atLimit} onClick={submit}>
          <Camera className="size-4" />{estimate?.due_now ? i18nText("Pay & take snapshot") : i18nText("Take snapshot")}</ActionButton>
      </div>
    </Dialog>
  );
}
