import { getLocale } from "@/i18n";
import { i18nText } from "@/i18n";
import * as React from "react";
import { usePolling } from "@/hooks/usePolling";
import { Activity } from "lucide-react";
import { api, type LiveMetrics, type MetricsHistory, type MetricSample, type PackageSummary } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { parseDate, formatDateTime } from "@/lib/format";

const RANGES: { key: string; label: string }[] = [
  { key: "1h", label: "1h" },
  { key: "6h", label: "6h" },
  { key: "24h", label: "24h" },
  { key: "7d", label: "7d" },
  { key: "14d", label: "14d" },
];

// How often the history charts auto-refresh per window — tighter for short
// windows so the graph visibly streams (DigitalOcean-style), looser for long
// ones where each point covers minutes/hours anyway.
const HISTORY_REFRESH_MS: Record<string, number> = {
  "1h": 15000,
  "6h": 30000,
  "24h": 60000,
  "7d": 120000,
  "14d": 300000,
};

const RANGE_HOURS: Record<string, number> = {
  "1h": 1,
  "6h": 6,
  "24h": 24,
  "7d": 24 * 7,
  "14d": 24 * 14,
};

/**
 * Odoo.sh-style per-customer performance history: CPU / RAM (% of plan) and
 * storage over a selectable window, up to the 14-day retention. Data comes from
 * the tenant-isolated /metrics/history endpoint (only the owner's instance).
 * Dependency-free SVG charts so the bundle stays lean.
 */
