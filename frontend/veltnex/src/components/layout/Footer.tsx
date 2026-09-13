import { Link } from "react-router-dom";
import { Github, Twitter, Linkedin } from "lucide-react";
import { Logo } from "@/components/Logo";
import { useSections } from "@/lib/useSections";

export function Footer() {
  const sections = useSections();
  const year = new Date().getFullYear();

  const links = [
    { label: "Docs", to: "/docs" },
    { label: "Help", to: "/help" },
    sections.hosting && { label: "Hosting", to: "/hosting" },
    sections.services && { label: "Services", to: "/services" },
    { label: "Sign in", to: "/login" },
  ].filter(Boolean) as { label: string; to: string }[];

  return (
    <footer className="border-t border-border bg-background">
      <div className="mx-auto flex w-full flex-col items-center gap-3 px-4 py-5 text-xs text-muted sm:flex-row sm:px-6 lg:px-8">
        <div className="flex items-center gap-2.5">
          <Logo withWordmark={false} />
          <span>© {year} VELTNEX, Inc.</span>
        </div>

        <nav className="flex flex-wrap items-center justify-center gap-x-5 gap-y-2 sm:ml-2">
          {links.map((l) => (
            <Link key={l.label} to={l.to} className="transition-colors hover:text-foreground">
              {l.label}
            </Link>
          ))}
        </nav>

        <div className="flex items-center gap-3 sm:ml-auto">
          {[Twitter, Github, Linkedin].map((Icon, i) => (
            <a key={i} href="#" aria-label="Social link" className="transition-colors hover:text-foreground">
              <Icon className="size-4" />
            </a>
          ))}
        </div>
      </div>
    </footer>
  );
}
