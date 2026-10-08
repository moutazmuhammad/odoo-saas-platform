import { menuPosition } from "@/lib/menuPosition";
import { i18nText, getLanguage } from "@/i18n";
import * as React from "react";
import { usePermissions } from "@/lib/permissions";
import { usePolling } from "@/hooks/usePolling";
import { useNavigate, useParams } from "react-router-dom";
import {
  Plus,
  Database,
  KeyRound,
  Trash2,
  ExternalLink,
  Copy,
  Check,
  MoreHorizontal,
  Loader2,
  Download,
  CopyPlus,
  RefreshCw,
  UploadCloud,
  Settings2,
} from "lucide-react";
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
import { HelpHint } from "@/components/HelpHint";
import { api, ApiError, uploadToBucket, type DbListData, type ApiBackup, type ApiInstance } from "@/lib/api";
import { formatDateTime, formatSizeMb } from "@/lib/format";
import { cn } from "@/lib/utils";

export default function Databases({ embedId }: { embedId?: number } = {}) {
  const routeParams = useParams();
  const id = embedId != null ? String(embedId) : (routeParams.id ?? "");
  const instanceId = Number(id);
  const can = usePermissions(instanceId);
  const embedded = embedId != null;
  const navigate = useNavigate();
  const toast = useToast();

  const [data, setData] = React.useState<DbListData | null>(null);
  const [instance, setInstance] = React.useState<ApiInstance | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [resetTarget, setResetTarget] = React.useState<string | null>(null);
  const [restoreOpen, setRestoreOpen] = React.useState(false);
  const [backupsTarget, setBackupsTarget] = React.useState<string | null>(null);
  const [backups, setBackups] = React.useState<ApiBackup[]>([]);
  const [openMenu, setOpenMenu] = React.useState<string | null>(null);
  // The actions menu is rendered at fixed viewport coords (anchored to
  // the trigger button) so it isn't clipped by the table/card overflow
  // — which happened when a single short row left no room below it.
  const [menuPos, setMenuPos] = React.useState<{ top: number; left: number }>({ top: 0, left: 0 });

  // `background` = a poll/refresh (not the first load). On a background
  // failure we keep what's already on screen and retry silently — a
  // transient blip while a create is in flight (or the instance is busy)
  // must NOT replace the page with an error banner.
  const load = React.useCallback(async (background = false) => {
    try {
      const [d, b] = await Promise.all([
        api.databases(instanceId),
        api.backups(instanceId).then((r) => r.backups).catch(() => [] as ApiBackup[]),
      ]);
      setData(d);
      setBackups(b);
      setError(null);
    } catch (e) {
      if (!background) {
        setError(e instanceof ApiError ? e.message : i18nText("Could not load databases."));
      }
    }
  }, [instanceId]);

  React.useEffect(() => {
    load();
  }, [load]);

  // Database management is hosting-only (self-managed). Managed services get
  // a server-provisioned DB and no DB ops — bounce to the instance overview.
  React.useEffect(() => {
    api.instance(instanceId).then(setInstance).catch(() => {});
  }, [instanceId]);
  React.useEffect(() => {
    if (instance && !instance.is_hosting) {
      navigate(`/my/instances/${id}`, { replace: true });
    }
  }, [instance, id, navigate]);

  // Per-database, non-snapshot backups for a given DB name, newest first.
  const backupsFor = (name: string) =>
    backups.filter((b) => !b.is_full_instance && b.db_name === name);
  const backupRunning = backups.some((b) => b.status === "in_progress");

  // While async create/drop ops — or a backup — are in flight, poll.
  const hasPending = !!data?.pending_ops?.length || backupRunning;
  // Operation currently running against a given DB name (if any), so a
  // row can show the right label ("Creating…" vs "Deleting…").
  const pendingOp = (name: string) =>
    data?.pending_ops?.find((o) => o.db_name === name)?.operation;
  // A create that's mid-flight: while the DB hasn't appeared in the
  // list yet we surface it as a synthetic loading row. Once it shows up
  // in `databases` (mid-create), its own row carries the spinner — so
  // exclude those here to avoid a duplicate row.
  const existingNames = new Set((data?.databases ?? []).map((d) => d.name));
  // A create / duplicate / restore-into-new-name produces a database
  // that isn't in the list yet (or is mid-replacement) — surface those
  // as synthetic in-flight rows so the customer sees the work.
  const creatingOps = (data?.pending_ops ?? []).filter(
    (o) =>
      (o.operation === "create" || o.operation === "duplicate" || o.operation === "restore") &&
      !existingNames.has(o.db_name),
  );
  // Any create in flight (existing-in-list or not) locks the button.
  const isCreating = (data?.pending_ops ?? []).some((o) => o.operation === "create");
  usePolling(() => load(true), { interval: 5000, enabled: hasPending });


  // One-click backup download: trigger a fresh on-demand backup, poll
  // until it's built + uploaded to the bucket, then start the browser
  // download straight from the (short-lived) bucket URL. The customer
  // sees a single "Download backup" action — the build/upload happen
  // transparently, and the bucket object is reaped within the hour so
  // nothing is retained. Throws on failure so the dialog surfaces it.
  const downloadBackup = React.useCallback(
    async (name: string, format: "zip" | "dump") => {
      const { backup_id } = await api.dbBackup(instanceId, name, format);
      // Poll until the build (dump + multipart upload) finishes. The
      // build runs server-side in a background thread — there's no HTTP
      // request held open — so there's no request timeout to hit; the
      // only question is how long we keep watching. We adapt to DB size:
      // as long as the backup is still building we keep waiting, up to a
      // generous 30-minute backstop. A handful of consecutive network
      // blips are tolerated rather than aborting. Refresh the list as we
      // go so the dialog shows progress and a manual fallback link.
      const DEADLINE_MS = 30 * 60 * 1000;
      const startedAt = Date.now();
      let misses = 0;
      while (Date.now() - startedAt < DEADLINE_MS) {
        await new Promise((r) => setTimeout(r, 3000));
        let b: ApiBackup | undefined;
        try {
          const r = await api.backups(instanceId);
          setBackups(r.backups);
          b = r.backups.find((x) => x.id === backup_id);
          misses = 0;
        } catch {
          if (++misses > 20) {
            throw new ApiError(i18nText("Lost connection while preparing the backup. Please try again."));
          }
          continue; // transient blip — keep polling
        }
        if (!b) {
          // The record vanished (e.g. a newer backup wiped this slot).
          throw new ApiError(i18nText("This backup is no longer available. Please try again."));
        }
        if (b.status === "failed") {
          throw new ApiError(i18nText("The backup couldn't be created. Please try again."));
        }
        if (b.status === "available" && b.download_url) {
          triggerBrowserDownload(b.download_url);
          void load(true);
          return;
        }
        // status is still "in_progress" — keep waiting.
      }
      throw new ApiError(
        i18nText("Your backup is taking longer than expected — it'll be ready shortly. Try Download again in a moment."),
      );
    },
    [instanceId, load],
  );

  return (
    <div className="animate-fade-in">

      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{i18nText("Databases")}<HelpHint anchor="create-database" className="ms-1.5" /></h1>
          <p className="mt-1 text-sm text-muted">{i18nText("This server runs one database. Back it up, restore your own backup into it, or reset its admin password.")}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="secondary"
            onClick={() => setRestoreOpen(true)}
            disabled={!can("db.restore") || !data?.ready}
            title={data?.ready ? undefined : i18nText("Available once your instance is running.")}
          >
            <UploadCloud className="size-4" />{i18nText("Restore database")}</Button>
        </div>
      </div>

      {error && <AlertBanner className="mt-6" variant="danger" title={i18nText("Database management")} description={error} />}

      {!data && !error ? (
        <div className="mt-20 flex justify-center">
          <Spinner size="lg" label={i18nText("Loading databases…")} />
        </div>
      ) : data && !data.ready ? (
        <EmptyState
          className="mt-8"
          icon={Database}
          title={i18nText("Instance not ready")}
          description={i18nText("Database management becomes available once your instance is running.")}
          action={<Button variant="secondary" onClick={() => navigate(`/my/instances/${id}`)}>{i18nText("Back to instance")}</Button>}
        />
      ) : data && data.databases.length === 0 && !isCreating ? (
        <EmptyState
          className="mt-8"
          icon={Database}
          title={i18nText("Your database is being prepared")}
          description={i18nText("It appears here in a moment. You can also restore one of your own backups.")}
          action={can("db.restore") ? <Button variant="secondary" onClick={() => setRestoreOpen(true)}><UploadCloud className="size-4" />{i18nText("Restore database")}</Button> : undefined}
        />
      ) : data ? (
        <Card className="mt-6 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-start text-xs uppercase tracking-wide text-muted">
                  <th className="px-5 py-3 font-medium">{i18nText("Database")}</th>
                  <th className="hidden px-5 py-3 font-medium sm:table-cell">{i18nText("Admin login")}</th>
                  <th className="px-5 py-3 font-medium">{i18nText("Status")}</th>
                  <th className="px-5 py-3 text-end font-medium">{i18nText("Actions")}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {creatingOps.map((op) => (
                  <tr key={`creating-${op.db_name}`} className="bg-info/5">
                    <td className="px-5 py-4">
                      <div className="flex items-center gap-2.5">
                        <Database className="size-4 text-muted" />
                        <span className="font-medium">{i18nText("Your database")}</span>
                      </div>
                    </td>
                    <td className="hidden px-5 py-4 text-muted sm:table-cell">—</td>
                    <td className="px-5 py-4">
                      <span className="inline-flex items-center gap-1.5 text-xs text-info">
                        <Loader2 className="size-3.5 animate-spin" /> {op.operation === "duplicate" ? i18nText("Duplicating…") : op.operation === "restore" ? i18nText("Restoring…") : i18nText("Creating…")}
                      </span>
                    </td>
                    <td className="px-5 py-4" />
                  </tr>
                ))}
                {data.databases.map((db) => {
                  const op = pendingOp(db.name);
                  const pending = !!op;
                  return (
                    <tr key={db.name} className="transition-colors hover:bg-card/60">
                      <td className="px-5 py-4">
                        <div className="flex items-center gap-2.5">
                          <Database className="size-4 text-muted" />
                          <span className="font-medium">{i18nText("Production database")}</span>
                        </div>
                      </td>
                      <td className="hidden px-5 py-4 text-muted sm:table-cell">{db.login || "—"}</td>
                      <td className="px-5 py-4">
                        {pending ? (
                          <span className="inline-flex items-center gap-1.5 text-xs text-info">
                            <Loader2 className="size-3.5 animate-spin" /> {op === "drop" ? i18nText("Deleting…") : op === "duplicate" ? i18nText("Duplicating…") : op === "upgrade" ? i18nText("Upgrading…") : op === "restore" ? i18nText("Restoring…") : i18nText("Creating…")}
                          </span>
                        ) : (
                          <StatusBadge status="running" label={i18nText("Active")} />
                        )}
                      </td>
                      <td className="px-5 py-4">
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={pending || !data?.url}
                            title={data?.url ? undefined : i18nText("Instance URL unavailable")}
                            onClick={() =>
                              data?.url &&
                              window.open(
                                `${data.url.replace(/\/$/, "")}/web?db=${encodeURIComponent(db.name)}`,
                                "_blank",
                                "noopener,noreferrer",
                              )
                            }
                          >
                            <ExternalLink className="size-4" />
                            <span className="hidden lg:inline">{i18nText("Open")}</span>
                          </Button>
                          <div className="relative">
                            <Button
                              size="icon"
                              variant="ghost"
                              disabled={pending}
                              aria-label={i18nText("More actions")}
                              onClick={(e) => {
                                if (openMenu === db.name) {
                                  setOpenMenu(null);
                                  return;
                                }
                                const r = e.currentTarget.getBoundingClientRect();
                                setMenuPos(menuPosition(r, window.innerWidth, getLanguage() === "ar"));
                                setOpenMenu(db.name);
                              }}
                            >
                              <MoreHorizontal className="size-4" />
                            </Button>
                            {openMenu === db.name && (
                              <>
                                <div className="fixed inset-0 z-30" onClick={() => setOpenMenu(null)} />
                                <div
                                  className="fixed z-40 w-44 overflow-hidden rounded-lg border border-border bg-card shadow-card animate-scale-in"
                                  style={{ top: menuPos.top, left: menuPos.left }}
                                >
                                  <MenuItem disabled={!can("backup.download") || !can("backup.create")} icon={Download} label={i18nText("Download backup")} onClick={() => { setOpenMenu(null); setBackupsTarget(db.name); }} />
                                  <MenuItem disabled={!can("db.password")} icon={KeyRound} label={i18nText("Reset password")} onClick={() => { setOpenMenu(null); setResetTarget(db.name); }} />
                                </div>
                              </>
                            )}
                          </div>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}




      <RestoreDatabaseDialog
        open={restoreOpen}
        instanceId={instanceId}
        existing={data?.databases.map((d) => d.name) || []}
        prefix={data?.prefix || ""}
        confirmWord={instance?.name || ""}
        onClose={() => setRestoreOpen(false)}
        onDone={() => {
          // The target DB now shows a live "Restoring…" row; the instance
          // stays running and the page keeps working.
          setRestoreOpen(false);
          void load(true);
        }}
      />

      <ResetPasswordDialog
        dbName={resetTarget}
        onClose={() => setResetTarget(null)}
        onReset={async (name, password, targetLogin) => {
          const { login } = await api.dbResetPassword(instanceId, name, password, targetLogin);
          toast.success(i18nText("Password reset"), i18nText("New admin password set for {0}.", [name]));
          return login;
        }}
      />


      <DatabaseBackupsDialog
        canCreate={can("backup.create")}
        dbName={backupsTarget}
        backups={backupsTarget ? backupsFor(backupsTarget) : []}
        onDownload={downloadBackup}
        onClose={() => setBackupsTarget(null)}
      />
    </div>
  );
}

// Start a browser download from a (cross-origin) bucket URL without
// navigating the SPA away. A hidden anchor click downloads the file in
// place — the object is a .zip/.dump, so the browser saves rather than
// renders it.
function triggerBrowserDownload(url: string) {
  const a = document.createElement("a");
  a.href = url;
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function DatabaseBackupsDialog({
  canCreate,
  dbName,
  backups,
  onDownload,
  onClose,
}: {
  canCreate: boolean;
  dbName: string | null;
  backups: ApiBackup[];
  onDownload: (name: string, format: "zip" | "dump") => Promise<void>;
  onClose: () => void;
}) {
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [done, setDone] = React.useState(false);
  const [format, setFormat] = React.useState<"zip" | "dump">("zip");

  React.useEffect(() => {
    if (dbName) {
      setLoading(false);
      setError(null);
      setDone(false);
      setFormat("zip");
    }
  }, [dbName]);

  // The most recent ready backup (if the customer wants to re-trigger
  // the save from the same short-lived link).
  const ready = backups.find((b) => b.status === "available" && b.download_url);

  const start = async () => {
    if (!dbName) return;
    setError(null);
    setDone(false);
    setLoading(true);
    try {
      await onDownload(dbName, format);
      setDone(true);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : i18nText("Couldn't prepare the backup."));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog
      open={!!dbName}
      onClose={onClose}
      title={i18nText("Download backup")}
      description={dbName ? i18nText("Download an available backup of your database.") : undefined}
    >
      {error && <AlertBanner className="mb-4" variant="danger" title={i18nText("Backup")} description={error} />}
      {done && !error && (
        <AlertBanner
          className="mb-4"
          variant="success"
          title={i18nText("Your download has started")}
          description={i18nText("If it didn't begin automatically, use the link below. The file is removed from our storage shortly after.")}
        />
      )}
      <div className="space-y-2">
        <Label>{i18nText("Format")}</Label>
        <div className="grid grid-cols-2 gap-2">
          {([
            { v: "zip", title: "ZIP", hint: i18nText("Database + files") },
            { v: "dump", title: i18nText("Dump"), hint: i18nText("Database only") },
          ] as const).map((o) => (
            <button
              key={o.v}
              type="button"
              disabled={loading}
              onClick={() => setFormat(o.v)}
              className={cn(
                "rounded-lg border px-3 py-2 text-start transition-colors disabled:opacity-50",
                format === o.v ? "border-primary bg-primary/10" : "border-border hover:bg-border/40",
              )}
            >
              <p className="text-sm font-medium">{o.title}</p>
              <p className="text-xs text-muted">{o.hint}</p>
            </button>
          ))}
        </div>
      </div>

      <p className="mt-4 text-xs text-muted">{i18nText("We build the backup, then your download starts automatically. Larger databases take a little longer — keep this window open.")}</p>

      <div className="mt-4 flex items-center justify-between gap-3">
        {ready && ready.download_url ? (
          <div className="min-w-0">
            <a
              href={ready.download_url}
              className="inline-flex items-center gap-1.5 text-sm font-medium text-primary underline-offset-2 hover:underline"
            >
              <Download className="size-4" />{i18nText("Download last backup")}</a>
            <p className="mt-0.5 text-xs text-muted">
              {formatDateTime(ready.created)}
              {ready.size_mb > 0 && <> · {formatSizeMb(ready.size_mb)}</>}
            </p>
          </div>
        ) : (
          <span className="text-sm text-muted">{i18nText("No backup yet.")}</span>
        )}
        <ActionButton disabled={!canCreate} loading={loading} loadingText={i18nText("Preparing\u2026")} onClick={start}>
          <Download className="size-4" />{i18nText("Download backup")}</ActionButton>
      </div>

      <div className="mt-6 flex justify-end">
        <Button variant="secondary" onClick={onClose} disabled={loading}>{i18nText("Close")}</Button>
      </div>
    </Dialog>
  );
}

// Cheap, fail-fast client check: a real .zip starts with the local-file
// magic "PK\x03\x04". This catches an obviously-wrong file before we
// upload a (potentially huge) file; the server then does the
// authoritative integrity + Odoo-backup check before touching any DB.
async function looksLikeZip(file: File): Promise<boolean> {
  try {
    const head = new Uint8Array(await file.slice(0, 4).arrayBuffer());
    return head[0] === 0x50 && head[1] === 0x4b && head[2] === 0x03 && head[3] === 0x04;
  } catch {
    return false;
  }
}

function RestoreDatabaseDialog({
  open,
  instanceId,
  existing,
  prefix,
  confirmWord,
  onClose,
  onDone,
}: {
  open: boolean;
  instanceId: number;
  existing: string[];
  prefix: string;
  /** The project name the customer types to confirm a replacement. */
  confirmWord: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [file, setFile] = React.useState<File | null>(null);
  const [fileError, setFileError] = React.useState<string | null>(null);
  const [confirmation, setConfirmation] = React.useState("");
  // Odoo.sh model: the backup replaces this server's (single) database.
  // Without one yet, it becomes the server's main database.
  const overwrite = existing.length > 0;
  const target = existing[0] ?? `${prefix}main`;
  const [phase, setPhase] = React.useState<"idle" | "uploading" | "starting" | "done">("idle");
  const [progress, setProgress] = React.useState(0);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (open) {
      setFile(null);
      setFileError(null);
      setConfirmation("");
      setPhase("idle");
      setProgress(0);
      setError(null);
    }
  }, [open]);

  const busy = phase === "uploading" || phase === "starting";
  const fullTarget = target;
  const replacementConfirmed = !!confirmWord && confirmation.trim() === confirmWord;
  const canSubmit = !!file && !fileError && !busy && (!overwrite || replacementConfirmed);

  const pickFile = async (f: File | null) => {
    setError(null);
    setFileError(null);
    setFile(f);
    if (f) {
      if (!f.name.toLowerCase().endsWith(".zip") || !(await looksLikeZip(f))) {
        setFileError(i18nText("That doesn't look like a .zip backup. Choose an Odoo backup file (.zip)."));
      }
    }
  };

  const start = async () => {
    if (!canSubmit || !file) return;
    setError(null);
    try {
      setPhase("uploading");
      setProgress(0);
      const { backup_id, upload_url } = await api.dbRestoreUploadUrl(instanceId, target, overwrite);
      await uploadToBucket(upload_url, file, setProgress);
      setPhase("starting");
      await api.dbRestoreStart(instanceId, backup_id);
      setPhase("done");
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : i18nText("The restore couldn't be started."));
      setPhase("idle");
    }
  };

  const pct = Math.round(progress * 100);

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={i18nText("Restore from file")}
      description={i18nText("Upload one of your own Odoo backups (.zip). It replaces the database on this server.")}
    >
      {error && <AlertBanner className="mb-4" variant="danger" title={i18nText("Restore")} description={error} />}

      <div className="space-y-2">
        <Label htmlFor="restore-file">{i18nText("Backup file (.zip)")}</Label>
        <Input
          id="restore-file"
          type="file"
          accept=".zip"
          disabled={busy}
          onChange={(e) => pickFile(e.target.files?.[0] || null)}
        />
        {fileError && <p className="text-xs text-danger">{fileError}</p>}
      </div>

      <div className="mt-4 space-y-2">
        {overwrite ? (
          <>
            <p className="text-xs text-danger">{i18nText("This permanently replaces your database and all its data. Download a backup before continuing.")}</p>
            <Label htmlFor="restore-confirm">{i18nText("Type ")}{confirmWord}{i18nText(" to confirm replacement")}</Label>
            <Input id="restore-confirm" data-technical value={confirmation} disabled={busy} onChange={(e) => setConfirmation(e.target.value)} autoComplete="off" />
          </>
        ) : (
          <p className="text-xs text-muted">{i18nText("Your backup becomes this server's database.")}</p>
        )}
      </div>

      {phase === "uploading" && (
        <div className="mt-4">
          <div className="mb-1 flex justify-between text-xs text-muted">
            <span>{i18nText("Uploading…")}</span>
            <span>{pct}%</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-border">
            <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}
      {phase === "starting" && (
        <p className="mt-4 text-sm text-muted">{i18nText("Upload complete — verifying and starting the restore…")}</p>
      )}

      <div className="mt-6 flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose} disabled={busy}>{i18nText("Cancel")}</Button>
        <ActionButton
          loading={busy}
          loadingText={phase === "starting" ? i18nText("Starting…") : i18nText("Uploading…")}
          disabled={!canSubmit}
          onClick={start}
        >
          <UploadCloud className="size-4" />{i18nText("Upload & restore")}</ActionButton>
      </div>
    </Dialog>
  );
}

function MenuItem({ icon: Icon, label, onClick, danger, disabled }: { icon: typeof KeyRound; label: string; onClick: () => void; danger?: boolean; disabled?: boolean }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "disabled:opacity-40 disabled:cursor-not-allowed flex w-full items-center gap-2.5 px-3 py-2.5 text-start text-sm transition-colors",
        danger ? "text-danger hover:bg-danger/10" : "text-foreground hover:bg-border/50"
      )}
    >
      <Icon className="size-4" />
      {label}
    </button>
  );
}

function ResetPasswordDialog({
  dbName,
  onClose,
  onReset,
}: {
  dbName: string | null;
  onClose: () => void;
  onReset: (name: string, password: string, targetLogin?: string) => Promise<string>;
}) {
  const [password, setPassword] = React.useState("");
  const [targetLogin, setTargetLogin] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [done, setDone] = React.useState(false);
  const [adminLogin, setAdminLogin] = React.useState("");
  const [copiedField, setCopiedField] = React.useState<"login" | "password" | null>(null);

  React.useEffect(() => {
    if (dbName) {
      setPassword("");
      setTargetLogin("");
      setError(null);
      setLoading(false);
      setDone(false);
      setAdminLogin("");
      setCopiedField(null);
    }
  }, [dbName]);

  const copy = (field: "login" | "password", value: string) => {
    navigator.clipboard?.writeText(value);
    setCopiedField(field);
    setTimeout(() => setCopiedField(null), 1500);
  };

  const submit = async () => {
    if (!dbName) return;
    if (password.length < 6) return setError(i18nText("Choose a password of at least 6 characters."));
    setError(null);
    setLoading(true);
    try {
      const login = await onReset(dbName, password, targetLogin.trim() || undefined);
      setAdminLogin(login);
      setDone(true);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : i18nText("Couldn't reset the password."));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={!!dbName} onClose={onClose} title={i18nText("Reset admin password")} description={dbName ? i18nText("Set a new admin password for your database.") : undefined}>
      {!done ? (
        <>
          {error && <AlertBanner className="mb-4" variant="danger" title={i18nText("Couldn't reset password")} description={error} />}
          <AlertBanner variant="warning" title={i18nText("This rotates the admin password")} description={i18nText("The admin user will need the new password to sign in.")} />
          <div className="mt-4 space-y-2">
            <Label htmlFor="reset-login">{i18nText("Administrator login ")}<span className="font-normal text-muted">{i18nText("(optional)")}</span></Label>
            <Input id="reset-login" data-technical placeholder={i18nText("Leave blank to reset the main administrator")} value={targetLogin} onChange={(e) => setTargetLogin(e.target.value)} />
            <p className="text-xs text-muted">{i18nText("If you replaced the default admin with your own user, enter that login. Otherwise leave this blank.")}</p>
          </div>
          <div className="mt-4 space-y-2">
            <Label htmlFor="new-pass">{i18nText("New password")}</Label>
            <Input id="new-pass" type="password" placeholder="••••••••" value={password} autoFocus onChange={(e) => setPassword(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submit()} />
          </div>
          <div className="mt-6 flex justify-end gap-2">
            <Button variant="secondary" onClick={onClose} disabled={loading}>{i18nText("Cancel")}</Button>
            <ActionButton variant="danger" loading={loading} loadingText={i18nText("Resetting\u2026")} onClick={submit}>{i18nText("Reset password")}</ActionButton>
          </div>
        </>
      ) : (
        <>
          <AlertBanner variant="success" title={i18nText("Password reset")} description={i18nText("The admin password has been updated. Use the login below to sign in — handy if you forgot which user is the admin.")} />
          <div className="mt-4">
            <Label>{i18nText("Admin login")}</Label>
            <div className="mt-2 flex items-center gap-2 rounded-lg border border-border bg-background p-2.5">
              <code className="flex-1 truncate font-mono text-sm">{adminLogin || "admin"}</code>
              <Button size="sm" variant="ghost" onClick={() => copy("login", adminLogin || "admin")}>
                {copiedField === "login" ? <Check className="size-4 text-success" /> : <Copy className="size-4" />}
                {copiedField === "login" ? i18nText("Copied") : i18nText("Copy")}
              </Button>
            </div>
          </div>
          <div className="mt-4">
            <Label>{i18nText("New password")}</Label>
            <div className="mt-2 flex items-center gap-2 rounded-lg border border-border bg-background p-2.5">
              <code className="flex-1 truncate font-mono text-sm">{password}</code>
              <Button size="sm" variant="ghost" onClick={() => copy("password", password)}>
                {copiedField === "password" ? <Check className="size-4 text-success" /> : <Copy className="size-4" />}
                {copiedField === "password" ? i18nText("Copied") : i18nText("Copy")}
              </Button>
            </div>
          </div>
          <div className="mt-6 flex justify-end">
            <Button onClick={onClose}>{i18nText("Done")}</Button>
          </div>
        </>
      )}
    </Dialog>
  );
}
