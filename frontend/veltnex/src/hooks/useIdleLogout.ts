import * as React from "react";

export const IDLE_LOGOUT_MS = 24 * 60 * 60 * 1000;
export const IDLE_WARNING_MS = 60 * 1000;
export const ACTIVITY_STORAGE_KEY = "veltnex.session.lastActivity";
const ACTIVITY_EVENTS = ["mousedown", "mousemove", "keydown", "touchstart", "scroll"] as const;

/** Count browser activity across tabs, so an unattended tab cannot end an active session. */
export function useIdleLogout(enabled: boolean, onWarn: () => void, onIdle: () => void) {
  const warnTimer = React.useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const idleTimer = React.useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const lastActivity = React.useRef(0);
  const lastPublished = React.useRef(0);
  const onWarnRef = React.useRef(onWarn);
  const onIdleRef = React.useRef(onIdle);
  onWarnRef.current = onWarn;
  onIdleRef.current = onIdle;

  React.useEffect(() => {
    const clear = () => {
      if (warnTimer.current !== undefined) clearTimeout(warnTimer.current);
      if (idleTimer.current !== undefined) clearTimeout(idleTimer.current);
    };
    if (!enabled) { clear(); return; }

    const remaining = () => {
      try {
        const stored = Number(localStorage.getItem(ACTIVITY_STORAGE_KEY));
        if (Number.isFinite(stored) && stored <= Date.now()) lastActivity.current = Math.max(lastActivity.current, stored);
      } catch { /* Activity tracking still works when storage is unavailable. */ }
      return IDLE_LOGOUT_MS - (Date.now() - lastActivity.current);
    };
    const schedule = () => {
      clear();
      const timeLeft = remaining();
      warnTimer.current = setTimeout(() => {
        if (remaining() > IDLE_WARNING_MS) schedule();
        else onWarnRef.current();
      }, Math.max(0, timeLeft - IDLE_WARNING_MS));
      idleTimer.current = setTimeout(() => {
        // Recheck shared activity after background-tab timers resume.
        if (remaining() > 0) schedule();
        else onIdleRef.current();
      }, Math.max(0, timeLeft));
    };
    const activity = () => {
      const now = Date.now();
      lastActivity.current = now;
      if (!lastPublished.current || now - lastPublished.current >= 5000) {
        try { localStorage.setItem(ACTIVITY_STORAGE_KEY, String(now)); } catch { /* Storage may be disabled. */ }
        lastPublished.current = now;
      }
      schedule();
    };
    const sharedActivity = (event: StorageEvent) => {
      if (event.key !== ACTIVITY_STORAGE_KEY) return;
      const timestamp = Number(event.newValue);
      if (Number.isFinite(timestamp) && timestamp > lastActivity.current && timestamp <= Date.now()) {
        lastActivity.current = timestamp;
        schedule();
      }
    };
    activity();
    ACTIVITY_EVENTS.forEach(event => window.addEventListener(event, activity));
    window.addEventListener("storage", sharedActivity);
    return () => {
      ACTIVITY_EVENTS.forEach(event => window.removeEventListener(event, activity));
      window.removeEventListener("storage", sharedActivity);
      clear();
    };
  }, [enabled]);
}
