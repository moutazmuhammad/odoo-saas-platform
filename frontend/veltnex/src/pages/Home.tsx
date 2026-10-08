import { i18nText } from "@/i18n";
import { Link, useNavigate } from "react-router-dom";
import {
  ArrowRight,
  Activity,
  CheckCircle2,
  Database,
  GitBranch,
  Layers,
  Lock,
  Server,
  ShieldCheck,
  Sparkles,
  TrendingUp,
  type LucideIcon,
} from "lucide-react";
import * as React from "react";
import { Button } from "@/components/ui/button";
import { ServiceIcon } from "@/components/ServiceIcon";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { api, type ApiService } from "@/lib/api";
import { useSections } from "@/lib/useSections";
import { cn } from "@/lib/utils";

// The globe pulls in three.js — lazy so the rest of the page paints first.
const GlobeViz = React.lazy(() =>
  import("@/components/Globe").then((m) => ({ default: m.Globe })),
);

const ODOO_VERSIONS = ["13", "14", "15", "16", "17", "18", "19", "20"];

// ---------------------------------------------------------------------
// Shared building blocks
// ---------------------------------------------------------------------

function Container({ className, children }: { className?: string; children: React.ReactNode }) {
  return <div className={cn("mx-auto w-full max-w-7xl px-4 sm:px-6 lg:px-8", className)}>{children}</div>;
}

function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-xs font-semibold uppercase tracking-[0.18em] text-primary">{children}</p>
  );
}

function SectionHeading({
  eyebrow,
  title,
  subtitle,
  align = "center",
  wide = false,
}: {
  eyebrow?: React.ReactNode;
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  align?: "center" | "start";
  wide?: boolean;
}) {
  return (
    <div className={cn(wide ? "max-w-3xl" : "max-w-2xl", align === "center" ? "mx-auto text-center" : "text-start")}>
      {eyebrow && <Eyebrow>{eyebrow}</Eyebrow>}
      <h2 className="mt-3 text-3xl font-bold tracking-tight text-foreground sm:text-4xl">{title}</h2>
      {subtitle && <p className="mt-4 text-base leading-relaxed text-muted sm:text-lg">{subtitle}</p>}
    </div>
  );
}

function StatusDot({ className }: { className?: string }) {
  return (
    <span className={cn("relative flex size-2", className)}>
      <span className="absolute inline-flex size-full animate-ping rounded-full bg-success opacity-60" />
      <span className="relative inline-flex size-2 rounded-full bg-success" />
    </span>
  );
}

// ---------------------------------------------------------------------
// Hero
// ---------------------------------------------------------------------

