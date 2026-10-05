import { i18nText } from "@/i18n";
// Customer-facing documentation for the public /docs pages.
//
// Editorial content (not backend data) kept local on purpose. Each
// article has a `body` of lines using tiny markup, rendered by the
// DocArticle page:
//   "## ..."  → subheading
//   "1. ..."  → ordered step (consecutive lines group into a list)
//   "- ..."   → bullet
//   anything else → paragraph
//
// For short definitions of a single option, the /help page is the
// companion reference; /docs is the how-to narrative.

export interface DocArticle {
  id: string;
  title: string;
  readMinutes: number;
  summary: string;
  body: string[];
  // Optional thumbnail filename under static/spa/docs-img/ (e.g.
  // "launch-instance.png"). Rendered at the top of the article; absent
  // or broken images are simply not shown.
  image?: string;
}

export interface DocFolder {
  id: string;
  title: string;
  description: string;
  articles: DocArticle[];
}

export const DOC_FOLDERS: DocFolder[] = [
  {
    id: "getting-started",
    title: i18nText("Getting started"),
    description: i18nText("What the platform is, creating your account, and launching your first instance."),
    articles: [
      {
        id: "overview",
        image: "dashboard.svg",
        title: i18nText("What is VELTNEX?"),
        readMinutes: 2,
        summary: i18nText("A quick tour of the platform and what you can do with it."),
        body: [
          i18nText("VELTNEX is managed hosting for Odoo. We run your Odoo instance for you — servers, backups, SSL, monitoring and uptime — so you can focus on using Odoo, not operating it."),
          i18nText("There are two ways to start:"),
          i18nText("- Hosting — launch your own Odoo instance, pick its size, version and (optionally) your own custom modules from Git."),
          i18nText("- Ready-made services — pre-configured Odoo packages you can subscribe to and use immediately."),
          i18nText("Everything is managed from your dashboard: create instances, watch their health, manage databases, take and restore backups, and handle billing — all in one place."),
        ],
      },
      {
        id: "create-account",
        image: "register.svg",
        title: i18nText("Creating your account"),
        readMinutes: 3,
        summary: i18nText("Sign up, verify your phone, and you're ready to launch."),
        body: [
          i18nText("Click Get started (or Sign in → Create an account) and fill in your details."),
          i18nText("1. Enter your name, work email and a password."),
          i18nText("2. Add your phone number — we send a 6-digit code by WhatsApp to verify it."),
          i18nText("3. Enter the code to confirm, and add your billing country and city."),
          i18nText("Your email is used for sign-in, billing and important notifications, so use one you check. Once verified you're taken straight into configuring your first instance."),
        ],
      },
      {
        id: "launch-instance",
        image: "configurator.svg",
        title: i18nText("Launching your first instance"),
        readMinutes: 5,
        summary: i18nText("Step through the hosting configurator and go live."),
        body: [
          i18nText("From Hosting, choose a ready-made plan card or build a custom size, then configure it:"),
          i18nText("1. Choose a subdomain — the name in front of your web address, e.g. acme.example.com."),
          i18nText("2. Pick the Odoo version your instance will run."),
          i18nText("3. Choose a region close to your users."),
          i18nText("4. Set workers and storage for the capacity you need."),
          i18nText("5. Optionally add daily backups and a support plan."),
          i18nText("6. Choose monthly or yearly billing and continue to checkout."),
          i18nText("After payment your instance is provisioned automatically. When it shows Running you can open it from its page and log in to Odoo."),
          i18nText("Custom modules from Git and Python packages are set up afterwards, from your instance's own page — not during purchase."),
          i18nText("Every option on this screen has a “?” marker — hover it for a one-line explanation, or click it for the full definition."),
        ],
      },
      {
        id: "free-trial",
        image: "configurator.svg",
        title: i18nText("Trying it free"),
        readMinutes: 2,
        summary: i18nText("Explore a real instance before paying."),
        body: [
          i18nText("When a free trial is available you can launch a working instance for a limited number of days without paying or entering a card."),
          i18nText("Use the trial to set up Odoo and make sure it fits. When the trial ends the instance pauses until you upgrade to a paid plan — your data is kept, so nothing is lost when you continue."),
        ],
      },
    ],
  },
  {
    id: "choosing-plan",
    title: i18nText("Choosing your plan"),
    description: i18nText("Sizing, regions, versions and billing — how to pick what's right."),
    articles: [
      {
        id: "sizing",
        image: "configurator.svg",
        title: i18nText("Workers & storage — picking a size"),
        readMinutes: 4,
        summary: i18nText("How much capacity you need, and how to change it later."),
        body: [
          i18nText("Two numbers set your instance's capacity:"),
          i18nText("- Workers decide how many people can use the instance at the same time without it slowing down. More workers = more simultaneous users."),
          i18nText("- Storage is the total space for your data and uploaded files (documents, images, attachments)."),
          i18nText("Start with a size that fits your team today — you can change it any time. Increases take effect immediately; reducing workers takes effect on your next bill, and storage can't go below what you're already using."),
          i18nText("If you get close to your storage limit we'll suggest a larger plan. Daily backups are kept separately and do not use your storage allowance."),
        ],
      },
      {
        id: "region-version",
        image: "configurator.svg",
        title: i18nText("Region & Odoo version"),
        readMinutes: 3,
        summary: i18nText("Where your instance runs and which Odoo it runs."),
        body: [
          i18nText("Region is the data-centre location your instance runs in. Pick the one closest to your users for the best speed. The region is fixed once the instance is created."),
          i18nText("Odoo version is the release your instance runs (for example 17, 18 or 19), Community or Enterprise. Pick the version your team and modules target. Moving to a newer major version later is a planned migration, not an automatic switch."),
        ],
      },
      {
        id: "billing-options",
        image: "configurator.svg",
        title: i18nText("Monthly vs yearly billing"),
        readMinutes: 2,
        summary: i18nText("Flexibility vs a discount."),
        body: [
          i18nText("Monthly billing charges you each month and is the most flexible."),
          i18nText("Yearly billing is paid once up front and costs less than 12 monthly payments — the saving is shown as you configure. You can switch between the two when you change your plan."),
        ],
      },
      {
        id: "custom-code",
        image: "configurator.svg",
        title: i18nText("Running your own modules"),
        readMinutes: 3,
        summary: i18nText("Deploy custom Odoo modules from your Git repository — after launch."),
        body: [
          i18nText("If your team writes its own Odoo modules, you can have us deploy them from your Git repository (GitHub, GitLab or Bitbucket)."),
          i18nText("This is set up after your instance is launched — from the instance's own page, not during purchase — so buying stays simple and you can connect or change the repository any time."),
          i18nText("You provide the repository URL and branch (plus an access token for private repositories) and your Python package requirements; we pull the code and apply it to your instance."),
        ],
      },
    ],
  },
  {
    id: "managing",
    title: i18nText("Managing your instance"),
    description: i18nText("Access, power controls, scaling, monitoring and reactivation."),
    articles: [
      {
        id: "access-power",
        image: "instance.svg",
        title: i18nText("Accessing & powering your instance"),
        readMinutes: 3,
        summary: i18nText("Open Odoo, and start / stop / restart safely."),
        body: [
          i18nText("Open your instance's page from Dashboard → Instances. While it's running, the URL at the top opens your Odoo login in a new tab."),
          i18nText("The power controls do what they say:"),
          i18nText("- Start boots a stopped instance."),
          i18nText("- Stop shuts it down gracefully (it stays billable but isn't running)."),
          i18nText("- Restart restarts a running instance."),
          i18nText("Live CPU, memory and storage are shown on the same page so you can see how busy the instance is."),
        ],
      },
      {
        id: "change-plan",
        image: "instance.svg",
        title: i18nText("Changing or upgrading your plan"),
        readMinutes: 4,
        summary: i18nText("Adjust workers, storage and billing — and what applies when."),
        body: [
          i18nText("Use Change plan (or Upgrade plan on a trial) on your instance page to adjust workers, storage or billing."),
          i18nText("- Upgrades (more workers/storage) take effect immediately. You only pay the difference, because we credit the days you already paid for on your current plan."),
          i18nText("- Reducing workers takes effect at your next billing cycle, and storage can't be reduced below what you're using."),
          i18nText("If you scheduled a downgrade for next cycle you can cancel it from the same page before it applies."),
        ],
      },
      {
        id: "monitoring",
        image: "instance.svg",
        title: i18nText("Monitoring & logs"),
        readMinutes: 2,
        summary: i18nText("Watch resource usage and stream live logs."),
        body: [
          i18nText("Your instance page shows live CPU, memory and storage usage as a share of your plan. Brief spikes are normal; if a value sits high for a long time, consider a larger plan."),
          i18nText("The Logs page streams your instance's activity in real time, colour-coded by severity — useful when troubleshooting. Logs stream only while the instance is running."),
        ],
      },
      {
        id: "reactivate",
        image: "instance.svg",
        title: i18nText("Reactivating a cancelled instance"),
        readMinutes: 3,
        summary: i18nText("Bring back a cancelled instance from its retained snapshot."),
        body: [
          i18nText("When an instance is cancelled we keep its most recent snapshot for a while. From the cancelled instance's page you can Reactivate it."),
          i18nText("Reactivating provisions a fresh instance and restores that snapshot, so you get your data back. Because we held the snapshot in storage, a one-time restoration fee may apply — it's shown before you confirm."),
        ],
      },
    ],
  },
  {
    id: "databases",
    title: i18nText("Databases"),
    description: i18nText("Create, open and manage the databases on a hosting instance."),
    articles: [
      {
        id: "manage-databases",
        image: "databases.svg",
        title: i18nText("Creating & managing databases"),
        readMinutes: 4,
        summary: i18nText("Run more than one Odoo database on a hosting instance."),
        body: [
          i18nText("A hosting instance can hold several independent Odoo databases — for example a live one and a test copy. Manage them from your instance's Databases page."),
          i18nText("- Create database opens a dialog to name a new database and set its admin login and password."),
          i18nText("- Open launches Odoo for that database in a new tab."),
          i18nText("- Reset password sets a new admin password."),
          i18nText("- Delete permanently removes a database (you'll confirm first)."),
          i18nText("Each database is independent, with its own data and its own admin user."),
        ],
      },
    ],
  },
  {
    id: "backups",
    title: i18nText("Backups & snapshots"),
    description: i18nText("Protect your data and restore when you need to."),
    articles: [
      {
        id: "daily-backups",
        image: "backups.svg",
        title: i18nText("Daily backups"),
        readMinutes: 3,
        summary: i18nText("Automatic daily protection you can turn on per instance."),
        body: [
          i18nText("Daily backups are an optional add-on. Once enabled, we take an automatic encrypted copy of your whole instance every day and keep the last 7."),
          i18nText("Turn it on from your instance's Snapshots page. It's billed monthly, and the backups are stored separately — they do not use your plan's storage allowance."),
          i18nText("If a backup invoice goes unpaid the daily backups pause (your existing backups are kept) and resume automatically once it's settled."),
        ],
      },
      {
        id: "restore",
        image: "backups.svg",
        title: i18nText("Restoring from a snapshot"),
        readMinutes: 3,
        summary: i18nText("Roll your instance back to an earlier day."),
        body: [
          i18nText("On the Snapshots page you'll see the available daily snapshots. To roll back, choose one and click Restore."),
          i18nText("Restoring replaces your instance's current data with the state captured in that snapshot. We take a fresh safety snapshot first, but anything created since the chosen snapshot will be rolled back — so you'll be asked to type the instance name to confirm."),
        ],
      },
      {
        id: "ondemand-backups",
        image: "databases.svg",
        title: i18nText("On-demand database backups"),
        readMinutes: 2,
        summary: i18nText("Download a one-off copy of a single database."),
        body: [
          i18nText("Besides the automatic daily snapshots, you can take a one-off backup of a single database from the Databases page and download it."),
          i18nText("These are handy before a big change or to keep a local copy. The download link is available for a short time after the backup is prepared."),
        ],
      },
    ],
  },
  {
    id: "billing",
    title: i18nText("Billing & add-ons"),
    description: i18nText("Invoices, payments, support plans and how charges work."),
    articles: [
      {
        id: "invoices",
        image: "billing.svg",
        title: i18nText("Invoices & payments"),
        readMinutes: 3,
        summary: i18nText("Find, read and pay your invoices."),
        body: [
          i18nText("All your invoices are under Billing. Each has a status:"),
          i18nText("- Open — issued and awaiting payment."),
          i18nText("- Overdue — past its due date."),
          i18nText("- Paid — settled."),
          i18nText("- Partially paid — some balance remains."),
          i18nText("Open an invoice to see its line items and pay it, or download a PDF. Settle Open and Overdue invoices promptly — unpaid invoices can eventually pause the related instance."),
        ],
      },
      {
        id: "optional-charges",
        image: "billing.svg",
        title: i18nText("Optional charges & upgrade credit"),
        readMinutes: 2,
        summary: i18nText("Declining optional charges, and how mid-cycle upgrades are priced."),
        body: [
          i18nText("Some invoices are optional — for example a plan upgrade you started but changed your mind about. For those you'll see a Decline option that cancels the charge and the change it was for. Your active plan's renewal can't be declined."),
          i18nText("When you upgrade partway through a month you're given an upgrade credit for the days you already paid for, so you only pay the difference."),
        ],
      },
      {
        id: "support-plans",
        image: "configurator.svg",
        title: i18nText("Support plans"),
        readMinutes: 2,
        summary: i18nText("Choose how quickly we respond when you need help."),
        body: [
          i18nText("A support plan sets how quickly we aim to respond when you raise a request — for example within 24 hours, 4 hours, or 1 hour on the top tier."),
          i18nText("The free tier is best-effort. Paid tiers give faster, prioritised responses for business-critical workloads and are billed monthly alongside your plan."),
        ],
      },
    ],
  },
];

export function findArticle(
  slug: string
): { folder: DocFolder; article: DocArticle } | null {
  for (const folder of DOC_FOLDERS) {
    const article = folder.articles.find((a) => a.id === slug);
    if (article) return { folder, article };
  }
  return null;
}
