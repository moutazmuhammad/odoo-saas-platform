// Single source of truth for customer-facing help.
//
// Every option a customer can pick has an entry here: a short `tip` (the
// one-line hover explanation shown by the "?" marker) and a longer `body`
// (rendered on the /help page, one section per anchor). The "?" links to
// /help#<anchor>, so the anchor must stay stable.
//
// QWeb help markers (the hosting configure funnel) can't import this file,
// so their tip text is written inline in the template — keep the wording
// in sync with the `tip` here.

export interface HelpTopic {
  anchor: string;
  category: string;
  title: string;
  tip: string; // one line, shown on hover
  body: string[]; // paragraphs, shown on /help
}

export const HELP_TOPICS: HelpTopic[] = [
  // ---------------------------------------------------------------- Plan
  {
    anchor: "workers",
    category: "Configuring your plan",
    title: "Workers",
    tip: "How many people can use your instance at the same time — more workers = more simultaneous users.",
    body: [
      "Workers decide how many people can use your instance at the same time without it slowing down. More workers = more simultaneous users.",
      "If your team grows or pages feel slow when many people are active, add workers. You can increase them any time; reducing them takes effect on your next bill.",
    ],
  },
  {
    anchor: "storage",
    category: "Configuring your plan",
    title: "Storage",
    tip: "Total space for your database and files (uploaded documents, images, attachments).",
    body: [
      "Storage is the total space your instance can use — your data plus your uploaded files (documents, images, attachments).",
      "Daily backups are kept separately and do NOT count against your storage.",
      "If you get close to the limit we'll suggest a larger plan. Storage can be increased any time but not reduced below what you're using.",
    ],
  },
  {
    anchor: "region",
    category: "Configuring your plan",
    title: "Region",
    tip: "The data-centre location your instance runs in. Pick the one closest to your users. Fixed after creation.",
    body: [
      "The region is the geographic location of the server your instance runs on. Choose the one closest to your users for the best speed.",
      "Pricing can vary slightly by region. The region is fixed once the instance is created — to move regions you'd create a new instance.",
    ],
  },
  {
    anchor: "odoo-version",
    category: "Configuring your plan",
    title: "Odoo version",
    tip: "Which release of Odoo your instance runs (e.g. 17, 18, 19), Community or Enterprise.",
    body: [
      "This is the Odoo release your instance runs. Newer versions have more features; older versions may be needed for compatibility with specific modules.",
      "Pick the version your modules and team target. Upgrading between major versions is a migration, not an automatic switch — plan it deliberately.",
    ],
  },
  {
    anchor: "billing-period",
    category: "Configuring your plan",
    title: "Monthly vs yearly billing",
    tip: "Pay monthly, or yearly for a discount. Yearly is billed once up front.",
    body: [
      "Monthly billing charges you each month and is the most flexible. Yearly billing is paid once up front and comes with a discount.",
      "You can switch between monthly and yearly when you change your plan.",
    ],
  },
  {
    anchor: "yearly-discount",
    category: "Configuring your plan",
    title: "Yearly discount",
    tip: "The percentage you save by paying for a year up front instead of monthly.",
    body: [
      "When you choose yearly billing you pay less than 12 monthly payments — the difference is the yearly discount.",
      "The exact percentage is shown next to the price as you configure your plan.",
    ],
  },
  {
    anchor: "subdomain",
    category: "Configuring your plan",
    title: "Subdomain",
    tip: "The name in front of your instance's web address, e.g. \"acme\" in acme.example.com.",
    body: [
      "The subdomain is the unique name at the start of your instance's URL — for example \"acme\" gives you acme.example.com.",
      "It must be unique and uses only letters, numbers and hyphens. Choose something short and recognisable; it identifies your instance.",
    ],
  },
  {
    anchor: "repo",
    category: "Managing your instance",
    title: "Git repository & Python packages",
    tip: "Your custom modules and packages — set up from your instance after launch, not during purchase.",
    body: [
      "If you write your own Odoo modules, you can point us at your Git repository (GitHub, GitLab or Bitbucket) and the branch to deploy, along with any Python packages your code needs.",
      "This is configured from your instance's own page after it's created — not during purchase — so you can connect or change it any time. For private repositories you'll provide an access token.",
    ],
  },

  // -------------------------------------------------------------- Add-ons
  {
    anchor: "daily-backup",
    category: "Add-ons",
    title: "Daily backups",
    tip: "A paid add-on: an automatic encrypted snapshot every day, last 7 days kept, so you can restore.",
    body: [
      "Daily backups take an automatic, encrypted copy of your whole instance once a day, and keep the last 7 days.",
      "If anything goes wrong you can restore your instance to any of those days from the Snapshots page.",
      "It's an optional add-on billed monthly.",
    ],
  },
  {
    anchor: "support-plan",
    category: "Add-ons",
    title: "Support plan",
    tip: "How fast we promise to respond when you need help. Higher tiers = faster guaranteed response.",
    body: [
      "A support plan sets how quickly we aim to respond when you raise a request — for example within 24 hours, 4 hours, or 1 hour for the top tier.",
      "Higher tiers give faster, prioritised responses for business-critical workloads. The free tier is best-effort with no guaranteed time.",
      "If a paid tier is selected it's billed as a flat monthly fee alongside your plan.",
    ],
  },

  // -------------------------------------------------------------- Billing
  {
    anchor: "trial",
    category: "Billing",
    title: "Free trial",
    tip: "Try a full instance free for a limited period — no credit card. It pauses at the end until you pay.",
    body: [
      "A free trial gives you a working instance for a limited number of days with no payment required.",
      "When the trial ends the instance pauses until you upgrade to a paid plan — your data is kept so you can pick up where you left off.",
    ],
  },
  {
    anchor: "proration",
    category: "Billing",
    title: "Upgrade credit",
    tip: "When you upgrade partway through a month, you're credited for the days you already paid for.",
    body: [
      "If you change plan partway through a billing period you don't pay twice. We credit the days you've already paid for on your current plan against the new one, so you only pay the difference.",
    ],
  },
  {
    anchor: "invoice-status",
    category: "Billing",
    title: "Invoice status",
    tip: "Paid, Open (awaiting payment), Overdue (past due), or Partially paid.",
    body: [
      "Open means the invoice is issued and waiting for payment. Overdue means its due date has passed. Paid means it's settled. Partially paid means some balance remains.",
      "Unpaid invoices can eventually pause the related instance, so settle Open/Overdue ones to keep services running.",
    ],
  },
  {
    anchor: "decline-invoice",
    category: "Billing",
    title: "Declining an optional charge",
    tip: "Reject an optional charge (like a plan change) you don't want, instead of paying it.",
    body: [
      "Some invoices are optional — for example a plan upgrade you started but changed your mind about. For those you'll see a Decline option.",
      "Declining cancels that optional charge and the change it was for. Mandatory charges (your active plan's renewal) can't be declined.",
    ],
  },

  // ----------------------------------------------------------- Managing
  {
    anchor: "change-plan",
    category: "Managing your instance",
    title: "Change plan",
    tip: "Adjust workers, storage or billing. Increases apply now; worker cuts apply next cycle.",
    body: [
      "Change plan lets you adjust your workers, storage and billing period. Upgrades take effect immediately (with a proration credit).",
      "Reducing workers takes effect at your next billing cycle, and storage can't be reduced below what you're currently using.",
    ],
  },
  {
    anchor: "reactivate",
    category: "Managing your instance",
    title: "Reactivate a cancelled instance",
    tip: "Bring back a cancelled instance from its retained snapshot. A one-time restoration fee may apply.",
    body: [
      "When an instance is cancelled we keep its last snapshot for a while. Reactivating provisions a fresh instance and restores that snapshot so you get your data back.",
      "Because we held the snapshot in storage, a one-time restoration fee may apply — it's shown before you confirm.",
    ],
  },
  {
    anchor: "snapshots",
    category: "Managing your instance",
    title: "Snapshots",
    tip: "Daily full-instance backups you can restore from. The most recent 7 are kept.",
    body: [
      "Snapshots are the daily full-instance backups created by the Daily Backups add-on. Each captures every database and your files at that point in time.",
      "We keep the 7 most recent. You can restore your instance to any of them from the Snapshots page.",
    ],
  },
  {
    anchor: "restore",
    category: "Managing your instance",
    title: "Restoring from a snapshot",
    tip: "Roll your instance back to a chosen snapshot. Replaces current data — a safety snapshot is taken first.",
    body: [
      "Restoring replaces your instance's current databases and files with the state captured in the snapshot you pick.",
      "We take a fresh safety snapshot first, but anything created since the chosen snapshot will be rolled back — so you're asked to type the instance name to confirm.",
    ],
  },

  // --------------------------------------------------------- Monitoring
  {
    anchor: "cpu-usage",
    category: "Monitoring",
    title: "CPU usage",
    tip: "How much of your plan's processing power your instance is using right now.",
    body: [
      "This shows, in real time, how much of your allocated processing power the instance is using. Brief spikes are normal.",
      "If it sits consistently high, your instance is busy — consider adding workers for smoother performance.",
    ],
  },
  {
    anchor: "ram-usage",
    category: "Monitoring",
    title: "Memory (RAM) usage",
    tip: "How much of your plan's memory your instance is using right now.",
    body: [
      "This shows how much of your allocated memory the instance is using in real time.",
      "Consistently high memory use can slow things down — a larger plan adds headroom.",
    ],
  },
  {
    anchor: "storage-usage",
    category: "Monitoring",
    title: "Storage usage",
    tip: "How much of your plan's storage allowance is used (database + files).",
    body: [
      "This is how much of your storage allowance is in use — your database and uploaded files. Daily backups are stored separately and don't count here.",
      "As you near the limit we'll prompt an upgrade so you don't run out of space.",
    ],
  },
  {
    anchor: "logs",
    category: "Monitoring",
    title: "Live logs",
    tip: "A real-time stream of your instance's activity, useful for troubleshooting.",
    body: [
      "Logs are a live stream of what your instance is doing, colour-coded by severity. They're handy when debugging an error or a slow request.",
      "Logs stream only while the instance is running. Pausing or clearing the view doesn't affect the server.",
    ],
  },

  // --------------------------------------------------------- Databases
  {
    anchor: "create-database",
    category: "Databases",
    title: "Databases",
    tip: "A hosting instance can hold several independent Odoo databases — create, open, back up or delete each.",
    body: [
      "On a hosting instance you can run more than one independent Odoo database — for example production and a test copy.",
      "Each database has its own admin login. You can create, open, reset the admin password, back up, or delete a database from this page.",
    ],
  },
];

export function helpTip(anchor: string): string {
  return HELP_TOPICS.find((t) => t.anchor === anchor)?.tip || "";
}
