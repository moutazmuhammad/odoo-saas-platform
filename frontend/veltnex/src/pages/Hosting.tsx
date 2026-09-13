import * as React from "react";
import { useSearchParams } from "react-router-dom";
import { ArrowRight, Check, ShieldCheck, Cpu, HardDrive, Globe, Sparkles, SlidersHorizontal, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { AlertBanner } from "@/components/AlertBanner";
import { PlanBuilder, type PlanConfig } from "@/components/PlanBuilder";
import { FieldHint } from "@/components/FieldHint";
import {
  api,
  ApiError,
  type Meta,
  type PriceResult,
  type ProjectPriceResult,
  type ApiTier,
  type ApiRegion,
} from "@/lib/api";
import { formatBytes, recommendedUsers } from "@/lib/format";
import { useAuth } from "@/context/AuthContext";
import { cn } from "@/lib/utils";

function money(amount: number, currency = "USD") {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(amount);
}

/** Turn a free-text project name into a valid subdomain slug. */
function toSubdomain(s: string) {
  return s
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63);
}

/** Yearly saving vs paying month-by-month for a tier (amount + % off). */
function yearlySaving(t: { monthly: number; yearly: number }) {
  const annual = t.monthly * 12;
  const amount = t.yearly > 0 && t.yearly < annual ? annual - t.yearly : 0;
  const pct = amount > 0 ? Math.round((amount / annual) * 100) : 0;
  return { amount, pct };
}

function Stepper({
  label,
  value,
  onChange,
  hint,
}: {
  label: string;
  value: number;
  onChange: (n: number) => void;
  hint?: string;
}) {
  return (
    <div className="flex items-center justify-between rounded-lg border border-border p-3">
      <span className="flex items-center gap-1.5 text-sm font-medium">
        {label}
        {hint && <FieldHint text={hint} />}
      </span>
      <div className="flex items-center gap-3">
        <button
          type="button"
          aria-label={`Decrease ${label}`}
          onClick={() => onChange(Math.max(0, value - 1))}
          disabled={value <= 0}
          className="flex size-8 items-center justify-center rounded-md border border-border text-lg leading-none text-muted transition-colors hover:text-foreground disabled:opacity-40"
        >
          −
        </button>
        <span className="w-6 text-center text-sm font-semibold tabular-nums">{value}</span>
        <button
          type="button"
          aria-label={`Increase ${label}`}
          onClick={() => onChange(value + 1)}
          className="flex size-8 items-center justify-center rounded-md border border-border text-lg leading-none text-muted transition-colors hover:text-foreground"
        >
          +
        </button>
      </div>
    </div>
  );
}

const INCLUDED = [
  "Daily automated backups",
  "Zero-downtime upgrades",
  "Free SSL & custom domains",
  "99.99% uptime SLA",
  "Streaming logs & metrics",
  "24/7 expert support",
];

const SPECS = [
  { icon: Cpu, title: "Dedicated compute", desc: "Isolated CPU and memory per instance — no noisy neighbors." },
  { icon: HardDrive, title: "NVMe storage", desc: "Fast, redundant storage for databases and filestore." },
  { icon: Globe, title: "Global regions", desc: "Deploy close to your users." },
  { icon: ShieldCheck, title: "Hardened by default", desc: "Encrypted backups, audit logs, and IP allow-lists." },
];

