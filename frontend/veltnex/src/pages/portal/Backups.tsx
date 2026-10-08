import { i18nText } from "@/i18n";
import * as React from "react";
import { usePermissions } from "@/lib/permissions";
import { usePolling } from "@/hooks/usePolling";
import { useNavigate, useParams } from "react-router-dom";
import { Archive, RotateCcw, Clock, ShieldCheck, ShieldAlert } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Dialog } from "@/components/ui/dialog";
import { ActionButton } from "@/components/ActionButton";
import { AlertBanner } from "@/components/AlertBanner";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState } from "@/components/EmptyState";
import { InfoCard } from "@/components/InfoCard";
import { HelpHint } from "@/components/HelpHint";
import { Spinner } from "@/components/Spinner";
import { useToast } from "@/context/ToastContext";
import { api, ApiError, type ApiBackup, type ApiInstance } from "@/lib/api";
import { formatDate, formatDateTime } from "@/lib/format";

export default function Backups({ embedId }: { embedId?: number } = {}) {
  const routeParams = useParams();
  const id = embedId != null ? String(embedId) : (routeParams.id ?? "");
  const instanceId = Number(id);
  const can = usePermissions(instanceId);
  const embedded = embedId != null;
  const navigate = useNavigate();
  const toast = useToast();
  const [backups, setBackups] = React.useState<ApiBackup[] | null>(null);
  // Snapshots are only accessible while the instance is running. When it's
  // stopped/suspended the endpoint returns ready=false (backups=[]).
  const [ready, setReady] = React.useState(true);
  const [instance, setInstance] = React.useState<ApiInstance | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [enabling, setEnabling] = React.useState(false);
  const [restoreTarget, setRestoreTarget] = React.useState<ApiBackup | null>(null);

  const handleRestore = async (backup: ApiBackup, confirm: string) => {
    await api.backupRestore(instanceId, backup.id, confirm);
    // The instance flips to provisioning while restic restores it —
    // send the customer to the instance page to watch the progress.
    navigate(`/my/instances/${id}`);
  };

  const load = React.useCallback(async () => {
    try {
      const [b, inst] = await Promise.all([
        api.backups(instanceId),
        api.instance(instanceId).catch(() => null),
      ]);
      setBackups(b.backups);
      setReady(b.ready);
      setInstance(inst);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : i18nText("Could not load snapshots."));
    }
  }, [instanceId]);

  React.useEffect(() => {
    load();
  }, [load]);

  const [disableOpen, setDisableOpen] = React.useState(false);
  const [disabling, setDisabling] = React.useState(false);
  const disableDailyBackup = async () => {
    setDisabling(true);
    try {
      await api.dailyBackupDisable(instanceId);
      toast.success(i18nText("Daily backups switched off"));
      setDisableOpen(false);
      await load();
    } catch (e) {
      toast.error(i18nText("Couldn't switch daily backups off"), e instanceof ApiError ? e.message : i18nText("Please try again."));
    } finally {
      setDisabling(false);
    }
  };

  const enableDailyBackup = async () => {
    setEnabling(true);
    try {
      const { checkout_url } = await api.dailyBackupEnable(instanceId);
      window.location.href = checkout_url;
    } catch (e) {
      toast.error(i18nText("Couldn't start checkout"), e instanceof ApiError ? e.message : i18nText("Please try again."));
      setEnabling(false);
    }
  };

  // Snapshots shown here: hosting takes FULL-INSTANCE snapshots (its
  // per-database/on-demand backups live on the Databases page); managed
  // services take per-DB snapshots and have no Databases page, so this is
  // their only backup view. Default to the hosting filter while the
  // instance is still loading to avoid a flicker.
  const isManagedService = !!instance && !instance.is_hosting;
  // On-demand snapshots have their own page (Snapshots.tsx).
  const snapshots = backups
    ? backups.filter((b) => (isManagedService ? !b.is_full_instance : b.is_full_instance) && b.source !== "snapshot")
    : null;

  // Poll while a snapshot is in progress.
  const hasRunning = !!snapshots?.some((b) => b.status === "in_progress");
  usePolling(load, { interval: 5000, enabled: hasRunning });

  const lastDone = snapshots?.find((b) => b.status === "available");

  return (
    <div className="animate-fade-in">

      <div>
        <h1 className="text-2xl font-bold tracking-tight">{i18nText("Backups")}<HelpHint anchor="snapshots" className="ms-1.5" />
        </h1>
        <p className="mt-1 text-sm text-muted">
          {isManagedService
            ? i18nText("Automatic daily backups of your service. Restore any backup with one click.")
            : i18nText("Automatic daily backups of your whole server, kept for the last 7 days. Restore any of them onto this server. For a copy you keep as long as you like, take a snapshot.")}
        </p>
      </div>

      {!ready ? (
        <AlertBanner
          className="mt-6"
          variant="warning"
          title={i18nText("Backups unavailable")}
          description={
            instance?.state === "suspended"
              ? i18nText("This instance is suspended. Settle the outstanding invoice to access backups.")
              : instance?.state === "stopped"
                ? i18nText("This instance is stopped. Start it to access backups.")
                : i18nText("Backups become available once the instance is running.")
          }
        />
      ) : (
        <>
          {instance && can("billing.manage") && (
            <DailyBackupCard
              instance={instance}
              enabling={enabling}
              onEnable={enableDailyBackup}
              onCheckout={() => (window.location.href = `/my/instances/${id}/daily-backup/checkout`)}
              onBilling={() => navigate("/my/billing")}
              onDisable={() => setDisableOpen(true)}
            />
          )}

          {error && <AlertBanner className="mt-6" variant="danger" title={i18nText("Backups")} description={error} />}

          {!backups && !error ? (
        <div className="mt-20 flex justify-center">
          <Spinner size="lg" label={i18nText("Loading backups…")} />
        </div>
      ) : snapshots ? (
        <>
          <div className="mt-6 grid gap-4 sm:grid-cols-2">
            <InfoCard label={i18nText("Backups")} value={snapshots.length} icon={Archive} />
            <InfoCard
              label={i18nText("Latest")}
              value={<span className="text-base">{lastDone ? formatDate(lastDone.created) : "—"}</span>}
              icon={ShieldCheck}
            />
          </div>

          {snapshots.length === 0 ? (
            <EmptyState
              className="mt-8"
              icon={Archive}
              title={i18nText("No backups yet")}
              description={i18nText("Backups run automatically every day once Daily Backups is on; the first one appears here when it completes.")}
            />
          ) : (
            <Card className="mt-6 divide-y divide-border">
              {snapshots.map((b) => (
                <div key={b.id} className="flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex items-start gap-3">
                    <span className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border bg-card text-muted">
                      <Archive className="size-4" />
                    </span>
                    <div>
                      <div className="flex items-center gap-2">
                        <p className="font-medium">{formatDateTime(b.created)}</p>
                        <span className="rounded-full border border-border px-2 py-0.5 text-[11px] text-muted">{i18nText("Automatic")}</span>
                      </div>
                      <p className="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
                        <Clock className="size-3" />{i18nText("Full server backup")}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 sm:justify-end">
                    <StatusBadge status={b.status} />
                    {can("backup.download") && b.download_url && <a className="text-sm text-primary" href={b.download_url}>{i18nText("Download")}</a>}
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={!can("db.restore") || b.status !== "available"}
                      onClick={() => setRestoreTarget(b)}
                    >
                      <RotateCcw className="size-4" />
                      <span className="hidden sm:inline">{i18nText("Restore")}</span>
                    </Button>
                  </div>
                </div>
              ))}
            </Card>
          )}
        </>
      ) : null}
        </>
      )}

      <Dialog open={disableOpen} onClose={() => setDisableOpen(false)} title={i18nText("Switch daily backups off?")}>
        <p className="text-sm text-muted">{i18nText("Daily backups stop immediately and the existing automatic backups are removed. The current period is not refunded. Your snapshots are not affected, and you can switch backups back on at any time.")}</p>
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setDisableOpen(false)} disabled={disabling}>{i18nText("Keep backups")}</Button>
          <ActionButton variant="danger" loading={disabling} loadingText={i18nText("Switching off…")} onClick={disableDailyBackup}>{i18nText("Switch off")}</ActionButton>
        </div>
      </Dialog>

      <RestoreSnapshotDialog
        backup={restoreTarget}
        instanceName={instance?.name || ""}
        onClose={() => setRestoreTarget(null)}
        onRestore={handleRestore}
      />
    </div>
  );
}

