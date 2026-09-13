import * as React from "react";
import { useNavigate } from "react-router-dom";
import { Server, Plus, Search, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState } from "@/components/EmptyState";
import { AlertBanner } from "@/components/AlertBanner";
import { PageHeader } from "@/components/PageHeader";
import { DataTable, type Column } from "@/components/DataTable";
import { useInstances } from "@/context/InstancesContext";
import { useSections } from "@/lib/useSections";
import { formatBytes, formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { ApiInstance } from "@/lib/api";

const projectLink = (i: ApiInstance) =>
  i.is_hosting ? `/my/instances/${i.id}/environments` : `/my/instances/${i.id}`;

export default function Instances() {
  const { instances, loading, error } = useInstances();
  const navigate = useNavigate();
  const sections = useSections();
  // Send "Create instance" to whichever offering is live.
  const createTo = sections.hosting ? "/hosting" : "/services";
  const [query, setQuery] = React.useState("");
  const [onlyRunning, setOnlyRunning] = React.useState(false);

  const runningTotal = instances.filter((i) => i.state === "running").length;

  const filtered = instances.filter((i) => {
    const q = query.toLowerCase();
    const matchesQuery =
      i.name.toLowerCase().includes(q) || (i.region || "").toLowerCase().includes(q);
    return matchesQuery && (!onlyRunning || i.state === "running");
  });

  const columns: Column<ApiInstance>[] = [
    {
      key: "name",
      header: "Name",
      sortValue: (i) => i.name.toLowerCase(),
      render: (i) => (
        <div className="flex items-center gap-3">
          <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Server className="size-4" />
          </span>
          <div className="min-w-0">
            <p className="truncate font-medium text-foreground">{i.name}</p>
            <p className="truncate font-mono text-xs text-muted">{i.domain}</p>
          </div>
        </div>
      ),
    },
    { key: "type", header: "Type", hideBelow: "sm", sortValue: (i) => (i.is_hosting ? 0 : 1), className: "text-muted", render: (i) => (i.is_hosting ? "Hosting" : "Service") },
    { key: "region", header: "Region", hideBelow: "md", className: "text-muted", render: (i) => i.region || "—" },
    { key: "size", header: "Size", hideBelow: "lg", className: "text-muted", render: (i) => `${i.workers} workers · ${formatBytes(i.storage_gb)}` },
    { key: "status", header: "Status", sortValue: (i) => i.state, render: (i) => <StatusBadge status={i.state} label={i.state_label} /> },
    { key: "created", header: "Created", hideBelow: "md", className: "text-muted", sortValue: (i) => i.created, render: (i) => (i.created ? formatDate(i.created) : "—") },
    { key: "go", header: "", align: "right", width: "44px", render: () => <ChevronRight className="size-4 text-muted" /> },
  ];

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="Projects"
        subtitle="Manage and monitor all your Odoo projects."
        actions={
          <Button onClick={() => navigate(createTo)}>
            <Plus className="size-4" />
            Create project
          </Button>
        }
      />

      {error && <AlertBanner className="mt-6" variant="danger" title="Couldn't load projects" description={error} />}

      <DataTable<ApiInstance>
        className="mt-2"
        loading={loading && instances.length === 0}
        rows={filtered}
        getKey={(i) => i.id}
        onRowClick={(i) => navigate(projectLink(i))}
        initialSortKey="name"
        toolbar={
          <>
            <div className="relative w-full max-w-xs">
              <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
              <Input
                className="h-9 pl-9"
                placeholder="Search projects…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <div className="ml-auto inline-flex rounded-md border border-border p-0.5">
              {([
                { key: false, label: "All" },
                { key: true, label: `Running (${runningTotal})` },
              ] as const).map((opt) => (
                <button
                  key={String(opt.key)}
                  onClick={() => setOnlyRunning(opt.key)}
                  className={cn(
                    "rounded px-3 py-1 text-sm font-medium transition-colors",
                    onlyRunning === opt.key ? "bg-primary/15 text-primary" : "text-muted hover:text-foreground",
                  )}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </>
        }
        emptyState={
          <EmptyState
            className="m-0 py-14"
            icon={Server}
            title={query || onlyRunning ? "No matching projects" : "No projects yet"}
            description={
              query || onlyRunning
                ? "Try a different search term or filter."
                : "Create your first project to deploy an Odoo environment."
            }
            action={!query && !onlyRunning && <Button onClick={() => navigate(createTo)}>Create project</Button>}
          />
        }
        columns={columns}
      />
    </div>
  );
}
