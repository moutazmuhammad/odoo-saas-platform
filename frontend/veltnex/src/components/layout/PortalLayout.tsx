import * as React from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  Receipt,
  Settings,
  LogOut,
  Search,
  LifeBuoy,
  LayoutGrid,
  ChevronRight,
  ChevronLeft,
  Menu,
  HelpCircle,
  LayoutDashboard,
  Activity,
  Archive,
  Layers,
} from "lucide-react";
import { Logo } from "@/components/Logo";
import { ThemeToggle } from "@/components/ThemeToggle";
import { CommandPalette } from "@/components/CommandPalette";
import { NotificationsBell } from "@/components/NotificationsBell";
import { ProjectSwitcher } from "@/components/ProjectSwitcher";
import { useAuth } from "@/context/AuthContext";
import { useInstances } from "@/context/InstancesContext";
import { useToast } from "@/context/ToastContext";
import { cn } from "@/lib/utils";

// Projects are reached via the top-bar project switcher (GCP-style), so the
// left menu carries the account sections only — no duplicate Projects entry.
const NAV = [
  { to: "/my/billing", label: "Billing", icon: Receipt },
  // Account "Settings" lives in the avatar dropdown — no duplicate in the rail.
];

type NavItem = { to: string; label: string; icon: typeof Receipt; end?: boolean; tab?: string };

// GCP-Console-style resource nav: when viewing an instance, the left rail shows
// every section of THAT instance so the whole project is one click away.
// For HOSTING projects every section is a tab of the one Environments workspace
// (it never navigates away — the section swaps in place via ?tab=). Managed
// Services keep the classic standalone read-only set.
function instanceSections(id: number, isHosting: boolean): NavItem[] {
  const base = `/my/instances/${id}`;
  if (isHosting) {
    const env = `${base}/environments`;
    // Metrics / Databases / Shell / SQL / Logs / Snapshots are tabs inside the
    // Environments workspace itself — so the rail only carries the workspace
    // entry (Overview) and the project-wide Project settings.
    return [
      { to: env, label: "Overview", icon: Layers, tab: "overview" },
      { to: `${env}?tab=code`, label: "Project settings", icon: Settings, tab: "code" },
    ];
  }
  return [
    { to: base, label: "Overview", icon: LayoutDashboard, end: true },
    { to: `${base}/metrics`, label: "Metrics", icon: Activity },
    { to: `${base}/backups`, label: "Snapshots", icon: Archive },
  ];
}