export function RestoreSnapshotDialog({
  backup,
  instanceName,
  onClose,
  onRestore,
}: {
  backup: ApiBackup | null;
  instanceName: string;
  onClose: () => void;
  onRestore: (backup: ApiBackup, confirm: string) => Promise<void>;
}) {
  const [confirm, setConfirm] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (backup) {
      setConfirm("");
      setLoading(false);
      setError(null);
    }
  }, [backup]);

  const ok = confirm.trim() === instanceName && !!instanceName;

  const submit = async () => {
    if (!backup || !ok) return;
    setError(null);
    setLoading(true);
    try {
      await onRestore(backup, confirm.trim());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : i18nText("Couldn't start the restore."));
      setLoading(false);
    }
  };

  return (
    <Dialog open={!!backup} onClose={onClose} title={i18nText("Restore from snapshot")}>
      {error && <AlertBanner className="mb-4" variant="danger" title={i18nText("Couldn't restore")} description={error} />}
      <div className="flex gap-3">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-full bg-warning/10 text-warning">
          <RotateCcw className="size-5" />
        </span>
        <div className="text-sm">
          <p className="font-medium text-foreground">{i18nText("Restore this instance to the snapshot?")}</p>
          <p className="mt-1 text-muted">{i18nText("This replaces the instance's current databases, files, and configuration with the state captured in this snapshot.")}{backup ? ` (${formatDateTime(backup.created)})` : ""}{" "}{i18nText("A fresh pre-restore snapshot is taken first, but anything created since cannot be recovered otherwise.")}</p>
        </div>
      </div>
      <div className="mt-5 space-y-2">
        <Label htmlFor="restore-confirm">{i18nText("Type ")}<code className="rounded-sm bg-border/60 px-1 py-0.5 font-mono text-xs text-foreground">{instanceName}</code>{i18nText(" to confirm")}</Label>
        <Input
          id="restore-confirm"
          data-technical
          autoFocus
          autoComplete="off"
          placeholder={instanceName}
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && ok && submit()}
        />
      </div>
      <div className="mt-6 flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose} disabled={loading}>{i18nText("Cancel")}</Button>
        <ActionButton variant="danger" loading={loading} loadingText={i18nText("Starting restore\u2026")} disabled={!ok} onClick={submit}>{i18nText("Restore instance")}</ActionButton>
      </div>
    </Dialog>
  );
}

