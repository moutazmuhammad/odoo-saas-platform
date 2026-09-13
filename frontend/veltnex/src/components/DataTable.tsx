import * as React from "react";
import { ArrowUp, ArrowDown } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export interface Column<T> {
  key: string;
  header: React.ReactNode;
  render: (row: T) => React.ReactNode;
  /** Provide to make the column sortable. */
  sortValue?: (row: T) => string | number;
  align?: "left" | "right" | "center";
  /** Tailwind responsive hide, e.g. "sm:table-cell" applied as hidden + this. */
  hideBelow?: "sm" | "md" | "lg";
  className?: string;
  width?: string;
}

/** Material / Google-console style data table: sticky-feel header, sortable
 *  columns, hover rows, an optional toolbar (search/filters/actions), and
 *  built-in loading + empty states. */
export function DataTable<T>({
  columns,
  rows,
  getKey,
  onRowClick,
  toolbar,
  loading = false,
  emptyState,
  initialSortKey,
  initialSortDir = "asc",
  className,
}: {
  columns: Column<T>[];
  rows: T[];
  getKey: (row: T) => string | number;
  onRowClick?: (row: T) => void;
  toolbar?: React.ReactNode;
  loading?: boolean;
  emptyState?: React.ReactNode;
  initialSortKey?: string;
  initialSortDir?: "asc" | "desc";
  className?: string;
}) {
  const [sortKey, setSortKey] = React.useState<string | undefined>(initialSortKey);
  const [sortDir, setSortDir] = React.useState<"asc" | "desc">(initialSortDir);

  const sorted = React.useMemo(() => {
    const col = columns.find((c) => c.key === sortKey);
    if (!col?.sortValue) return rows;
    const dir = sortDir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const va = col.sortValue!(a);
      const vb = col.sortValue!(b);
      return va < vb ? -dir : va > vb ? dir : 0;
    });
  }, [rows, sortKey, sortDir, columns]);

  const toggleSort = (c: Column<T>) => {
    if (!c.sortValue) return;
    if (sortKey === c.key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(c.key);
      setSortDir("asc");
    }
  };

  const hideClass = (c: Column<T>) =>
    c.hideBelow ? `hidden ${c.hideBelow}:table-cell` : "";

  return (
    <Card className={cn("overflow-hidden", className)}>
      {toolbar && (
        <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">{toolbar}</div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted">
              {columns.map((c) => (
                <th
                  key={c.key}
                  style={{ width: c.width }}
                  onClick={() => toggleSort(c)}
                  className={cn(
                    "px-4 py-3 font-medium transition-colors",
                    c.align === "right" && "text-right",
                    c.align === "center" && "text-center",
                    c.sortValue && "cursor-pointer select-none hover:text-foreground",
                    c.sortValue && sortKey === c.key && "text-foreground",
                    hideClass(c),
                    c.className,
                  )}
                >
                  <span className={cn("inline-flex items-center gap-1", c.align === "right" && "flex-row-reverse")}>
                    {c.header}
                    {c.sortValue && sortKey === c.key && (
                      sortDir === "asc" ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />
                    )}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {loading ? (
              Array.from({ length: 5 }).map((_, i) => (
                <tr key={i}>
                  {columns.map((c) => (
                    <td key={c.key} className={cn("px-4 py-3.5", hideClass(c))}>
                      <Skeleton className="h-4 w-24" />
                    </td>
                  ))}
                </tr>
              ))
            ) : sorted.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="p-0">
                  {emptyState}
                </td>
              </tr>
            ) : (
              sorted.map((row) => (
                <tr
                  key={getKey(row)}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  className={cn(
                    "transition-colors",
                    onRowClick && "cursor-pointer hover:bg-primary/[0.05]",
                  )}
                >
                  {columns.map((c) => (
                    <td
                      key={c.key}
                      className={cn(
                        "px-4 py-3.5 align-middle",
                        c.align === "right" && "text-right",
                        c.align === "center" && "text-center",
                        hideClass(c),
                        c.className,
                      )}
                    >
                      {c.render(row)}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
