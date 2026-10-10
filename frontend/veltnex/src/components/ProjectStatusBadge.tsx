import type { ApiInstance } from "@/lib/api";
import { projectStatus } from "@/lib/projects";
import { StatusBadge } from "./StatusBadge";

/** Subscription state of a whole project (Active / Cancelled), never availability. */
export function ProjectStatusBadge({ project }: { project: Pick<ApiInstance, "state"> }) {
  return <StatusBadge status={projectStatus(project)} />;
}
