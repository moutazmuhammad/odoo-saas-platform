import { i18nText } from "@/i18n";
import * as React from "react";
import { api, ApiError, type ApiInstance } from "@/lib/api";
import { useAuth } from "./AuthContext";
import { usePolling } from "@/hooks/usePolling";

// States that are mid-transition — we poll these so the UI moves to
// running/stopped on its own without a manual refresh.
const TRANSITIONAL = new Set([
  "provisioning",
  "pending_provision",
  "paid",
  "pending_payment",
]);

const ACTIVE_POLL_MS = 12_000;
const IDLE_POLL_MS = 60_000;
const IDLE = new Set(["stopped", "suspended", "failed"]);

/** Status-poll cadence for an instance in ms, or null when it isn't polled. */
export function pollIntervalFor(i: Pick<ApiInstance, "state" | "runtime_state">): number | null {
  if (TRANSITIONAL.has(i.state)) return 0; // every tick (4s)
  if (i.state === "running") return ACTIVE_POLL_MS;
  if (IDLE.has(i.state)) {
    return i.runtime_state === "starting" || i.runtime_state === "stopping" ? ACTIVE_POLL_MS : IDLE_POLL_MS;
  }
  return null;
}

interface InstancesContextValue {
  instances: ApiInstance[];
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
  getInstance: (id: number) => ApiInstance | undefined;
  /** Run a lifecycle action and refresh the affected instance. */
  runAction: (id: number, action: "start" | "stop" | "restart") => Promise<void>;
  /** Merge a freshly-fetched instance back into the list cache. */
  patch: (instance: ApiInstance) => void;
}

const InstancesContext = React.createContext<InstancesContextValue | null>(null);

export function InstancesProvider({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, user } = useAuth();
  const workspaceReady = isAuthenticated && !user?.must_change_password && !user?.must_verify_phone;
  const [instances, setInstances] = React.useState<ApiInstance[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const reload = React.useCallback(async () => {
    if (!workspaceReady) {
      setInstances([]);
      setError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await api.instances();
      setInstances(data);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : i18nText("Could not load your instances."));
    } finally {
      setLoading(false);
    }
  }, [workspaceReady]);

  React.useEffect(() => {
    reload();
  }, [reload]);

  const patch = React.useCallback((instance: ApiInstance) => {
    setInstances((prev) => {
      const exists = prev.some((i) => i.id === instance.id);
      return exists
        ? prev.map((i) => (i.id === instance.id ? { ...i, ...instance } : i))
        : [instance, ...prev];
    });
  }, []);

  // Poll status so usage + state stay live: running instances every 12s
  // (4s while something provisions); idle (stopped/suspended/failed) ones only
  // about once a minute unless they're mid start/stop.
  const lastPolled = React.useRef(new Map<number, number>());
  const _hasTransitional = instances.some((i) => TRANSITIONAL.has(i.state));
  const _shouldPoll = workspaceReady && instances.some((i) => pollIntervalFor(i) !== null);
  usePolling(
    async () => {
      const now = Date.now();
      const targets = instances.filter((i) => {
        const every = pollIntervalFor(i);
        // Slack so a target isn't skipped by a tick landing just short of it.
        return every !== null && now - (lastPolled.current.get(i.id) ?? 0) >= every - 1000;
      });
      await Promise.all(
        targets.map(async (inst) => {
          lastPolled.current.set(inst.id, now);
          const s = await api.instanceStatus(inst.id);
          setInstances((prev) =>
            prev.map((i) =>
              i.id === inst.id
                ? { ...i, ...s, url: s.url || i.url, usage: s.usage || i.usage }
                : i
            )
          );
        })
      );
    },
    // Faster cadence while something is provisioning.
    { interval: _hasTransitional ? 4000 : ACTIVE_POLL_MS, enabled: _shouldPoll }
  );

  const getInstance = React.useCallback(
    (id: number) => instances.find((i) => i.id === id),
    [instances]
  );

  const runAction = React.useCallback(
    async (id: number, action: "start" | "stop" | "restart") => {
      const status = await api.instanceAction(id, action);
      setInstances((prev) =>
        prev.map((i) =>
          i.id === id ? { ...i, ...status } : i
        )
      );
    },
    []
  );

  const value = React.useMemo<InstancesContextValue>(
    () => ({ instances, loading, error, reload, getInstance, runAction, patch }),
    [instances, loading, error, reload, getInstance, runAction, patch]
  );

  return (
    <InstancesContext.Provider value={value}>{children}</InstancesContext.Provider>
  );
}

export function useInstances() {
  const ctx = React.useContext(InstancesContext);
  if (!ctx) throw new Error("useInstances must be used within InstancesProvider");
  return ctx;
}
