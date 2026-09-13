import { Link } from "react-router-dom";
import { cn } from "@/lib/utils";

export function Logo({
  className,
  withWordmark = true,
}: {
  className?: string;
  withWordmark?: boolean;
}) {
  return (
    <Link to="/" className={cn("group inline-flex items-center gap-2.5", className)}>
      <span className="relative flex size-8 items-center justify-center rounded-lg bg-primary shadow-glow transition-transform group-hover:scale-105">
        <svg viewBox="0 0 32 32" className="size-5" aria-hidden>
          <path
            d="M8 9 L16 23 L24 9"
            fill="none"
            stroke="#FAFAFA"
            strokeWidth="2.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </span>
      {withWordmark && (
        <span className="text-base font-bold tracking-tight">
          VELT<span className="text-primary">NEX</span>
        </span>
      )}
    </Link>
  );
}
