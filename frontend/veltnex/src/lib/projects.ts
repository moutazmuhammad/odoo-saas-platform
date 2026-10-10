import type { ApiInstance, ProjectStatus } from "./api";

export function groupProjects(instances: ApiInstance[]) {
  const projects = [...new Map(instances.filter(project => !project.parent_id).map(project => [project.id, project])).values()];
  const owned = projects.filter(project => project.is_owned_project ?? project.is_project_owner ?? false);
  const shared = projects.filter(project => !(project.is_owned_project ?? project.is_project_owner ?? false));
  return { projects, owned, shared };
}

const CANCELLED_STATES = new Set(["cancelled", "cancelled_by_client"]);
const UNPAID_STATES = new Set(["draft", "pending_payment"]);

/**
 * A project shows only its subscription: Active, Unpaid, Suspended or
 * Cancelled. Stopped/offline belong to each server (production, staging,
 * dev), which shows its own availability. The server computes this (it
 * knows about overdue invoices); the state mapping is a fallback.
 */
export function projectStatus(project: Pick<ApiInstance, "state" | "project_status">): ProjectStatus {
  if (project.project_status) return project.project_status;
  if (CANCELLED_STATES.has(project.state)) return "cancelled";
  if (project.state === "suspended") return "suspended";
  if (UNPAID_STATES.has(project.state)) return "unpaid";
  return "active";
}
