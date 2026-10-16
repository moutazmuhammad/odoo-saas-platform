import { getLocale, i18nText } from "@/i18n";
import { AlertBanner } from "@/components/AlertBanner";
import type { ApiInstance, ApiUpgradeStatus } from "@/lib/api";

/** "Complete" stays visible this long after delivery, then the banner goes. */
const COMPLETE_VISIBLE_MS = 60 * 60 * 1000;

function clock(value: string) {
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleTimeString(getLocale(), { hour: "2-digit", minute: "2-digit" });
}

/** The customer's view of a paid-for upgrade (C6.4): a bounded wait, said
 *  plainly, and whether they have been charged. */
export function upgradeMessage(upgrade: ApiUpgradeStatus, now = Date.now()) {
  const notCharged = upgrade.charged ? "" : " " + i18nText("You haven't been charged.");
  switch (upgrade.status) {
    case "in_progress":
      return {
        variant: "info" as const,
        title: i18nText("Applying your upgrade"),
        description: i18nText("Usually done by {0}.", [clock(upgrade.target_at)])
          + (upgrade.charged ? "" : " " + i18nText("You'll be charged when it's complete.")),
      };
    case "delayed":
      return {
        variant: "warning" as const,
        title: i18nText("This is taking longer than usual"),
        description: i18nText("Our team is already on it; nothing for you to do.") + notCharged,
      };
    case "action_available":
      return {
        variant: "warning" as const,
        title: i18nText("Your upgrade is delayed"),
        description: (upgrade.can_cancel
          ? i18nText("You can keep waiting or cancel it.")
          : i18nText("Our team is on it; we'll let you know as soon as it's done.")) + notCharged,
      };
    case "complete": {
      const done = new Date(upgrade.completed_at).getTime();
      if (Number.isNaN(done) || now - done > COMPLETE_VISIBLE_MS) return null;
      return {
        variant: "success" as const,
        title: i18nText("Upgrade complete"),
        description: i18nText("Your server now runs its new resources."),
      };
    }
    default:
      return null;
  }
}

export function UpgradeStatusBanner({ project }: { project: Pick<ApiInstance, "upgrade"> }) {
  const message = project.upgrade ? upgradeMessage(project.upgrade) : null;
  if (!message) return null;
  return <AlertBanner className="mb-4" variant={message.variant} title={message.title}
    description={message.description} />;
}
