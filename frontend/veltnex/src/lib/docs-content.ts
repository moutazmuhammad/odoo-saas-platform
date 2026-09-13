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
    title: "Getting started",
    description: "What the platform is, creating your account, and launching your first instance.",
    articles: [
      {
        id: "overview",
        image: "dashboard.svg",
        title: "What is VELTNEX?",
        readMinutes: 2,
        summary: "A quick tour of the platform and what you can do with it.",
        body: [
          "VELTNEX is managed hosting for Odoo. We run your Odoo instance for you — servers, backups, SSL, monitoring and uptime — so you can focus on using Odoo, not operating it.",
          "There are two ways to start:",
          "- Hosting — launch your own Odoo instance, pick its size, version and (optionally) your own custom modules from Git.",
          "- Ready-made services — pre-configured Odoo packages you can subscribe to and use immediately.",
          "Everything is managed from your dashboard: create instances, watch their health, manage databases, take and restore backups, and handle billing — all in one place.",
        ],
      },
      {
        id: "create-account",
        image: "register.svg",
        title: "Creating your account",
        readMinutes: 3,
        summary: "Sign up, verify your phone, and you're ready to launch.",
        body: [
          "Click Get started (or Sign in → Create an account) and fill in your details.",
          "1. Enter your name, work email and a password.",
          "2. Add your phone number — we send a 6-digit code by SMS to verify it.",
          "3. Enter the code to confirm, and add your billing country and city.",
          "Your email is used for sign-in, billing and important notifications, so use one you check. Once verified you're taken straight into configuring your first instance.",
        ],
      },
      {
        id: "launch-instance",
        image: "configurator.svg",
        title: "Launching your first instance",
        readMinutes: 5,
        summary: "Step through the hosting configurator and go live.",
        body: [
          "From Hosting, choose a ready-made plan card or build a custom size, then configure it:",
          "1. Choose a subdomain — the name in front of your web address, e.g. acme.example.com.",
          "2. Pick the Odoo version your instance will run.",
          "3. Choose a region close to your users.",
          "4. Set workers and storage for the capacity you need.",
          "5. Optionally add daily backups and a support plan.",
          "6. Choose monthly or yearly billing and continue to checkout.",
          "After payment your instance is provisioned automatically. When it shows Running you can open it from its page and log in to Odoo.",
          "Custom modules from Git and Python packages are set up afterwards, from your instance's own page — not during purchase.",
          "Every option on this screen has a “?” marker — hover it for a one-line explanation, or click it for the full definition.",
        ],
      },
      {
        id: "free-trial",
        image: "configurator.svg",
        title: "Trying it free",
        readMinutes: 2,
        summary: "Explore a real instance before paying.",
        body: [
          "When a free trial is available you can launch a working instance for a limited number of days without paying or entering a card.",
          "Use the trial to set up Odoo and make sure it fits. When the trial ends the instance pauses until you upgrade to a paid plan — your data is kept, so nothing is lost when you continue.",
        ],
      },
    ],
  },
  {
    id: "choosing-plan",
    title: "Choosing your plan",
    description: "Sizing, regions, versions and billing — how to pick what's right.",
    articles: [
      {
        id: "sizing",
        image: "configurator.svg",
        title: "Workers & storage — picking a size",
        readMinutes: 4,
        summary: "How much capacity you need, and how to change it later.",
        body: [
          "Two numbers set your instance's capacity:",
          "- Workers decide how many people can use the instance at the same time without it slowing down. More workers = more simultaneous users.",
          "- Storage is the total space for your data and uploaded files (documents, images, attachments).",
          "Start with a size that fits your team today — you can change it any time. Increases take effect immediately; reducing workers takes effect on your next bill, and storage can't go below what you're already using.",
          "If you get close to your storage limit we'll suggest a larger plan. Daily backups are kept separately and do not use your storage allowance.",
        ],
      },
      {
        id: "region-version",
        image: "configurator.svg",
        title: "Region & Odoo version",
        readMinutes: 3,
        summary: "Where your instance runs and which Odoo it runs.",
        body: [
          "Region is the data-centre location your instance runs in. Pick the one closest to your users for the best speed. The region is fixed once the instance is created.",
          "Odoo version is the release your instance runs (for example 17, 18 or 19), Community or Enterprise. Pick the version your team and modules target. Moving to a newer major version later is a planned migration, not an automatic switch.",
        ],
      },
      {
        id: "billing-options",
        image: "configurator.svg",
        title: "Monthly vs yearly billing",
        readMinutes: 2,
        summary: "Flexibility vs a discount.",
        body: [
          "Monthly billing charges you each month and is the most flexible.",
          "Yearly billing is paid once up front and costs less than 12 monthly payments — the saving is shown as you configure. You can switch between the two when you change your plan.",
        ],
      },
      {
        id: "custom-code",
        image: "configurator.svg",
        title: "Running your own modules",
        readMinutes: 3,
        summary: "Deploy custom Odoo modules from your Git repository — after launch.",
        body: [
          "If your team writes its own Odoo modules, you can have us deploy them from your Git repository (GitHub, GitLab or Bitbucket).",
          "This is set up after your instance is launched — from the instance's own page, not during purchase — so buying stays simple and you can connect or change the repository any time.",
          "You provide the repository URL and branch (plus an access token for private repositories) and your Python package requirements; we pull the code and apply it to your instance.",
        ],
      },
    ],
  },
  {
    id: "managing",
    title: "Managing your instance",
    description: "Access, power controls, scaling, monitoring and reactivation.",
    articles: [
      {
        id: "access-power",
        image: "instance.svg",
        title: "Accessing & powering your instance",
        readMinutes: 3,
        summary: "Open Odoo, and start / stop / restart safely.",
        body: [
          "Open your instance's page from Dashboard → Instances. While it's running, the URL at the top opens your Odoo login in a new tab.",
          "The power controls do what they say:",
          "- Start boots a stopped instance.",
          "- Stop shuts it down gracefully (it stays billable but isn't running).",
          "- Restart restarts a running instance.",
          "Live CPU, memory and storage are shown on the same page so you can see how busy the instance is.",
        ],
      },
      {
        id: "change-plan",
        image: "instance.svg",
        title: "Changing or upgrading your plan",
        readMinutes: 4,
        summary: "Adjust workers, storage and billing — and what applies when.",
        body: [
          "Use Change plan (or Upgrade plan on a trial) on your instance page to adjust workers, storage or billing.",
          "- Upgrades (more workers/storage) take effect immediately. You only pay the difference, because we credit the days you already paid for on your current plan.",
          "- Reducing workers takes effect at your next billing cycle, and storage can't be reduced below what you're using.",
          "If you scheduled a downgrade for next cycle you can cancel it from the same page before it applies.",
        ],
      },
      {
        id: "monitoring",
        image: "instance.svg",
        title: "Monitoring & logs",
        readMinutes: 2,
        summary: "Watch resource usage and stream live logs.",
        body: [
          "Your instance page shows live CPU, memory and storage usage as a share of your plan. Brief spikes are normal; if a value sits high for a long time, consider a larger plan.",
          "The Logs page streams your instance's activity in real time, colour-coded by severity — useful when troubleshooting. Logs stream only while the instance is running.",
        ],
      },
      {
        id: "reactivate",
        image: "instance.svg",
        title: "Reactivating a cancelled instance",
        readMinutes: 3,
        summary: "Bring back a cancelled instance from its retained snapshot.",
        body: [
          "When an instance is cancelled we keep its most recent snapshot for a while. From the cancelled instance's page you can Reactivate it.",
          "Reactivating provisions a fresh instance and restores that snapshot, so you get your data back. Because we held the snapshot in storage, a one-time restoration fee may apply — it's shown before you confirm.",
        ],
      },
    ],
  },
  {
    id: "databases",
    title: "Databases",
    description: "Create, open and manage the databases on a hosting instance.",
    articles: [
      {
        id: "manage-databases",
        image: "databases.svg",
        title: "Creating & managing databases",
        readMinutes: 4,
        summary: "Run more than one Odoo database on a hosting instance.",
        body: [
          "A hosting instance can hold several independent Odoo databases — for example a live one and a test copy. Manage them from your instance's Databases page.",
          "- Create database opens a dialog to name a new database and set its admin login and password.",
          "- Open launches Odoo for that database in a new tab.",
          "- Reset password sets a new admin password.",
          "- Delete permanently removes a database (you'll confirm first).",
          "Each database is independent, with its own data and its own admin user.",
        ],
      },
    ],
  },
  {
    id: "backups",
    title: "Backups & snapshots",
    description: "Protect your data and restore when you need to.",
    articles: [
      {
        id: "daily-backups",
        image: "backups.svg",
        title: "Daily backups",
        readMinutes: 3,
        summary: "Automatic daily protection you can turn on per instance.",
        body: [
          "Daily backups are an optional add-on. Once enabled, we take an automatic encrypted copy of your whole instance every day and keep the last 7.",
          "Turn it on from your instance's Snapshots page. It's billed monthly, and the backups are stored separately — they do not use your plan's storage allowance.",
          "If a backup invoice goes unpaid the daily backups pause (your existing backups are kept) and resume automatically once it's settled.",
        ],
      },
      {
        id: "restore",
        image: "backups.svg",
        title: "Restoring from a snapshot",
        readMinutes: 3,
        summary: "Roll your instance back to an earlier day.",
        body: [
          "On the Snapshots page you'll see the available daily snapshots. To roll back, choose one and click Restore.",
          "Restoring replaces your instance's current data with the state captured in that snapshot. We take a fresh safety snapshot first, but anything created since the chosen snapshot will be rolled back — so you'll be asked to type the instance name to confirm.",
        ],
      },
      {
        id: "ondemand-backups",
        image: "databases.svg",
        title: "On-demand database backups",
        readMinutes: 2,
        summary: "Download a one-off copy of a single database.",
        body: [
          "Besides the automatic daily snapshots, you can take a one-off backup of a single database from the Databases page and download it.",
          "These are handy before a big change or to keep a local copy. The download link is available for a short time after the backup is prepared.",
        ],
      },
    ],
  },
  {
    id: "billing",
    title: "Billing & add-ons",
    description: "Invoices, payments, support plans and how charges work.",
    articles: [
      {
        id: "invoices",
        image: "billing.svg",
        title: "Invoices & payments",
        readMinutes: 3,
        summary: "Find, read and pay your invoices.",
        body: [
          "All your invoices are under Billing. Each has a status:",
          "- Open — issued and awaiting payment.",
          "- Overdue — past its due date.",
          "- Paid — settled.",
          "- Partially paid — some balance remains.",
          "Open an invoice to see its line items and pay it, or download a PDF. Settle Open and Overdue invoices promptly — unpaid invoices can eventually pause the related instance.",
        ],
      },
      {
        id: "optional-charges",
        image: "billing.svg",
        title: "Optional charges & upgrade credit",
        readMinutes: 2,
        summary: "Declining optional charges, and how mid-cycle upgrades are priced.",
        body: [
          "Some invoices are optional — for example a plan upgrade you started but changed your mind about. For those you'll see a Decline option that cancels the charge and the change it was for. Your active plan's renewal can't be declined.",
          "When you upgrade partway through a month you're given an upgrade credit for the days you already paid for, so you only pay the difference.",
        ],
      },
      {
        id: "support-plans",
        image: "configurator.svg",
        title: "Support plans",
        readMinutes: 2,
        summary: "Choose how quickly we respond when you need help.",
        body: [
          "A support plan sets how quickly we aim to respond when you raise a request — for example within 24 hours, 4 hours, or 1 hour on the top tier.",
          "The free tier is best-effort. Paid tiers give faster, prioritised responses for business-critical workloads and are billed monthly alongside your plan.",
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
