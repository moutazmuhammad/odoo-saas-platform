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
  const { isAuthenticated } = useAuth();
  const [instances, setInstances] = React.useState<ApiInstance[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const reload = React.useCallback(async () => {
    if (!isAuthenticated) {
      setInstances([]);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await api.instances();
      setInstances(data);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load your instances.");
    } finally {
      setLoading(false);
    }
  }, [isAuthenticated]);

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

  // Poll status for any transitional/running instances so usage + state
  // stay live. Running instances refresh more slowly than provisioning.
  const _hasTransitional = instances.some((i) => TRANSITIONAL.has(i.state));
  const _shouldPoll =
    isAuthenticated &&
    (_hasTransitional || instances.some((i) => i.state === "running"));
  usePolling(
    async () => {
      const targets = instances.filter(
        (i) => TRANSITIONAL.has(i.state) || i.state === "running"
      );
      await Promise.all(
        targets.map(async (inst) => {
          const s = await api.instanceStatus(inst.id);
          setInstances((prev) =>
            prev.map((i) =>
              i.id === inst.id
                ? { ...i, state: s.state, state_label: s.state_label, url: s.url || i.url, usage: s.usage || i.usage }
                : i
            )
          );
        })
      );
    },
    // Faster cadence while something is provisioning.
    { interval: _hasTransitional ? 4000 : 12000, enabled: _shouldPoll }
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
          i.id === id ? { ...i, state: status.state, state_label: status.state_label } : i
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
