import * as React from "react";
import { Copy, Check } from "lucide-react";
import { cn } from "@/lib/utils";

/** Small inline copy-to-clipboard button with a brief "copied" confirmation.
 *  Reusable across the app (instance URLs, repo URLs, webhook URLs, …). */
export function CopyButton({
  value,
  label = "Copy",
  className,
}: {
  value: string;
  label?: string;
  className?: string;
}) {
  const [copied, setCopied] = React.useState(false);
  const copy = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable — no-op */
    }
  };
  return (
    <button
      type="button"
      onClick={copy}
      title={copied ? "Copied" : label}
      aria-label={label}
      className={cn(
        "inline-flex items-center justify-center rounded-md p-1 text-muted transition-colors hover:bg-foreground/[0.06] hover:text-foreground",
        className,
      )}
    >
      {copied ? <Check className="size-3.5 text-success" /> : <Copy className="size-3.5" />}
    </button>
  );
}
