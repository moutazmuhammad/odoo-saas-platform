import { useId, useState } from "react";
import { Activity, Archive, ArrowUpRight, Check, ChevronRight, Database, FlaskConical, GitBranch, LayoutDashboard, Rocket, Server } from "lucide-react";
import { i18nText } from "@/i18n";
import { cn } from "@/lib/utils";

// A read-only adaptation of the Environments workspace. All values are
// illustrative; this preview never requests or modifies a customer's project.
const SAMPLE = { brand: "VELTNEX", mark: "V", repository: "example / odoo-addons", database: "veltnex_demo" };
const ENVIRONMENTS = [
  { name: "Production", branch: "main", icon: Server },
  { name: "Staging", branch: "staging", icon: FlaskConical },
  { name: "Development", branch: "development", icon: Rocket },
] as const;
const TABS = [
  { name: "Overview", icon: LayoutDashboard },
  { name: "Databases", icon: Database },
  { name: "Snapshots", icon: Archive },
] as const;

export function ProductPreview({ hosting = true }: { hosting?: boolean }) {
  const [environment, setEnvironment] = useState(0);
  const [tab, setTab] = useState(0);
  const id = useId();
  const tabs = hosting ? TABS : TABS.filter(item => item.name !== "Databases");
  const selected = ENVIRONMENTS[hosting ? environment : 0];
  const activeTab = Math.min(tab, tabs.length - 1);

  return (
    <figure className="home-product">
      <div className="home-product-bar">
        <span className="flex items-center gap-2">
          <span className="home-product-mark">
            {SAMPLE.mark}
          </span>
          {SAMPLE.brand}
          <ChevronRight className="size-3 text-muted" />
          <span className="text-muted">
            {i18nText("Example project")}
          </span>
        </span>
        <span className="home-preview-label">
          {i18nText("Interactive preview")}
        </span>
      </div>
      <div className={cn("home-workspace", !hosting && "home-workspace-service")}>
        {hosting && <aside className="home-environments" aria-label={i18nText("Environments")}>
          <p className="home-preview-eyebrow">
            {i18nText("Environments")}
          </p>
          {ENVIRONMENTS.map((env, index) => (
            <button
            key={env.name}
            type="button"
            aria-pressed={environment === index}
            onClick={() => setEnvironment(index)}
            className={cn("home-env focus-ring", environment === index && "is-selected")}
          >
            <env.icon className="size-4 shrink-0" />
            <span>
              <strong>
                {i18nText(env.name)}
              </strong>
              <small>
                <GitBranch className="size-3" />
                {env.branch}
              </small>
            </span>
            <span className="home-status-dot" />
          </button>
          ))}
          <div className="home-env-note">
            <GitBranch className="size-4 text-primary" />
            <p>
              {i18nText("One project. Separate environments.")}
            </p>
          </div>
        </aside>}
        <div className="home-preview-main">
          <div className="home-preview-heading">
            <div>
              <p className="home-preview-eyebrow">
                {i18nText("Environment workspace")}
              </p>
              <h3>
                {i18nText(selected.name)}
              </h3>
            </div>
            <span className="home-online">
              <span className="home-status-dot" />
              {i18nText("Online")}
            </span>
          </div>
          <div className="home-preview-tabs" role="tablist" aria-label={i18nText("Explore the workspace")}>
            {tabs.map((item, index) => <button
              key={item.name}
              type="button"
              role="tab"
              id={`${id}-tab-${index}`}
              aria-selected={activeTab === index}
              aria-controls={`${id}-panel`}
              tabIndex={activeTab === index ? 0 : -1}
              className={cn("focus-ring", activeTab === index && "is-selected")}
              onClick={() => setTab(index)}
              onKeyDown={event => {
              const direction = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
              const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : direction ? (index + direction + tabs.length) % tabs.length : null;
              if (next !== null) { event.preventDefault(); setTab(next); document.getElementById(`${id}-tab-${next}`)?.focus(); }
            }}
            >
              <item.icon className="size-3.5" />
              {i18nText(item.name)}
            </button>)}
          </div>
          <div
            className="home-preview-panel"
            role="tabpanel"
            id={`${id}-panel`}
            aria-labelledby={`${id}-tab-${activeTab}`}
            tabIndex={0}
          >
            {tabs[activeTab].name === "Overview" && <>
              <div className="home-preview-facts">
                {[{ label: "Odoo version", value: "18.0" }, { label: "Workers", value: "2" }, { label: "Storage", value: "20 GB" }].map(item => <div key={item.label}>
                  <p>
                    {i18nText(item.label)}
                  </p>
                  <strong data-technical>
                    {item.value}
                  </strong>
                </div>)}
              </div>
              {hosting && <div className="home-repo">
                <GitBranch className="size-5 text-primary" />
                <div>
                  <p>
                    {i18nText("Git repository")}
                  </p>
                  <strong data-technical>
                    {SAMPLE.repository}
                  </strong>
                </div>
                <code>
                  {selected.branch}
                </code>
              </div>}
              <div className="home-preview-bottom">
                <span>
                  <Activity className="size-4 text-primary" />
                  {i18nText("Metrics & logs in your workspace")}
                </span>
                <Check className="size-4 text-success" />
              </div>
            </>}
            {tabs[activeTab].name === "Databases" && <div className="home-preview-table">
              <div className="home-preview-table-heading">
                <Database className="size-4 text-primary" />
                <strong>
                  {i18nText("Databases")}
                </strong>
              </div>
              <div className="home-preview-table-row">
                <code>
                  {SAMPLE.database}
                </code>
                <span className="home-online">
                  {i18nText("Ready")}
                </span>
              </div>
              <p>
                {i18nText("Back up, restore, and manage your Odoo database.")}
              </p>
            </div>}
            {tabs[activeTab].name === "Snapshots" && <div className="home-preview-table">
              <div className="home-preview-table-heading">
                <Archive className="size-4 text-primary" />
                <strong>
                  {i18nText("Snapshots")}
                </strong>
              </div>
              <div className="home-preview-table-row">
                <span>
                  {i18nText("Example snapshot")}
                </span>
                <span className="home-online">
                  <Check className="size-3" />
                  {i18nText("Available")}
                </span>
              </div>
              <p>
                {i18nText(hosting ? "Review snapshots and restore your environment. Daily backups are an optional add-on." : "Review database snapshots and restore your data. Daily backups are an optional add-on.")}
              </p>
            </div>}
          </div>
        </div>
      </div>
      <figcaption>
        <span>
          {i18nText("Illustrative workspace · sample data")}
        </span>
        <span className="hidden items-center gap-1 sm:flex">
          {i18nText("Select a tab to explore")}
          <ArrowUpRight className="size-3" />
        </span>
      </figcaption>
    </figure>
  );
}
