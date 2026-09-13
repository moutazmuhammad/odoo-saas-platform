import * as React from "react";
import { Play, Database, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api, ApiError, type SqlResult } from "@/lib/api";

/**
 * Odoo.sh-style read-only SQL console for a hosted instance.
 *
 * The backend (`/saas/api/v1/instances/<id>/sql`) runs the statement
 * inside a `READ ONLY` transaction against the chosen database, so the
 * customer can explore any of the multiple databases on their server
 * without being able to mutate data. A SQL-level error (syntax, etc.)
 * comes back as an ApiError with code `sql_error` and is shown inline.
 */
export default function SqlConsole({ instanceId }: { instanceId: number }) {
  const [dbs, setDbs] = React.useState<string[]>([]);
  const [db, setDb] = React.useState("");
  const [loadingDbs, setLoadingDbs] = React.useState(true);
  const [query, setQuery] = React.useState(
    "SELECT id, name FROM res_partner ORDER BY id LIMIT 20;",
  );
  const [running, setRunning] = React.useState(false);
  const [result, setResult] = React.useState<SqlResult | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let live = true;
    setLoadingDbs(true);
    api
      .databases(instanceId)
      .then((d) => {
        if (!live) return;
        const names = (d.databases || []).map((x) => x.name);
        setDbs(names);
        setDb((cur) => cur || names[0] || "");
      })
      .catch(() => {})
      .finally(() => {
        if (live) setLoadingDbs(false);
      });
    return () => {
      live = false;
    };
  }, [instanceId]);

  const run = async () => {
    if (!db || !query.trim() || running) return;
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.sqlQuery(instanceId, db, query);
      setResult(r);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't run the query.");
    } finally {
      setRunning(false);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      run();
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Toolbar: DB picker + Run */}
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        <div className="relative">
          <Database className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <select
            value={db}
            onChange={(e) => setDb(e.target.value)}
            disabled={loadingDbs || !dbs.length}
            className="h-9 rounded-md border border-border bg-background pl-8 pr-3 text-sm outline-none focus:border-primary/50 disabled:opacity-60"
          >
            {dbs.length === 0 ? (
              <option value="">{loadingDbs ? "Loading…" : "No databases"}</option>
            ) : (
              dbs.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))
            )}
          </select>
        </div>
        <Button size="sm" onClick={run} disabled={running || !db || !query.trim()}>
          {running ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
          Run
        </Button>
        <span className="text-xs text-muted">Read-only · ⌘/Ctrl + Enter to run</span>
      </div>

      {/* SQL editor */}
      <textarea
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={onKeyDown}
        spellCheck={false}
        placeholder="SELECT …"
        className="mt-3 h-32 shrink-0 resize-none rounded-md border border-border bg-background/60 p-3 font-mono text-xs leading-relaxed outline-none focus:border-primary/50"
      />

      {/* Results */}
      <div className="mt-3 min-h-0 flex-1 overflow-auto rounded-md border border-border bg-background/60">
        {error ? (
          <div className="whitespace-pre-wrap p-3 font-mono text-xs text-danger">{error}</div>
        ) : result ? (
          result.columns.length === 0 ? (
            <div className="p-3 text-xs text-muted">Statement executed (no rows returned).</div>
          ) : (
            <table className="w-full border-collapse text-left font-mono text-xs">
              <thead className="sticky top-0 bg-card">
                <tr>
                  {result.columns.map((c) => (
                    <th
                      key={c}
                      className="border-b border-border px-3 py-1.5 font-semibold text-muted"
                    >
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {result.rows.map((row, i) => (
                  <tr key={i} className="hover:bg-border/30">
                    {row.map((cell, j) => (
                      <td key={j} className="border-b border-border/50 px-3 py-1 align-top">
                        {cell === null ? (
                          <span className="italic text-muted/60">null</span>
                        ) : (
                          String(cell)
                        )}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )
        ) : (
          <div className="p-3 text-xs text-muted">Run a query to see results.</div>
        )}
      </div>

      {result && result.columns.length > 0 && (
        <p className="mt-2 shrink-0 text-xs text-muted">
          {result.rows.length} row{result.rows.length === 1 ? "" : "s"}
          {result.truncated ? " (truncated)" : ""}
        </p>
      )}
    </div>
  );
}
