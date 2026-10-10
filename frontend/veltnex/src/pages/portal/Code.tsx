import { formatDateTime } from "@/lib/format";
import { usePolling } from "@/hooks/usePolling";
import { i18nText } from "@/i18n";
import * as React from "react";
import { hasPermission } from "@/lib/permissions";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { GitBranch, Package, Plus, X, Unplug, Boxes, CheckCircle2 } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { ActionButton } from "@/components/ActionButton";
import { AlertBanner } from "@/components/AlertBanner";
import { EmptyState } from "@/components/EmptyState";
import { Spinner } from "@/components/Spinner";
import { Dialog } from "@/components/ui/dialog";
import { HelpHint } from "@/components/HelpHint";
import { useToast } from "@/context/ToastContext";
import { api, ApiError, type ApiInstance } from "@/lib/api";

const parsePkgs = (s?: string) =>
  (s || "").split("\n").map((l) => l.trim()).filter(Boolean);

export default function Code({ embedId }: { embedId?: number } = {}) {
  const routeParams = useParams();
  const id = embedId != null ? String(embedId) : (routeParams.id ?? "");
  const instanceId = Number(id);
  const embedded = embedId != null;
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const toast = useToast();

  const [instance, setInstance] = React.useState<ApiInstance | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    try {
      setInstance(await api.instance(instanceId, params.get("access_token") || undefined));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : i18nText("Instance not found."));
    }
  }, [instanceId, params]);

  React.useEffect(() => {
    load();
  }, [load]);

  usePolling(load, { interval: 10000, enabled: !!instance?.repo?.webhook_enabled });

  // Code & packages is a hosting-only (self-managed) feature. Managed
  // services have no code access — bounce back to the instance overview.
  React.useEffect(() => {
    if (instance && !instance.is_hosting) {
      navigate(`/my/instances/${id}`, { replace: true });
    }
  }, [instance, id, navigate]);

  if (error) {
    return (
      <EmptyState className="mt-10" icon={GitBranch} title={i18nText("Unavailable")} description={error} />
    );
  }
  if (!instance) {
    return (
      <div className="mt-20 flex justify-center"><Spinner size="lg" label={i18nText("Loading…")} /></div>
    );
  }

  const canDeploy = hasPermission(instance.permissions, "project.configure") && (instance.state === "running" || instance.state === "stopped");

  return (
    <div className="animate-fade-in">

      {!embedded && (
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{i18nText("Code & packages")}<HelpHint anchor="repo" className="ms-1.5" />
          </h1>
          <p className="mt-1 text-sm text-muted">{i18nText("Connect your Git modules. Python dependencies come from your repository's ")}<code>{"requirements.txt"}</code>{i18nText(". Applying a change pulls your code and restarts the instance (brief downtime).")}</p>
        </div>
      )}

      {!canDeploy && (
        <AlertBanner
          className="mt-6"
          variant="warning"
          title={i18nText("Can't deploy right now")}
          description={i18nText("The instance must be running or stopped to apply code or package changes.")}
        />
      )}

      <RepoSection instance={instance} disabled={!canDeploy} onDeployed={() => navigate(`/my/instances/${id}`)} />
      <RequirementsInfo instance={instance} />
    </div>
  );
}

