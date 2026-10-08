import * as React from "react";
import LAND from "./globe-land.json";

/**
 * Dotted 3D globe drawn on a plain 2D canvas.
 *
 * The continents come from Natural Earth's 110m land polygons, sampled
 * at build time onto an evenly spread (Fibonacci) set of points and shipped
 * as a tiny [lat, lon, lat, lon, ...] list. No texture decoding, no WebGL,
 * no three.js: the globe paints on the very first frame together with the
 * rest of the page.
 *
 * Each frame: rotate every land point around the (tilted) polar axis,
 * project orthographically, cull the back hemisphere, and draw a dot whose
 * size and opacity fade towards the limb so the sphere reads as round.
 */

export interface GlobeProps {
  /** Extra Tailwind classes applied to the square wrapper. */
  className?: string;
  /** Rotation speed (radians per second). Default 0.18 — slow & cinematic. */
  speed?: number;
  /** If true, accept pointer events. Off by default for background use. */
  interactive?: boolean;
  /** Axial tilt in radians (slight tilt looks more dimensional). */
  tilt?: number;
}

// Brand palette. Keep in sync with tailwind.config theme.colors.primary.
const DOT_DARK = [96, 134, 240];
const DOT_LIGHT = [32, 60, 134];
const GRID_DARK = "rgba(96, 134, 240, 0.16)";
const GRID_LIGHT = "rgba(32, 60, 134, 0.14)";

/** Unit vectors (x up-right-handed: y = north) for every land sample. */
const POINTS: Float32Array = (() => {
  const src = LAND as number[];
  const n = src.length / 2;
  const out = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    const lat = (src[i * 2] * Math.PI) / 180;
    const lon = (src[i * 2 + 1] * Math.PI) / 180;
    const c = Math.cos(lat);
    out[i * 3] = c * Math.cos(lon);
    out[i * 3 + 1] = Math.sin(lat);
    // Negative z puts east to the viewer's right.
    out[i * 3 + 2] = -c * Math.sin(lon);
  }
  return out;
})();

/** Read the active theme from the <html data-theme="..."> attribute the
 *  inline FOUC script sets before paint, falling back to dark. */
function readTheme(): "light" | "dark" {
  if (typeof document === "undefined") return "dark";
  return document.documentElement.getAttribute("data-theme") === "light"
    ? "light"
    : "dark";
}

function useThemeColor() {
  const [theme, setTheme] = React.useState<"light" | "dark">(readTheme);
  React.useEffect(() => {
    const obs = new MutationObserver(() => setTheme(readTheme()));
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-theme"],
    });
    return () => obs.disconnect();
  }, []);
  return theme;
}

export function Globe({
  className = "",
  speed = 0.18,
  interactive = false,
  tilt = 0.4,
}: GlobeProps) {
  const canvasRef = React.useRef<HTMLCanvasElement>(null);
  const theme = useThemeColor();

  React.useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dot = theme === "dark" ? DOT_DARK : DOT_LIGHT;
    const grid = theme === "dark" ? GRID_DARK : GRID_LIGHT;
    const base = theme === "dark" ? "#0c0d12" : "#f4f4f5";

    let size = 0;
    let dpr = 1;
    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      size = Math.max(1, Math.round(rect.width));
      dpr = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = size * dpr;
      canvas.height = size * dpr;
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    const cosT = Math.cos(tilt);
    const sinT = Math.sin(tilt);
    let angle = 0;
    let last = performance.now();
    let raf = 0;
    // Respect reduced-motion: draw once, don't spin.
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

    const draw = (now: number) => {
      const delta = Math.min(0.1, (now - last) / 1000);
      last = now;
      if (!still) angle -= delta * speed; // eastward spin, like the real thing

      const s = size * dpr;
      const cx = s / 2;
      const cy = s / 2;
      const R = s * 0.46; // sphere radius (leaves room for the halo)
      ctx.clearRect(0, 0, s, s);

      // Halo + sphere body.
      const halo = ctx.createRadialGradient(cx, cy, R * 0.92, cx, cy, R * 1.12);
      halo.addColorStop(0, `rgba(${dot[0]}, ${dot[1]}, ${dot[2]}, 0.28)`);
      halo.addColorStop(1, `rgba(${dot[0]}, ${dot[1]}, ${dot[2]}, 0)`);
      ctx.fillStyle = halo;
      ctx.beginPath();
      ctx.arc(cx, cy, R * 1.12, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = base;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.fill();

      // Subtle meridian/parallel grid.
      ctx.strokeStyle = grid;
      ctx.lineWidth = Math.max(1, dpr * 0.7);
      for (let i = 1; i < 6; i++) {
        const y = -1 + i / 3; // parallels at -60..60
        const ry = Math.sqrt(1 - y * y) * R;
        ctx.beginPath();
        ctx.ellipse(cx, cy - y * cosT * R, ry, Math.max(0.5, ry * Math.abs(sinT)), 0, 0, Math.PI * 2);
        ctx.stroke();
      }
      for (let i = 0; i < 6; i++) {
        const a = angle + (i * Math.PI) / 6;
        const rx = Math.abs(Math.cos(a)) * R;
        ctx.beginPath();
        ctx.ellipse(cx, cy, Math.max(0.5, rx), R, 0, 0, Math.PI * 2);
        ctx.stroke();
      }

      // Land dots.
      const cosA = Math.cos(angle);
      const sinA = Math.sin(angle);
      const dotR = Math.max(1, s * 0.0036);
      const n = POINTS.length / 3;
      for (let i = 0; i < n; i++) {
        const x0 = POINTS[i * 3];
        const y0 = POINTS[i * 3 + 1];
        const z0 = POINTS[i * 3 + 2];
        // Spin around the polar axis (east goes to the right when viewed
        // from the front), then tilt the axis towards the viewer.
        const x1 = x0 * cosA - z0 * sinA;
        const z1 = x0 * sinA + z0 * cosA;
        const y2 = y0 * cosT - z1 * sinT;
        const z2 = y0 * sinT + z1 * cosT;
        if (z2 <= 0.02) continue; // back hemisphere
        const alpha = 0.35 + 0.65 * z2;
        ctx.fillStyle = `rgba(${dot[0]}, ${dot[1]}, ${dot[2]}, ${alpha.toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(cx + x1 * R, cy - y2 * R, dotR * (0.6 + 0.4 * z2), 0, Math.PI * 2);
        ctx.fill();
      }

      // Limb shading for depth.
      const shade = ctx.createRadialGradient(cx - R * 0.3, cy - R * 0.3, R * 0.2, cx, cy, R);
      shade.addColorStop(0, "rgba(0, 0, 0, 0)");
      shade.addColorStop(1, theme === "dark" ? "rgba(0, 0, 0, 0.45)" : "rgba(32, 60, 134, 0.12)");
      ctx.fillStyle = shade;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.fill();

      if (!still) raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [theme, speed, tilt]);

  return (
    <div
      className={`relative aspect-square w-full ${className}`}
      style={{ pointerEvents: interactive ? "auto" : "none" }}
    >
      <canvas ref={canvasRef} className="block size-full" aria-hidden="true" />
    </div>
  );
}
