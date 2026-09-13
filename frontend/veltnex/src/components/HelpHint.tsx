import { HelpCircle } from "lucide-react";
import { helpTip } from "@/lib/helpTopics";
import { cn } from "@/lib/utils";

/**
 * Small "?" marker shown next to a customer choice. Hovering shows a
 * one-line explanation (native tooltip); clicking opens the matching
 * Help section in a new tab. Tip text defaults to the topic's `tip`
 * from helpTopics, so callers usually pass only `anchor`.
 */
export function HelpHint({
  anchor,
  tip,
  className,
}: {
  anchor: string;
  tip?: string;
  className?: string;
}) {
  const text = tip ?? helpTip(anchor);
  return (
    <a
      href={`/help#${anchor}`}
      target="_blank"
      rel="noopener noreferrer"
      title={text}
      aria-label={text || "Learn more"}
      onClick={(e) => e.stopPropagation()}
      className={cn(
        // [cursor:inherit] = don't change the mouse pointer on hover.
        "inline-flex size-4 shrink-0 items-center justify-center align-middle text-muted transition-colors hover:text-primary [cursor:inherit]",
        className
      )}
    >
      <HelpCircle className="size-3.5" />
    </a>
  );
}
