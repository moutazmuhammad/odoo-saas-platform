import * as React from "react";
import { ApiError } from "@/lib/api";

interface PollOptions {
  /** Base interval in ms. */
  interval: number;
  /** Poll only while true (e.g. an operation is in progress). Default true. */
  enabled?: boolean;
  /** Run once immediately on (re)enable, before the first interval. */
  immediate?: boolean;
  /** Back-off ceiling in ms. Default 60s. */
  maxInterval?: number;
}

/**
 * Resilient polling (PERF-008 / UX-006). Replaces ad-hoc `setInterval`:
 *  - **exponential back-off** on error (reset to base on success), so a flaky
 *    or overloaded backend isn't hammered;
 *  - **pauses while the tab is hidden** (no work for background tabs) and
 *    resumes immediately on focus;
 *  - **stops permanently on `auth_required`** — the session is gone, so polling
 *    would just 401 forever (AuthContext handles the redirect).
 *
 * The callback is read from a ref, so it always sees the latest closure without
 * resetting the timer; the timer only resets when `enabled`/`interval` change.
 */
export function usePolling(
  callback: () => void | Promise<void>,
  { interval, enabled = true, immediate = false, maxInterval = 60000 }: PollOptions,
) {
  const cbRef = React.useRef(callback);
  cbRef.current = callback;

  React.useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let current = interval;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = (ms: number) => {
      if (!stopped) timer = setTimeout(tick, ms);
    };

    const tick = async () => {
      // Don't poll a backgrounded tab; re-check at the base interval.
      if (typeof document !== "undefined" && document.hidden) {
        schedule(interval);
        return;
      }
      try {
        await cbRef.current();
        current = interval; // success resets back-off
      } catch (e) {
        if (e instanceof ApiError && e.code === "auth_required") {
          stopped = true; // session gone — AuthContext redirects; stop polling
          return;
        }
        current = Math.min(current * 2, maxInterval); // back off on error
      }
      schedule(current);
    };

    if (immediate) tick();
    else schedule(interval);

    const onVisible = () => {
      if (!document.hidden && !stopped) {
        clearTimeout(timer);
        tick(); // refresh right away when the tab is focused again
      }
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, interval, maxInterval, immediate]);
}
