import * as React from "react";
import { usePolling } from "@/hooks/usePolling";
import { Activity } from "lucide-react";
import { api, type MetricsHistory, type MetricSample } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

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
  const [loading, setLoading] = React.useState(true);
  // Live current reading (DigitalOcean-style): polled frequently, and the poll
  // itself marks the instance "watched" so the background sampler keeps
  // measuring it while this view is open.
  const [live, setLive] = React.useState<{ cpu: number; ram: number; at: string } | null>(null);

  // History: load on range change, then auto-refresh on an interval matched to
  // the window so new points stream into the charts without a manual reload.
  const loadHistory = React.useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.instanceMetricsHistory(instanceId, range, accessToken));
    } finally {
      setLoading(false);
    }
  }, [instanceId, range, accessToken]);
  usePolling(loadHistory, {
    interval: HISTORY_REFRESH_MS[range] ?? 60000, immediate: true,
  });

  // Live poll every 5s — keeps the instance watched + drives the live readout.
  const loadLive = React.useCallback(async () => {
    setLive(await api.instanceMetrics(instanceId, accessToken));
  }, [instanceId, accessToken]);
  usePolling(loadLive, { interval: 5000, immediate: true });

  const samples = data?.samples ?? [];
  const lastSample = samples[samples.length - 1];
  const cpuNow = live?.cpu ?? lastSample?.cpu ?? 0;
  const ramNow = live?.ram ?? lastSample?.ram ?? 0;
  const storagePct = lastSample?.storage_pct ?? 0;
  const storageMb = lastSample?.storage_mb ?? 0;

  // Time window for the charts: plot x by REAL time so "now" is always the
  // right edge and sparse/old samples sit at their true position (not stretched
  // across the width). End at now (or the latest sample, if the clock's ahead).
  const hours = data?.hours ?? RANGE_HOURS[range] ?? 24;
  const lastMs = lastSample ? new Date(lastSample.t).getTime() : Date.now();
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
          storage_mb: storageMb,
          storage_pct: storagePct,
        },
      ]
    : samples;

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <Activity className="size-4 text-primary" />
          Performance
          {live && (
            <span className="inline-flex items-center gap-1 rounded-full bg-success/10 px-1.5 py-0.5 text-[10px] font-medium text-success">
              <span className="size-1.5 rounded-full bg-success animate-pulse-soft" />
              Live
            </span>
          )}
          <span className="text-xs font-normal text-muted">
            · retained {data?.retention_days ?? 14} days
          </span>
        </div>
        <div className="inline-flex rounded-lg border border-border bg-background p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.key}
              onClick={() => setRange(r.key)}
              className={cn(
                "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                range === r.key
                  ? "bg-card text-foreground shadow-sm"
                  : "text-muted hover:text-foreground",
              )}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {/* Live readout — current CPU / Memory / Disk, refreshed every few secs. */}
      <div className="mt-4 grid grid-cols-3 gap-3">
        <LiveStat label="CPU" pct={cpuNow} color="#4c8dff" live={!!live} />
        <LiveStat label="Memory" pct={ramNow} color="#a142f4" live={!!live} />
        <LiveStat label="Disk" pct={storagePct} sub={`${storageMb.toFixed(0)} MB`} color="#12b886" />
      </div>

      {loading ? (
        <div className="mt-6 h-48 animate-pulse rounded-lg bg-background" />
      ) : chartSamples.length < 2 ? (
        <div className="mt-6 flex h-48 flex-col items-center justify-center gap-1 text-sm text-muted">
          <Activity className="size-6 opacity-40" />
          No performance data yet for this window.
          <span className="text-xs">Samples are recorded every few minutes.</span>
        </div>
      ) : (
        <div className="mt-4 grid gap-5">
          <AreaChart
            label="CPU"
            unit="% of plan"
            color="#4c8dff"
            samples={chartSamples}
            pick={(s) => s.cpu}
            startMs={startMs}
            endMs={endMs}
          />
          <AreaChart
            label="Memory"
            unit="% of plan"
            color="#a142f4"
            samples={chartSamples}
            pick={(s) => s.ram}
            startMs={startMs}
            endMs={endMs}
          />
          <AreaChart
            label="Storage"
            unit="MB"
            color="#12b886"
            samples={chartSamples}
            pick={(s) => s.storage_mb}
            startMs={startMs}
            endMs={endMs}
          />
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
  startMs,
  endMs,
}: {
  label: string;
  unit: string;
  color: string;
  samples: MetricSample[];
  pick: (s: MetricSample) => number;
  startMs: number;
  endMs: number;
}) {
  const W = 600;
  const H = 120;
  const PAD = 4;
  const isPct = unit !== "MB";
  const span = Math.max(1, endMs - startMs);
  const tMs = (s: MetricSample) => new Date(s.t).getTime();
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
        <span className="font-medium text-foreground">{label}</span>
        <span className="text-muted">
          now <span className="font-semibold tabular-nums text-foreground">{fmt(last)}</span>
          <span className="mx-1.5 opacity-40">·</span>peak{" "}
          <span className="tabular-nums">{fmt(peak)}</span>
        </span>
      </div>
      <div className="flex gap-1.5">
        {/* y-axis scale labels (auto-scaled so small values are readable) */}
        <div className="flex w-8 shrink-0 flex-col justify-between py-0.5 text-right text-[9px] tabular-nums text-muted">
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
            style={{ left: `${((new Date(ht).getTime() - startMs) / span) * 100}%` }}
          >
            <div className="font-semibold tabular-nums">{fmt(hv)}</div>
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
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
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
            title="Live"
            className="size-1.5 rounded-full bg-success animate-pulse-soft"
          />
        )}
      </div>
      <div className="mt-1 flex items-baseline gap-0.5">
        <span className="text-2xl font-semibold tabular-nums">{pct.toFixed(0)}</span>
        <span className="text-sm text-muted">%</span>
        {sub && <span className="ml-auto text-[11px] text-muted">{sub}</span>}
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
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
