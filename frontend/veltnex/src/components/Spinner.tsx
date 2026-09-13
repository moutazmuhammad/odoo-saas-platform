import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

interface SpinnerProps {
  size?: "sm" | "md" | "lg";
  className?: string;
  label?: string;
}

const sizeMap = { sm: "size-4", md: "size-6", lg: "size-10" } as const;

export function Spinner({ size = "md", className, label }: SpinnerProps) {
  return (
    <span
      role="status"
      aria-label={label ?? "Loading"}
      className={cn("inline-flex items-center gap-2 text-muted", className)}
    >
      <Loader2 className={cn("animate-spin text-primary", sizeMap[size])} />
      {label && <span className="text-sm">{label}</span>}
    </span>
  );
}