export function PerformanceHistory({
  instanceId,
  accessToken,
}: {
  instanceId: number;
  accessToken?: string;
}) {
  const [range, setRange] = React.useState("1h");
  const [data, setData] = React.useState<MetricsHistory | null>(null);
  // Range the shown data belongs to. The placeholder is only for a first
  // load or a range switch: background refreshes swap the data in place,
  // so the page height (and the reader's scroll position) never jumps.
  const [dataRange, setDataRange] = React.useState<string | null>(null);
  const loading = dataRange !== range;
  // Live current reading (DigitalOcean-style): polled frequently, and the poll
  // itself marks the instance "watched" so the background sampler keeps
  // measuring it while this view is open.
  const [live, setLive] = React.useState<LiveMetrics | null>(null);

  // History: load on range change, then auto-refresh on an interval matched to
  // the window so new points stream into the charts without a manual reload.
  const loadHistory = React.useCallback(async () => {
    try {
      setData(await api.instanceMetricsHistory(instanceId, range, accessToken));
    } finally {
      setDataRange(range);
    }
  }, [instanceId, range, accessToken]);
  usePolling(loadHistory, {
    interval: HISTORY_REFRESH_MS[range] ?? 60000, immediate: true,
  });

  // Live poll every 5s (Prometheus, cached server-side) — drives the live readout.
  const loadLive = React.useCallback(async () => {
    const m = await api.instanceMetrics(instanceId, accessToken);
    setLive(m.available ? m : null);
  }, [instanceId, accessToken]);
  usePolling(loadLive, { interval: 5000, immediate: true });

  const samples = data?.samples ?? [];
  const lastSample = samples[samples.length - 1];
  const now = live ?? lastSample;
  const cpuNow = now?.cpu ?? 0;
  const ramNow = now?.ram ?? 0;
  const pkg = data?.package && "cpu_cores" in data.package ? (data.package as PackageSummary) : null;
  const split = (odoo?: number, db?: number) =>
    odoo == null && db == null ? undefined : i18nText("Odoo {0}% · DB {1}%", [(odoo ?? 0).toFixed(0), (db ?? 0).toFixed(0)]);
  const storagePct = lastSample?.storage_pct ?? 0;
  const storageMb = lastSample?.storage_mb ?? 0;

  // Time window for the charts: plot x by REAL time so "now" is always the
  // right edge and sparse/old samples sit at their true position (not stretched
  // across the width). End at now (or the latest sample, if the clock's ahead).
  const hours = data?.hours ?? RANGE_HOURS[range] ?? 24;
  const lastMs = lastSample ? parseDate(lastSample.t).getTime() : Date.now();
  const endMs = Math.max(Date.now(), lastMs);
  const startMs = endMs - hours * 3600 * 1000;

  // Mirror the live cards into the charts: append the current reading as the
  // newest point (at "now") so the big graph's right edge advances every poll,
  // exactly in step with the small cards above.
  const chartSamples: MetricSample[] = live
    ? [
        ...samples,
        {
          t: new Date(endMs).toISOString(),
          cpu: live.cpu,
          ram: live.ram,
          odoo_cpu: live.odoo_cpu,
          db_cpu: live.db_cpu,
          odoo_ram: live.odoo_ram,
          db_ram: live.db_ram,
          storage_mb: storageMb,
          storage_pct: storagePct,
        },
      ]
    : samples;

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <Activity className="size-4 text-primary" />{i18nText("Performance")}{live && (
            <span className="inline-flex items-center gap-1 rounded-full bg-success/10 px-1.5 py-0.5 text-[10px] font-medium text-success">
              <span className="size-1.5 rounded-full bg-success animate-pulse-soft" />{i18nText("Live")}</span>
          )}
          <span className="text-xs font-normal text-muted">{i18nText("· retained ")}{data?.retention_days ?? 14}{i18nText(" days")}</span>
        </div>
        <div className="inline-flex rounded-lg border border-border bg-background p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.key}
              onClick={() => setRange(r.key)}
              className={cn(
                "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                range === r.key
                  ? "bg-card text-foreground shadow-xs"
                  : "text-muted hover:text-foreground",
              )}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {pkg && (
        <p className="mt-2 text-xs text-muted">{i18nText("Your package: ")}{pkg.workers}{i18nText(" worker")}{pkg.workers === 1 ? "" : "s"}
          {pkg.tier ? ` · ${pkg.tier}` : ""} · {pkg.storage_gb}{i18nText(" GB storage, including the managed PostgreSQL database.")}</p>
      )}

      {/* Live readout — current CPU / Memory / Disk, refreshed every few secs. */}
      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <LiveStat label="CPU" pct={cpuNow} sub={split(now?.odoo_cpu, now?.db_cpu)} color="#4c8dff" live={!!live} />
        <LiveStat label={i18nText("Memory")} pct={ramNow} sub={split(now?.odoo_ram, now?.db_ram)} color="#a142f4" live={!!live} />
        <LiveStat
          label={i18nText("Disk")}
          pct={pkg ? pkg.storage_pct : storagePct}
          sub={pkg ? `${formatMb(pkg.used_mb)} of ${pkg.storage_gb} GB` : `${storageMb.toFixed(0)} MB`}
          color="#12b886"
        />
      </div>

      {loading ? (
        <div className="mt-6 h-48 animate-pulse rounded-lg bg-background" />
      ) : chartSamples.length < 2 ? (
        <div className="mt-6 flex h-48 flex-col items-center justify-center gap-1 text-sm text-muted">
          <Activity className="size-6 opacity-40" />{i18nText("No performance data yet for this window.")}<span className="text-xs">{i18nText("Samples are recorded every few minutes.")}</span>
        </div>
      ) : (
        <div className="mt-4 grid gap-5">
          <AreaChart
            label="CPU"
            unit={i18nText("% of plan")}
            color="#4c8dff"
            samples={chartSamples}
            pick={(s) => s.cpu}
            pickDb={(s) => s.db_cpu}
            startMs={startMs}
            endMs={endMs}
          />
          <AreaChart
            label={i18nText("Memory")}
            unit={i18nText("% of plan")}
            color="#a142f4"
            samples={chartSamples}
            pick={(s) => s.ram}
            pickDb={(s) => s.db_ram}
            startMs={startMs}
            endMs={endMs}
          />
          {pkg ? (
            <StorageBreakdown pkg={pkg} />
          ) : (
            <AreaChart
              label={i18nText("Storage")}
              unit="MB"
              color="#12b886"
              samples={chartSamples}
              pick={(s) => s.storage_mb}
              startMs={startMs}
              endMs={endMs}
            />
          )}
        </div>
      )}
    </Card>
  );
}

