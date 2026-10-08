import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Archive, ArrowRight, ArrowUpRight, Check, Database, GitBranch, Layers, Server, ShieldCheck, Terminal } from "lucide-react";
import { i18nText } from "@/i18n";
import { buttonVariants } from "@/components/ui/button";
import { ServiceIcon } from "@/components/ServiceIcon";
import { ProductPreview } from "@/components/home/ProductPreview";
import { api, type ApiService } from "@/lib/api";
import { useSections, type Sections } from "@/lib/useSections";
import { cn } from "@/lib/utils";
import "./Home.css";

function HomeLink({ to, children, secondary = false }: { to: string; children: ReactNode; secondary?: boolean }) {
  return <Link
    to={to}
    className={cn(buttonVariants({ size: "lg", variant: secondary ? "outline" : "default" }), "home-button")}
  >
    {children}
    <ArrowRight aria-hidden="true" />
  </Link>;
}

function LaunchAction({ sections }: { sections: Sections }) {
  return sections.hosting ? <HomeLink to="/hosting">
    {i18nText("Configure your hosting")}
  </HomeLink> : sections.services ? <HomeLink to="/services">
    {i18nText("Explore ready-made apps")}
  </HomeLink> : <HomeLink to="/register">
    {i18nText("Create an account")}
  </HomeLink>;
}

function SectionIntro({ label, title, children }: { label: string; title: string; children?: ReactNode }) {
  return <div className="home-section-intro">
    <p className="home-eyebrow">
      {label}
    </p>
    <h2>
      {title}
    </h2>
    {children && <p className="home-description">
      {children}
    </p>}
  </div>;
}

function LaunchPaths({ sections }: { sections: Sections }) {
  if (!sections.hosting && !sections.services) return null;
  return <section className="home-section home-container" aria-labelledby="launch-heading">
    <div className="home-launch-intro">
      <p className="home-eyebrow">
        {i18nText("Your starting point")}
      </p>
      <h2 id="launch-heading">
        {i18nText(sections.hosting && sections.services ? "Your code. Or a ready-made app." : sections.hosting ? "Your Odoo. Your configuration." : "Start with a ready-made Odoo app.")}
      </h2>
      <p className="home-description">
        {i18nText("Choose how you start. Manage your project in one place.")}
      </p>
    </div>
    <div className={cn("home-paths", sections.hosting && sections.services && "home-paths-two")}>
      {sections.hosting && <article className="home-path">
        <div className="home-path-top">
          <Server className="size-5 text-primary" />
          <span>
            {i18nText("For developers & Odoo teams")}
          </span>
        </div>
        <h3>
          {i18nText("Odoo hosting")}
        </h3>
        <p>
          {i18nText("Bring your custom addons. Choose your Odoo version, workers, storage, and region, then connect your Git repository.")}
        </p>
        <ul>
          {["Community or Enterprise edition", "Custom addons & Python dependencies", "Optional Staging & Development environments"].map(item => <li key={item}>
            <Check className="size-4" />
            {i18nText(item)}
          </li>)}
        </ul>
        <HomeLink to="/hosting">
          {i18nText("Configure your hosting")}
        </HomeLink>
      </article>}
      {sections.services && <article className="home-path">
        <div className="home-path-top">
          <Layers className="size-5 text-primary" />
          <span>
            {i18nText("For teams ready to get to work")}
          </span>
        </div>
        <h3>
          {i18nText("Ready-made apps")}
        </h3>
        <p>
          {i18nText("Start with a preconfigured Odoo service, with its modules and database already in place. Explore the available apps to find your fit.")}
        </p>
        <ul>
          {["Preconfigured modules & database", "Managed service workspace", "Plans available in the app catalog"].map(item => <li key={item}>
            <Check className="size-4" />
            {i18nText(item)}
          </li>)}
        </ul>
        <HomeLink to="/services" secondary={sections.hosting}>
          {i18nText("Explore ready-made apps")}
        </HomeLink>
      </article>}
    </div>
  </section>;
}

