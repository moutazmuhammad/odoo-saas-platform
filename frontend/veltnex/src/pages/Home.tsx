import { Link, useNavigate } from "react-router-dom";
import {
  ArrowRight,
  ShieldCheck,
  Zap,
  Activity,
  Database,
  Globe,
  Cpu,
  Lock,
  Gauge,
  CheckCircle2,
  GitBranch,
  Package,
  Layers,
  RefreshCw,
  Server,
  Sparkles,
  Terminal,
  TrendingUp,
} from "lucide-react";
import * as React from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ServiceIcon } from "@/components/ServiceIcon";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { api, type ApiService } from "@/lib/api";
import { useSections } from "@/lib/useSections";

// Lazy-loaded so three.js (~210KB gzipped) lives in its own chunk and
// is only fetched on the home page — keeping login/portal lean.
const GlobeViz = React.lazy(() =>
  import("@/components/Globe").then((m) => ({ default: m.Globe })),
);

const STATS = [
  { value: "99.99%", label: "Uptime SLA" },
  { value: "Daily", label: "Automatic Backups" },
  { value: "Free", label: "SSL Certificates" },
  { value: "24/7", label: "Expert support" },
];

const ODOO_VERSIONS = ["13", "14", "15", "16", "17", "18", "19"];

const HOW_IT_WORKS = [
  {
    step: "01",
    icon: Sparkles,
    title: "Sign up",
    description:
      "Create your account in under a minute — no credit card needed to explore the console.",
  },
  {
    step: "02",
    icon: Server,
    title: "Pick your stack",
    description:
      "Choose any Odoo version (13 → 19), set workers, RAM and storage, and pick a region.",
  },
  {
    step: "03",
    icon: GitBranch,
    title: "Bring your code",
    description:
      "Link a GitHub, GitLab, or Bitbucket repo. We clone on boot and redeploy on every push.",
  },
  {
    step: "04",
    icon: TrendingUp,
    title: "Scale & ship",
    description:
      "Dial resources up or down anytime. We handle backups, SSL, monitoring — you ship features.",
  },
];

// Shared building blocks ------------------------------------------------

const SectionPill = ({
  icon: Icon,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
}) => (
  <span className="inline-flex items-center gap-2 rounded-full border border-primary/30 bg-primary/10 px-3 py-1 text-xs font-medium text-primary">
    <Icon className="size-3.5" />
    {children}
  </span>
);

const SectionHeading = ({
  pill,
  title,
  subtitle,
  align = "center",
}: {
  pill?: React.ReactNode;
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  align?: "center" | "left";
}) => (
  <div
    className={
      align === "center"
        ? "mx-auto max-w-2xl text-center"
        : "max-w-2xl text-left"
    }
  >
    {pill}
    <h2 className="mt-4 text-3xl font-bold tracking-tight sm:text-4xl">
      {title}
    </h2>
    {subtitle && (
      <p className="mt-4 text-base text-muted sm:text-lg">{subtitle}</p>
    )}
  </div>
);

// ---------------------------------------------------------------------

