import { i18nText } from "@/i18n";
import { Sun, Moon } from "lucide-react";
import { useTheme } from "@/hooks/useTheme";

/**
 * Theme toggle button. Shows the icon of the theme you'd switch TO
 * (sun in dark mode, moon in light mode) — the same pattern Vercel,
 * GitHub, etc. use, which reads as "click to switch to <icon>".
 */
export function ThemeToggle({ className = "" }: { className?: string }) {
  const { theme, toggle } = useTheme();
  const label = theme === "dark" ? i18nText("Switch to light mode") : i18nText("Switch to dark mode");
  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={label}
      title={label}
      className={
        "inline-flex size-9 items-center justify-center rounded-md border border-border bg-card text-muted transition-colors hover:bg-card hover:text-foreground focus-ring " +
        className
      }
    >
      {theme === "dark" ? (
        <Sun className="size-4" />
      ) : (
        <Moon className="size-4" />
      )}
    </button>
  );
}