function HostingWorkflow() {
  return <section className="home-workflow-section">
    <div className="home-container home-workflow">
      <SectionIntro
        label={i18nText("From development to production")}
        title={i18nText("A workspace for the whole lifecycle.")}
      >
        {i18nText("Keep your code, environments, and operations connected. Build on a Development branch, test in Staging, and manage Production from the same project.")}
        <Link className="home-text-link" to="/docs/project-workspace">
          {i18nText("Explore the project workspace")}
          <ArrowRight className="size-4" />
        </Link>
      </SectionIntro>
      <div className="home-flow" aria-label={i18nText("Development, Staging, and Production")}>
        {[{ number: "01", title: "Development", branch: "development", text: "Build custom addons on a separate Git branch." }, { number: "02", title: "Staging", branch: "staging", text: "Copy databases between environments to test your changes." }, { number: "03", title: "Production", branch: "main", text: "Merge code and inspect deployment history in your workspace." }].map(step => <div className="home-flow-step" key={step.number}>
          <span className="home-flow-number">
            {step.number}
          </span>
          <div>
            <div className="home-flow-title">
              <h3>
                {i18nText(step.title)}
              </h3>
              <code>
                <GitBranch className="size-3" />
                {step.branch}
              </code>
            </div>
            <p>
              {i18nText(step.text)}
            </p>
          </div>
        </div>)}
        <p className="home-flow-footnote">
          {i18nText("Staging and Development require reserved environment slots and a connected repository.")}
        </p>
      </div>
    </div>
  </section>;
}

function Operations({ hosting }: { hosting: boolean }) {
  return <section className="home-container home-section">
    <SectionIntro label={i18nText("Day-to-day operations")} title={i18nText("Less switching. More visibility.")}>
      {i18nText("The tools to understand your Odoo environment, look after your data, and give your team the right access.")}
    </SectionIntro>
    <div className="home-operations">
      <article className="home-data-feature">
        <div className="home-feature-copy">
          <Archive className="size-5 text-primary" />
          <h3>
            {i18nText("Keep your data within reach.")}
          </h3>
          <p>
            {i18nText(hosting ? "Manage databases, create on-demand database backups, and restore full-instance snapshots. Enable optional daily backups when your project needs them." : "Review and restore database snapshots from your service workspace. Enable optional daily backups when your project needs them.")}
          </p>
          <Link to="/docs/daily-backups" className="home-text-link">
            {i18nText("Read about backups")}
            <ArrowRight className="size-4" />
          </Link>
        </div>
        <div className="home-data-diagram" aria-label={i18nText("Backup and restore workflow")}>
          <Database className="size-6" />
          <span className="home-diagram-line" />
          <Archive className="size-6" />
          <span className="home-diagram-line" />
          <ShieldCheck className="size-6" />
          <span>
            {i18nText("Database")}
          </span>
          <span>
            {i18nText("Snapshot")}
          </span>
          <span>
            {i18nText("Restore")}
          </span>
        </div>
      </article>
      <div className="home-operation-details">
        <article>
          <Terminal className="size-5 text-primary" />
          <div>
            <h3>
              {i18nText("See what your environment is doing.")}
            </h3>
            <p>
              {i18nText(hosting ? "Inspect resource metrics and application logs. Open the browser Shell or read-only SQL console when you need a closer look." : "Inspect resource metrics and application logs from your service workspace.")}
            </p>
            <Link to="/docs/monitoring" className="home-text-link">
              {i18nText("Explore monitoring")}
              <ArrowUpRight className="size-4" />
            </Link>
          </div>
        </article>
        <article>
          <ShieldCheck className="size-5 text-primary" />
          <div>
            <h3>
              {i18nText("Give each teammate the right access.")}
            </h3>
            <p>
              {i18nText("Assign project roles and scope access to specific environments. Manage teammates and invitations from the portal.")}
            </p>
            <Link to="/docs/iam-overview" className="home-text-link">
              {i18nText("Understand team permissions")}
              <ArrowUpRight className="size-4" />
            </Link>
          </div>
        </article>
      </div>
    </div>
  </section>;
}