function DailyBackupCard({
  instance,
  enabling,
  onEnable,
  onCheckout,
  onBilling,
  onDisable,
}: {
  instance: ApiInstance;
  enabling: boolean;
  onEnable: () => void;
  onCheckout: () => void;
  onBilling: () => void;
  onDisable: () => void;
}) {
  const price = instance.daily_backup_price || 0;
  const next = instance.daily_backup_next_invoice_date;

  // Active and paid up.
  if (instance.daily_backup_enabled && !instance.daily_backup_suspended) {
    return (
      <Card className="mt-6 flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-success/10 text-success">
            <ShieldCheck className="size-5" />
          </span>
          <div>
            <p className="font-medium">{i18nText("Daily backups are on")}</p>
            <p className="text-xs text-muted">{i18nText("Billed with your plan")}{price > 0 ? i18nText(" · currently {0}/month", [price]) : ""}{next ? i18nText(" · next charge {0}", [formatDate(next)]) : ""}.
            </p>
          </div>
        </div>
        <Button variant="ghost" className="shrink-0 text-muted" onClick={onDisable}>{i18nText("Switch off")}</Button>
      </Card>
    );
  }

  // Subscribed but paused for non-payment.
  if (instance.daily_backup_enabled && instance.daily_backup_suspended) {
    return (
      <Card className="mt-6 flex flex-col gap-3 border-warning/40 p-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-warning/10 text-warning">
            <ShieldAlert className="size-5" />
          </span>
          <div>
            <p className="font-medium">{i18nText("Daily backups paused")}</p>
            <p className="text-xs text-muted">{i18nText("Your monthly backup invoice is overdue. Backups resume automatically once it's paid.")}</p>
          </div>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="ghost" className="text-muted" onClick={onDisable}>{i18nText("Switch off")}</Button>
          <Button variant="secondary" onClick={onBilling}>{i18nText("Go to billing")}</Button>
        </div>
      </Card>
    );
  }

  // Activation invoice issued, awaiting payment.
  if (instance.daily_backup_pending) {
    return (
      <Card className="mt-6 flex flex-col gap-3 border-info/40 p-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-info/10 text-info">
            <Clock className="size-5" />
          </span>
          <div>
            <p className="font-medium">{i18nText("Payment pending")}</p>
            <p className="text-xs text-muted">{i18nText("Finish checkout to turn on daily backups.")}</p>
          </div>
        </div>
        <Button className="shrink-0" onClick={onCheckout}>{i18nText("Complete checkout")}</Button>
      </Card>
    );
  }

  // Off — offer to enable.
  return (
    <Card className="mt-6 flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-start gap-3">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border bg-card text-muted">
          <ShieldAlert className="size-5" />
        </span>
        <div>
          <p className="font-medium">{i18nText("Daily backups are off")}<HelpHint anchor="daily-backup" className="ms-1.5" /></p>
          <p className="text-xs text-muted">{i18nText("Automatic daily backups of your whole server, billed monthly by used storage")}{price > 0 ? i18nText(" (currently {0}/month)", [price]) : ""}{i18nText(". Renews monthly; pauses if a renewal goes unpaid.")}</p>
        </div>
      </div>
      <ActionButton className="shrink-0" loading={enabling} loadingText={i18nText("Starting\u2026")} onClick={onEnable}>
        <ShieldCheck className="size-4" />{i18nText("Enable daily backups")}</ActionButton>
    </Card>
  );
}