export default function Hosting() {
  const { isAuthenticated } = useAuth();
  const [searchParams] = useSearchParams();
  // Trial mode: the buyer arrived via the "Start free trial" CTA
  // (/hosting?trial=1). The wizard runs the SAME funnel — name, subdomain,
  // version, region — but skips the paid specs/pricing step and all paid
  // add-ons, and finishes with a $0 "Start free trial" instead of payment.
  const isTrial = searchParams.get("trial") === "1";
  const [meta, setMeta] = React.useState<Meta | null>(null);
  const [tiers, setTiers] = React.useState<ApiTier[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [config, setConfig] = React.useState<PlanConfig | null>(null);
  const [price, setPrice] = React.useState<PriceResult | null>(null);
  // When tiers exist we show cards by default; "customize" reveals the slider.
  const [customize, setCustomize] = React.useState(false);
  // Region-aware pricing. `regionId === undefined` means "regions not loaded
  // yet"; `null` means "no regions configured" (x1.0). The picker only shows
  // when there's a real choice (> 1 available region).
  const [regions, setRegions] = React.useState<ApiRegion[]>([]);
  const [regionId, setRegionId] = React.useState<number | null | undefined>(undefined);
  // Cheapest entry price (region-aware) — the lowest of the slider floor and
  // any published tier — shown as "Starting from $X/mo".
  const [floorPrice, setFloorPrice] = React.useState<number | null>(null);
  const configInit = React.useRef(false);
  // Purchase wizard (Production-first):
  //   1 = choose Production specs, 2 = name the project,
  //   3 = subdomain + region + Staging/Dev counts.
  // Trials run on a fixed starter spec, so step 1 is skipped — start at 2.
  const [step, setStep] = React.useState(isTrial ? 2 : 1);
  const [projectName, setProjectName] = React.useState("");
  // Subdomain is its own editable field (defaults from the project name).
  const [subdomain, setSubdomain] = React.useState("");
  const [stagingCount, setStagingCount] = React.useState(0);
  const [devCount, setDevCount] = React.useState(0);
  const [projectQuote, setProjectQuote] = React.useState<ProjectPriceResult | null>(null);
  // Step 3 now collects EVERYTHING (the configure page is review + pay only).
  const [domainId, setDomainId] = React.useState<number | null>(null);
  const [versionId, setVersionId] = React.useState<number | null>(null);
  const [supportCode, setSupportCode] = React.useState("");
  const [dailyBackup, setDailyBackup] = React.useState(false);
  const [showGit, setShowGit] = React.useState(false);
  const [repoUrl, setRepoUrl] = React.useState("");
  const [repoBranch, setRepoBranch] = React.useState("main");
  const [gitToken, setGitToken] = React.useState("");
  const [ordering, setOrdering] = React.useState(false);
  const [orderError, setOrderError] = React.useState<string | null>(null);

  // Load limits/defaults + available regions once.
  React.useEffect(() => {
    Promise.all([api.meta(), api.regions().catch(() => [] as ApiRegion[])])
      .then(([m, regs]) => {
        setMeta(m);
        setRegions(regs);
        // Defaults for the now-in-Step-3 fields.
        if (m.domains?.length) setDomainId(m.domains[0].id);
        if (m.hosting_versions?.length) setVersionId(m.hosting_versions[0].id);
        const defSup = m.support_plans?.find((s) => s.is_default) || m.support_plans?.[0];
        if (defSup) setSupportCode(defSup.code);
        // Pre-select the RECOMMENDED region (the API marks it `default`).
        // Fall back to the cheapest by multiplier, then the first region.
        const recommended = regs.find((r) => r.default || r.recommended);
        const cheapest = regs.length
          ? regs.reduce((a, b) => (b.multiplier < a.multiplier ? b : a))
          : null;
        const chosen = recommended ?? cheapest;
        setRegionId(chosen ? chosen.id : null);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not load hosting plans."));
  }, []);

  // (Re)load published tiers whenever the chosen region changes — the prices
  // come back already scaled by that region's multiplier.
  React.useEffect(() => {
    if (regionId === undefined || !meta) return;
    let cancelled = false;
    api
      .tiers("hosting", regionId)
      .catch(() => [] as ApiTier[])
      .then((t) => {
        if (cancelled) return;
        setTiers(t);
        // Pick the initial config from the recommended tier exactly once;
        // region changes after that must NOT reset the customer's choice.
        if (!configInit.current) {
          configInit.current = true;
          const rec = t.find((x) => x.recommended) || t[0];
          setConfig({
            workers: rec ? rec.workers : meta.hosting_config.min_workers,
            storageGb: rec ? rec.storage : meta.hosting_config.min_storage,
            cycle: "monthly",
          });
          if (!t.length) setCustomize(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [regionId, meta]);

  // Cheapest entry price (region-aware): the slider floor at the minimum
  // config. Combined with the tier prices below to show "Starting from $X".
  React.useEffect(() => {
    if (!meta || regionId === undefined) return;
    let cancelled = false;
    api
      .hostingCalculate(
        meta.hosting_config.min_workers,
        meta.hosting_config.min_storage,
        "monthly",
        regionId,
      )
      .then((p) => !cancelled && setFloorPrice(p.total))
      .catch(() => !cancelled && setFloorPrice(null));
    return () => {
      cancelled = true;
    };
  }, [meta, regionId]);

  // Recompute the (server-authoritative) slider price whenever the config or
  // the region changes.
  React.useEffect(() => {
    if (!config || regionId === undefined) return;
    let cancelled = false;
    setPrice(null);
    const t = setTimeout(() => {
      api
        .hostingCalculate(config.workers, config.storageGb, config.cycle, regionId)
        .then((p) => !cancelled && setPrice(p))
        .catch(() => !cancelled && setPrice(null));
    }, 180);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [config, regionId]);

  const currency = price?.currency || meta?.hosting_config.currency || "USD";

  // Region-aware "starting from" — the lowest of the slider floor and any
  // published tier's monthly price.
  const startingFrom = React.useMemo(() => {
    const vals: number[] = [];
    if (tiers && tiers.length) vals.push(...tiers.map((t) => t.monthly));
    if (floorPrice != null) vals.push(floorPrice);
    return vals.length ? Math.min(...vals) : null;
  }, [tiers, floorPrice]);

  const selectedRegion = regions.find((r) => r.id === regionId) || null;
  const showRegionPicker = regions.length > 1;
  // Cheapest-first so the lowest entry price sits at the top of the
  // dropdown (and is the pre-selected default).
  const sortedRegions = React.useMemo(
    () => [...regions].sort((a, b) => a.multiplier - b.multiplier),
    [regions],
  );
  const cheapestRegion = sortedRegions[0] || null;

  // Largest yearly saving (as an AMOUNT) across tiers — drives the
  // "Save up to $X/yr" badge on the billing toggle.
  const maxSave = React.useMemo(() => {
    let best = { amount: 0, currency: currency };
    for (const t of tiers || []) {
      const s = yearlySaving(t);
      if (s.amount > best.amount) best = { amount: s.amount, currency: t.currency };
    }
    return best;
  }, [tiers, currency]);

  // Pick a Production tier's specs and move on to naming the project.
  const selectTier = (workers: number, storage: number) => {
    setConfig((c) => (c ? { ...c, workers, storageGb: storage } : c));
    setStep(2);
  };

  // All the order fields collected across the wizard.
  const orderFields = (): Record<string, string> => {
    const f: Record<string, string> = {
      subdomain,
      project_name: projectName || subdomain,
      workers: String(config?.workers ?? 0),
      storage: String(config?.storageGb ?? 0),
      billing: config?.cycle ?? "monthly",
      billing_period: config?.cycle ?? "monthly",
      staging_count: String(stagingCount),
      dev_count: String(devCount),
      daily_backup: dailyBackup ? "1" : "0",
    };
    // Trials carry the flag through the order (and through the register
    // redirect for anonymous buyers). The backend uses the trial plan,
    // skips billing, and deploys immediately — no payment step.
    if (isTrial) f.is_trial = "1";
    if (regionId != null) f.region_id = String(regionId);
    if (domainId != null) f.domain_id = String(domainId);
    if (versionId != null) f.odoo_version_id = String(versionId);
    if (supportCode) f.support_code = supportCode;
    if (repoUrl.trim()) {
      f.repo_url = repoUrl.trim();
      f.repo_branch = repoBranch.trim() || "main";
      if (gitToken.trim()) f.git_token = gitToken.trim();
    }
    return f;
  };

  // Straight to payment — no Review page. Anonymous buyers sign up first
  // (carrying the order), then the order is placed from the register flow.
  const placeOrder = async () => {
    if (!config || !subdomain || ordering) return;
    const fields = orderFields();
    if (!isAuthenticated) {
      const qs = new URLSearchParams({ hosting: "1", ...fields });
      window.location.href = `/register?${qs.toString()}`;
      return;
    }
    setOrdering(true);
    setOrderError(null);
    try {
      const { redirect_url } = await api.hostingOrder(fields);
      window.location.href = redirect_url;
    } catch (e) {
      if (e instanceof ApiError && e.code === "auth_required") {
        const qs = new URLSearchParams({ hosting: "1", ...fields });
        window.location.href = `/register?${qs.toString()}`;
        return;
      }
      setOrderError(e instanceof ApiError ? e.message : "Couldn't place your order.");
      setOrdering(false);
    }
  };

  // Project total (plan + chosen staging/dev servers) for the env step.
  React.useEffect(() => {
    if (step !== 3 || !config || regionId === undefined) return;
    let cancelled = false;
    api
      .hostingCalculateProject({
        workers: config.workers,
        storage: config.storageGb,
        billing: config.cycle,
        region: regionId,
        staging_count: stagingCount,
        dev_count: devCount,
      })
      .then((p) => !cancelled && setProjectQuote(p))
      .catch(() => !cancelled && setProjectQuote(null));
    return () => {
      cancelled = true;
    };
  }, [step, config, regionId, stagingCount, devCount]);

  const setCycle = (cycle: "monthly" | "yearly") =>
    setConfig((c) => (c ? { ...c, cycle } : c));

  // Sizing hint: recommended users = workers × [min..max] (light → heavy
  // usage), tuned in Settings → "Users / worker: light → heavy". Shown on
  // tier cards and next to the workers slider.
  const usersPerWorkerMin = meta?.hosting_config.users_per_worker_min || 6;
  const usersPerWorkerMax =
    meta?.hosting_config.users_per_worker_max || usersPerWorkerMin;

  const limits = meta
    ? {
        workers: { min: meta.hosting_config.min_workers, max: meta.hosting_config.max_workers },
        storage: { min: meta.hosting_config.min_storage, max: meta.hosting_config.max_storage },
      }
    : { workers: { min: 1, max: 8 }, storage: { min: 5, max: 200 } };

  // Add-on costs for the Step-3 total. Support + daily backup are FLAT
  // monthly fees billed ×12 on yearly (no discount) — the yearly discount
  // applies to infra only — so the project quote (plan + env servers)
  // doesn't include them. Add them here so the shown total is correct.
  const perLabel = config?.cycle === "yearly" ? "/yr" : "/mo";
  const cycleMult = config?.cycle === "yearly" ? 12 : 1;
  const selSupport = meta?.support_plans?.find((s) => s.code === supportCode);
  const supportLine = (selSupport?.monthly_price ?? 0) * cycleMult;
  const backupLine = (dailyBackup ? meta?.daily_backup_price ?? 0 : 0) * cycleMult;
  const baseTotal = projectQuote?.project_total ?? price?.total ?? 0;
  const grandTotal = baseTotal + supportLine + backupLine;

  return (
    <div className="animate-fade-in">
      <section className="relative overflow-hidden border-b border-border">
        <div className="pointer-events-none absolute left-1/2 top-0 h-72 w-[700px] -translate-x-1/2 rounded-full bg-primary/15 blur-[120px]" />
        <div className="relative mx-auto max-w-7xl px-4 py-16 text-center sm:px-6 lg:px-8">
          <p className="text-sm font-medium text-primary">Hosting</p>
          <h1 className="mx-auto mt-2 max-w-3xl text-4xl font-bold tracking-tight sm:text-5xl">
            Pay for exactly what you run
          </h1>
          <p className="mx-auto mt-4 max-w-2xl text-muted">
            Move the sliders to shape your instance. One transparent total —
            no per-resource math, no surprises on the invoice.
          </p>
          {startingFrom != null && (
            <p className="mt-6 text-sm text-muted">
              Plans starting from{" "}
              <span className="text-2xl font-bold text-foreground align-middle">
                {money(startingFrom, currency)}
              </span>
              <span className="text-muted">/mo</span>
              {selectedRegion && showRegionPicker && (
                <span className="text-muted"> in {selectedRegion.name}</span>
              )}
            </p>
          )}
          {!isTrial && meta?.trial.hosting_available && meta.trial.days > 0 && (
            <div className="mt-8 flex flex-col items-center gap-2">
              <Button size="lg" onClick={() => (window.location.href = "/hosting?trial=1")}>
                <Sparkles className="size-4" />
                Start your {meta.trial.days}-day free trial
              </Button>
              <span className="text-xs text-muted">No credit card required.</span>
            </div>
          )}
        </div>
      </section>

      <section className="mx-auto max-w-7xl px-4 py-14 sm:px-6 lg:px-8">
        {error && (
          <AlertBanner className="mb-6" variant="danger" title="Couldn't load hosting plans" description={error} />
        )}

        {!config ? (
          <p className="py-16 text-center text-sm text-muted">Loading plans…</p>
        ) : (
          <div className="mx-auto w-full max-w-5xl">
            {/* Trial badge — shown only in trial mode */}
            {isTrial && (
              <div className="mb-6 flex justify-center">
                <span className="inline-flex items-center gap-1.5 rounded-full border border-success/40 bg-success/10 px-3 py-1 text-sm font-medium text-success">
                  <Sparkles className="size-4" />
                  {meta?.trial.days ? `${meta.trial.days}-day free trial` : "Free trial"} — no credit card, no payment
                </span>
              </div>
            )}

            {/* Step indicator — paid funnel only. A trial is a single step,
                so the badge above is enough (no numbered stepper). */}
            {!isTrial && (
            <ol className="mb-10 flex items-center justify-center gap-1 sm:gap-3">
              {[
                { n: 1, label: "Production specs" },
                { n: 2, label: "Project & code" },
                { n: 3, label: "Region & environments" },
              ].map((s, i, arr) => (
                <li key={s.n} className="flex items-center gap-2">
                  <button
                    type="button"
                    disabled={s.n > step}
                    onClick={() => s.n < step && setStep(s.n)}
                    className={cn(
                      "flex items-center gap-2 rounded-full px-2 py-1 text-sm transition-colors",
                      s.n < step && "cursor-pointer hover:bg-card",
                    )}
                  >
                    <span
                      className={cn(
                        "flex size-7 items-center justify-center rounded-full text-xs font-semibold",
                        step >= s.n ? "bg-primary text-primary-foreground" : "bg-card text-muted ring-1 ring-border",
                      )}
                    >
                      {i + 1}
                    </span>
                    <span className={cn("hidden sm:inline", step === s.n ? "font-semibold text-foreground" : "text-muted")}>
                      {s.label}
                    </span>
                  </button>
                  {i < arr.length - 1 && <span className="h-px w-6 bg-border sm:w-10" />}
                </li>
              ))}
            </ol>
            )}

            {/* ── Step 1: choose Production specs ── */}
            {step === 1 && (
              <div className="space-y-8">
                <div className="mx-auto max-w-2xl text-center">
                  <h2 className="inline-flex items-center justify-center gap-1.5 text-lg font-semibold">
                    Choose your Production specs
                    <FieldHint text="Workers = CPU processes handling requests (more = more concurrent users). Storage = disk for your databases and files." />
                  </h2>
                  <p className="mt-1 text-sm text-muted">
                    These are your{" "}
                    <span className="font-medium text-foreground">Production</span>{" "}
                    server's resources. Staging &amp; Development servers are added
                    later at the lowest spec.
                  </p>
                </div>

                {/* Billing cycle + region — both drive the prices shown below */}
                <div className="mx-auto flex max-w-2xl flex-col items-stretch justify-center gap-4 sm:flex-row">
                  <div className="w-full sm:max-w-xs">
                    <div className="mb-1.5 flex items-center justify-center gap-1.5 text-xs font-medium text-muted">
                      Billing cycle
                      <FieldHint text="Pay monthly, or yearly to save. The yearly discount applies to infrastructure; support and backups are billed monthly ×12." />
                    </div>
                    <div className="inline-flex w-full rounded-xl border border-border bg-card p-1">
                      {(["monthly", "yearly"] as const).map((c) => (
                        <button
                          key={c}
                          onClick={() => setCycle(c)}
                          className={cn(
                            "flex flex-1 items-center justify-center gap-1.5 rounded-lg px-4 py-2 text-sm font-medium capitalize transition-colors",
                            config.cycle === c ? "bg-primary/20 text-foreground ring-1 ring-primary/40" : "text-muted hover:text-foreground",
                          )}
                        >
                          {c}
                          {c === "yearly" && maxSave.amount > 0 && (
                            <span className="rounded bg-success/20 px-1.5 py-0.5 text-[10px] font-semibold text-success">Save</span>
                          )}
                        </button>
                      ))}
                    </div>
                  </div>

                  {showRegionPicker && (
                    <div className="w-full sm:max-w-xs">
                      <div className="mb-1.5 flex items-center justify-center gap-1.5 text-xs font-medium text-muted">
                        <Globe className="size-3.5 text-primary" /> Region
                        <FieldHint text="Where your server runs. Price varies by region — pick the cheapest (Budget) or the one nearest your users (Recommended). The prices below update instantly." />
                      </div>
                      <select
                        value={regionId ?? ""}
                        onChange={(e) => setRegionId(Number(e.target.value))}
                        className="h-[42px] w-full cursor-pointer rounded-xl border border-border bg-card px-3 text-sm outline-none ring-primary/40 focus:ring-1"
                      >
                        {sortedRegions.map((r) => {
                          const tags: string[] = [];
                          if (r.recommended || r.default) tags.push("Recommended");
                          if (r.budget || (cheapestRegion && r.id === cheapestRegion.id)) tags.push("Cheapest");
                          const delta =
                            r.multiplier !== 1
                              ? ` (${r.multiplier > 1 ? "+" : ""}${Math.round((r.multiplier - 1) * 100)}%)`
                              : "";
                          const tag = tags.length ? ` — ${tags.join(" · ")}` : "";
                          return (
                            <option key={r.id} value={r.id}>
                              {r.name}{tag}{delta}
                            </option>
                          );
                        })}
                      </select>
                    </div>
                  )}
                </div>

                {/* How much yearly billing saves (tiers view; the custom
                    builder shows its own exact figure). */}
                {!customize && maxSave.amount > 0 && (
                  <p className="-mt-3 text-center text-sm text-muted">
                    {config.cycle === "monthly" ? (
                      <>
                        Switch to <span className="font-medium text-foreground">yearly</span> billing and save up to{" "}
                        <span className="font-semibold text-success">{money(maxSave.amount, maxSave.currency)}/yr</span>.
                      </>
                    ) : (
                      <>
                        You're saving up to{" "}
                        <span className="font-semibold text-success">{money(maxSave.amount, maxSave.currency)}/yr</span>{" "}
                        with yearly billing.
                      </>
                    )}
                  </p>
                )}

                {tiers && tiers.length > 0 ? (
                  <>
                    {/* Standard (static) plans AND a Custom card, side by side */}
                    <div className="grid gap-5 sm:grid-cols-2 xl:grid-cols-4">
                      {tiers.map((t) => {
                        const amount = config.cycle === "yearly" ? t.yearly : t.monthly;
                        const per = config.cycle === "yearly" ? "/yr" : "/mo";
                        return (
                          <Card key={t.id} className={cn("relative flex flex-col p-6", t.recommended && "ring-2 ring-primary")}>
                            {t.recommended && (
                              <span className="absolute -top-3 left-1/2 -translate-x-1/2 whitespace-nowrap rounded-full bg-primary px-3 py-0.5 text-xs font-semibold text-white">
                                {t.badge || "Most popular"}
                              </span>
                            )}
                            <h3 className="text-lg font-semibold">{t.name}</h3>
                            <p className="mt-3 text-3xl font-bold">
                              {money(amount, t.currency)}
                              <span className="text-base font-normal text-muted">{per}</span>
                            </p>
                            <ul className="mt-5 space-y-2 text-sm text-muted">
                              <li className="flex items-center gap-2"><Cpu className="size-4 text-primary" /> {t.workers} dedicated workers</li>
                              <li className="flex items-center gap-2"><Users className="size-4 text-primary" /> Recommended for {recommendedUsers(t.workers, usersPerWorkerMin, usersPerWorkerMax)} users</li>
                              <li className="flex items-center gap-2"><HardDrive className="size-4 text-primary" /> {formatBytes(t.storage)} storage</li>
                            </ul>
                            <Button className="mt-6 w-full" size="lg" variant={t.recommended ? "default" : "secondary"} onClick={() => { setCustomize(false); selectTier(t.workers, t.storage); }}>
                              Choose {t.name} <ArrowRight />
                            </Button>
                          </Card>
                        );
                      })}
                      {/* Custom plan card — sits right next to the standard tiers */}
                      <Card className={cn("relative flex flex-col border-dashed p-6 transition-shadow", customize && "ring-2 ring-primary")}>
                        <span className="flex size-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
                          <SlidersHorizontal className="size-5" />
                        </span>
                        <h3 className="mt-3 text-lg font-semibold">Custom</h3>
                        <p className="mt-3 text-sm text-muted">Build a plan with the exact workers and storage you need.</p>
                        <ul className="mt-5 space-y-2 text-sm text-muted">
                          <li className="flex items-center gap-2"><Cpu className="size-4 text-primary" /> Choose your workers</li>
                          <li className="flex items-center gap-2"><HardDrive className="size-4 text-primary" /> Choose your storage</li>
                          <li className="flex items-center gap-2"><Check className="size-4 text-primary" /> Pay only for what you pick</li>
                        </ul>
                        <Button className="mt-6 w-full" size="lg" variant={customize ? "default" : "secondary"} onClick={() => setCustomize(true)}>
                          <SlidersHorizontal className="size-4" /> Build custom
                        </Button>
                      </Card>
                    </div>
                    {/* Reveal the slider in place when the Custom card is chosen */}
                    {customize && (
                      <div className="mx-auto mt-8 max-w-5xl space-y-4 border-t border-border pt-8">
                        <h3 className="text-center text-base font-semibold">Build your custom plan</h3>
                        <PlanBuilder
                          config={config}
                          onChange={setConfig}
                          limits={limits}
                          price={price}
                          currency={price?.currency || meta?.hosting_config.currency}
                          usersPerWorkerMin={usersPerWorkerMin}
                          usersPerWorkerMax={usersPerWorkerMax}
                          footer={
                            <Button className="w-full" size="lg" onClick={() => setStep(2)}>
                              Continue <ArrowRight />
                            </Button>
                          }
                        />
                      </div>
                    )}
                  </>
                ) : (
                  <div className="mx-auto max-w-5xl space-y-6">
                    <PlanBuilder
                      config={config}
                      onChange={setConfig}
                      limits={limits}
                      price={price}
                      currency={price?.currency || meta?.hosting_config.currency}
                      usersPerWorkerMin={usersPerWorkerMin}
                      usersPerWorkerMax={usersPerWorkerMax}
                      footer={
                        <Button className="w-full" size="lg" onClick={() => setStep(2)}>
                          Continue <ArrowRight />
                        </Button>
                      }
                    />
                  </div>
                )}
              </div>
            )}

            {/* ── Step 2: project identity + code ── */}
            {step === 2 && (
              <Card className="mx-auto max-w-2xl p-6">
                <h2 className="text-lg font-semibold">Project &amp; code</h2>
                <p className="mt-1 text-sm text-muted">
                  Name your project, choose its address and Odoo version, and
                  optionally connect your repository.
                </p>

                <div className="mt-5 grid gap-4 sm:grid-cols-2">
                  <div className="space-y-1">
                    <label htmlFor="project-name" className="flex items-center gap-1.5 text-sm font-medium">
                      Project name
                      <FieldHint text="A friendly name for your project, shown in your dashboard. It does not affect your web address." />
                    </label>
                    <input
                      id="project-name"
                      autoFocus
                      value={projectName}
                      onChange={(e) => setProjectName(e.target.value)}
                      placeholder="My company ERP"
                      className="h-10 w-full rounded-lg border border-border bg-card px-3 text-sm outline-none ring-primary/40 focus:ring-1"
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="flex items-center gap-1.5 text-sm font-medium">
                      Odoo version
                      <FieldHint text="The Odoo release your instance runs (e.g. 17.0). Pick the version your apps and custom modules target." />
                    </label>
                    <select
                      value={versionId ?? ""}
                      onChange={(e) => setVersionId(Number(e.target.value))}
                      className="h-10 w-full cursor-pointer rounded-lg border border-border bg-card px-3 text-sm outline-none ring-primary/40 focus:ring-1"
                    >
                      {(meta?.hosting_versions || []).map((v) => (
                        <option key={v.id} value={v.id}>{v.name}</option>
                      ))}
                    </select>
                  </div>
                  <div className="space-y-1">
                    <label htmlFor="subdomain" className="flex items-center gap-1.5 text-sm font-medium">
                      Subdomain
                      <FieldHint text="The address of your instance: subdomain.basedomain. Use lowercase letters, numbers and hyphens." />
                    </label>
                    <input
                      id="subdomain"
                      value={subdomain}
                      onChange={(e) => setSubdomain(toSubdomain(e.target.value))}
                      placeholder="my-company-erp"
                      className="h-10 w-full rounded-lg border border-border bg-card px-3 font-mono text-sm outline-none ring-primary/40 focus:ring-1"
                    />
                  </div>
                  {(meta?.domains?.length ?? 0) >= 1 && (
                    <div className="space-y-1">
                      <label className="flex items-center gap-1.5 text-sm font-medium">
                        Domain
                        <FieldHint text="The domain your instance lives under. Your full address becomes subdomain.domain — pick the one you want." />
                      </label>
                      <select
                        value={domainId ?? ""}
                        onChange={(e) => setDomainId(Number(e.target.value))}
                        className="h-10 w-full cursor-pointer rounded-lg border border-border bg-card px-3 text-sm outline-none ring-primary/40 focus:ring-1"
                      >
                        {(meta?.domains || []).map((d) => (
                          <option key={d.id} value={d.id}>{d.name}</option>
                        ))}
                      </select>
                    </div>
                  )}
                </div>
                {subdomain && (
                  <p className="mt-2 text-xs text-muted">
                    Your instance:{" "}
                    <span className="font-mono text-foreground">
                      {subdomain}.{meta?.domains?.find((d) => d.id === domainId)?.name ?? ""}
                    </span>
                  </p>
                )}

                {/* Git config */}
                <div className="mt-5">
                  <span className="inline-flex items-center gap-1.5">
                    <button
                      type="button"
                      onClick={() => setShowGit((v) => !v)}
                      className="text-sm font-medium text-primary underline-offset-2 hover:underline"
                    >
                      {showGit ? "− Hide repository" : "+ Connect a Git repository (optional)"}
                    </button>
                    <FieldHint text="Optional: your custom Odoo addons repository. Needed only to create Staging/Development environments — you can add it later." />
                  </span>
                  {showGit && (
                    <div className="mt-3 space-y-3 rounded-lg border border-border p-3">
                      <input
                        value={repoUrl}
                        onChange={(e) => setRepoUrl(e.target.value)}
                        placeholder="https://github.com/you/your-addons.git"
                        className="h-10 w-full rounded-lg border border-border bg-card px-3 text-sm outline-none ring-primary/40 focus:ring-1"
                      />
                      <div className="grid gap-3 sm:grid-cols-2">
                        <input
                          value={repoBranch}
                          onChange={(e) => setRepoBranch(e.target.value)}
                          placeholder="main"
                          className="h-10 w-full rounded-lg border border-border bg-card px-3 text-sm outline-none ring-primary/40 focus:ring-1"
                        />
                        <input
                          value={gitToken}
                          onChange={(e) => setGitToken(e.target.value)}
                          placeholder="Access token (private repos)"
                          className="h-10 w-full rounded-lg border border-border bg-card px-3 text-sm outline-none ring-primary/40 focus:ring-1"
                        />
                      </div>
                      <p className="text-xs text-muted">
                        A repo is only needed to create Staging/Development environments — you can add it later.
                      </p>
                    </div>
                  )}
                </div>

                {/* Trial = single step: pick a region (only if there's a real
                    choice) and start — no separate Region step, no payment. */}
                {isTrial && showRegionPicker && (
                  <div className="mt-5 space-y-1">
                    <label className="flex items-center gap-1.5 text-sm font-medium">
                      <Globe className="size-3.5 text-primary" /> Region
                      <FieldHint text="The data-center location your trial runs in. Pick the one closest to your users — you can change region when you upgrade to a paid plan." />
                    </label>
                    <select
                      value={regionId ?? ""}
                      onChange={(e) => setRegionId(Number(e.target.value))}
                      className="h-10 w-full cursor-pointer rounded-lg border border-border bg-card px-3 text-sm outline-none ring-primary/40 focus:ring-1"
                    >
                      {sortedRegions.map((r) => {
                        const tag = (r.default || r.recommended)
                          ? " — Recommended"
                          : (r.budget || (cheapestRegion && r.id === cheapestRegion.id))
                            ? " — Budget"
                            : r.multiplier !== 1 ? ` (×${r.multiplier.toFixed(2)})` : "";
                        return <option key={r.id} value={r.id}>{r.name}{tag}</option>;
                      })}
                    </select>
                  </div>
                )}

                {isTrial && orderError && (
                  <p className="mt-4 text-xs text-danger">{orderError}</p>
                )}

                <div className="mt-6 flex items-center gap-3">
                  {!isTrial && (
                    <Button variant="secondary" onClick={() => setStep(1)}>← Back</Button>
                  )}
                  {isTrial ? (
                    <Button
                      className="flex-1"
                      size="lg"
                      disabled={!projectName.trim() || !subdomain || ordering}
                      onClick={placeOrder}
                    >
                      {ordering ? "Starting trial…" : "Start free trial"} <ArrowRight />
                    </Button>
                  ) : (
                    <Button className="flex-1" size="lg" disabled={!projectName.trim() || !subdomain} onClick={() => setStep(3)}>
                      Continue <ArrowRight />
                    </Button>
                  )}
                </div>
                {isTrial && (
                  <p className="mt-3 text-center text-xs text-muted">
                    {meta?.trial.days ? `Free for ${meta.trial.days} days · ` : ""}
                    no credit card · upgrade any time
                  </p>
                )}
              </Card>
            )}

            {/* ── Step 3: everything on one screen (inputs + live price) ── */}
            {step === 3 && (
              <div className="grid items-start gap-6 lg:grid-cols-[1fr_20rem]">
                {/* Left: region, support, backup & environments */}
                <Card className="p-5">
                  <h2 className="text-lg font-semibold">Region, support &amp; environments</h2>
                  <p className="mt-1 text-xs text-muted">
                    Choose where it runs, your support level, and how many extra
                    environments to buy.
                  </p>

                  <div className="mt-4 grid gap-4 sm:grid-cols-2">
                    {showRegionPicker && (
                      <div className="space-y-1">
                        <label className="flex items-center gap-1.5 text-sm font-medium">
                          <Globe className="size-3.5 text-primary" /> Region
                          <FieldHint text="The data-center location your server runs in. Pick the one closest to your users; price can vary by region." />
                        </label>
                        <select
                          value={regionId ?? ""}
                          onChange={(e) => setRegionId(Number(e.target.value))}
                          className="h-10 w-full cursor-pointer rounded-lg border border-border bg-card px-3 text-sm outline-none ring-primary/40 focus:ring-1"
                        >
                          {sortedRegions.map((r) => {
                            const tag = (r.default || r.recommended)
                              ? " — Recommended"
                              : (r.budget || (cheapestRegion && r.id === cheapestRegion.id))
                                ? " — Budget"
                                : r.multiplier !== 1 ? ` (×${r.multiplier.toFixed(2)})` : "";
                            return <option key={r.id} value={r.id}>{r.name}{tag}</option>;
                          })}
                        </select>
                      </div>
                    )}
                    {!isTrial && (meta?.support_plans?.length ?? 0) > 1 && (
                      <div className="space-y-1">
                        <label className="flex items-center gap-1.5 text-sm font-medium">
                          Support plan
                          <FieldHint text="Your level of help: Free is best-effort; paid tiers add priority response and channels. Billed as a flat monthly fee." />
                        </label>
                        <select
                          value={supportCode}
                          onChange={(e) => setSupportCode(e.target.value)}
                          className="h-10 w-full cursor-pointer rounded-lg border border-border bg-card px-3 text-sm outline-none ring-primary/40 focus:ring-1"
                        >
                          {(meta?.support_plans || []).map((s) => (
                            <option key={s.code} value={s.code}>
                              {s.name}{s.monthly_price > 0 ? ` (+${money(s.monthly_price, currency)}/mo)` : " — included"}
                            </option>
                          ))}
                        </select>
                      </div>
                    )}
                  </div>

                  {/* Staging / Dev counts side by side — paid add-ons, not in trials */}
                  {!isTrial && (
                    <>
                      <div className="mt-4 grid gap-3 sm:grid-cols-2">
                        <Stepper label="Staging" value={stagingCount} onChange={setStagingCount}
                          hint="A copy of your app to test changes safely before they reach Production. Runs on its own Git branch." />
                        <Stepper label="Development" value={devCount} onChange={setDevCount}
                          hint="A lightweight environment for building and testing new features, on its own Git branch." />
                      </div>
                      <p className="mt-1.5 text-xs text-muted">
                        Create up to the number you buy for free, any time.
                      </p>
                    </>
                  )}

                  {/* Daily backup */}
                  {!isTrial && (meta?.daily_backup_price ?? 0) > 0 && (
                    <label className="mt-4 flex cursor-pointer items-center gap-3 rounded-lg border border-border p-2.5">
                      <input
                        type="checkbox"
                        checked={dailyBackup}
                        onChange={(e) => setDailyBackup(e.target.checked)}
                        className="size-4"
                      />
                      <span className="text-sm">
                        <span className="font-medium">Daily off-site backups</span>
                        <FieldHint className="ml-1" text="Automatic daily backups of your databases, stored off-site. Priced by the amount of storage actually used." />
                        <span className="ml-1 text-muted">— from {money(meta?.daily_backup_price ?? 0, currency)}/mo</span>
                      </span>
                    </label>
                  )}
                </Card>

                {/* Right: sticky summary — live price, or "Free" for trials */}
                <Card className="self-start p-5 lg:sticky lg:top-24">
                  <h3 className="text-sm font-semibold">{isTrial ? "Your free trial" : "Order summary"}</h3>
                  {isTrial ? (
                    <div className="mt-3 space-y-3 text-sm">
                      <ul className="space-y-2 text-muted">
                        <li className="flex items-center gap-2"><Check className="size-4 text-success" /> Full Production instance</li>
                        <li className="flex items-center gap-2"><Check className="size-4 text-success" /> Your subdomain &amp; chosen Odoo version</li>
                        <li className="flex items-center gap-2"><Check className="size-4 text-success" /> No credit card required</li>
                      </ul>
                      <div className="flex items-baseline justify-between border-t border-border pt-3">
                        <span className="font-semibold">Total today</span>
                        <span className="text-2xl font-bold text-success">Free</span>
                      </div>
                      {meta?.trial.days ? (
                        <p className="text-xs text-muted">
                          Free for {meta.trial.days} days. Upgrade to a paid plan any time — no charge until you do.
                        </p>
                      ) : null}
                    </div>
                  ) : (
                    <div className="mt-3 space-y-1.5 text-sm">
                      <div className="flex justify-between"><span className="text-muted">Production plan</span><span className="font-medium">{money(price?.total ?? 0, currency)}{perLabel}</span></div>
                      {projectQuote && (stagingCount + devCount) > 0 && (
                        <div className="flex justify-between"><span className="text-muted">{stagingCount + devCount} × env server</span><span className="font-medium">{money(projectQuote.env_total, currency)}{perLabel}</span></div>
                      )}
                      {supportLine > 0 && (
                        <div className="flex justify-between"><span className="text-muted">Support — {selSupport?.name}</span><span className="font-medium">{money(supportLine, currency)}{perLabel}</span></div>
                      )}
                      {backupLine > 0 && (
                        <div className="flex justify-between"><span className="text-muted">Daily backups</span><span className="font-medium">{money(backupLine, currency)}{perLabel}</span></div>
                      )}
                      <div className="flex justify-between border-t border-border pt-2 text-base font-semibold">
                        <span>Total</span>
                        <span>{money(grandTotal, currency)}{perLabel}</span>
                      </div>
                    </div>
                  )}
                  {orderError && (
                    <p className="mt-3 text-xs text-danger">{orderError}</p>
                  )}
                  <Button className="mt-4 w-full" size="lg" disabled={!subdomain || ordering} onClick={placeOrder}>
                    {ordering
                      ? (isTrial ? "Starting trial…" : "Placing order…")
                      : (isTrial ? "Start free trial" : "Continue to payment")} <ArrowRight />
                  </Button>
                  <Button variant="secondary" className="mt-2 w-full" onClick={() => setStep(2)}>← Back</Button>
                </Card>
              </div>
            )}
          </div>
        )}
      </section>

      <section className="border-y border-border bg-card/30">
        <div className="mx-auto max-w-7xl px-4 py-16 sm:px-6 lg:px-8">
          <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-4">
            {SPECS.map((s) => (
              <Card key={s.title} className="p-6">
                <span className="flex size-11 items-center justify-center rounded-xl bg-primary/15 text-primary">
                  <s.icon className="size-5" />
                </span>
                <h3 className="mt-4 text-base font-semibold">{s.title}</h3>
                <p className="mt-2 text-sm text-muted">{s.desc}</p>
              </Card>
            ))}
          </div>
        </div>
      </section>

      <section className="mx-auto max-w-3xl px-4 py-16 sm:px-6 lg:px-8">
        <h2 className="text-center text-2xl font-bold tracking-tight">Every plan includes</h2>
        <div className="mt-8 grid gap-3 sm:grid-cols-2">
          {INCLUDED.map((item) => (
            <div key={item} className="flex items-center gap-3 rounded-lg border border-border bg-card p-4 text-sm">
              <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-success/15 text-success">
                <Check className="size-3" />
              </span>
              {item}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
