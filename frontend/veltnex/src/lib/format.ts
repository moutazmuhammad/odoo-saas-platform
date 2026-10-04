/** Odoo stores timestamps in UTC; older responses omit the timezone. */
export function parseDate(value: string | Date): Date {
  if (value instanceof Date) return value;
  const text = value.trim();
  // A calendar date (invoice/due date) must stay on that local calendar day.
  const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (day) return new Date(Number(day[1]), Number(day[2]) - 1, Number(day[3]));
  const timestamp = text.replace(" ", "T");
  const naive = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(timestamp);
  return new Date(naive ? `${timestamp}Z` : timestamp);
}

export function formatDate(value: string | Date) {
  const d = parseDate(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function formatDateTime(value: string | Date) {
  const d = parseDate(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
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
