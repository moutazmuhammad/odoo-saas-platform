import * as React from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Server, ChevronDown, ChevronRight, Search, GitBranch, FolderOpen } from "lucide-react";
import { api, type ApiInstance, type ProjectEnvironments } from "@/lib/api";
import { cn } from "@/lib/utils";

const projectLink = (p: ApiInstance) =>
  p.is_hosting ? `/my/instances/${p.id}/environments` : `/my/instances/${p.id}`;

/** Google Cloud-style project picker in the top bar: shows the current
 *  project as a header, opens a searchable dropdown of all projects, and
 *  lists the environments inside the active project for quick jumps. */
export function ProjectSwitcher({ className }: { className?: string }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [open, setOpen] = React.useState(false);
  const [projects, setProjects] = React.useState<ApiInstance[]>([]);
  const [envs, setEnvs] = React.useState<ProjectEnvironments | null>(null);
  const [query, setQuery] = React.useState("");
  const ref = React.useRef<HTMLDivElement>(null);

  const m = pathname.match(/\/my\/instances\/(\d+)/);
  const currentId = m ? Number(m[1]) : null;
  const current = projects.find((p) => p.id === currentId) || null;

  React.useEffect(() => {
    if (!open) return;
    setQuery("");
    api.instances().then(setProjects).catch(() => setProjects([]));
  }, [open]);

  React.useEffect(() => {
    if (open && current && current.is_hosting) {
      api.environments(current.id).then(setEnvs).catch(() => setEnvs(null));
    } else {
      setEnvs(null);
    }
  }, [open, current?.id]);

  React.useEffect(() => setOpen(false), [pathname]);

  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const q = query.trim().toLowerCase();
  const filtered = q
    ? projects.filter(
        (p) => p.name.toLowerCase().includes(q) || (p.region || "").toLowerCase().includes(q),
      )
    : projects;

  const go = (to: string) => {
    setOpen(false);
    navigate(to);
  };

  const envList = envs ? [envs.production, ...envs.environments] : [];

  return (
    <div ref={ref} className={cn("relative", className)}>
      <button
        onClick={() => setOpen((o) => !o)}
        className={cn(
          "flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-sm text-foreground transition-colors hover:bg-background",
          open && "bg-background",
        )}
      >
        <Server className="size-4 text-muted" />
        <span className="max-w-[10rem] truncate font-medium">{current ? current.name : "Select a project"}</span>
        <ChevronDown className="size-3.5 text-muted" />
      </button>

      {open && (
        <div className="absolute left-0 z-50 mt-2 w-[22rem] overflow-hidden rounded-lg border border-border bg-card shadow-2xl animate-fade-in">
          <div className="border-b border-border p-3">
            <p className="mb-2 text-sm font-medium">Select a project</p>
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
              <input
                autoFocus
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search projects"
                className="h-9 w-full rounded-md border border-border bg-background pl-9 pr-3 text-sm outline-none ring-primary/40 focus:ring-1"
              />
            </div>
          </div>

          {/* Environments inside the active project */}
          {current && envList.length > 0 && (
            <div className="border-b border-border p-2">
              <p className="px-2 py-1 text-[11px] font-semibold uppercase tracking-wide text-muted">
                {current.name} · environments
              </p>
              <div className="max-h-40 overflow-y-auto">
                {envList.map((e) => (
                  <button
                    key={e.id}
                    onClick={() => go(`/my/instances/${current.id}/environments?env=${e.id}`)}
                    className="flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left text-sm transition-colors hover:bg-foreground/[0.06]"
                  >
                    <GitBranch className="size-3.5 shrink-0 text-muted" />
                    <span className="truncate">{e.name}</span>
                    <span className="ml-auto shrink-0 text-xs text-muted">{e.environment_label}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* All projects */}
          <div className="max-h-72 overflow-y-auto p-2">
            <p className="px-2 py-1 text-[11px] font-semibold uppercase tracking-wide text-muted">
              {q ? "Results" : "All projects"}
            </p>
            {filtered.length === 0 ? (
              <p className="px-2 py-6 text-center text-sm text-muted">No projects found.</p>
            ) : (
              filtered.map((p) => (
                <button
                  key={p.id}
                  onClick={() => go(projectLink(p))}
                  className={cn(
                    "flex w-full items-center gap-3 rounded-md px-2 py-2 text-left text-sm transition-colors hover:bg-foreground/[0.06]",
                    p.id === currentId && "bg-primary/[0.06]",
                  )}
                >
                  <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
                    <FolderOpen className="size-4" />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium text-foreground">{p.name}</span>
                    <span className="block truncate text-xs text-muted">
                      {p.is_hosting ? "Hosting" : "Service"}
                      {p.region ? ` · ${p.region}` : ""}
                    </span>
                  </span>
                </button>
              ))
            )}
          </div>

          <div className="border-t border-border p-2">
            <button
              onClick={() => go("/my/instances")}
              className="flex w-full items-center justify-between rounded-md px-2 py-2 text-left text-sm font-medium text-primary transition-colors hover:bg-foreground/[0.06]"
            >
              View all projects
              <ChevronRight className="size-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