export default function Home() {
  const sections = useSections();
  const [services, setServices] = useState<ApiService[]>([]);
  useEffect(() => {
    const originalTitle = document.title;
    const description = document.querySelector('meta[name="description"]');
    const originalDescription = description?.getAttribute("content");
    document.title = i18nText("VELTNEX — The Odoo hosting platform");
    description?.setAttribute("content", i18nText("Launch and manage Odoo on Veltnex. Hosting, ready-made apps, project environments, databases, snapshots, and monitoring in one workspace."));
    return () => {
      document.title = originalTitle;
      if (originalDescription != null) description?.setAttribute("content", originalDescription);
    };
  }, []);
  useEffect(() => {
    if (!sections.services) return;
    let active = true;
    api.services().then(data => { if (active) setServices(data); }).catch(() => { if (active) setServices([]); });
    return () => { active = false; };
  }, [sections.services]);

  return <div className="veltnex-home">
    <section className="home-hero home-container" aria-labelledby="home-heading">
      <div className="home-hero-copy">
        <div>
          <p className="home-eyebrow">
            <span className="home-eyebrow-rule" />
            {i18nText("The Odoo hosting platform")}
          </p>
          <h1 id="home-heading">
            {i18nText("Your Odoo.")}
            <br />
            <span>
              {i18nText("Under control.")}
            </span>
          </h1>
        </div>
        <div className="home-hero-summary">
          <p>
            {i18nText(sections.hosting ? "Launch and manage Odoo in one workspace. Connect your code, work across environments, and keep your databases and operations in view." : "Launch a ready-made Odoo app and manage it in one workspace, with your databases and operations in view.")}
          </p>
          <div className="home-actions">
            <LaunchAction sections={sections} />
            <HomeLink to="/docs/overview" secondary>
              {i18nText("Meet the platform")}
            </HomeLink>
          </div>
        </div>
      </div>
      <ProductPreview hosting={sections.hosting} />
      <div className="home-capability-strip">
        <p>
          {i18nText("Built around your Odoo project")}
        </p>
        <div>
          {(sections.hosting ? ["Git-based deployments", "Separate environments", "Databases & snapshots", "Metrics & logs"] : ["Ready-made apps", "Database snapshots", "Metrics & logs", "Team permissions"]).map(item => <span key={item}>
            <Check className="size-3.5 text-primary" />
            {i18nText(item)}
          </span>)}
        </div>
      </div>
    </section>
    <LaunchPaths sections={sections} />
    {sections.hosting && <HostingWorkflow />}
    <Operations hosting={sections.hosting} />
    {sections.services && services.length > 0 && <section className="home-catalog-section">
      <div className="home-container home-section">
        <div className="home-catalog-heading">
          <SectionIntro label={i18nText("Available on Veltnex")} title={i18nText("Find your starting point.")}>
            {i18nText("Explore the ready-made services available on the platform.")}
          </SectionIntro>
          <Link to="/services" className="home-text-link">
            {i18nText("View all apps")}
            <ArrowRight className="size-4" />
          </Link>
        </div>
        <div className="home-catalog">
          {services.slice(0, 3).map(service => <Link key={service.id} to={`/services/${service.id}`} className="home-catalog-item focus-ring">
            <ServiceIcon icon={service.icon} className="size-5 text-primary" />
            <h3>
              {service.name}
            </h3>
            <p>
              {service.tagline}
            </p>
            <span>
              {i18nText("Explore app")}
              <ArrowUpRight className="size-4" />
            </span>
          </Link>)}
        </div>
      </div>
    </section>}
    <section className="home-container home-section home-start">
      <div>
        <p className="home-eyebrow">
          {i18nText("Your next project starts here")}
        </p>
        <h2>
          {i18nText("Make room for your Odoo.")}
        </h2>
        <p className="home-description">
          {i18nText(sections.hosting ? "Choose the configuration that fits your project. Your code and your operations, together on Veltnex." : "Explore available services and choose the app that fits your team.")}
        </p>
      </div>
      <div className="home-start-actions">
        <LaunchAction sections={sections} />
        <Link to={sections.hosting ? "/docs/launch-instance" : "/docs/managed-services"} className="home-text-link">
          {i18nText("Read the getting-started guide")}
          <ArrowUpRight className="size-4" />
        </Link>
      </div>
    </section>
  </div>;
}