function AreaChart({
  label,
  unit,
  color,
  samples,
  pick,
  pickDb,
  startMs,
  endMs,
}: {
  label: string;
  unit: string;
  color: string;
  samples: MetricSample[];
  pick: (s: MetricSample) => number;
  /** The database's share of the total, drawn as a band at the bottom. */
  pickDb?: (s: MetricSample) => number | undefined;
  startMs: number;
  endMs: number;
}) {
  const W = 600;
  const H = 120;
  const PAD = 4;
  const isPct = unit !== "MB";
  const span = Math.max(1, endMs - startMs);
  const tMs = (s: MetricSample) => parseDate(s.t).getTime();
  const values = samples.map(pick);
  const dataMax = Math.max(...values, 0);
  // Auto-scale the y-axis to the data (DigitalOcean-style) so small CPU values
  // are actually visible — rounded up to a "nice" ceiling, never a flat line
  // pinned to 0–100%.
  const max = isPct ? nicePctCeil(dataMax) : Math.max(dataMax * 1.15, 1);
  const axisLabel = (v: number) => (isPct ? `${v.toFixed(0)}%` : `${v.toFixed(0)}M`);
  const [hover, setHover] = React.useState<number | null>(null);

  // x is positioned by REAL time within [startMs, endMs] → "now" is the right
  // edge and gaps in sampling read as gaps, not stretched lines.
  const x = (ms: number) => ((ms - startMs) / span) * (W - PAD * 2) + PAD;
  const y = (v: number) => H - PAD - (Math.min(v, max) / max) * (H - PAD * 2);

  const pts = samples.map((s, i) => ({ px: x(tMs(s)), py: y(values[i]) }));
  const line = pts.map((p) => `${p.px.toFixed(1)},${p.py.toFixed(1)}`).join(" ");
  const area = pts.length
    ? `${pts[0].px.toFixed(1)},${H - PAD} ${line} ${pts[pts.length - 1].px.toFixed(1)},${H - PAD}`
    : "";
  const dbValues = pickDb ? samples.map((s) => pickDb(s) ?? 0) : null;
  const hasDb = !!dbValues && dbValues.some((v) => v > 0);
  const dbLine = hasDb
    ? samples.map((s, i) => `${x(tMs(s)).toFixed(1)},${y(dbValues![i]).toFixed(1)}`).join(" ")
    : "";
  const dbArea = hasDb && pts.length
    ? `${pts[0].px.toFixed(1)},${H - PAD} ${dbLine} ${pts[pts.length - 1].px.toFixed(1)},${H - PAD}`
    : "";
  const gid = `grad-${label}`;

  const last = values[values.length - 1] ?? 0;
  const peak = values.length ? Math.max(...values) : 0;
  const fmt = (v: number) => (unit === "MB" ? `${v.toFixed(0)} MB` : `${v.toFixed(0)}%`);

  const hv = hover != null ? values[hover] : null;
  const ht = hover != null ? samples[hover].t : null;
  const ticks = [0, 1 / 3, 2 / 3, 1].map((f) => startMs + f * span);

  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between text-xs">
        <span className="flex items-center gap-2 font-medium text-foreground">
          {label}
          {hasDb && (
            <span className="flex items-center gap-2 font-normal text-muted">
              <span className="inline-flex items-center gap-1">
                <span className="size-2 rounded-sm" style={{ backgroundColor: color, opacity: 0.5 }} />{i18nText("Odoo")}</span>
              <span className="inline-flex items-center gap-1">
                <span className="size-2 rounded-sm" style={{ backgroundColor: DB_COLOR }} />{i18nText("Database")}</span>
            </span>
          )}
        </span>
        <span className="text-muted">{i18nText("now ")}<span className="font-semibold tabular-nums text-foreground">{fmt(last)}</span>
          <span className="mx-1.5 opacity-40">·</span>{i18nText("peak")}{" "}
          <span className="tabular-nums">{fmt(peak)}</span>
        </span>
      </div>
      <div className="flex gap-1.5">
        {/* y-axis scale labels (auto-scaled so small values are readable) */}
        <div className="flex w-8 shrink-0 flex-col justify-between py-0.5 text-end text-[9px] tabular-nums text-muted">
          <span>{axisLabel(max)}</span>
          <span>{axisLabel(max / 2)}</span>
          <span>{axisLabel(0)}</span>
        </div>
        <div className="min-w-0 flex-1">
        <div className="relative">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          className="h-28 w-full rounded-lg bg-background"
          onMouseLeave={() => setHover(null)}
          onMouseMove={(e) => {
            const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
            const relMs = startMs + ((e.clientX - r.left) / r.width) * span;
            let best = 0;
            let bestD = Infinity;
            samples.forEach((s, i) => {
              const d = Math.abs(tMs(s) - relMs);
              if (d < bestD) {
                bestD = d;
                best = i;
              }
            });
            setHover(samples.length ? best : null);
          }}
        >
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity="0.28" />
              <stop offset="100%" stopColor={color} stopOpacity="0.02" />
            </linearGradient>
          </defs>
          {[0.25, 0.5, 0.75].map((g) => (
            <line
              key={g}
              x1={PAD}
              x2={W - PAD}
              y1={H * g}
              y2={H * g}
              stroke="currentColor"
              className="text-border"
              strokeWidth="0.5"
              strokeDasharray="3 3"
            />
          ))}
          <polygon points={area} fill={`url(#${gid})`} />
          {hasDb && <polygon points={dbArea} fill={DB_COLOR} fillOpacity="0.35" />}
          <polyline
            points={line}
            fill="none"
            stroke={color}
            strokeWidth="1.5"
            vectorEffect="non-scaling-stroke"
          />
          {hover != null && (
            <line
              x1={pts[hover].px}
              x2={pts[hover].px}
              y1={PAD}
              y2={H - PAD}
              stroke={color}
              strokeWidth="1"
              vectorEffect="non-scaling-stroke"
              opacity="0.5"
            />
          )}
          {hover != null && (
            <circle cx={pts[hover].px} cy={pts[hover].py} r="2.5" fill={color} vectorEffect="non-scaling-stroke" />
          )}
        </svg>
        {hv != null && ht && (
          <div
            className="pointer-events-none absolute -top-1 z-10 -translate-x-1/2 -translate-y-full whitespace-nowrap rounded-md border border-border bg-card px-2 py-1 text-[11px] shadow-lg"
            style={{ left: `${((parseDate(ht).getTime() - startMs) / span) * 100}%` }}
          >
            <div className="font-semibold tabular-nums">{fmt(hv)}</div>
            {hasDb && hover != null && (
              <div className="tabular-nums text-muted">{i18nText("Odoo ")}{fmt(Math.max(0, hv - dbValues![hover]))}{i18nText(" · DB ")}{fmt(dbValues![hover])}
              </div>
            )}
            <div className="text-muted">{formatTime(ht)}</div>
          </div>
        )}
      </div>
        {/* x-axis time labels — make it obvious the right edge is "now". */}
        <div className="mt-1 flex justify-between text-[10px] tabular-nums text-muted">
          {ticks.map((t, i) => (
            <span key={i}>{formatAxis(t, span)}</span>
          ))}
        </div>
        </div>
      </div>
    </div>
  );
}

