import { getLocale } from "@/i18n";
import { i18nText } from "@/i18n";
import * as React from "react";
import { Sparkles } from "lucide-react";
import { Card } from "./ui/card";
import { SliderControl } from "./SliderControl";
import { BreakdownRow } from "./BreakdownRow";
import { Spinner } from "./Spinner";
import { formatBytes, recommendedUsers } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { PriceResult } from "@/lib/api";

export type BillingCycle = "monthly" | "yearly";

export interface PlanConfig {
  workers: number;
  storageGb: number;
  cycle: BillingCycle;
}

interface Limits {
  workers: { min: number; max: number };
  storage: { min: number; max: number };
}

interface PlanBuilderProps {
  config: PlanConfig;
  onChange: (config: PlanConfig) => void;
  limits: Limits;
  /** Server-computed price for the current config; null while loading. */
  price: PriceResult | null;
  currency?: string;
  /** Sizing hint: recommended users = workers × [min..max] (light → heavy). */
  usersPerWorkerMin?: number;
  usersPerWorkerMax?: number;
  footer?: React.ReactNode;
  className?: string;
}

function money(amount: number, currency = "USD") {
  return new Intl.NumberFormat(getLocale(), {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(amount);
}

/**
 * Slider-driven plan builder. Pricing is always server-computed and passed
 * in via `price` — the UI only ever shows workers, storage, and one final
 * total (per-resource rates stay on the backend).
 */
export function PlanBuilder({
  config,
  onChange,
  limits,
  price,
  currency = "USD",
  usersPerWorkerMin = 6,
  usersPerWorkerMax = 10,
  footer,
  className,
}: PlanBuilderProps) {
  const savingsPercent = price?.savings_percent ?? 20;
  const usersLabel = recommendedUsers(
    config.workers,
    usersPerWorkerMin,
    usersPerWorkerMax,
  );

  return (
    <div className={cn("grid gap-6 lg:grid-cols-[1fr_360px]", className)}>
      <Card className="p-6 sm:p-8">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium">{i18nText("Billing cycle")}</p>
            <p className="text-xs text-muted">{i18nText("Switch anytime, no penalty.")}</p>
          </div>
          <div className="relative flex rounded-lg border border-border bg-background p-1">
            {(["monthly", "yearly"] as BillingCycle[]).map((c) => (
              <button
                key={c}
                onClick={() => onChange({ ...config, cycle: c })}
                className={cn(
                  "relative z-10 rounded-md px-4 py-1.5 text-sm font-medium capitalize transition-colors",
                  config.cycle === c ? "text-foreground" : "text-muted hover:text-foreground"
                )}
              >
                {i18nText(c === "yearly" ? "Yearly" : "Monthly")}
                {c === "yearly" && (
                  <span className="ms-1.5 rounded-sm bg-success/20 px-1.5 py-0.5 text-[10px] font-semibold text-success">
                    -{savingsPercent}%
                  </span>
                )}
                {config.cycle === c && (
                  <span className="absolute inset-0 -z-10 rounded-md bg-primary/20 ring-1 ring-primary/40" />
                )}
              </button>
            ))}
          </div>
        </div>

        <div className="mt-8 space-y-8">
          <SliderControl
            label={i18nText("Workers")}
            value={config.workers}
            min={limits.workers.min}
            max={limits.workers.max}
            step={1}
            onChange={(v) => onChange({ ...config, workers: v })}
            format={(v) => `${v} ${v === 1 ? "worker" : "workers"}`}
            hint={i18nText("Concurrent request capacity · recommended for {0} users", [usersLabel])}
          />
          <SliderControl
            label={i18nText("Storage")}
            value={config.storageGb}
            min={limits.storage.min}
            max={limits.storage.max}
            step={5}
            onChange={(v) => onChange({ ...config, storageGb: v })}
            format={(v) => formatBytes(v)}
            hint={i18nText("Database + filestore")}
          />
        </div>
      </Card>

      <Card glass className="flex flex-col p-6">
        <p className="text-sm font-medium text-muted">{i18nText("Your configuration")}</p>
        <div className="mt-2">
          <BreakdownRow label={i18nText("Workers")} value={config.workers} />
          <BreakdownRow label={i18nText("Storage")} value={formatBytes(config.storageGb)} />
          <BreakdownRow
            label={i18nText("Billing")}
            value={i18nText(config.cycle === "yearly" ? "Yearly" : "Monthly")}
          />
        </div>

        <div className="mt-2 border-t border-border pt-4">
          {!price ? (
            <div className="flex h-20 items-center justify-center">
              <Spinner label={i18nText("Calculating…")} />
            </div>
          ) : (
            <>
              <p className="text-sm text-muted">
                {config.cycle === "yearly" ? i18nText("Billed yearly") : i18nText("Billed monthly")}
              </p>
              <p className="mt-1 text-3xl font-bold tracking-tight">
                {money(price.total, currency)}
                <span className="text-base font-normal text-muted">
                  /{config.cycle === "yearly" ? "yr" : "mo"}
                </span>
              </p>
              {config.cycle === "yearly" ? (
                <div className="mt-3 flex items-center gap-2 rounded-lg border border-success/30 bg-success/10 px-3 py-2 text-sm text-success">
                  <Sparkles className="size-4" />{i18nText("You save ")}{money(price.yearly_savings, currency)}{i18nText(" per year")}</div>
              ) : (
                price.yearly_savings > 0 && (
                  <div className="mt-3 flex items-center gap-2 rounded-lg border border-success/30 bg-success/10 px-3 py-2 text-sm text-success">
                    <Sparkles className="size-4" />{i18nText("Switch to yearly and save ")}{money(price.yearly_savings, currency)} ({price.savings_percent}%)
                  </div>
                )
              )}
            </>
          )}
        </div>

        {footer && <div className="mt-6">{footer}</div>}
      </Card>
    </div>
  );
}