function RepoSection({
  instance,
  disabled,
  onDeployed,
}: {
  instance: ApiInstance;
  disabled: boolean;
  onDeployed: () => void;
}) {
  const toast = useToast();
  const navigate = useNavigate();
  const repo = instance.repo || { url: "", branch: "main", has_token: false, state: "" };
  const connected = !!repo.url;
  const webhookStatus = connected && (
    <p className={`mt-4 text-sm ${repo.webhook_health === "error" ? "text-danger" : "text-muted"}`}>
      {i18nText("Automatic deployment: ")}
      {!repo.webhook_enabled ? i18nText("Disabled")
        : !repo.webhook_registered ? i18nText("Webhook registration not confirmed")
        : repo.webhook_health === "healthy" ? i18nText("Webhook delivery confirmed")
        : repo.webhook_health === "error" ? i18nText("Webhook verification failed")
        : i18nText("Waiting for webhook delivery confirmation")}
      {repo.webhook_last_received && (
        <span className="mt-1 block text-xs text-muted">
          {i18nText("Last webhook received: {0}", [formatDateTime(repo.webhook_last_received)])}
        </span>
      )}
    </p>
  );
  // Repo + token are configured ONCE per project (Production). On a child
  // environment, the repo is inherited and read-only here.
  const isChild = !!instance.parent_id && instance.environment !== "production";
  const [url, setUrl] = React.useState(repo.url || "");
  const [branch, setBranch] = React.useState(repo.branch || "main");
  const [token, setToken] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [confirmOff, setConfirmOff] = React.useState(false);

  const save = async () => {
    setSaving(true);
    try {
      const p: { repo_url: string; repo_branch: string; git_token?: string } = {
        repo_url: url.trim(),
        repo_branch: branch.trim() || "main",
      };
      if (token.trim()) p.git_token = token.trim();
      await api.setRepo(instance.id, p);
      toast.success(i18nText("Deploying…"), i18nText("Pulling your repository and restarting the instance."));
      onDeployed();
    } catch (e) {
      toast.error(i18nText("Couldn't deploy"), e instanceof ApiError ? e.message : i18nText("Please try again."));
      setSaving(false);
    }
  };

  const disconnect = async () => {
    setSaving(true);
    try {
      await api.setRepo(instance.id, { repo_url: "", repo_branch: "main" });
      toast.success(i18nText("Disconnecting…"), i18nText("Removing the repository and restarting the instance."));
      onDeployed();
    } catch (e) {
      toast.error(i18nText("Couldn't disconnect"), e instanceof ApiError ? e.message : i18nText("Please try again."));
      setSaving(false);
      setConfirmOff(false);
    }
  };

  // Child environment: the repo is inherited from the project (read-only).
  if (isChild) {
    return (
      <Card className="mt-6 p-5">
        <div className="flex items-center gap-3">
          <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/15 text-primary">
            <GitBranch className="size-4" />
          </span>
          <div className="flex-1">
            <h2 className="font-semibold">{i18nText("Git repository")}</h2>
            <p className="text-xs text-muted">{i18nText("Inherited from the project. The repository and token are managed once on the Production environment.")}</p>
          </div>
          <Button
            variant="secondary"
            onClick={() => navigate(`/my/instances/${instance.parent_id}/code`)}
          >{i18nText("Manage on project")}</Button>
        </div>
        {webhookStatus}
        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <div>
            <Label>{i18nText("Repository")}</Label>
            <p className="mt-1 truncate font-mono text-sm text-muted">{repo.url || "—"}</p>
          </div>
          <div>
            <Label>{i18nText("Branch")}</Label>
            <p className="mt-1 font-mono text-sm text-muted">{repo.branch || "main"}</p>
          </div>
        </div>
      </Card>
    );
  }

  return (
    <Card className="mt-6 p-5">
      <div className="flex items-center gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/15 text-primary">
          <GitBranch className="size-4" />
        </span>
        <div className="flex-1">
          <h2 className="font-semibold">{i18nText("Git repository")}</h2>
          <p className="text-xs text-muted">
            {connected
              ? i18nText("Your custom modules are deployed from this repository.")
              : i18nText("Deploy your own Odoo modules from GitHub, GitLab or Bitbucket.")}
          </p>
        </div>
        {connected && (
          <Button variant="secondary" disabled={disabled || saving} onClick={() => setConfirmOff(true)}>
            <Unplug className="size-4" />
            <span className="hidden sm:inline">{i18nText("Disconnect")}</span>
          </Button>
        )}
      </div>

      {webhookStatus}
      <div className="mt-5 space-y-4">
        <div>
          <Label htmlFor="repo-url">{i18nText("Repository URL")}</Label>
          <Input id="repo-url" data-technical value={url} onChange={(e) => setUrl(e.target.value)}
                 placeholder="https://github.com/you/your-odoo-modules.git" />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <Label htmlFor="repo-branch">{i18nText("Branch")}</Label>
            <Input id="repo-branch" data-technical value={branch} onChange={(e) => setBranch(e.target.value)} placeholder={"main"} />
          </div>
          <div>
            <Label htmlFor="repo-token">{i18nText("Access token ")}{repo.has_token && <span className="text-muted">{i18nText("(set — blank keeps it)")}</span>}
            </Label>
            <Input id="repo-token" type="password" value={token} onChange={(e) => setToken(e.target.value)}
                   placeholder={repo.has_token ? "••••••••" : i18nText("for private repositories")} />
          </div>
        </div>
      </div>

      <div className="mt-5 flex justify-end">
        <ActionButton loading={saving} loadingText={i18nText("Deploying\u2026")} disabled={disabled || !url.trim()} onClick={save}>
          {connected ? i18nText("Save & redeploy") : i18nText("Connect & deploy")}
        </ActionButton>
      </div>

      <Dialog open={confirmOff} onClose={() => setConfirmOff(false)} title={i18nText("Disconnect repository?")}>
        <p className="text-sm text-muted">{i18nText("This removes your custom modules from the instance and restarts it. Your databases and data are not affected. You can reconnect any time.")}</p>
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setConfirmOff(false)} disabled={saving}>{i18nText("Cancel")}</Button>
          <ActionButton variant="danger" loading={saving} loadingText={i18nText("Disconnecting\u2026")} onClick={disconnect}>{i18nText("Disconnect")}</ActionButton>
        </div>
      </Dialog>
    </Card>
  );
}

