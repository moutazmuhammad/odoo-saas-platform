import * as React from "react";

// SEC-018: session validity was purely server-side (the Odoo session
// cookie's own lifetime) with no client-side idle cutoff at all — an
// authenticated tab left open and unattended on a shared/public machine
// stayed usable indefinitely. This adds a client-side idle timeout on
// top of that, not instead of it.
export const IDLE_LOGOUT_MS = 30 * 60 * 1000; // 30 minutes of no activity
export const IDLE_WARNING_MS = 60 * 1000; // warn this long before logging out

const ACTIVITY_EVENTS = ["mousedown", "mousemove", "keydown", "touchstart", "scroll"] as const;

/**
 * Calls `onWarn` once after (IDLE_LOGOUT_MS - IDLE_WARNING_MS) of no
 * activity, then `onIdle` after IDLE_LOGOUT_MS total, unless activity
 * resets the timers first. No-ops entirely while `enabled` is false, and
 * clears its own timers on unmount/disable.
 */
export function useIdleLogout(enabled: boolean, onWarn: () => void, onIdle: () => void) {
  const warnTimer = React.useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const idleTimer = React.useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Refs so a re-render with a new (but behaviorally-identical) callback
  // doesn't tear down and restart the idle timers.
  const onWarnRef = React.useRef(onWarn);
  const onIdleRef = React.useRef(onIdle);
  onWarnRef.current = onWarn;
  onIdleRef.current = onIdle;

  const clear = React.useCallback(() => {
    if (warnTimer.current) clearTimeout(warnTimer.current);
    if (idleTimer.current) clearTimeout(idleTimer.current);
  }, []);

  const reset = React.useCallback(() => {
    clear();
    warnTimer.current = setTimeout(() => {
      onWarnRef.current();
    }, IDLE_LOGOUT_MS - IDLE_WARNING_MS);
    idleTimer.current = setTimeout(() => {
      onIdleRef.current();
    }, IDLE_LOGOUT_MS);
  }, [clear]);

  React.useEffect(() => {
    if (!enabled) {
      clear();
      return;
    }
    reset();
    ACTIVITY_EVENTS.forEach((ev) => window.addEventListener(ev, reset));
    return () => {
      ACTIVITY_EVENTS.forEach((ev) => window.removeEventListener(ev, reset));
      clear();
    };
  }, [enabled, reset, clear]);
}
