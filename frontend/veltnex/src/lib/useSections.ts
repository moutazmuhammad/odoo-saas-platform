import * as React from "react";
import { api } from "./api";

/**
 * Which public sections are enabled (Settings → Website → Show
 * Hosting/Services Section). Backed by /saas/api/v1/meta → sections.
 *
 * Cached module-wide so the nav, home page and route guards share one
 * fetch. Defaults to both-on while loading and on any error, so a slow
 * or failed request never hides everything.
 */
export interface Sections {
  services: boolean;
  hosting: boolean;
}

const DEFAULT: Sections = { services: true, hosting: true };

let cache: Sections | null = null;
let inflight: Promise<Sections> | null = null;

export function fetchSections(): Promise<Sections> {
  if (cache) return Promise.resolve(cache);
  if (!inflight) {
    inflight = api
      .meta()
      .then((m) => {
        cache = {
          services: m?.sections?.services !== false,
          hosting: m?.sections?.hosting !== false,
        };
        return cache;
      })
      .catch(() => DEFAULT);
  }
  return inflight;
}

export function useSections(): Sections {
  const [sections, setSections] = React.useState<Sections>(cache || DEFAULT);
  React.useEffect(() => {
    let alive = true;
    fetchSections().then((s) => {
      if (alive) setSections(s);
    });
    return () => {
      alive = false;
    };
  }, []);
  return sections;
}