/** Small dropdown helper: button + panel that closes on outside-click / esc. */
function Dropdown({
  trigger,
  align = "left",
  width = "w-56",
  children,
}: {
  trigger: (open: boolean) => React.ReactNode;
  align?: "left" | "right";
  width?: string;
  children: (close: () => void) => React.ReactNode;
}) {
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef<HTMLDivElement>(null);
  const { pathname } = useLocation();
  React.useEffect(() => setOpen(false), [pathname]);
  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div ref={ref} className="relative">
      <button type="button" onClick={() => setOpen((o) => !o)}>
        {trigger(open)}
      </button>
      {open && (
        <div
          className={cn(
            "absolute z-50 mt-2 overflow-hidden rounded-xl border border-border bg-card p-1.5 shadow-2xl animate-fade-in",
            width,
            align === "right" ? "right-0" : "left-0",
          )}
        >
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}

export function PortalLayout() {
  const { user, logout } = useAuth();
  const { getInstance } = useInstances();
  const toast = useToast();
  const navigate = useNavigate();
  const { pathname, search } = useLocation();
  const [paletteOpen, setPaletteOpen] = React.useState(false);
  // Left nav is an icon rail by default; it expands on hover (or when pinned
  // open via the hamburger). `hovered` drives the auto open/close.
  const [navCollapsed, setNavCollapsed] = React.useState(true);
  const [hovered, setHovered] = React.useState(false);
  const [mobileNav, setMobileNav] = React.useState(false);

  // Which instance (if any) are we inside? Drives the contextual left rail.
  const instMatch = pathname.match(/^\/my\/instances\/(\d+)/);
  const instId = instMatch ? Number(instMatch[1]) : null;
  const inst = instId ? getInstance(instId) : undefined;

  React.useEffect(() => {
    window.scrollTo({ top: 0 });
    setMobileNav(false);
  }, [pathname]);

  // The rail stays collapsed by default (icons only, with hover tooltips); the
  // user expands it on demand via the hamburger. We no longer auto-expand on
  // entering an instance.

  // Global ⌘K / Ctrl+K opens the command palette.
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const handleLogout = () => {
    logout();
    toast.info("Signed out", "You've been securely logged out.");
    navigate("/");
  };

  const NavRow = ({
    item,
    collapsed,
    active,
  }: {
    item: NavItem;
    collapsed: boolean;
    active: boolean;
  }) => (
    <button
      key={item.to}
      title={item.label}
      onClick={() => { setMobileNav(false); navigate(item.to); }}
      className={cn(
        "flex items-center gap-4 rounded-r-full py-2.5 text-sm transition-colors",
        collapsed ? "mx-2 justify-center rounded-lg px-0" : "pl-6 pr-4",
        active
          ? "bg-primary/10 font-medium text-primary"
          : "text-foreground/80 hover:bg-foreground/[0.06]",
      )}
    >
      <item.icon className="size-5 shrink-0" />
      {!collapsed && <span className="truncate">{item.label}</span>}
    </button>
  );

  const isActive = (item: NavItem) => {
    // Hosting sections are tabs of the Environments workspace: same path, the
    // active one is decided by the ?tab= query (default = overview).
    if (item.tab !== undefined) {
      const cur = new URLSearchParams(search).get("tab") || "overview";
      return /\/environments$/.test(pathname) && cur === item.tab;
    }
    return item.end ? pathname === item.to : pathname.startsWith(item.to);
  };

  const NavList = ({ collapsed }: { collapsed: boolean }) => (
    <nav className="flex flex-col gap-0.5 py-2 pr-2">
      {/* INSTANCE CONTEXT: every section of the current project, one click away */}
      {inst && instId != null && (
        <>
          {!collapsed && (
            <div className="mb-1 truncate px-6 pb-1 text-sm font-semibold text-foreground">
              {inst.name}
            </div>
          )}
          {instanceSections(instId, inst.is_hosting).map((item) => (
            <NavRow key={item.to} item={item} collapsed={collapsed} active={isActive(item)} />
          ))}
          <div className="mx-3 my-2 border-t border-border" />
        </>
      )}

      {/* GLOBAL / account sections */}
      {NAV.map((item) => (
        <NavRow key={item.to} item={item} collapsed={collapsed} active={pathname.startsWith(item.to)} />
      ))}
      <div className="mx-3 my-2 border-t border-border" />
      <button
        title="Help & support"
        onClick={() => { setMobileNav(false); navigate("/docs"); }}
        className={cn(
          "flex items-center gap-4 rounded-r-full py-2.5 text-sm text-foreground/80 transition-colors hover:bg-foreground/[0.06]",
          collapsed ? "mx-2 justify-center rounded-lg px-0" : "pl-6 pr-4",
        )}
      >
        <LifeBuoy className="size-5 shrink-0" />
        {!collapsed && <span>Help &amp; support</span>}
      </button>
      {user?.is_internal && (
        <a
          href="/odoo"
          title="Backend"
          className={cn(
            "flex items-center gap-4 rounded-r-full py-2.5 text-sm text-foreground/80 transition-colors hover:bg-foreground/[0.06]",
            collapsed ? "mx-2 justify-center rounded-lg px-0" : "pl-6 pr-4",
          )}
        >
          <LayoutGrid className="size-5 shrink-0" />
          {!collapsed && <span>Backend</span>}
        </a>
      )}
    </nav>
  );

  return (
    <div className="min-h-screen bg-background">
      {/* Google Cloud-style top app bar: menu · product · project · search ·
          help/notifications/account. */}
      <header className="sticky top-0 z-40 flex h-16 items-center gap-1 border-b border-border bg-card px-2 sm:px-4">
        <button
          onClick={() => { setNavCollapsed((c) => !c); setMobileNav((o) => !o); }}
          aria-label="Toggle navigation"
          className="rounded-full p-2.5 text-muted transition-colors hover:bg-foreground/[0.06]"
        >
          <Menu className="size-5" />
        </button>
        <Logo />
        <ProjectSwitcher className="ml-1 hidden sm:block" />

        {/* Center search (GCP hallmark) */}
        <button
          onClick={() => setPaletteOpen(true)}
          className="mx-auto hidden h-11 w-full max-w-2xl items-center gap-3 rounded-lg bg-background px-4 text-sm text-muted transition-colors hover:bg-background/70 hover:ring-1 hover:ring-border md:flex"
        >
          <Search className="size-5" />
          <span>Search resources, projects, and actions</span>
          <kbd className="ml-auto rounded border border-border bg-card px-1.5 py-0.5 text-[10px]">⌘K</kbd>
        </button>

        <div className="ml-auto flex items-center gap-0.5">
          <button
            onClick={() => setPaletteOpen(true)}
            aria-label="Search"
            className="rounded-full p-2.5 text-muted transition-colors hover:bg-foreground/[0.06] md:hidden"
          >
            <Search className="size-5" />
          </button>
          <button
            onClick={() => navigate("/docs")}
            aria-label="Help"
            className="rounded-full p-2.5 text-muted transition-colors hover:bg-foreground/[0.06]"
          >
            <HelpCircle className="size-5" />
          </button>
          <NotificationsBell />
          <ThemeToggle />
          <Dropdown
            align="right"
            width="w-64"
            trigger={(open) => (
              <span
                className={cn(
                  "ml-0.5 flex size-9 items-center justify-center rounded-full bg-primary text-sm font-semibold text-primary-foreground ring-2 ring-transparent transition-all",
                  open && "ring-primary/30",
                )}
              >
                {user?.initials}
              </span>
            )}
          >
            {(close) => (
              <>
                <div className="px-3 py-2">
                  <p className="truncate text-sm font-medium">{user?.name}</p>
                  <p className="truncate text-xs text-muted">{user?.email}</p>
                </div>
                <div className="my-1 border-t border-border" />
                <button
                  onClick={() => { close(); navigate("/my/settings"); }}
                  className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm font-medium text-foreground/90 transition-colors hover:bg-foreground/[0.06]"
                >
                  <Settings className="size-4" />
                  Settings
                </button>
                <button
                  onClick={() => { close(); handleLogout(); }}
                  className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm font-medium text-danger transition-colors hover:bg-danger/10"
                >
                  <LogOut className="size-4" />
                  Sign out
                </button>
              </>
            )}
          </Dropdown>
        </div>
      </header>

      <div className="flex">
        {/* Desktop left nav — an icon rail that expands on hover as an overlay
            (no content reflow), or stays open when pinned via the hamburger.
            The aside reserves the collapsed width in flow; the inner panel
            floats wider over the content when expanded. */}
        <aside
          onMouseEnter={() => setHovered(true)}
          onMouseLeave={() => setHovered(false)}
          className="sticky top-16 z-30 hidden h-[calc(100vh-4rem)] w-16 shrink-0 lg:block"
        >
          <div
            className={cn(
              "absolute inset-y-0 left-0 overflow-y-auto overflow-x-hidden border-r border-border bg-card transition-[width] duration-200",
              hovered || !navCollapsed ? "w-64 shadow-2xl" : "w-16",
            )}
          >
            <NavList collapsed={!(hovered || !navCollapsed)} />
          </div>
        </aside>

        <main className="min-w-0 flex-1">
          <div className="mx-auto w-full px-4 py-6 sm:px-6 lg:px-8">
            <Outlet />
          </div>
        </main>
      </div>

      {/* Mobile nav drawer */}
      {mobileNav && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <div className="absolute inset-0 bg-black/40" onClick={() => setMobileNav(false)} />
          <aside className="absolute inset-y-0 left-0 w-64 overflow-y-auto border-r border-border bg-card pt-2 shadow-2xl">
            <div className="flex h-14 items-center px-4"><Logo /></div>
            <NavList collapsed={false} />
          </aside>
        </div>
      )}

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}

/** Small breadcrumb used at the top of portal sub-pages. */
export function PortalBreadcrumb({
  items,
}: {
  items: { label: string; to?: string }[];
}) {
  return (
    <nav className="mb-6 flex items-center gap-1.5 text-sm text-muted">
      {items.map((item, i) => (
        <span key={i} className="flex items-center gap-1.5">
          {i > 0 && <ChevronRight className="size-3.5 text-muted/50" />}
          {item.to ? (
            <NavLink to={item.to} className="transition-colors hover:text-foreground">
              {item.label}
            </NavLink>
          ) : (
            <span className="text-foreground">{item.label}</span>
          )}
        </span>
      ))}
    </nav>
  );
}
