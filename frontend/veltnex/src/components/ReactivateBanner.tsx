import { getLocale, i18nText } from "@/i18n";
import * as React from "react";
import { RotateCw } from "lucide-react";
import { AlertBanner } from "@/components/AlertBanner";
import { Button } from "@/components/ui/button";
import { api, type ApiInstance } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { hasPermission } from "@/lib/permissions";
import { projectStatus } from "@/lib/projects";

export const reactivateUrl = (project: Pick<ApiInstance, "id" | "reactivate_url">) =>
  project.reactivate_url || `/my/instances/${project.id}/reactivate`;

/** Whether this viewer may reactivate a cancelled project. */
export function canReactivate(project: Pick<ApiInstance, "state" | "project_status" | "permissions">) {
  return projectStatus(project) === "cancelled" && hasPermission(project.permissions, "billing.manage");
}

/** Reactivation lives on the server's plan-builder page, so the button is a
 *  full navigation, not an SPA route. */
export function goReactivate(project: Pick<ApiInstance, "id" | "reactivate_url">) {
  window.location.assign(reactivateUrl(project));
}

/**
 * Shown at the top of a cancelled project for whoever can pay for it.
 * The list data has no snapshot details, so the project detail is fetched
 * once to say whether the last snapshot was kept and what restoring costs.
 */
export function ReactivateBanner({ project }: { project: ApiInstance }) {
  const [detail, setDetail] = React.useState<ApiInstance | null>(null);
  const show = canReactivate(project);

  React.useEffect(() => {
    if (!show) return;
    let alive = true;
    api.instance(project.id).then((d) => alive && setDetail(d)).catch(() => {});
    return () => {
      alive = false;
    };
  }, [show, project.id]);

  if (!show || detail?.has_unpaid_invoice) return null;

  const kept = !!detail?.has_retained_snapshot;
  const fee = detail?.restoration_fee
    ? new Intl.NumberFormat(getLocale(), { style: "currency", currency: detail.currency || "USD" }).format(detail.restoration_fee)
    : "";
  return (
    <AlertBanner
      className="mb-6"
      variant="warning"
      title={kept ? i18nText("This project is cancelled — your last snapshot is kept") : i18nText("This project is cancelled")}
      description={
        kept
          ? i18nText("We've kept your most recent full snapshot{0}. Reactivate the project to restore it. A one-time data-restoration fee{1} applies on top of the new plan.", [
              detail?.retained_snapshot_date ? ` (${formatDate(detail.retained_snapshot_date)})` : "",
              fee ? ` (${fee})` : "",
            ])
          : i18nText("Reactivate to start the project again on a new plan. No snapshot was kept, so it starts empty.")
      }
      action={
        <Button size="sm" onClick={() => goReactivate(detail ?? project)}>
          <RotateCw className="size-4" />{i18nText("Reactivate")}</Button>
      }
    />
  );
}
