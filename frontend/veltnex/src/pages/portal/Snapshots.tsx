import { i18nText } from "@/i18n";
import * as React from "react";
import { usePermissions } from "@/lib/permissions";
import { usePolling } from "@/hooks/usePolling";
import { useNavigate, useParams } from "react-router-dom";
import { Camera, RotateCcw, Trash2, PlusCircle, Rocket } from "lucide-react";
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
import { api, ApiError, type ApiBackup, type ApiInstance } from "@/lib/api";
import { formatDateTime, formatSizeMb } from "@/lib/format";
import { RestoreSnapshotDialog } from "@/pages/portal/Backups";

/** DigitalOcean-style snapshots: taken on demand, kept until deleted.
 *  Restore onto this server, or start a new Production project from one. */
export default function Snapshots({ embedId }: { embedId?: number } = {}) {
  const routeParams = useParams();
  const id = embedId != null ? String(embedId) : (routeParams.id ?? "");
  const instanceId = Number(id);
  const can = usePermissions(instanceId);
  const navigate = useNavigate();
  const toast = useToast();
  const [backups, setBackups] = React.useState<ApiBackup[] | null>(null);
  const [ready, setReady] = React.useState(true);
  const [instance, setInstance] = React.useState<ApiInstance | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [takeOpen, setTakeOpen] = React.useState(false);
  const [restoreTarget, setRestoreTarget] = React.useState<ApiBackup | null>(null);
  const [deleteTarget, setDeleteTarget] = React.useState<ApiBackup | null>(null);

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

  const snapshots = backups ? backups.filter((b) => b.source === "snapshot") : null;
  const taking = !!snapshots?.some((b) => b.status === "in_progress");
  usePolling(load, { interval: 5000, enabled: taking });

  const handleRestore = async (backup: ApiBackup, confirm: string) => {
    await api.backupRestore(instanceId, backup.id, confirm);
    navigate(`/my/instances/${id}`);
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      await api.snapshotDelete(instanceId, deleteTarget.id);
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

  return (
    <div className="animate-fade-in">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{i18nText("Snapshots")}</h1>
          <p className="mt-1 text-sm text-muted">{i18nText("A snapshot captures your whole server, database and files, whenever you want. Keep it as long as you like, restore it here, or start a new project from it.")}</p>
        </div>
        {can("backup.create") && (
          <Button onClick={() => setTakeOpen(true)} disabled={!ready || taking}>
            <Camera className="size-4" />{i18nText("Take snapshot")}</Button>
        )}
      </div>

      {!ready ? (
        <AlertBanner className="mt-6" variant="warning" title={i18nText("Snapshots unavailable")}
          description={i18nText("Snapshots become available once the server is running.")} />
      ) : error ? (
        <AlertBanner className="mt-6" variant="danger" title={i18nText("Snapshots")} description={error} />
      ) : !snapshots ? (
        <div className="mt-20 flex justify-center"><Spinner size="lg" label={i18nText("Loading snapshots…")} /></div>
      ) : snapshots.length === 0 ? (
        <EmptyState
          className="mt-8"
          icon={Camera}
          title={i18nText("No snapshots yet")}
          description={i18nText("Take one before a big change, or to clone this server into a new project.")}
          action={can("backup.create") ? <Button onClick={() => setTakeOpen(true)}><PlusCircle className="size-4" />{i18nText("Take snapshot")}</Button> : undefined}
        />
      ) : (
        <Card className="mt-6 divide-y divide-border">
          {snapshots.map((b) => (
            <div key={b.id} className="flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex items-start gap-3">
                <span className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border bg-card text-muted">
                  <Camera className="size-4" />
                </span>
                <div>
                  <p className="font-medium">{b.label}</p>
                  <p className="mt-0.5 text-xs text-muted">
                    {formatDateTime(b.created)}{b.size_mb ? ` · ${formatSizeMb(b.size_mb)}` : ""}
                  </p>
                  {b.status === "failed" && b.error && <p className="mt-1 text-xs text-danger">{b.error}</p>}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2 sm:justify-end">
                <StatusBadge status={b.status} />
                {instance?.is_hosting && can("backup.create") && (
                  <Button size="sm" variant="secondary" disabled={b.status !== "available"} onClick={() => startProject(b)}>
                    <Rocket className="size-4" /><span className="hidden sm:inline">{i18nText("New project")}</span>
                  </Button>
                )}
                <Button size="sm" variant="secondary" disabled={!can("db.restore") || b.status !== "available"} onClick={() => setRestoreTarget(b)}>
                  <RotateCcw className="size-4" /><span className="hidden sm:inline">{i18nText("Restore")}</span>
                </Button>
                {can("backup.create") && (
                  <Button size="sm" variant="ghost" aria-label={i18nText("Delete snapshot")} disabled={b.status === "in_progress"} onClick={() => setDeleteTarget(b)}>
                    <Trash2 className="size-4" />
                  </Button>
                )}
              </div>
            </div>
          ))}
        </Card>
      )}

      <TakeSnapshotDialog
        open={takeOpen}
        onClose={() => setTakeOpen(false)}
        onTake={async (name) => {
          await api.snapshotCreate(instanceId, name);
          setTakeOpen(false);
          toast.success(i18nText("Snapshot started"), i18nText("It appears in the list as soon as it is ready."));
          await load();
        }}
      />

      <RestoreSnapshotDialog
        backup={restoreTarget}
        instanceName={instance?.name || ""}
        onClose={() => setRestoreTarget(null)}
        onRestore={handleRestore}
      />

      <Dialog open={!!deleteTarget} onClose={() => setDeleteTarget(null)} title={i18nText("Delete snapshot")}>
        <p className="text-sm text-muted">{i18nText("Delete snapshot {0}? This cannot be undone.", [deleteTarget?.label || ""])}</p>
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setDeleteTarget(null)}>{i18nText("Cancel")}</Button>
          <Button variant="danger" onClick={handleDelete}><Trash2 className="size-4" />{i18nText("Delete")}</Button>
        </div>
      </Dialog>
    </div>
  );
}

function TakeSnapshotDialog({ open, onClose, onTake }: { open: boolean; onClose: () => void; onTake: (name: string) => Promise<void> }) {
  const [name, setName] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  React.useEffect(() => { if (open) { setName(""); setLoading(false); setError(null); } }, [open]);
  const valid = /^[\w .-]{1,60}$/.test(name.trim());
  const submit = async () => {
    if (!valid) return;
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
      <div className="mt-6 flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose} disabled={loading}>{i18nText("Cancel")}</Button>
        <ActionButton loading={loading} loadingText={i18nText("Starting…")} disabled={!valid} onClick={submit}>
          <Camera className="size-4" />{i18nText("Take snapshot")}</ActionButton>
      </div>
    </Dialog>
  );
}