export default function Home() {
  const navigate = useNavigate();
  const sections = useSections();
  const [services, setServices] = React.useState<ApiService[]>([]);

  React.useEffect(() => {
    api.services().then(setServices).catch(() => setServices([]));
  }, []);

  return (
    <div className="animate-fade-in">
      {/* ============================================================== */}
      {/* HERO — text-first, globe in background, no competing visuals   */}
      {/* ============================================================== */}
      <section className="relative overflow-hidden">
        <div className="pointer-events-none absolute inset-0 bg-grid-faint bg-[size:48px_48px] [mask-image:radial-gradient(ellipse_at_top,black,transparent_70%)]" />
        <div className="pointer-events-none absolute left-1/2 top-[-160px] h-[620px] w-[1040px] -translate-x-1/2 rounded-full bg-primary/20 blur-[180px]" />

        <div className="relative mx-auto max-w-5xl px-4 pb-28 pt-4 text-center sm:px-6 lg:px-8 lg:pt-6">
          {/* Globe + overlay stack: BOTH the status pill and the H1 sit
              centered on top of the globe as a single vertical column.
              Everything below the globe (subtitle, CTAs) follows in
              normal flow underneath. */}
          <div className="relative mx-auto w-[min(600px,92vw)]">
            <div className="aspect-square [mask-image:radial-gradient(circle_at_center,black_65%,transparent_95%)]">
              <ErrorBoundary>
                <React.Suspense fallback={null}>
                  <GlobeViz speed={0.12} />
                </React.Suspense>
              </ErrorBoundary>
            </div>
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-5">
              <span className="inline-flex items-center gap-2 rounded-full border border-border bg-card/70 px-3 py-1 text-xs font-medium text-muted backdrop-blur">
                <span className="relative flex size-1.5">
                  <span className="absolute inline-flex size-full animate-ping rounded-full bg-success opacity-75" />
                  <span className="relative inline-flex size-1.5 rounded-full bg-success" />
                </span>
                All systems operational
              </span>
              <h1 className="whitespace-nowrap text-5xl font-bold leading-[0.95] tracking-tight drop-shadow-[0_2px_28px_rgba(0,0,0,0.65)] sm:text-6xl lg:text-7xl xl:text-8xl">
                The Logic of
                <br />
                <span className="bg-gradient-to-br from-primary-glow via-info to-foreground bg-clip-text text-transparent">
                  Stability
                </span>
              </h1>
            </div>
          </div>

          <p className="mx-auto mt-2 max-w-2xl text-lg text-muted sm:text-xl">
            One managed platform, two ways to run Odoo:{" "}
            <strong className="text-foreground">host your own code</strong> on
            any version, or launch a{" "}
            <strong className="text-foreground">ready-made app</strong> that
            ships with its database already set up. Pick a path to see how it
            works.
          </p>

          <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
            {sections.hosting && (
              <Button size="lg" onClick={() => navigate("/hosting")}>
                Explore Hosting
                <ArrowRight />
              </Button>
            )}
            {sections.services && (
              <Button
                size="lg"
                variant={sections.hosting ? "secondary" : "default"}
                onClick={() => navigate("/services")}
              >
                Explore Ready-Made Apps
                {!sections.hosting && <ArrowRight />}
              </Button>
            )}
          </div>

          <p className="mt-4 text-sm text-muted">
            No setup fees · Cancel anytime · Free trial available inside each
          </p>

          {/* Capability strip — the full offering, scannable above the fold */}
          <div className="mx-auto mt-12 flex max-w-3xl flex-wrap items-center justify-center gap-x-7 gap-y-3 text-sm text-muted">
            {[
              "Any Odoo 13 → 19",
              "Git-based deploys",
              "Staging & dev environments",
              "Automated backups",
              "Live metrics & logs",
              "One-click scaling",
            ].map((c) => (
              <span key={c} className="inline-flex items-center gap-2">
                <CheckCircle2 className="size-4 text-primary" />
                {c}
              </span>
            ))}
          </div>
        </div>
      </section>

      {/* ============================================================== */}
      {/* TWO WAYS TO LAUNCH — positions Hosting vs Ready-Made Services  */}
      {/* upfront so every visitor sees both product lines, even when    */}
      {/* the dynamic services list at the bottom is empty. Only shown    */}
      {/* when BOTH sections are live — the "two ways" framing makes no    */}
      {/* sense with one offering disabled.                               */}
      {/* ============================================================== */}
      {sections.services && sections.hosting && (
      <section className="border-y border-border bg-card/20">
        <div className="mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-8">
          <SectionHeading
            pill={<SectionPill icon={Sparkles}>Two ways to launch</SectionPill>}
            title={
              <>
                Bring your own code,{" "}
                <span className="text-muted">or start from a ready-made app</span>
              </>
            }
            subtitle="Same managed platform, same backups, SSL and uptime — the difference is what's inside when it boots. Open either one to see the full details and start."
          />

          <div className="mt-14 grid gap-6 lg:grid-cols-2">
            {/* Card 1 — Custom Hosting (you bring the code; empty database) */}
            <Card className="group relative flex flex-col overflow-hidden p-8">
              <div className="pointer-events-none absolute -right-20 -top-20 size-72 rounded-full bg-primary/15 blur-3xl transition-opacity group-hover:opacity-80" />
              <div className="relative flex h-full flex-col">
                <span className="flex size-12 items-center justify-center rounded-xl bg-primary/15 text-primary">
                  <Server className="size-6" />
                </span>
                <h3 className="mt-6 text-2xl font-semibold">Hosting</h3>
                <p className="mt-1 text-sm font-medium text-primary">
                  You bring the code — we run it on a clean instance.
                </p>
                <p className="mt-3 text-muted">
                  For teams who write their own Odoo. Pick any version (13 → 19),
                  connect your Git repo, install custom Python packages, and scale
                  on demand. Boots as an <strong className="text-foreground">empty
                  Odoo</strong> — you own the modules and the data.
                </p>
                <ul className="mt-6 space-y-2.5 text-sm">
                  {[
                    "Any Odoo version, Community or Enterprise",
                    "Link a GitHub / GitLab / Bitbucket repo",
                    "Install custom pip packages per instance",
                    "Scale workers, RAM, and storage independently",
                  ].map((it) => (
                    <li key={it} className="flex items-start gap-2">
                      <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />
                      <span>{it}</span>
                    </li>
                  ))}
                </ul>
                <div className="mt-8 flex items-center gap-3 pt-4">
                  <Button onClick={() => navigate("/hosting")}>
                    Explore Hosting
                    <ArrowRight />
                  </Button>
                </div>
              </div>
            </Card>

            {/* Card 2 — Ready-Made Services (ready code + ready database) */}
            <Card className="group relative flex flex-col overflow-hidden p-8">
              <div className="pointer-events-none absolute -right-20 -top-20 size-72 rounded-full bg-info/15 blur-3xl transition-opacity group-hover:opacity-80" />
              <div className="relative flex h-full flex-col">
                <span className="flex size-12 items-center justify-center rounded-xl bg-info/15 text-info">
                  <Layers className="size-6" />
                </span>
                <h3 className="mt-6 text-2xl font-semibold">Ready-Made Services</h3>
                <p className="mt-1 text-sm font-medium text-info">
                  Ready code <em>and</em> a ready database — running on day one.
                </p>
                <p className="mt-3 text-muted">
                  Pre-built Odoo apps for a specific job — pharmacy management,
                  retail, clinics and more. Sign up and your instance boots with
                  the <strong className="text-foreground">modules and starter
                  data already in place</strong>. Use it as-is, or tailor it to
                  how you work.
                </p>
                <ul className="mt-6 space-y-2.5 text-sm">
                  {[
                    "Industry-tuned app, set up and ready to use",
                    "Ships with its database — no blank screen",
                    "Customize it freely whenever you want",
                    "Same backups, SSL & uptime as hosting",
                  ].map((it) => (
                    <li key={it} className="flex items-start gap-2">
                      <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />
                      <span>{it}</span>
                    </li>
                  ))}
                </ul>
                <div className="mt-8 flex items-center gap-3 pt-4">
                  <Button onClick={() => navigate("/services")}>
                    Explore Ready-Made Apps
                    <ArrowRight />
                  </Button>
                </div>
              </div>
            </Card>
          </div>
        </div>
      </section>
      )}

      {/* ============================================================== */}
      {/* BUILT FOR THE ODOO ECOSYSTEM — header + dashboard preview +    */}
      {/* ecosystem partners strip, all in one cohesive section.         */}
      {/* ============================================================== */}
      <section className="border-y border-border bg-card/20">
        <div className="mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-8">
          <p className="text-center text-xs font-medium uppercase tracking-[0.22em] text-muted">
            Built for the Odoo ecosystem
          </p>

          {/* Operations console preview — proof of what "managed
              hosting for Odoo" looks like in practice. */}
          <div className="relative mx-auto mt-10 max-w-6xl">
            <div className="pointer-events-none absolute -inset-x-8 -bottom-8 -top-4 rounded-[2rem] bg-gradient-to-b from-primary/25 to-transparent blur-3xl" />
            <Card className="relative overflow-hidden border-border bg-card p-2 shadow-glow">
              <div className="rounded-xl border border-border bg-background/95 p-5 text-left sm:p-7">
                <div className="flex items-center justify-between border-b border-border pb-4">
                  <div className="flex items-center gap-2.5">
                    <Globe className="size-4 text-primary" />
                    <span className="font-mono text-sm">
                      my-company.veltnex.com
                    </span>
                    <span className="hidden rounded border border-border bg-card px-1.5 py-0.5 font-mono text-[10px] text-muted sm:inline">
                      v18.0
                    </span>
                  </div>
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-success/30 bg-success/10 px-2.5 py-0.5 text-xs font-medium text-success">
                    <span className="size-1.5 animate-pulse-soft rounded-full bg-success" />
                    Running
                  </span>
                </div>
                <div className="grid grid-cols-2 gap-3 pt-5 sm:grid-cols-4 sm:gap-4">
                  {[
                    { icon: Cpu, label: "CPU", value: "34%", trend: "stable" },
                    { icon: Activity, label: "Memory", value: "58%", trend: "+2%" },
                    { icon: Database, label: "Storage", value: "47%", trend: "+1%" },
                    { icon: TrendingUp, label: "Req/min", value: "1,284", trend: "+12%" },
                  ].map((m) => (
                    <div
                      key={m.label}
                      className="rounded-lg border border-border bg-card p-3"
                    >
                      <div className="flex items-center gap-2 text-muted">
                        <m.icon className="size-3.5" />
                        <span className="text-xs">{m.label}</span>
                      </div>
                      <p className="mt-2 text-xl font-semibold tabular-nums">
                        {m.value}
                      </p>
                      <p className="mt-0.5 text-[10px] uppercase tracking-wide text-muted">
                        {m.trend}
                      </p>
                    </div>
                  ))}
                </div>
                <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-3">
                  {[
                    { icon: ShieldCheck, label: "SSL", value: "Valid · 89d" },
                    { icon: RefreshCw, label: "Last backup", value: "2h ago" },
                    { icon: GitBranch, label: "Branch", value: "main · a1b2c3d" },
                  ].map((r) => (
                    <div
                      key={r.label}
                      className="flex items-center justify-between rounded-lg border border-border bg-card/60 px-3 py-2.5 text-xs"
                    >
                      <span className="flex items-center gap-2 text-muted">
                        <r.icon className="size-3.5 text-primary" />
                        {r.label}
                      </span>
                      <span className="font-mono text-foreground">{r.value}</span>
                    </div>
                  ))}
                </div>
              </div>
            </Card>
          </div>

        </div>
      </section>

      {/* ============================================================== */}
      {/* STATS — compact, gradient numerals                             */}
      {/* ============================================================== */}
      <section className="border-b border-border">
        <div className="mx-auto grid w-full grid-cols-2 gap-px bg-border lg:grid-cols-4">
          {STATS.map((s) => (
            <div
              key={s.label}
              className="bg-background py-14 text-center transition-colors hover:bg-card/40"
            >
              <p className="bg-gradient-to-br from-foreground to-muted bg-clip-text text-4xl font-bold tracking-tight text-transparent sm:text-5xl">
                {s.value}
              </p>
              <p className="mt-2 text-sm text-muted">{s.label}</p>
            </div>
          ))}
        </div>
      </section>

      {/* ============================================================== */}
      {/* ODOO VERSIONS                                                  */}
      {/* ============================================================== */}
      <section className="relative overflow-hidden border-b border-border">
        <div className="pointer-events-none absolute left-1/2 top-1/2 size-[680px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary/8 blur-[140px]" />
        <div className="relative mx-auto max-w-7xl px-4 py-20 text-center sm:px-6 lg:px-8">
          <SectionHeading
            pill={<SectionPill icon={Layers}>Version freedom</SectionPill>}
            title={
              <>
                Every Odoo version —{" "}
                <span className="text-muted">Community &amp; Enterprise</span>
              </>
            }
            subtitle="Maintaining a legacy install? Starting fresh on the latest release? We run it. Pin your version, upgrade on your schedule, migrate when you're ready."
          />
          <div className="mt-14 flex flex-wrap items-center justify-center gap-3">
            {ODOO_VERSIONS.map((v) => (
              <span
                key={v}
                className="group inline-flex items-center gap-2 rounded-2xl border border-border bg-card px-6 py-3 text-lg font-bold transition-all hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-glow"
              >
                <span className="text-xs font-medium uppercase tracking-wider text-muted transition-colors group-hover:text-primary">
                  Odoo
                </span>
                {v}
              </span>
            ))}
          </div>
          <p className="mt-8 text-sm text-muted">
            Need an older release or a custom patch level?{" "}
            <Link
              to="/docs"
              className="font-medium text-primary hover:underline"
            >
              Custom images welcome →
            </Link>
          </p>
        </div>
      </section>

      {/* ============================================================== */}
      {/* BRING YOUR OWN CODE — split layout with terminal mock          */}
      {/* ============================================================== */}
      <section className="border-b border-border bg-card/30">
        <div className="mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-8">
          <div className="grid items-center gap-14 lg:grid-cols-2">
            <div>
              <SectionHeading
                align="left"
                pill={<SectionPill icon={GitBranch}>Bring your own code</SectionPill>}
                title={
                  <>
                    Connect your repo.
                    <br />
                    <span className="text-muted">We deploy your modules.</span>
                  </>
                }
                subtitle={
                  <>
                    Link a <strong className="text-foreground">GitHub</strong>,{" "}
                    <strong className="text-foreground">GitLab</strong>, or{" "}
                    <strong className="text-foreground">Bitbucket</strong>{" "}
                    repository. Custom addons are cloned on first boot, and a
                    webhook redeploys them on every push — no SSH, no SCP, no
                    downtime windows.
                  </>
                }
              />
              <ul className="mt-9 space-y-3.5 text-sm">
                {[
                  "Private repositories via personal access token",
                  "Auto-redeploy on git push (webhook-driven)",
                  "Pin a branch, tag, or commit per instance",
                  "Install Python dependencies in one click",
                ].map((it) => (
                  <li key={it} className="flex items-start gap-3">
                    <span className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full bg-success/15">
                      <CheckCircle2 className="size-3.5 text-success" />
                    </span>
                    <span>{it}</span>
                  </li>
                ))}
              </ul>
              <div className="mt-10">
                {sections.hosting && (
                <Button onClick={() => navigate("/hosting")}>
                  Start hosting your code
                  <ArrowRight />
                </Button>
                )}
              </div>
            </div>

            <div className="relative">
              <div className="pointer-events-none absolute -inset-6 rounded-3xl bg-gradient-to-br from-primary/30 to-info/10 opacity-50 blur-2xl" />
              <Card glass className="relative overflow-hidden p-1.5 shadow-glow">
                <div className="rounded-lg border border-border bg-background/95 text-left">
                  <div className="flex items-center justify-between border-b border-border px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span className="size-2.5 rounded-full bg-danger/70" />
                      <span className="size-2.5 rounded-full bg-warning/70" />
                      <span className="size-2.5 rounded-full bg-success/70" />
                    </div>
                    <div className="flex items-center gap-2 text-xs text-muted">
                      <Terminal className="size-3.5" />
                      <span>deploy.log</span>
                    </div>
                    <span className="font-mono text-xs text-muted">●</span>
                  </div>
                  <div className="px-5 py-4 font-mono text-[13px] leading-relaxed">
                    <div className="flex items-center gap-2 border-b border-border/60 pb-3">
                      <GitBranch className="size-3.5 text-primary" />
                      <span className="truncate text-xs sm:text-sm">
                        github.com/your-org/odoo-addons
                      </span>
                      <span className="ml-auto inline-flex items-center gap-1.5 rounded-full border border-success/30 bg-success/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-success">
                        <span className="size-1 animate-pulse-soft rounded-full bg-success" />
                        Synced
                      </span>
                    </div>
                    <pre className="mt-4 overflow-x-auto whitespace-pre text-xs">
{`$ git push origin main

`}<span className="text-primary">→</span>{` webhook received   `}<span className="text-muted">·</span>{` your-org/odoo-addons
`}<span className="text-primary">→</span>{` pulling commit     `}<span className="text-muted">·</span>{` `}<span className="text-info">a1b2c3d</span>{`
`}<span className="text-primary">→</span>{` instance           `}<span className="text-muted">·</span>{` my-company
`}<span className="text-primary">→</span>{` pip install -r requirements.txt
`}<span className="text-primary">→</span>{` restarting workers ... `}<span className="text-success">done</span>{`

`}<span className="text-success">✓ deployed in 12s</span>
                    </pre>
                  </div>
                </div>
              </Card>
            </div>
          </div>
        </div>
      </section>

      {/* ============================================================== */}
      {/* BENTO FEATURES — varied sizes, mini-visuals inside cards       */}
      {/* ============================================================== */}
      <section className="border-b border-border">
        <div className="mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-8">
          <SectionHeading
            pill={<SectionPill icon={Zap}>Production-ready by default</SectionPill>}
            title="Everything you need to run Odoo"
            subtitle="A complete operations layer — so your team ships features instead of babysitting servers."
          />

          <div className="mt-16 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-6">
            {/* Featured — Live observability with embedded charts */}
            <Card className="group relative overflow-hidden p-7 sm:col-span-2 lg:col-span-4 lg:row-span-2">
              <div className="pointer-events-none absolute -right-20 -top-20 size-72 rounded-full bg-primary/15 blur-3xl transition-opacity group-hover:opacity-80" />
              <div className="relative flex h-full flex-col">
                <span className="flex size-12 items-center justify-center rounded-xl bg-primary/15 text-primary">
                  <Activity className="size-6" />
                </span>
                <h3 className="mt-6 text-2xl font-semibold">
                  Live observability, baked in
                </h3>
                <p className="mt-3 max-w-xl text-muted">
                  Real-time CPU, memory and storage metrics with streaming
                  logs for every instance. Tail your logs from the dashboard,
                  no SSH required.
                </p>
                <div className="mt-8 grid flex-1 grid-cols-3 gap-3">
                  {[
                    { label: "CPU", value: "34%", bar: "w-1/3" },
                    { label: "Memory", value: "58%", bar: "w-3/5" },
                    { label: "Storage", value: "47%", bar: "w-[47%]" },
                  ].map((m) => (
                    <div
                      key={m.label}
                      className="rounded-lg border border-border bg-background/60 p-3"
                    >
                      <div className="flex items-baseline justify-between">
                        <span className="text-xs text-muted">{m.label}</span>
                        <span className="text-sm font-semibold tabular-nums">
                          {m.value}
                        </span>
                      </div>
                      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
                        <div
                          className={`h-full rounded-full bg-gradient-to-r from-primary to-primary-glow ${m.bar}`}
                        />
                      </div>
                    </div>
                  ))}
                </div>
                <div className="mt-4 rounded-lg border border-border bg-background/80 p-3 font-mono text-[11px] leading-relaxed text-muted">
                  <span className="text-success">[INFO]</span> worker pool ready
                  · 4 workers · <span className="text-primary">heartbeat ok</span>
                  <br />
                  <span className="text-success">[INFO]</span> request{" "}
                  <span className="text-info">GET</span> /web/login 200{" "}
                  <span className="text-muted">in 38ms</span>
                </div>
              </div>
            </Card>

            {/* Provision in seconds — with progress bar mock */}
            <Card className="group p-6 sm:col-span-1 lg:col-span-2">
              <span className="flex size-11 items-center justify-center rounded-xl bg-primary/15 text-primary">
                <Zap className="size-5" />
              </span>
              <h3 className="mt-5 text-lg font-semibold">Provision in seconds</h3>
              <p className="mt-2 text-sm text-muted">
                Production-ready Odoo with one click. Server, database,
                proxy — all wired up.
              </p>
              <div className="mt-5 rounded-lg border border-border bg-background/60 p-3">
                <div className="flex items-center justify-between text-xs">
                  <span className="font-mono text-muted">my-company</span>
                  <span className="font-mono text-success">✓ 12s</span>
                </div>
                <div className="mt-2 h-1 overflow-hidden rounded-full bg-border">
                  <div className="h-full w-full rounded-full bg-gradient-to-r from-primary via-primary-glow to-success" />
                </div>
              </div>
            </Card>

            {/* Stable by design */}
            <Card className="group p-6 sm:col-span-1 lg:col-span-2">
              <span className="flex size-11 items-center justify-center rounded-xl bg-primary/15 text-primary">
                <ShieldCheck className="size-5" />
              </span>
              <h3 className="mt-5 text-lg font-semibold">Stable by design</h3>
              <p className="mt-2 text-sm text-muted">
                Isolated environments, automatic failover, zero-downtime
                upgrades keep you online.
              </p>
              <div className="mt-5 flex flex-wrap gap-2">
                {["isolated", "auto-failover", "zero-downtime"].map((t) => (
                  <span
                    key={t}
                    className="rounded-md border border-border bg-background/60 px-2 py-0.5 font-mono text-[10px] text-muted"
                  >
                    {t}
                  </span>
                ))}
              </div>
            </Card>

            {/* Daily backups — with timeline mock */}
            <Card className="group p-6 sm:col-span-2 lg:col-span-3">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <span className="flex size-11 items-center justify-center rounded-xl bg-primary/15 text-primary">
                    <RefreshCw className="size-5" />
                  </span>
                  <h3 className="mt-5 text-lg font-semibold">
                    Daily backups + on-demand
                  </h3>
                  <p className="mt-2 text-sm text-muted">
                    Deduplicated daily snapshots with point-in-time restore,
                    plus one-click downloadable backups.
                  </p>
                </div>
              </div>
              <div className="mt-5 space-y-2 font-mono text-xs">
                {[
                  { name: "backup-2026-05-28", size: "1.24 GB", when: "2h ago", tag: "auto" },
                  { name: "backup-2026-05-27", size: "1.23 GB", when: "1d ago", tag: "auto" },
                  { name: "pre-upgrade", size: "1.21 GB", when: "3d ago", tag: "manual" },
                ].map((b) => (
                  <div
                    key={b.name}
                    className="flex items-center justify-between rounded-md border border-border bg-background/60 px-3 py-2"
                  >
                    <span className="flex items-center gap-2 truncate">
                      <RefreshCw className="size-3 text-primary" />
                      <span className="truncate text-muted">{b.name}</span>
                    </span>
                    <span className="flex items-center gap-3 text-[11px]">
                      <span className="text-muted">{b.size}</span>
                      <span className="text-muted">{b.when}</span>
                      <span
                        className={
                          b.tag === "auto"
                            ? "rounded border border-success/30 bg-success/10 px-1.5 py-0.5 text-success"
                            : "rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-primary"
                        }
                      >
                        {b.tag}
                      </span>
                    </span>
                  </div>
                ))}
              </div>
            </Card>

            {/* Custom Python packages — with code snippet */}
            <Card className="group p-6 sm:col-span-2 lg:col-span-3">
              <span className="flex size-11 items-center justify-center rounded-xl bg-primary/15 text-primary">
                <Package className="size-5" />
              </span>
              <h3 className="mt-5 text-lg font-semibold">
                Custom Python packages
              </h3>
              <p className="mt-2 text-sm text-muted">
                Add pip dependencies per instance from the dashboard — no
                Docker images to rebuild, no tickets to file.
              </p>
              <div className="mt-5 rounded-lg border border-border bg-background/80 p-3 font-mono text-xs leading-relaxed">
                <div className="text-muted">
                  <span className="text-primary">$</span> pip install{" "}
                  <span className="text-foreground">pandas openpyxl phonenumbers</span>
                </div>
                <div className="mt-1 text-success">✓ installed · pip 24.0</div>
              </div>
            </Card>

            {/* Effortless databases */}
            <Card className="group p-6 sm:col-span-2 lg:col-span-2">
              <span className="flex size-11 items-center justify-center rounded-xl bg-primary/15 text-primary">
                <Database className="size-5" />
              </span>
              <h3 className="mt-5 text-lg font-semibold">Effortless databases</h3>
              <p className="mt-2 text-sm text-muted">
                Create, clone, back up, restore, reset admin passwords — all
                from the portal.
              </p>
            </Card>

            {/* Scale on demand */}
            <Card className="group p-6 sm:col-span-1 lg:col-span-2">
              <span className="flex size-11 items-center justify-center rounded-xl bg-primary/15 text-primary">
                <Gauge className="size-5" />
              </span>
              <h3 className="mt-5 text-lg font-semibold">Scale on demand</h3>
              <p className="mt-2 text-sm text-muted">
                Workers, RAM, storage — dial up or down. Prorated, monthly
                or yearly.
              </p>
            </Card>

            {/* Security & SSL */}
            <Card className="group p-6 sm:col-span-1 lg:col-span-2">
              <span className="flex size-11 items-center justify-center rounded-xl bg-primary/15 text-primary">
                <Lock className="size-5" />
              </span>
              <h3 className="mt-5 text-lg font-semibold">Security & SSL</h3>
              <p className="mt-2 text-sm text-muted">
                Free wildcard SSL, encrypted backups, isolated networks,
                audit logs.
              </p>
            </Card>
          </div>
        </div>
      </section>

      {/* ============================================================== */}
      {/* HOW IT WORKS — 4 steps with connector line                     */}
      {/* ============================================================== */}
      <section className="border-b border-border bg-card/20">
        <div className="mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-8">
          <SectionHeading
            pill={<SectionPill icon={Sparkles}>From signup to production</SectionPill>}
            title="How it works"
            subtitle="Four steps from idea to a production-grade Odoo instance running your code."
          />

          <div className="relative mt-16">
            <div className="pointer-events-none absolute left-0 right-0 top-12 hidden h-px bg-gradient-to-r from-transparent via-border to-transparent lg:block" />
            <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
              {HOW_IT_WORKS.map((s) => (
                <Card
                  key={s.step}
                  className="h-full p-6 transition-all hover:-translate-y-0.5 hover:border-primary/40"
                >
                  <div className="flex items-center justify-between">
                    <span className="flex size-11 items-center justify-center rounded-xl bg-primary/15 text-primary">
                      <s.icon className="size-5" />
                    </span>
                    <span className="font-mono text-xs font-bold tracking-widest text-muted">
                      {s.step}
                    </span>
                  </div>
                  <h3 className="mt-5 text-lg font-semibold">{s.title}</h3>
                  <p className="mt-2 text-sm text-muted">{s.description}</p>
                </Card>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* ============================================================== */}
      {/* SERVICES STRIP — Ready-Made Services                           */}
      {/* ============================================================== */}
      {sections.services && services.length > 0 && (
        <section className="border-b border-border">
          <div className="mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-8">
            <div className="flex flex-col items-start justify-between gap-6 sm:flex-row sm:items-end">
              <div className="max-w-xl">
                <SectionPill icon={Layers}>Ready-made services</SectionPill>
                <h2 className="mt-5 text-3xl font-bold tracking-tight sm:text-4xl">
                  One platform, every service
                </h2>
                <p className="mt-3 text-muted">
                  From hosting to commerce to compliance — compose the stack
                  your business needs.
                </p>
              </div>
              <Button
                variant="secondary"
                onClick={() => navigate("/services")}
              >
                Browse all services
                <ArrowRight />
              </Button>
            </div>
            <div className="mt-12 grid gap-5 md:grid-cols-2 lg:grid-cols-3">
              {services.slice(0, 3).map((s) => (
                <Link key={s.id} to={`/services/${s.id}`}>
                  <Card className="group h-full p-6 transition-all hover:-translate-y-0.5 hover:border-primary/40">
                    <span className="flex size-11 items-center justify-center rounded-xl bg-primary/15 text-primary transition-colors group-hover:bg-primary/25">
                      <ServiceIcon icon={s.icon} className="size-5" />
                    </span>
                    <h3 className="mt-5 text-lg font-semibold">{s.name}</h3>
                    <p className="mt-2 text-sm text-muted">{s.tagline}</p>
                    <span className="mt-4 inline-flex items-center gap-1 text-sm font-medium text-primary opacity-0 transition-opacity group-hover:opacity-100">
                      Learn more <ArrowRight className="size-3.5" />
                    </span>
                  </Card>
                </Link>
              ))}
            </div>
          </div>
        </section>
      )}

      {/* ============================================================== */}
      {/* FINAL CTA                                                       */}
      {/* ============================================================== */}
      <section className="relative overflow-hidden">
        <div className="pointer-events-none absolute inset-0 bg-grid-faint bg-[size:48px_48px] [mask-image:radial-gradient(ellipse_at_center,black,transparent_70%)]" />
        <div className="mx-auto max-w-7xl px-4 py-20 sm:px-6 lg:px-8">
          <Card
            glass
            className="relative overflow-hidden p-10 text-center sm:p-16"
          >
            <div className="pointer-events-none absolute -left-20 top-1/2 size-96 -translate-y-1/2 rounded-full bg-primary/20 blur-3xl" />
            <div className="pointer-events-none absolute -right-20 top-1/2 size-96 -translate-y-1/2 rounded-full bg-info/15 blur-3xl" />
            <div className="relative">
              <h2 className="mx-auto max-w-2xl text-3xl font-bold tracking-tight sm:text-5xl">
                Ship on infrastructure that{" "}
                <span className="bg-gradient-to-br from-primary-glow to-info bg-clip-text text-transparent">
                  just stays up
                </span>
              </h2>
              <p className="mx-auto mt-5 max-w-xl text-lg text-muted">
                Configure your plan in under a minute. No credit card
                required to explore the console.
              </p>
              <div className="mt-10 flex flex-col items-center justify-center gap-3 sm:flex-row">
                {sections.hosting ? (
                <Button size="lg" onClick={() => navigate("/hosting")}>
                  Configure your plan
                  <ArrowRight />
                </Button>
                ) : sections.services ? (
                <Button size="lg" onClick={() => navigate("/services")}>
                  Browse services
                  <ArrowRight />
                </Button>
                ) : null}
                <Button
                  size="lg"
                  variant="outline"
                  onClick={() => navigate("/docs")}
                >
                  Read the docs
                </Button>
              </div>
              <div className="mt-8 flex flex-wrap items-center justify-center gap-x-6 gap-y-2 text-sm text-muted">
                {[
                  "No setup fees",
                  "Cancel anytime",
                  "Daily backups included",
                  "Free wildcard SSL",
                ].map((t) => (
                  <span key={t} className="flex items-center gap-1.5">
                    <CheckCircle2 className="size-4 text-success" />
                    {t}
                  </span>
                ))}
              </div>
            </div>
          </Card>
        </div>
      </section>
    </div>
  );
}