function Hero({ hosting, services }: { hosting: boolean; services: boolean }) {
  const navigate = useNavigate();
  const primaryTo = hosting ? "/hosting" : services ? "/services" : "/register";
  const primaryLabel = hosting
    ? i18nText("Start hosting")
    : services
      ? i18nText("Browse ready-made apps")
      : i18nText("Create an account");

  return (
    <section className="relative overflow-hidden">
      {/* Quiet backdrop: a faint grid that fades out, plus one soft glow behind the globe. */}
      <div className="pointer-events-none absolute inset-0 bg-grid-faint bg-size-[48px_48px] mask-[radial-gradient(ellipse_at_center,black_20%,transparent_75%)] opacity-70" />

      <Container className="relative grid items-center gap-8 pb-20 pt-8 lg:grid-cols-[1.05fr_1fr] lg:gap-8 lg:pb-28 lg:pt-20">
        {/* Copy */}
        <div className="order-2 text-center lg:order-1 lg:text-start">
          <span className="inline-flex items-center gap-2 rounded-full border border-border bg-card px-3 py-1 text-xs font-medium text-muted">
            <StatusDot />
            {i18nText("All systems operational")}
          </span>

          <h1 className="mt-6 text-4xl font-bold leading-[1.08] tracking-tight text-foreground sm:text-5xl lg:text-6xl">
            {i18nText("Run Odoo the way")}
            <br />
            <span className="text-primary">{i18nText("product teams ship.")}</span>
          </h1>

          <p className="mx-auto mt-6 max-w-lg text-lg leading-relaxed text-muted lg:mx-0">
            {hosting
              ? i18nText("Managed Odoo hosting for any version. Connect your code, go live in minutes, and leave the servers, backups and security to us.")
              : i18nText("Launch a ready-made Odoo app in minutes and leave the servers, backups and security to us.")}
          </p>

          <div className="mt-8 flex flex-col items-center gap-3 sm:flex-row sm:justify-center lg:justify-start">
            <Button size="lg" onClick={() => navigate(primaryTo)}>
              {primaryLabel}
              <ArrowRight />
            </Button>
            {hosting && services && (
              <Button size="lg" variant="outline" onClick={() => navigate("/services")}>
                {i18nText("Browse ready-made apps")}
              </Button>
            )}
            {!hosting && !services && (
              <Button size="lg" variant="outline" onClick={() => navigate("/docs")}>
                {i18nText("Read the docs")}
              </Button>
            )}
          </div>

          <ul className="mt-8 flex flex-wrap items-center justify-center gap-x-6 gap-y-2 text-sm text-muted lg:justify-start">
            {[i18nText("Free trial"), i18nText("No setup fees"), i18nText("Cancel anytime")].map((t) => (
              <li key={t} className="inline-flex items-center gap-2">
                <CheckCircle2 className="size-4 text-success" />
                {t}
              </li>
            ))}
          </ul>
        </div>

        {/* Globe */}
        <div className="order-1 lg:order-2">
          <div className="relative mx-auto w-[min(520px,72vw)] lg:w-[min(520px,88vw)]">
            <div className="pointer-events-none absolute inset-[12%] rounded-full bg-primary/25 blur-[90px]" />
            <div className="relative aspect-square mask-[radial-gradient(circle_at_center,black_62%,transparent_92%)]">
              <ErrorBoundary>
                <React.Suspense fallback={<div className="size-full rounded-full border border-border/60" />}>
                  <GlobeViz speed={0.1} />
                </React.Suspense>
              </ErrorBoundary>
            </div>

            {/* Two small, truthful facts pinned to the globe. */}
            <div className="pointer-events-none absolute -start-2 top-[18%] hidden sm:block">
              <HeroChip icon={GitBranch} title={i18nText("Deploys on every push")} detail={i18nText("Zero downtime")} />
            </div>
            <div className="pointer-events-none absolute -end-2 bottom-[16%] hidden sm:block">
              <HeroChip icon={ShieldCheck} title={i18nText("Secured with SSL")} detail={i18nText("Renews itself")} />
            </div>
          </div>
        </div>
      </Container>
    </section>
  );
}

function HeroChip({ icon: Icon, title, detail }: { icon: LucideIcon; title: string; detail: string }) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border bg-card/95 px-3 py-2 shadow-card">
      <span className="flex size-8 items-center justify-center rounded-md bg-primary/10 text-primary">
        <Icon className="size-4" />
      </span>
      <div className="text-start">
        <p className="text-xs font-semibold text-foreground">{title}</p>
        <p className="font-mono text-[11px] text-muted">{detail}</p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------
// Trust strip
// ---------------------------------------------------------------------

function TrustStrip({ hosting }: { hosting: boolean }) {
  const items = hosting
    ? ["GitHub", "GitLab", "Bitbucket", i18nText("Odoo 13 → 20"), i18nText("Community & Enterprise"), "Kubernetes"]
    : [i18nText("Odoo 13 → 20"), i18nText("Community & Enterprise"), "Kubernetes", i18nText("Daily backups"), i18nText("Free TLS")];
  return (
    <section className="border-y border-border bg-card/30">
      <Container className="flex flex-wrap items-center justify-center gap-x-10 gap-y-3 py-5">
        <span className="text-xs font-medium uppercase tracking-[0.18em] text-muted">{i18nText("Works with")}</span>
        {items.map((t) => (
          <span key={t} className="text-sm font-semibold text-muted/90">{t}</span>
        ))}
      </Container>
    </section>
  );
}

// ---------------------------------------------------------------------
// Product showcase — a faithful mock of the Environments console
// ---------------------------------------------------------------------

