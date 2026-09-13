import * as React from "react";
import { useNavigate } from "react-router-dom";
import {
  Search,
  Server,
  Receipt,
  Settings,
  LifeBuoy,
  Plus,
  CornerDownLeft,
  Box,
} from "lucide-react";
import { api, type ApiInstance } from "@/lib/api";
import { useSections } from "@/lib/useSections";
import { cn } from "@/lib/utils";

interface Cmd {
  id: string;
  label: string;
  hint?: string;
  icon: React.ComponentType<{ className?: string }>;
  run: () => void;
  group: "Go to" | "Actions" | "Projects";
}

/** Global ⌘K command palette: jump to any page or project, run common
 *  actions. The fastest path through the app at scale. */
export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const sections = useSections();
  const [query, setQuery] = React.useState("");
  const [projects, setProjects] = React.useState<ApiInstance[]>([]);
  const [active, setActive] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
    setTimeout(() => inputRef.current?.focus(), 20);
    api.instances().then(setProjects).catch(() => setProjects([]));
  }, [open]);

  const go = React.useCallback(
    (to: string) => {
      onClose();
      navigate(to);
    },
    [navigate, onClose],
  );

  const commands = React.useMemo<Cmd[]>(() => {
    const nav: Cmd[] = [
      { id: "projects", label: "Projects", icon: Server, group: "Go to", run: () => go("/my/instances") },
      { id: "billing", label: "Billing", icon: Receipt, group: "Go to", run: () => go("/my/billing") },
      { id: "settings", label: "Settings", icon: Settings, group: "Go to", run: () => go("/my/settings") },
      { id: "docs", label: "Documentation", icon: LifeBuoy, group: "Go to", run: () => go("/docs") },
    ];
    const actions: Cmd[] = [
      {
        id: "new-project",
        label: "New project",
        icon: Plus,
        group: "Actions",
        run: () => go(sections.hosting ? "/hosting" : "/services"),
      },
    ];
    const projectCmds: Cmd[] = projects.map((p) => ({
      id: `p-${p.id}`,
      label: p.name,
      hint: p.region || p.domain,
      icon: Box,
      group: "Projects",
      run: () => go(p.is_hosting ? `/my/instances/${p.id}/environments` : `/my/instances/${p.id}`),
    }));
    return [...nav, ...actions, ...projectCmds];
  }, [projects, sections.hosting, go]);

  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter(
      (c) => c.label.toLowerCase().includes(q) || c.hint?.toLowerCase().includes(q),
    );
  }, [commands, query]);

  React.useEffect(() => setActive(0), [query]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      filtered[active]?.run();
    } else if (e.key === "Escape") {
      onClose();
    }
  };

  if (!open) return null;

  // Group while preserving order.
  const groups: { name: Cmd["group"]; items: Cmd[] }[] = [];
  for (const c of filtered) {
    let g = groups.find((x) => x.name === c.group);
    if (!g) {
      g = { name: c.group, items: [] };
      groups.push(g);
    }
    g.items.push(c);
  }
  let idx = -1;

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center p-4 pt-[12vh]">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div
        role="dialog"
        aria-label="Command palette"
        className="relative w-full max-w-xl overflow-hidden rounded-xl border border-border bg-card shadow-2xl animate-fade-in"
        onKeyDown={onKeyDown}
      >
        <div className="flex items-center gap-2.5 border-b border-border px-4">
          <Search className="size-4 shrink-0 text-muted" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search projects, pages, actions…"
            className="h-12 w-full bg-transparent text-sm outline-none placeholder:text-muted"
          />
          <kbd className="hidden shrink-0 rounded border border-border px-1.5 py-0.5 text-[10px] text-muted sm:block">
            ESC
          </kbd>
        </div>

        <div className="max-h-[50vh] overflow-y-auto p-2">
          {filtered.length === 0 ? (
            <p className="px-3 py-6 text-center text-sm text-muted">No results.</p>
          ) : (
            groups.map((g) => (
              <div key={g.name} className="mb-1">
                <p className="px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted/70">
                  {g.name}
                </p>
                {g.items.map((c) => {
                  idx++;
                  const isActive = idx === active;
                  const myIdx = idx;
                  return (
                    <button
                      key={c.id}
                      onMouseEnter={() => setActive(myIdx)}
                      onClick={c.run}
                      className={cn(
                        "flex w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm transition-colors",
                        isActive ? "bg-primary/15 text-foreground" : "text-muted",
                      )}
                    >
                      <c.icon className="size-4 shrink-0" />
                      <span className="flex-1 truncate text-foreground">{c.label}</span>
                      {c.hint && <span className="truncate text-xs text-muted">{c.hint}</span>}
                      {isActive && <CornerDownLeft className="size-3.5 shrink-0 text-muted" />}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