/** Python dependencies are no longer a manual list — they come from the
 *  repository's requirements.txt, validated before every deploy. This card
 *  just explains the mechanism and points to where failures are reported. */
function RequirementsInfo({ instance }: { instance: ApiInstance }) {
  const historyHref =
    instance.environment === "production"
      ? `/my/instances/${instance.id}/environments`
      : instance.parent_id
        ? `/my/instances/${instance.parent_id}/environments?env=${instance.id}`
        : `/my/instances/${instance.id}/environments`;
  return (
    <Card className="mt-6 p-5">
      <div className="flex items-center gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-info/15 text-info">
          <Package className="size-4" />
        </span>
        <div>
          <h2 className="font-semibold">{i18nText("Python dependencies")}</h2>
          <p className="text-xs text-muted">{i18nText("Managed from your repository's ")}<code className="font-mono">{"requirements.txt"}</code>{i18nText(" — no manual list.")}</p>
        </div>
      </div>

      <p className="mt-4 text-sm text-muted">{i18nText("Add a ")}<code className="rounded-sm bg-foreground/6 px-1 py-0.5 font-mono text-xs">{"requirements.txt"}</code>{i18nText(" to the root of your connected branch. On every deploy we install it in an isolated check ")}<strong className="text-foreground">{i18nText("before")}</strong>{i18nText(" touching your live instance.")}</p>
      <ul className="mt-4 space-y-2 text-sm text-muted">
        <li className="flex items-start gap-2">
          <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />{i18nText("Validated on a throwaway environment first — a broken dependency never reaches your instance.")}</li>
        <li className="flex items-start gap-2">
          <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />{i18nText("If validation fails, your code is not deployed and your instance keeps running.")}</li>
        <li className="flex items-start gap-2">
          <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />{i18nText("The deployment status — and the exact pip error — appear in")}{" "}
          <a href={historyHref} className="font-medium text-primary underline-offset-2 hover:underline">{i18nText("Deployment history")}</a>
          .
        </li>
      </ul>
    </Card>
  );
}
