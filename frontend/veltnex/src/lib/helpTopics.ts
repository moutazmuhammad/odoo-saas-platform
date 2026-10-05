import { i18nText } from "@/i18n";
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
    category: i18nText("Configuring your plan"),
    title: i18nText("Workers"),
    tip: i18nText("How many people can use your instance at the same time — more workers = more simultaneous users."),
    body: [
      i18nText("Workers decide how many people can use your instance at the same time without it slowing down. More workers = more simultaneous users."),
      i18nText("If your team grows or pages feel slow when many people are active, add workers. You can increase them any time; reducing them takes effect on your next bill."),
    ],
  },
  {
    anchor: "storage",
    category: i18nText("Configuring your plan"),
    title: i18nText("Storage"),
    tip: i18nText("Total space for your database and files (uploaded documents, images, attachments)."),
    body: [
      i18nText("Storage is the total space your instance can use — your data plus your uploaded files (documents, images, attachments)."),
      i18nText("Daily backups are kept separately and do NOT count against your storage."),
      i18nText("If you get close to the limit we'll suggest a larger plan. Storage can be increased any time but not reduced below what you're using."),
    ],
  },
  {
    anchor: "region",
    category: i18nText("Configuring your plan"),
    title: i18nText("Region"),
    tip: i18nText("The data-centre location your instance runs in. Pick the one closest to your users. Fixed after creation."),
    body: [
      i18nText("The region is the geographic location of the server your instance runs on. Choose the one closest to your users for the best speed."),
      i18nText("Pricing can vary slightly by region. The region is fixed once the instance is created — to move regions you'd create a new instance."),
    ],
  },
  {
    anchor: "odoo-version",
    category: i18nText("Configuring your plan"),
    title: i18nText("Odoo version"),
    tip: i18nText("Which release of Odoo your instance runs (e.g. 17, 18, 19), Community or Enterprise."),
    body: [
      i18nText("This is the Odoo release your instance runs. Newer versions have more features; older versions may be needed for compatibility with specific modules."),
      i18nText("Pick the version your modules and team target. Upgrading between major versions is a migration, not an automatic switch — plan it deliberately."),
    ],
  },
  {
    anchor: "billing-period",
    category: i18nText("Configuring your plan"),
    title: i18nText("Monthly vs yearly billing"),
    tip: i18nText("Pay monthly, or yearly for a discount. Yearly is billed once up front."),
    body: [
      i18nText("Monthly billing charges you each month and is the most flexible. Yearly billing is paid once up front and comes with a discount."),
      i18nText("You can switch between monthly and yearly when you change your plan."),
    ],
  },
  {
    anchor: "yearly-discount",
    category: i18nText("Configuring your plan"),
    title: i18nText("Yearly discount"),
    tip: i18nText("The percentage you save by paying for a year up front instead of monthly."),
    body: [
      i18nText("When you choose yearly billing you pay less than 12 monthly payments — the difference is the yearly discount."),
      i18nText("The exact percentage is shown next to the price as you configure your plan."),
    ],
  },
  {
    anchor: "subdomain",
    category: i18nText("Configuring your plan"),
    title: i18nText("Subdomain"),
    tip: i18nText("The name in front of your instance's web address, e.g. \"acme\" in acme.example.com."),
    body: [
      i18nText("The subdomain is the unique name at the start of your instance's URL — for example \"acme\" gives you acme.example.com."),
      i18nText("It must be unique and uses only letters, numbers and hyphens. Choose something short and recognisable; it identifies your instance."),
    ],
  },
  {
    anchor: "repo",
    category: i18nText("Managing your instance"),
    title: i18nText("Git repository & Python packages"),
    tip: i18nText("Your custom modules and packages — set up from your instance after launch, not during purchase."),
    body: [
      i18nText("If you write your own Odoo modules, you can point us at your Git repository (GitHub, GitLab or Bitbucket) and the branch to deploy, along with any Python packages your code needs."),
      i18nText("This is configured from your instance's own page after it's created — not during purchase — so you can connect or change it any time. For private repositories you'll provide an access token."),
    ],
  },

  // -------------------------------------------------------------- Add-ons
  {
    anchor: "daily-backup",
    category: "Add-ons",
    title: i18nText("Daily backups"),
    tip: i18nText("A paid add-on: an automatic encrypted snapshot every day, last 7 days kept, so you can restore."),
    body: [
      i18nText("Daily backups take an automatic, encrypted copy of your whole instance once a day, and keep the last 7 days."),
      i18nText("If anything goes wrong you can restore your instance to any of those days from the Snapshots page."),
      i18nText("It's an optional add-on billed monthly."),
    ],
  },
  {
    anchor: "compute-tiers",
    category: "Add-ons",
    title: i18nText("Compute tiers"),
    tip: i18nText("Standard (1 replica), HA (2), Scale (4) and beyond — more replicas means resilience and extra capacity."),
    body: [
      i18nText("Compute tiers set how many pod replicas your instance runs across. Standard is 1 replica (included); HA runs 2 for resilience — if one replica fails or is being updated, the other keeps serving your users with no downtime; Scale runs 4 for resilience plus extra capacity under higher traffic."),
      i18nText("It's only available on the Kubernetes backend; instances running on Docker Compose don't offer tier selection."),
      i18nText("Upgrading to a priced tier is billed monthly and takes effect once paid; downgrading takes effect immediately with no refund for the current period. Changing tiers scales your instance in place — no downtime, no data changes."),
    ],
  },
  {
    anchor: "support-plan",
    category: "Add-ons",
    title: i18nText("Support plan"),
    tip: i18nText("How fast we promise to respond when you need help. Higher tiers = faster guaranteed response."),
    body: [
      i18nText("A support plan sets how quickly we aim to respond when you raise a request — for example within 24 hours, 4 hours, or 1 hour for the top tier."),
      i18nText("Higher tiers give faster, prioritised responses for business-critical workloads. The free tier is best-effort with no guaranteed time."),
      i18nText("If a paid tier is selected it's billed as a flat monthly fee alongside your plan."),
    ],
  },

  // -------------------------------------------------------------- Billing
  {
    anchor: "trial",
    category: "Billing",
    title: i18nText("Free trial"),
    tip: i18nText("Try a full instance free for a limited period — no credit card. It pauses at the end until you pay."),
    body: [
      i18nText("A free trial gives you a working instance for a limited number of days with no payment required."),
      i18nText("When the trial ends the instance pauses until you upgrade to a paid plan — your data is kept so you can pick up where you left off."),
    ],
  },
  {
    anchor: "proration",
    category: "Billing",
    title: i18nText("Upgrade credit"),
    tip: i18nText("When you upgrade partway through a month, you're credited for the days you already paid for."),
    body: [
      i18nText("If you change plan partway through a billing period you don't pay twice. We credit the days you've already paid for on your current plan against the new one, so you only pay the difference."),
    ],
  },
  {
    anchor: "invoice-status",
    category: "Billing",
    title: i18nText("Invoice status"),
    tip: i18nText("Paid, Open (awaiting payment), Overdue (past due), or Partially paid."),
    body: [
      i18nText("Open means the invoice is issued and waiting for payment. Overdue means its due date has passed. Paid means it's settled. Partially paid means some balance remains."),
      i18nText("Unpaid invoices can eventually pause the related instance, so settle Open/Overdue ones to keep services running."),
    ],
  },
  {
    anchor: "decline-invoice",
    category: "Billing",
    title: i18nText("Declining an optional charge"),
    tip: i18nText("Reject an optional charge (like a plan change) you don't want, instead of paying it."),
    body: [
      i18nText("Some invoices are optional — for example a plan upgrade you started but changed your mind about. For those you'll see a Decline option."),
      i18nText("Declining cancels that optional charge and the change it was for. Mandatory charges (your active plan's renewal) can't be declined."),
    ],
  },

  // ----------------------------------------------------------- Managing
  {
    anchor: "change-plan",
    category: i18nText("Managing your instance"),
    title: i18nText("Change plan"),
    tip: i18nText("Adjust workers, storage or billing. Increases apply now; worker cuts apply next cycle."),
    body: [
      i18nText("Change plan lets you adjust your workers, storage and billing period. Upgrades take effect immediately (with a proration credit)."),
      i18nText("Reducing workers takes effect at your next billing cycle, and storage can't be reduced below what you're currently using."),
    ],
  },
  {
    anchor: "reactivate",
    category: i18nText("Managing your instance"),
    title: i18nText("Reactivate a cancelled instance"),
    tip: i18nText("Bring back a cancelled instance from its retained snapshot. A one-time restoration fee may apply."),
    body: [
      i18nText("When an instance is cancelled we keep its last snapshot for a while. Reactivating provisions a fresh instance and restores that snapshot so you get your data back."),
      i18nText("Because we held the snapshot in storage, a one-time restoration fee may apply — it's shown before you confirm."),
    ],
  },
  {
    anchor: "snapshots",
    category: i18nText("Managing your instance"),
    title: i18nText("Snapshots"),
    tip: i18nText("Daily full-instance backups you can restore from. The most recent 7 are kept."),
    body: [
      i18nText("Snapshots are the daily full-instance backups created by the Daily Backups add-on. Each captures every database and your files at that point in time."),
      i18nText("We keep the 7 most recent. You can restore your instance to any of them from the Snapshots page."),
    ],
  },
  {
    anchor: "restore",
    category: i18nText("Managing your instance"),
    title: i18nText("Restoring from a snapshot"),
    tip: i18nText("Roll your instance back to a chosen snapshot. Replaces current data — a safety snapshot is taken first."),
    body: [
      i18nText("Restoring replaces your instance's current databases and files with the state captured in the snapshot you pick."),
      i18nText("We take a fresh safety snapshot first, but anything created since the chosen snapshot will be rolled back — so you're asked to type the instance name to confirm."),
    ],
  },

  // --------------------------------------------------------- Monitoring
  {
    anchor: "cpu-usage",
    category: "Monitoring",
    title: i18nText("CPU usage"),
    tip: i18nText("How much of your plan's processing power your instance is using right now."),
    body: [
      i18nText("This shows, in real time, how much of your allocated processing power the instance is using. Brief spikes are normal."),
      i18nText("If it sits consistently high, your instance is busy — consider adding workers for smoother performance."),
    ],
  },
  {
    anchor: "ram-usage",
    category: "Monitoring",
    title: i18nText("Memory (RAM) usage"),
    tip: i18nText("How much of your plan's memory your instance is using right now."),
    body: [
      i18nText("This shows how much of your allocated memory the instance is using in real time."),
      i18nText("Consistently high memory use can slow things down — a larger plan adds headroom."),
    ],
  },
  {
    anchor: "storage-usage",
    category: "Monitoring",
    title: i18nText("Storage usage"),
    tip: i18nText("How much of your plan's storage allowance is used (database + files)."),
    body: [
      i18nText("This is how much of your storage allowance is in use — your database and uploaded files. Daily backups are stored separately and don't count here."),
      i18nText("As you near the limit we'll prompt an upgrade so you don't run out of space."),
    ],
  },
  {
    anchor: "logs",
    category: "Monitoring",
    title: i18nText("Live logs"),
    tip: i18nText("A real-time stream of your instance's activity, useful for troubleshooting."),
    body: [
      i18nText("Logs are a live stream of what your instance is doing, colour-coded by severity. They're handy when debugging an error or a slow request."),
      i18nText("Logs stream only while the instance is running. Pausing or clearing the view doesn't affect the server."),
    ],
  },

  // --------------------------------------------------------- Databases
  {
    anchor: "create-database",
    category: "Databases",
    title: i18nText("Databases"),
    tip: i18nText("A hosting instance can hold several independent Odoo databases — create, open, back up or delete each."),
    body: [
      i18nText("On a hosting instance you can run more than one independent Odoo database — for example production and a test copy."),
      i18nText("Each database has its own admin login. You can create, open, reset the admin password, back up, or delete a database from this page."),
    ],
  },
];

export function helpTip(anchor: string): string {
  return HELP_TOPICS.find((t) => t.anchor === anchor)?.tip || "";
}
