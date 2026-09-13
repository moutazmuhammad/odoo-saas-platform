import * as React from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "veltnex-theme";

function cookieTheme(): Theme | null {
  if (typeof document === "undefined") return null;
  const m = document.cookie.match(/(?:^|;\s*)veltnex-theme=(light|dark)\b/);
  return (m ? (m[1] as Theme) : null);
}

function getInitialTheme(): Theme {
  // The inline FOUC script in index.html has already set the theme on
  // <html> before React mounts; mirror that decision here so the React
  // state matches what the user sees from frame 0.
  //
  // We read the `data-theme` attribute (set unconditionally for both
  // themes) rather than the `.dark` class — that class is added in
  // dark mode but no `.light` class is added in light mode, so a
  // class-only check returns the wrong fallback in light mode and the
  // toggle button would render the wrong icon ("click to switch to
  // light" while already in light) — requiring a double-click to sync.
  if (typeof document !== "undefined") {
    const dt = document.documentElement.getAttribute("data-theme");
    if (dt === "light" || dt === "dark") return dt;
    const ck = cookieTheme();
    if (ck) return ck;
    if (document.documentElement.classList.contains("dark")) return "dark";
  }
  return "light";
}

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  // Mirror as a data-attribute too — QWeb pages and the DB manager
  // read it the same way, keeping one source of truth across the SPA
  // and the Odoo-rendered shell.
  root.setAttribute("data-theme", theme);
  // Keep the cookie in sync on every apply — covers initial mount
  // (where setTheme isn't called) and OS-preference changes. Without
  // this, a user whose only persistence is localStorage (e.g. set
  // before the cookie shipped) would never get the cookie written,
  // so the server-side QWeb render would default to dark on
  // navigation.
  document.cookie = `${STORAGE_KEY}=${theme}; path=/; max-age=31536000; SameSite=Lax`;
}

/**
 * Returns the active theme and a setter. Persists to localStorage and
 * applies the class to <html>. Listens for OS-level preference changes
 * only when the user hasn't picked an explicit theme.
 */
export function useTheme(): {
  theme: Theme;
  setTheme: (t: Theme) => void;
  toggle: () => void;
} {
  const [theme, setThemeState] = React.useState<Theme>(getInitialTheme);

  const setTheme = React.useCallback((t: Theme) => {
    try {
      localStorage.setItem(STORAGE_KEY, t);
    } catch {
      // localStorage may be disabled in private mode — apply anyway.
    }
    // applyTheme writes the cookie too — see its body.
    applyTheme(t);
    setThemeState(t);
  }, []);

  // On mount, mirror the resolved theme to the cookie so the next
  // QWeb / SPA navigation has a cookie for the server to read. This
  // handles the bootstrap case where localStorage already holds a
  // preference (set before the cookie shipped) but no cookie exists.
  React.useEffect(() => {
    applyTheme(theme);
    // We deliberately depend only on the initial `theme` — subsequent
    // updates flow through `setTheme` which already applies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggle = React.useCallback(() => {
    setTheme(theme === "dark" ? "light" : "dark");
  }, [theme, setTheme]);

  // Sync across tabs.
  React.useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY && (e.newValue === "dark" || e.newValue === "light")) {
        applyTheme(e.newValue);
        setThemeState(e.newValue);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  // Follow OS preference if the user hasn't expressed one.
  React.useEffect(() => {
    if (!window.matchMedia) return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (e: MediaQueryListEvent) => {
      try {
        if (localStorage.getItem(STORAGE_KEY)) return; // explicit choice wins
      } catch {
        // fall through
      }
      const next: Theme = e.matches ? "dark" : "light";
      applyTheme(next);
      setThemeState(next);
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return { theme, setTheme, toggle };
}
