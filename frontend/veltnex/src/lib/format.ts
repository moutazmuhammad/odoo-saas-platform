export function formatDate(value: string | Date) {
  const d = typeof value === "string" ? new Date(value) : value;
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function formatDateTime(value: string | Date) {
  const d = typeof value === "string" ? new Date(value) : value;
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatBytes(gb: number) {
  if (gb >= 1024) return `${(gb / 1024).toFixed(1)} TB`;
  return `${gb} GB`;
}

/**
 * Recommended-users sizing hint as a light → heavy range, e.g. "~12–20".
 * `min`/`max` are the per-worker factors tuned in Settings ("Users / worker:
 * light → heavy"); the range collapses to a single number when they're equal.
 */
export function recommendedUsers(workers: number, min: number, max: number) {
  const low = workers * min;
  const high = workers * max;
  return high > low ? `~${low}–${high}` : `~${low}`;
}

/** Human-readable size from a value in megabytes (KB / MB / GB). */
export function formatSizeMb(mb: number) {
  if (!mb || mb <= 0) return "—";
  if (mb < 1) return `${Math.max(1, Math.round(mb * 1024))} KB`;
  if (mb < 1024) return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(2)} GB`;
}
