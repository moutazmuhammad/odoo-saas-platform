import * as React from "react";
import { Canvas, useFrame, useLoader } from "@react-three/fiber";
import { TextureLoader } from "three";
import * as THREE from "three";

/**
 * Stylized 3D globe rendered with three.js + react-three-fiber.
 *
 * Instead of mapping a photo of Earth onto a sphere, we sample an
 * earth texture once on the CPU to find which points are land, then
 * render those positions as glowing dots in the brand's primary-glow
 * color. The result reads as a clean "3D illustration" of a globe —
 * continents visible as a dotted constellation, oceans negative space —
 * which sits naturally on the dark-blue theme.
 *
 * Layers (front-to-back):
 *   1. Atmospheric halo  (additive blue sphere, slightly larger)
 *   2. Continent dots    (Points where land was detected)
 *   3. Lat/lon wireframe (thin grid sphere, subtle)
 *   4. Solid dark sphere (occludes the dots on the back hemisphere)
 *
 * The whole globe rotates continuously around its tilted Y axis.
 */

export interface GlobeProps {
  /** Extra Tailwind classes applied to the square wrapper. */
  className?: string;
  /** Rotation speed (radians per second). Default 0.18 — slow & cinematic. */
  speed?: number;
  /** If true, accept pointer events. Off by default for background use. */
  interactive?: boolean;
  /** Earth axial tilt in radians (slight tilt looks more dimensional). */
  tilt?: number;
}

// Vite exposes the configured base path; in production the SPA is
// served from `/saas_website/static/spa/`, so we resolve relative to
// it instead of hardcoding a root-relative URL.
const EARTH_TEXTURE_URL = `${import.meta.env.BASE_URL}textures/earth.jpg`;

// Brand palette in three.js Color form. Keep in sync with
// tailwind.config theme.colors.primary.glow / .DEFAULT.
const PRIMARY_GLOW = new THREE.Color(0x3656b8);
const PRIMARY = new THREE.Color(0x203c86);
const GRID_COLOR = new THREE.Color(0x2f4faa);

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

/** Number of candidate sample points on the sphere. Higher = denser
 *  continents but more vertices on the GPU. 12k is a good balance. */
const SAMPLE_COUNT = 12000;

/**
 * Build a BufferGeometry of points positioned on a unit sphere wherever
 * the source texture indicates land. Uses a Fibonacci spiral for an
 * even distribution and a simple warm-vs-cool pixel test against
 * earth_atmos (continents = warm RGB, oceans = blue).
 */
function buildContinentGeometry(image: HTMLImageElement): THREE.BufferGeometry {
  const canvas = document.createElement("canvas");
  canvas.width = image.width;
  canvas.height = image.height;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) return new THREE.BufferGeometry();

  ctx.drawImage(image, 0, 0);
  const data = ctx.getImageData(0, 0, image.width, image.height).data;

  const positions: number[] = [];
  const goldenAngle = Math.PI * (Math.sqrt(5) - 1);

  for (let i = 0; i < SAMPLE_COUNT; i++) {
    // Fibonacci sphere: uniform distribution without polar clustering.
    const y = 1 - (i / (SAMPLE_COUNT - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const theta = goldenAngle * i;
    const x = Math.cos(theta) * r;
    const z = Math.sin(theta) * r;

    // Equirectangular UV from sphere position.
    const u = 0.5 + Math.atan2(z, x) / (2 * Math.PI);
    const v = 0.5 - Math.asin(y) / Math.PI;

    const px = Math.min(image.width - 1, Math.floor(u * image.width));
    const py = Math.min(image.height - 1, Math.floor(v * image.height));
    const idx = (py * image.width + px) * 4;
    const red = data[idx];
    const green = data[idx + 1];
    const blue = data[idx + 2];

    // Land tends to be "warmer" (more red+green) than ocean (mostly blue).
    if (red + green > blue + 30) {
      positions.push(x, y, z);
    }
  }

  const geo = new THREE.BufferGeometry();
  geo.setAttribute(
    "position",
    new THREE.Float32BufferAttribute(positions, 3),
  );
  return geo;
}

function ContinentDots({ speed }: { speed: number }) {
  const groupRef = React.useRef<THREE.Group>(null);
  const texture = useLoader(TextureLoader, EARTH_TEXTURE_URL);
  const theme = useThemeColor();

  // Build the points geometry once, when the texture image becomes
  // available. Memoized so re-renders don't re-sample the texture.
  const geometry = React.useMemo(
    () => buildContinentGeometry(texture.image as HTMLImageElement),
    [texture],
  );

  // Dispose the GPU buffer when the component unmounts to avoid leaks
  // across SPA navigations.
  React.useEffect(() => () => geometry.dispose(), [geometry]);

  useFrame((_, delta) => {
    if (groupRef.current) groupRef.current.rotation.y += delta * speed;
  });

  // In light mode the dark occluder sphere would look like a black
  // ball on a white page; use a near-white base so the globe reads as
  // a soft, illustrated planet instead.
  const baseColor = theme === "dark" ? 0x0c0d12 : 0xf4f4f5;
  // Dots get a small color shift: brighter glow in dark, deeper primary
  // in light so they have enough contrast against the lighter base.
  const dotColor = theme === "dark" ? PRIMARY_GLOW : PRIMARY;
  const wireOpacity = theme === "dark" ? 0.12 : 0.18;

  return (
    <group ref={groupRef} rotation={[0.35, 0, 0]}>
      {/* Solid occluder sphere just inside the dot radius — hides the
          back hemisphere so we don't see continents through the globe. */}
      <mesh>
        <sphereGeometry args={[0.985, 64, 64]} />
        <meshBasicMaterial color={baseColor} />
      </mesh>

      {/* Continent dots */}
      <points geometry={geometry}>
        <pointsMaterial
          size={0.018}
          color={dotColor}
          sizeAttenuation
          transparent
          opacity={0.95}
          depthWrite={false}
        />
      </points>

      {/* Subtle lat/lon wireframe overlay */}
      <mesh>
        <sphereGeometry args={[1.001, 24, 18]} />
        <meshBasicMaterial
          color={GRID_COLOR}
          wireframe
          transparent
          opacity={wireOpacity}
          depthWrite={false}
        />
      </mesh>
    </group>
  );
}

export function Globe({
  className = "",
  speed = 0.18,
  interactive = false,
}: GlobeProps) {
  return (
    <div
      className={`relative aspect-square w-full ${className}`}
      style={{ pointerEvents: interactive ? "auto" : "none" }}
    >
      <Canvas
        // Camera pulled back so the sphere + atmosphere halo (radius
        // 1.08) sit comfortably inside the square canvas with a margin
        // on every side. Too close → sphere overflows and gets clipped
        // to the canvas rectangle, which reads as "globe in a square".
        camera={{ position: [0, 0, 3.4], fov: 38 }}
        gl={{ alpha: true, antialias: true }}
        onCreated={({ gl }) => gl.setClearColor(0x000000, 0)}
        dpr={[1, 2]}
      >
        <React.Suspense fallback={null}>
          <ContinentDots speed={speed} />
        </React.Suspense>
      </Canvas>
    </div>
  );
}
