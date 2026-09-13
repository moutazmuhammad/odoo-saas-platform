import * as React from "react";
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
      setError(e instanceof ApiError ? e.message : "Instance not found.");
    }
  }, [instanceId, params]);

  React.useEffect(() => {
    load();
  }, [load]);

  // Code & packages is a hosting-only (self-managed) feature. Managed
  // services have no code access — bounce back to the instance overview.
  React.useEffect(() => {
    if (instance && !instance.is_hosting) {
      navigate(`/my/instances/${id}`, { replace: true });
    }
  }, [instance, id, navigate]);

  if (error) {
    return (
      <EmptyState className="mt-10" icon={GitBranch} title="Unavailable" description={error} />
    );
  }
  if (!instance) {
    return (
      <div className="mt-20 flex justify-center"><Spinner size="lg" label="Loading…" /></div>
    );
  }

  const canDeploy = instance.state === "running" || instance.state === "stopped";

  return (
    <div className="animate-fade-in">

      {!embedded && (
        <div>
          <h1 className="text-2xl font-bold tracking-tight">
            Code &amp; packages
            <HelpHint anchor="repo" className="ml-1.5" />
          </h1>
          <p className="mt-1 text-sm text-muted">
            Connect your Git modules. Python dependencies come from your
            repository's <code>requirements.txt</code>. Applying a change pulls
            your code and restarts the instance (brief downtime).
          </p>
        </div>
      )}

      {!canDeploy && (
        <AlertBanner
          className="mt-6"
          variant="warning"
          title="Can't deploy right now"
          description="The instance must be running or stopped to apply code or package changes."
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
      toast.success("Deploying…", "Pulling your repository and restarting the instance.");
      onDeployed();
    } catch (e) {
      toast.error("Couldn't deploy", e instanceof ApiError ? e.message : "Please try again.");
      setSaving(false);
    }
  };

  const disconnect = async () => {
    setSaving(true);
    try {
      await api.setRepo(instance.id, { repo_url: "", repo_branch: "main" });
      toast.success("Disconnecting…", "Removing the repository and restarting the instance.");
      onDeployed();
    } catch (e) {
      toast.error("Couldn't disconnect", e instanceof ApiError ? e.message : "Please try again.");
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
            <h2 className="font-semibold">Git repository</h2>
            <p className="text-xs text-muted">
              Inherited from the project. The repository and token are managed
              once on the Production environment.
            </p>
          </div>
          <Button
            variant="secondary"
            onClick={() => navigate(`/my/instances/${instance.parent_id}/code`)}
          >
            Manage on project
          </Button>
        </div>
        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <div>
            <Label>Repository</Label>
            <p className="mt-1 truncate font-mono text-sm text-muted">{repo.url || "—"}</p>
          </div>
          <div>
            <Label>Branch</Label>
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
          <h2 className="font-semibold">Git repository</h2>
          <p className="text-xs text-muted">
            {connected
              ? "Your custom modules are deployed from this repository."
              : "Deploy your own Odoo modules from GitHub, GitLab or Bitbucket."}
          </p>
        </div>
        {connected && (
          <Button variant="secondary" disabled={disabled || saving} onClick={() => setConfirmOff(true)}>
            <Unplug className="size-4" />
            <span className="hidden sm:inline">Disconnect</span>
          </Button>
        )}
      </div>

      <div className="mt-5 space-y-4">
        <div>
          <Label htmlFor="repo-url">Repository URL</Label>
          <Input id="repo-url" value={url} onChange={(e) => setUrl(e.target.value)}
                 placeholder="https://github.com/you/your-odoo-modules.git" />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <Label htmlFor="repo-branch">Branch</Label>
            <Input id="repo-branch" value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="main" />
          </div>
          <div>
            <Label htmlFor="repo-token">
              Access token {repo.has_token && <span className="text-muted">(set — blank keeps it)</span>}
            </Label>
            <Input id="repo-token" type="password" value={token} onChange={(e) => setToken(e.target.value)}
                   placeholder={repo.has_token ? "••••••••" : "for private repositories"} />
          </div>
        </div>
      </div>

      <div className="mt-5 flex justify-end">
        <ActionButton loading={saving} loadingText="Deploying…" disabled={disabled || !url.trim()} onClick={save}>
          {connected ? "Save & redeploy" : "Connect & deploy"}
        </ActionButton>
      </div>

      <Dialog open={confirmOff} onClose={() => setConfirmOff(false)} title="Disconnect repository?">
        <p className="text-sm text-muted">
          This removes your custom modules from the instance and restarts it. Your databases and
          data are not affected. You can reconnect any time.
        </p>
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setConfirmOff(false)} disabled={saving}>Cancel</Button>
          <ActionButton variant="danger" loading={saving} loadingText="Disconnecting…" onClick={disconnect}>
            Disconnect
          </ActionButton>
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
          <h2 className="font-semibold">Python dependencies</h2>
          <p className="text-xs text-muted">
            Managed from your repository's <code className="font-mono">requirements.txt</code> — no manual list.
          </p>
        </div>
      </div>

      <p className="mt-4 text-sm text-muted">
        Add a <code className="rounded bg-foreground/[0.06] px-1 py-0.5 font-mono text-xs">requirements.txt</code> to the
        root of your connected branch. On every deploy we install it in an
        isolated check <strong className="text-foreground">before</strong> touching your live instance.
      </p>
      <ul className="mt-4 space-y-2 text-sm text-muted">
        <li className="flex items-start gap-2">
          <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />
          Validated on a throwaway environment first — a broken dependency never reaches your instance.
        </li>
        <li className="flex items-start gap-2">
          <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />
          If it fails, your code is <strong className="text-foreground">not</strong> deployed and your instance keeps running.
        </li>
        <li className="flex items-start gap-2">
          <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />
          The deployment status — and the exact pip error — appear in{" "}
          <a href={historyHref} className="font-medium text-primary underline-offset-2 hover:underline">
            Deployment history
          </a>
          .
        </li>
      </ul>
    </Card>
  );
}
