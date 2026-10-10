import type { ApiInstance } from "./api";

export function groupProjects(instances: ApiInstance[]) {
  const projects = [...new Map(instances.filter(project => !project.parent_id).map(project => [project.id, project])).values()];
  const owned = projects.filter(project => project.is_owned_project ?? project.is_project_owner ?? false);
  const shared = projects.filter(project => !(project.is_owned_project ?? project.is_project_owner ?? false));
  return { projects, owned, shared };
}

const CANCELLED_STATES = new Set(["cancelled", "cancelled_by_client"]);

/**
 * A project shows only its subscription: Active or Cancelled. Whether a
 * database is actually reachable is shown on each environment instead.
 */
export function projectStatus(project: Pick<ApiInstance, "state">): "active" | "cancelled" {
  return CANCELLED_STATES.has(project.state) ? "cancelled" : "active";
}
