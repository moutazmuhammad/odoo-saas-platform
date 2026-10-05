import { i18nText } from "@/i18n";
import * as React from "react";
import type { ApiInstance } from "@/lib/api";

export function CustomerFilter({ projects, value, onChange }: {
  projects: ApiInstance[];
  value: string;
  onChange: (value: string) => void;
}) {
  const id = React.useId();
  const customers = React.useMemo(() => {
    const unique = new Map<number, { id: number; name: string }>();
    for (const project of projects) {
      if (project.customer) unique.set(project.customer.id, project.customer);
    }
    return [...unique.values()].sort((a, b) => a.name.localeCompare(b.name) || a.id - b.id);
  }, [projects]);

  return (
    <div className="flex items-center gap-2">
      <label htmlFor={id} className="text-sm text-muted">{i18nText("Customer")}</label>
      <select
        id={id}
        className="h-9 max-w-xs rounded-md border border-border bg-card px-3 text-sm"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">{i18nText("All customers")}</option>
        {customers.map((customer) => (
          <option key={customer.id} value={customer.id}>{customer.name}</option>
        ))}
      </select>
    </div>
  );
}