/** Storage as measured for the limit: files + databases out of the package. */
function StorageBreakdown({ pkg }: { pkg: PackageSummary }) {
  const totalMb = Math.max(1, pkg.storage_gb * 1024);
  const pct = (mb: number) => Math.min(100, (mb / totalMb) * 100);
  const freeMb = Math.max(0, totalMb - pkg.used_mb);
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between text-xs">
        <span className="font-medium text-foreground">{i18nText("Storage")}</span>
        <span className="text-muted">
          <span className="font-semibold tabular-nums text-foreground">{formatMb(pkg.used_mb)}</span>{i18nText(" of")}{" "}
          {pkg.storage_gb}{i18nText(" GB")}{pkg.measured_at && <span className="ms-1.5 opacity-70">{i18nText("· measured ")}{formatTime(pkg.measured_at + "Z")}</span>}
        </span>
      </div>
      <div className="flex h-3 overflow-hidden rounded-full bg-border">
        <div style={{ width: `${pct(pkg.files_mb)}%`, backgroundColor: "#12b886" }} />
        <div style={{ width: `${pct(pkg.databases_mb)}%`, backgroundColor: DB_COLOR }} />
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
        <span className="inline-flex items-center gap-1">
          <span className="size-2 rounded-sm" style={{ backgroundColor: "#12b886" }} />{i18nText("Files ")}{formatMb(pkg.files_mb)}
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="size-2 rounded-sm" style={{ backgroundColor: DB_COLOR }} />{i18nText("Databases ")}{formatMb(pkg.databases_mb)}
        </span>
        <span>{i18nText("Free ")}{formatMb(freeMb)}</span>
      </div>
    </div>
  );
}

// The database band in the stacked CPU / Memory charts.
const DB_COLOR = "#f59f00";

function formatMb(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(mb % 1024 === 0 ? 0 : 1)} GB` : `${mb.toFixed(0)} MB`;
}

// Round a percentage up to a readable axis ceiling so a tiny CPU signal still
// fills the chart with a labelled scale (e.g. 8% → 10, 39% → 50, 72% → 100).
function nicePctCeil(v: number): number {
  if (v <= 5) return 5;
  if (v <= 10) return 10;
  if (v <= 25) return 25;
  if (v <= 50) return 50;
  return 100;
}

// Axis tick: clock for windows ≤ 24h, date for longer ones.
function formatAxis(ms: number, span: number): string {
  const d = new Date(ms);
  if (span <= 24 * 3600 * 1000) {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString(getLocale(), { month: "short", day: "numeric" });
}

/** A single live metric (DigitalOcean-style): big current value + a usage bar,
 *  with a pulsing dot while the value is being polled live. */
function LiveStat({
  label,
  pct,
  sub,
  color,
  live,
}: {
  label: string;
  pct: number;
  sub?: string;
  color: string;
  live?: boolean;
}) {
  const width = Math.max(0, Math.min(100, pct));
  return (
    <div className="rounded-lg border border-border bg-background/60 p-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-muted">{label}</span>
        {live && (
          <span
            title={i18nText("Live")}
            className="size-1.5 rounded-full bg-success animate-pulse-soft"
          />
        )}
      </div>
      <div className="mt-1 flex items-baseline gap-0.5">
        <span className="text-2xl font-semibold tabular-nums">{pct.toFixed(0)}</span>
        <span className="text-sm text-muted">%</span>
        {sub && <span className="ms-auto text-[11px] text-muted">{sub}</span>}
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{ width: `${width}%`, backgroundColor: color }}
        />
      </div>
    </div>
  );
}

function formatTime(iso: string): string {
  const d = parseDate(iso);
  if (isNaN(d.getTime())) return iso;
  return formatDateTime(d);
}
