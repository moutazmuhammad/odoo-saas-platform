import type { ApiInstance } from "./api";

export function groupProjects(instances: ApiInstance[]) {
  const projects = [...new Map(instances.filter(project => !project.parent_id).map(project => [project.id, project])).values()];
  const owned = projects.filter(project => project.is_owned_project ?? project.is_project_owner ?? false);
  const shared = projects.filter(project => !(project.is_owned_project ?? project.is_project_owner ?? false));
  return { projects, owned, shared };
}