function ConsoleShowcase({ hosting }: { hosting: boolean }) {
  const envs = hosting
    ? [
        { name: "production", branch: "main", status: i18nText("Active"), tone: "success" as const, size: "4W · 50GB" },
        { name: "staging", branch: "staging", status: i18nText("Active"), tone: "success" as const, size: "2W · 20GB" },
        { name: "dev-invoices", branch: "feat/invoices", status: i18nText("Preparing"), tone: "info" as const, size: "2W · 20GB" },
      ]
    : [{ name: "production", branch: "—", status: i18nText("Active"), tone: "success" as const, size: "4W · 50GB" }];

  return (
    <section className="relative overflow-hidden py-20 lg:py-28">
      <Container>
        <SectionHeading
          eyebrow={i18nText("The console")}
          title={hosting ? i18nText("One project. Every environment in view.") : i18nText("Everything about your app, in one place.")}
          subtitle={hosting
            ? i18nText("Launch, test and ship from one dashboard. Your production, staging and development servers live side by side, so every change goes out with confidence.")
            : i18nText("Status, performance, backups and your database — all on one screen, no technical setup.")}
        />

        <div className="relative mx-auto mt-14 max-w-5xl">
          <div className="pointer-events-none absolute -inset-x-10 -bottom-10 top-10 rounded-[40px] bg-primary/15 blur-3xl" />
          <div className="relative overflow-hidden rounded-xl border border-border bg-card shadow-card">
            {/* Window chrome */}
            <div className="flex items-center gap-3 border-b border-border px-4 py-2.5">
              <span className="flex gap-1.5">
                <span className="size-2.5 rounded-full bg-border" />
                <span className="size-2.5 rounded-full bg-border" />
                <span className="size-2.5 rounded-full bg-border" />
              </span>
              <span className="ms-2 min-w-0 truncate rounded-md border border-border bg-background px-3 py-1 font-mono text-xs text-muted">
                acme.veltnex.app/my/instances/12/environments
              </span>
            </div>

            <div className="grid text-start md:grid-cols-[240px_1fr]">
              {/* Environment list */}
              <aside className="border-b border-border p-4 md:border-b-0 md:border-e">
                <p className="px-2 text-[11px] font-semibold uppercase tracking-wider text-muted">{i18nText("Environments")}</p>
                <ul className="mt-2 space-y-1">
                  {envs.map((e, i) => (
                    <li
                      key={e.name}
                      className={cn(
                        "flex items-center justify-between rounded-md px-2 py-2 text-sm",
                        i === 0 ? "bg-primary/10 text-foreground" : "text-muted",
                      )}
                    >
                      <span className="flex items-center gap-2">
                        <span className={cn("size-1.5 rounded-full", e.tone === "success" ? "bg-success" : "bg-info animate-pulse-soft")} />
                        <span className="font-medium">{e.name}</span>
                      </span>
                      <span className="font-mono text-[11px]">{e.branch}</span>
                    </li>
                  ))}
                </ul>
              </aside>

              {/* Main panel */}
              <div className="p-5 sm:p-6">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p className="font-mono text-sm text-foreground">acme.veltnex.app</p>
                    <p className="mt-0.5 text-xs text-muted">{"Odoo 18.0 · Frankfurt · "}{envs[0].size}</p>
                  </div>
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-success/30 bg-success/10 px-2.5 py-0.5 text-xs font-medium text-success">
                    <span className="size-1.5 rounded-full bg-success" />
                    {i18nText("Active")}
                  </span>
                </div>

                <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {[
                    { label: "CPU", value: "34%" },
                    { label: i18nText("Memory"), value: "58%" },
                    { label: i18nText("Storage"), value: "47%" },
                    { label: i18nText("Requests/min"), value: "1,284" },
                  ].map((m) => (
                    <div key={m.label} className="rounded-lg border border-border bg-background p-3">
                      <p className="text-xs text-muted">{m.label}</p>
                      <p className="mt-1 text-lg font-semibold tabular-nums text-foreground">{m.value}</p>
                    </div>
                  ))}
                </div>

                <div className="mt-4 grid gap-2 sm:grid-cols-3">
                  {[
                    { icon: GitBranch, label: i18nText("Last deployment"), value: i18nText("Succeeded · 2 min ago") },
                    { icon: Database, label: i18nText("Last backup"), value: i18nText("Today, 03:00") },
                    { icon: ShieldCheck, label: "SSL", value: i18nText("Valid · renews itself") },
                  ].map((r) => (
                    <div key={r.label} className="flex items-center gap-3 rounded-lg border border-border bg-background px-3 py-2.5">
                      <r.icon className="size-4 shrink-0 text-primary" />
                      <div className="min-w-0">
                        <p className="text-[11px] text-muted">{r.label}</p>
                        <p className="truncate text-xs font-medium text-foreground">{r.value}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>
      </Container>
    </section>
  );
}

// ---------------------------------------------------------------------
// Two ways to start
// ---------------------------------------------------------------------

function Paths() {
  const navigate = useNavigate();
  return (
    <section className="border-y border-border bg-card/30 py-20 lg:py-24">
      <Container>
        <SectionHeading
          wide
          eyebrow={i18nText("Two ways to start")}
          title={i18nText("Your code, or a ready-made app")}
          subtitle={i18nText("Same platform, same reliability. The difference is what's ready on day one.")}
        />
        <div className="mt-12 grid gap-6 lg:grid-cols-2">
          <PathCard
            icon={Server}
            title={i18nText("Hosting")}
            lead={i18nText("You bring the code. We run it.")}
            points={[
              i18nText("Any Odoo version, Community or Enterprise"),
              i18nText("Connect GitHub, GitLab or Bitbucket"),
              i18nText("Staging and development servers per branch"),
              i18nText("Scale up whenever you need"),
            ]}
            cta={i18nText("Explore hosting")}
            onClick={() => navigate("/hosting")}
          />
          <PathCard
            icon={Layers}
            title={i18nText("Ready-made apps")}
            lead={i18nText("Ready code and a ready database, on day one.")}
            points={[
              i18nText("Industry apps set up and ready to use"),
              i18nText("Ships with its database — no blank screen"),
              i18nText("Customize freely whenever you want"),
              i18nText("Same reliability and support as hosting"),
            ]}
            cta={i18nText("Explore ready-made apps")}
            onClick={() => navigate("/services")}
          />
        </div>
      </Container>
    </section>
  );
}

function PathCard({
  icon: Icon,
  title,
  lead,
  points,
  cta,
  onClick,
}: {
  icon: LucideIcon;
  title: string;
  lead: string;
  points: string[];
  cta: string;
  onClick: () => void;
}) {
  return (
    <article className="flex flex-col rounded-xl border border-border bg-card p-8 transition-colors hover:border-primary/40">
      <span className="flex size-11 items-center justify-center rounded-lg bg-primary/10 text-primary">
        <Icon className="size-5" />
      </span>
      <h3 className="mt-6 text-2xl font-semibold text-foreground">{title}</h3>
      <p className="mt-1 text-sm font-medium text-primary">{lead}</p>
      <ul className="mt-6 space-y-2.5 text-sm text-foreground">
        {points.map((p) => (
          <li key={p} className="flex items-start gap-2.5">
            <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />
            <span>{p}</span>
          </li>
        ))}
      </ul>
      <div className="mt-8 pt-2">
        <Button onClick={onClick}>
          {cta}
          <ArrowRight />
        </Button>
      </div>
    </article>
  );
}

// ---------------------------------------------------------------------
// Features
// ---------------------------------------------------------------------

function Features({ hosting }: { hosting: boolean }) {
  const items: { icon: LucideIcon; title: string; text: string }[] = [
    ...(hosting
      ? [
          { icon: GitBranch, title: i18nText("Git-based deploys"), text: i18nText("Push your code and it goes live automatically, with no downtime for your users.") },
          { icon: Layers, title: i18nText("Environments"), text: i18nText("Test changes on a safe copy of production before your customers ever see them.") },
        ]
      : []),
    { icon: Database, title: i18nText("Your database, ready"), text: i18nText("Set up from the first minute. Back it up, restore it or bring your own in a click.") },
    { icon: Activity, title: i18nText("Always in the picture"), text: i18nText("See how your Odoo is doing at a glance, and dig into the details whenever you need to.") },
    { icon: TrendingUp, title: i18nText("Scale on demand"), text: i18nText("Grow as your business grows. More power in minutes, and you only pay for what you use.") },
    { icon: Lock, title: i18nText("Secure by default"), text: i18nText("Free SSL, encrypted backups and isolated servers, so your data stays yours.") },
    ...(hosting ? [] : [{ icon: Sparkles, title: i18nText("Preconfigured apps"), text: i18nText("Everything in place from the first login, tuned to how your industry works.") }]),
  ];

  return (
    <section className="py-20 lg:py-24">
      <Container>
        <SectionHeading
          eyebrow={i18nText("Production-ready by default")}
          title={i18nText("Everything you need to run Odoo")}
          subtitle={i18nText("We take care of the servers, so your team can focus on the business.")}
        />
        <div className="mt-14 grid gap-px overflow-hidden rounded-xl border border-border bg-border sm:grid-cols-2 lg:grid-cols-3">
          {items.map((f) => (
            <div key={f.title} className="bg-background p-7 transition-colors hover:bg-card">
              <span className="flex size-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <f.icon className="size-5" />
              </span>
              <h3 className="mt-5 text-lg font-semibold text-foreground">{f.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-muted">{f.text}</p>
            </div>
          ))}
        </div>
      </Container>
    </section>
  );
}

// ---------------------------------------------------------------------
// How it works
// ---------------------------------------------------------------------

function HowItWorks({ hosting }: { hosting: boolean }) {
  const steps = hosting
    ? [
        { n: "01", title: i18nText("Sign up"), text: i18nText("Takes a minute. No credit card to start.") },
        { n: "02", title: i18nText("Choose your setup"), text: i18nText("Odoo version, size and region.") },
        { n: "03", title: i18nText("Connect your code"), text: i18nText("It deploys automatically on every push.") },
        { n: "04", title: i18nText("Go live"), text: i18nText("Your Odoo is online, with its database ready.") },
      ]
    : [
        { n: "01", title: i18nText("Sign up"), text: i18nText("Takes a minute. No credit card to start.") },
        { n: "02", title: i18nText("Choose an app"), text: i18nText("Pick the app and plan that fit your team.") },
        { n: "03", title: i18nText("Launch"), text: i18nText("Everything is set up for you.") },
        { n: "04", title: i18nText("Work"), text: i18nText("Sign in and get going. We keep it running smoothly.") },
      ];

  return (
    <section className="border-y border-border bg-card/30 py-20 lg:py-24">
      <Container>
        <SectionHeading eyebrow={i18nText("From sign-up to production")} title={i18nText("How it works")} />
        <ol className="mt-14 grid gap-8 md:grid-cols-4">
          {steps.map((s) => (
            <li key={s.n} className="relative">
              <span className="font-mono text-sm font-semibold text-primary">{s.n}</span>
              <div className="mt-3 h-px w-full bg-border" />
              <h3 className="mt-4 text-lg font-semibold text-foreground">{s.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-muted">{s.text}</p>
            </li>
          ))}
        </ol>
      </Container>
    </section>
  );
}

// ---------------------------------------------------------------------
// Versions
// ---------------------------------------------------------------------

function Versions() {
  return (
    <section className="py-20 lg:py-24">
      <Container>
        <SectionHeading
          eyebrow={i18nText("Version freedom")}
          title={i18nText("Every Odoo version, Community and Enterprise")}
          subtitle={i18nText("Keep the version you rely on, or start on the latest. Upgrade when it suits you, not when it suits us.")}
        />
        <div className="mt-12 flex flex-wrap items-center justify-center gap-3">
          {ODOO_VERSIONS.map((v) => (
            <span
              key={v}
              className="inline-flex items-baseline gap-1.5 rounded-lg border border-border bg-card px-5 py-2.5 text-lg font-semibold text-foreground transition-colors hover:border-primary/40"
            >
              <span className="text-xs font-medium uppercase tracking-wider text-muted">Odoo</span>
              {v}
            </span>
          ))}
        </div>
      </Container>
    </section>
  );
}

// ---------------------------------------------------------------------
// Ready-made catalog (dynamic)
// ---------------------------------------------------------------------

function Catalog({ services }: { services: ApiService[] }) {
  const navigate = useNavigate();
  if (!services.length) return null;
  return (
    <section className="border-t border-border bg-card/30 py-20 lg:py-24">
      <Container>
        <div className="flex flex-col gap-6 sm:flex-row sm:items-end sm:justify-between">
          <SectionHeading
            align="start"
            eyebrow={i18nText("Ready-made apps")}
            title={i18nText("Pick an app. It ships with its database.")}
          />
          <Button variant="outline" onClick={() => navigate("/services")}>
            {i18nText("Browse all apps")}
            <ArrowRight />
          </Button>
        </div>
        <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {services.slice(0, 6).map((s) => (
            <Link
              key={s.id}
              to={`/services/${s.id}`}
              className="group rounded-xl border border-border bg-background p-6 transition-colors hover:border-primary/40"
            >
              <ServiceIcon icon={s.icon} className="size-9 text-primary" />
              <h3 className="mt-4 text-base font-semibold text-foreground">{s.name}</h3>
              {s.description && <p className="mt-1.5 line-clamp-2 text-sm text-muted">{s.description}</p>}
              <span className="mt-4 inline-flex items-center gap-1 text-sm font-medium text-primary">
                {i18nText("View app")}
                <ArrowRight className="size-3.5 transition-transform group-hover:translate-x-0.5" />
              </span>
            </Link>
          ))}
        </div>
      </Container>
    </section>
  );
}

// ---------------------------------------------------------------------
// Final CTA
// ---------------------------------------------------------------------

function FinalCta({ hosting, services }: { hosting: boolean; services: boolean }) {
  const navigate = useNavigate();
  const stats = [
    { value: "99.9%", label: i18nText("Uptime target") },
    { value: i18nText("Daily"), label: i18nText("Automatic backups") },
    { value: i18nText("Free"), label: i18nText("TLS certificates") },
    { value: "13 → 20", label: i18nText("Odoo versions") },
  ];
  return (
    <section className="relative overflow-hidden border-t border-border">
      <div className="pointer-events-none absolute start-1/2 top-0 h-[420px] w-[900px] -translate-x-1/2 rounded-full bg-primary/15 blur-[140px]" />
      <Container className="relative py-24 text-center lg:py-32">
        <h2 className="mx-auto max-w-2xl text-3xl font-bold tracking-tight text-foreground sm:text-5xl">
          {i18nText("Ship on infrastructure that")}{" "}
          <span className="text-primary">{i18nText("just stays up")}</span>
        </h2>
        <p className="mx-auto mt-5 max-w-xl text-lg text-muted">
          {i18nText("Set up your plan in under a minute. No credit card to start.")}
        </p>
        <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
          {hosting && (
            <Button size="lg" onClick={() => navigate("/hosting")}>
              {i18nText("Configure your plan")}
              <ArrowRight />
            </Button>
          )}
          {services && (
            <Button size="lg" variant={hosting ? "outline" : "default"} onClick={() => navigate("/services")}>
              {i18nText("Browse ready-made apps")}
            </Button>
          )}
          {!hosting && !services && (
            <Button size="lg" onClick={() => navigate("/register")}>
              {i18nText("Create an account")}
              <ArrowRight />
            </Button>
          )}
        </div>

        <dl className="mx-auto mt-16 grid max-w-4xl grid-cols-2 gap-px overflow-hidden rounded-xl border border-border bg-border lg:grid-cols-4">
          {stats.map((s) => (
            <div key={s.label} className="bg-background px-4 py-8">
              <dd className="text-3xl font-bold tracking-tight text-foreground">{s.value}</dd>
              <dt className="mt-1 text-sm text-muted">{s.label}</dt>
            </div>
          ))}
        </dl>
      </Container>
    </section>
  );
}

// ---------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------

export default function Home() {
  const sections = useSections();
  const [services, setServices] = React.useState<ApiService[]>([]);

  React.useEffect(() => {
    if (!sections.services) return;
    let active = true;
    api.services()
      .then((data) => { if (active) setServices(data); })
      .catch(() => { if (active) setServices([]); });
    return () => { active = false; };
  }, [sections.services]);

  React.useEffect(() => {
    document.title = "VELTNEX — " + i18nText("Managed Odoo hosting");
  }, []);

  return (
    <div className="animate-fade-in">
      <Hero hosting={sections.hosting} services={sections.services} />
      <TrustStrip hosting={sections.hosting} />
      <ConsoleShowcase hosting={sections.hosting} />
      {sections.hosting && sections.services && <Paths />}
      <Features hosting={sections.hosting} />
      <HowItWorks hosting={sections.hosting} />
      {sections.hosting && <Versions />}
      {sections.services && <Catalog services={services} />}
      <FinalCta hosting={sections.hosting} services={sections.services} />
    </div>
  );
}
