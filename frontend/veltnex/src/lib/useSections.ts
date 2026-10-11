import * as React from "react";
import { api } from "./api";

/**
 * Which public sections are enabled (Settings → Website → Show
 * Hosting Section). Backed by /saas/api/v1/meta → sections.
 *
 * Cached module-wide so the nav, home page and route guards share one
 * fetch. Defaults to on while loading and on any error, so a slow
 * or failed request never hides everything.
 */
export interface Sections {
  hosting: boolean;
}

const DEFAULT: Sections = { hosting: true };

let cache: Sections | null = null;
let inflight: Promise<Sections> | null = null;

export function fetchSections(): Promise<Sections> {
  if (cache) return Promise.resolve(cache);
  if (!inflight) {
    inflight = api
      .meta()
      .then((m) => {
        cache = {
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
