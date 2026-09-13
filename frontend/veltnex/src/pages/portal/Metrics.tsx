import { useParams } from "react-router-dom";
import { PerformanceHistory } from "@/components/PerformanceHistory";

/** Dedicated metrics view — the per-instance performance history (14 days).
 *  Identity/header is provided by the shared InstanceLayout, or by the
 *  Environments workspace when embedded (`embedId`). */
export default function Metrics({ embedId }: { embedId?: number } = {}) {
  const { id = "" } = useParams();
  const instanceId = embedId != null ? embedId : Number(id);
  return (
    <div>
      <PerformanceHistory instanceId={instanceId} />
    </div>
  );
}
