import { cn } from "@/lib/utils";

interface SliderControlProps {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (value: number) => void;
  /** How to render the current value (e.g. "4 workers", "120 GB"). */
  format?: (value: number) => string;
  hint?: string;
  className?: string;
}

/** Branded range input with a filled track and live value readout. */
export function SliderControl({
  label,
  value,
  min,
  max,
  step = 1,
  onChange,
  format,
  hint,
  className,
}: SliderControlProps) {
  const pct = ((value - min) / (max - min)) * 100;

  return (
    <div className={cn("space-y-3", className)}>
      <div className="flex items-center justify-between">
        <label className="text-sm font-medium text-foreground/90">{label}</label>
        <span className="rounded-md bg-card px-2.5 py-1 text-sm font-semibold tabular-nums text-foreground border border-border">
          {format ? format(value) : value}
        </span>
      </div>
      <input
        type="range"
        className="veltnex-range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{
          background: `linear-gradient(to right, #203c86 0%, #3656b8 ${pct}%, #27272a ${pct}%, #27272a 100%)`,
        }}
        aria-label={label}
      />
      <div className="flex items-center justify-between text-xs text-muted/70">
        <span>{format ? format(min) : min}</span>
        {hint && <span className="text-muted">{hint}</span>}
        <span>{format ? format(max) : max}</span>
      </div>
    </div>
  );
}
