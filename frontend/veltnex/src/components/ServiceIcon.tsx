import {
  Boxes,
  Server,
  Database,
  ShoppingCart,
  Globe,
  Mail,
  Shield,
  Workflow,
  Stethoscope,
  Cpu,
  Cloud,
  type LucideIcon,
} from "lucide-react";

// Odoo products carry a Font-Awesome class (e.g. "fa fa-medkit"); the SPA
// renders lucide icons, so map common ones and fall back to a neutral cube.
const MAP: { match: string; icon: LucideIcon }[] = [
  { match: "server", icon: Server },
  { match: "database", icon: Database },
  { match: "cloud", icon: Cloud },
  { match: "shopping", icon: ShoppingCart },
  { match: "cart", icon: ShoppingCart },
  { match: "globe", icon: Globe },
  { match: "envelope", icon: Mail },
  { match: "mail", icon: Mail },
  { match: "shield", icon: Shield },
  { match: "lock", icon: Shield },
  { match: "cogs", icon: Workflow },
  { match: "sitemap", icon: Workflow },
  { match: "medkit", icon: Stethoscope },
  { match: "heartbeat", icon: Stethoscope },
  { match: "microchip", icon: Cpu },
];

export function resolveServiceIcon(faClass?: string): LucideIcon {
  if (!faClass) return Boxes;
  const lower = faClass.toLowerCase();
  return MAP.find((m) => lower.includes(m.match))?.icon ?? Boxes;
}

export function ServiceIcon({
  icon,
  className,
}: {
  icon?: string;
  className?: string;
}) {
  const Icon = resolveServiceIcon(icon);
  return <Icon className={className} />;
}
