import { Info } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Small info icon shown next to a field label. Hovering (or focusing)
 * reveals a one-line explanation so the customer knows what the choice
 * does — like Odoo's "?" helps. Pure CSS tooltip, no dependencies.
 */
export function FieldHint({ text, className }: { text: string; className?: string }) {
  return (
    <span className={cn("group relative inline-flex align-middle", className)}>
      <Info
        tabIndex={0}
        aria-label={text}
        className="size-3.5 cursor-help text-muted outline-none transition-colors hover:text-primary focus-visible:text-primary"
      />
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 z-30 mb-1.5 w-56 -translate-x-1/2 rounded-md border border-border bg-card px-2.5 py-1.5 text-xs font-normal leading-snug text-foreground opacity-0 shadow-lg transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100"
      >
        {text}
      </span>
    </span>
  );
}
