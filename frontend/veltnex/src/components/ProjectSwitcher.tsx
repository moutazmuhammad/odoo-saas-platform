import { i18nText } from "@/i18n";
import * as React from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Server, ChevronDown, ChevronRight, Search, FolderOpen } from "lucide-react";
import { type ApiInstance } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useInstances } from "@/context/InstancesContext";
import { groupProjects } from "@/lib/projects";

const projectLink = (p: ApiInstance) =>
  p.is_hosting ? `/my/instances/${p.id}/environments` : `/my/instances/${p.id}`;

/** Google Cloud-style project picker in the top bar: shows the current
 *  project as a header and groups searchable root projects by ownership. */
export function ProjectSwitcher({ className }: { className?: string }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [open, setOpen] = React.useState(false);
  const { instances, loading, error } = useInstances();
  const { projects } = groupProjects(instances);
  const [query, setQuery] = React.useState("");
  const ref = React.useRef<HTMLDivElement>(null);

  const m = pathname.match(/\/my\/instances\/(\d+)/);
  const currentId = m ? Number(m[1]) : null;
  const current = projects.find((p) => p.id === currentId) || null;

  React.useEffect(() => { setOpen(false); setQuery(""); }, [pathname]);

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

  const groups = groupProjects(filtered);
  const categories = [{ label: i18nText("My projects"), projects: groups.owned }, { label: i18nText("Shared projects"), projects: groups.shared }];

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
        <span className="max-w-40 truncate font-medium">{current ? current.name : i18nText("Select a project")}</span>
        <ChevronDown className="size-3.5 text-muted" />
      </button>

      {open && (
        <div className="absolute start-0 z-50 mt-2 w-88 overflow-hidden rounded-lg border border-border bg-card shadow-2xl animate-fade-in">
          <div className="border-b border-border p-3">
            <p className="mb-2 text-sm font-medium">{i18nText("Select a project")}</p>
            <div className="relative">
              <Search className="pointer-events-none absolute start-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
              <input
                autoFocus
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={i18nText("Search projects")}
                className="h-9 w-full rounded-md border border-border bg-background ps-9 pe-3 text-sm outline-hidden ring-primary/40 focus:ring-1"
              />
            </div>
          </div>

          {/* All projects */}
          <div className="max-h-72 overflow-y-auto p-2">
            {error && <p className="px-2 py-3 text-sm text-danger">{error}</p>}
            {loading && projects.length === 0 ? <p className="px-2 py-6 text-center text-sm text-muted">{i18nText("Loading projects…")}</p> : filtered.length === 0 ? (
              <p className="px-2 py-6 text-center text-sm text-muted">{i18nText("No projects found.")}</p>
            ) : categories.map(category => <section key={category.label} aria-label={category.label} className="mb-2 last:mb-0">
              <p className="px-2 py-1 text-[11px] font-semibold uppercase tracking-wide text-muted">{category.label}</p>
              {category.projects.length === 0 && <p className="px-2 py-2 text-xs text-muted">{i18nText("No projects")}</p>}
              {category.projects.map((p) => (
                <button
                  key={p.id}
                  onClick={() => go(projectLink(p))}
                  className={cn(
                    "flex w-full items-center gap-3 rounded-md px-2 py-2 text-start text-sm transition-colors hover:bg-foreground/6",
                    p.id === currentId && "bg-primary/6",
                  )}
                >
                  <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
                    <FolderOpen className="size-4" />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium text-foreground">{p.name}</span>
                    <span className="block truncate text-xs text-muted">
                      {p.is_hosting ? i18nText("Hosting") : i18nText("Service")}
                      {p.region ? ` · ${p.region}` : ""}
                    </span>
                  </span>
                </button>
              ))}
            </section>)}
          </div>

          <div className="border-t border-border p-2">
            <button
              onClick={() => go("/my/instances")}
              className="flex w-full items-center justify-between rounded-md px-2 py-2 text-start text-sm font-medium text-primary transition-colors hover:bg-foreground/6"
            >{i18nText("View all projects")}<ChevronRight className="size-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
