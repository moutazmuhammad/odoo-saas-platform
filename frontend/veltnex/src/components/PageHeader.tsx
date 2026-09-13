import * as React from "react";
import { PortalBreadcrumb } from "@/components/layout/PortalLayout";

/** Consistent page header (Google-console style): breadcrumb, a regular-weight
 *  title, an optional subtitle, and a right-aligned actions slot. Use at the
 *  top of every portal page so structure + spacing are identical everywhere. */
export function PageHeader({
  breadcrumb,
  title,
  subtitle,
  actions,
  className,
}: {
  breadcrumb?: { label: string; to?: string }[];
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      {breadcrumb && breadcrumb.length > 0 && <PortalBreadcrumb items={breadcrumb} />}
      <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <h1 className="truncate text-[1.5rem] font-normal leading-tight tracking-tight text-foreground">
            {title}
          </h1>
          {subtitle && <p className="mt-1 text-sm text-muted">{subtitle}</p>}
        </div>
        {actions && <div className="flex flex-shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}
