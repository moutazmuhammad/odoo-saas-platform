import { i18nText } from "@/i18n";
import * as React from "react";
import { Link } from "react-router-dom";
import { DocInline } from "@/components/DocInline";
import { Search, BookOpen, LifeBuoy } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/EmptyState";
import { HELP_TOPICS, type HelpTopic } from "@/lib/helpTopics";

// Group topics by category, preserving first-seen order.
function grouped(topics: HelpTopic[]): { category: string; topics: (HelpTopic & { aliases: string[] })[] }[] {
  const out: { category: string; topics: (HelpTopic & { aliases: string[] })[] }[] = [];
  for (const t of topics) {
    let g = out.find((x) => x.category === t.category);
    if (!g) {
      g = { category: t.category, topics: [] };
      out.push(g);
    }
    const existing = g.topics.find(topic => topic.article === t.article);
    if (existing) existing.aliases.push(t.anchor);
    else g.topics.push({ ...t, aliases: [] });
  }
  return out;
}

export default function Help() {
  const [query, setQuery] = React.useState("");

  const filtered = HELP_TOPICS.filter((t) => {
    const q = query.toLowerCase();
    return (
      !q ||
      t.title.toLowerCase().includes(q) ||
      t.tip.toLowerCase().includes(q) ||
      t.body.some((p) => p.toLowerCase().includes(q))
    );
  });
  const groups = grouped(filtered);

  // Scroll to the anchor from the "?" link once content is rendered.
  React.useEffect(() => {
    if (query) return;
    const hash = window.location.hash.replace("#", "");
    if (!hash) return;
    const anchor = document.getElementById(hash);
    const el = anchor?.closest<HTMLElement>("[data-help-topic]") || anchor;
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
      el.classList.add("ring-2", "ring-primary/40", "rounded-xl");
      const t = setTimeout(
        () => el.classList.remove("ring-2", "ring-primary/40", "rounded-xl"),
        2000
      );
      return () => clearTimeout(t);
    }
  }, [query]);

  return (
    <div className="mx-auto max-w-7xl animate-fade-in px-4 py-16 sm:px-6 lg:px-8">
      <div className="text-center">
        <span className="mx-auto flex size-12 items-center justify-center rounded-2xl bg-primary/15 text-primary">
          <LifeBuoy className="size-6" />
        </span>
        <h1 className="mt-5 text-4xl font-bold tracking-tight">{i18nText("Help & definitions")}</h1>
        <p className="mt-3 text-muted">{i18nText("Plain-language explanations of every option you can choose.")}</p>
      </div>

      <div className="relative mx-auto mt-8 max-w-xl">
        <Search className="absolute start-3.5 top-1/2 size-4 -translate-y-1/2 text-muted" />
        <Input
          className="h-12 ps-10"
          placeholder={i18nText("Search help…")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {groups.length === 0 ? (
        <EmptyState
          className="mt-12"
          icon={BookOpen}
          title={i18nText("Nothing found")}
          description={i18nText("Nothing matches \"{0}\". Try a different term.", [query])}
        />
      ) : (
        <div className="mt-12 space-y-12">
          {groups.map((g) => (
            <section key={g.category}>
              <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">
                {g.category}
              </h2>
              <div className="mt-4 space-y-4">
                {g.topics.map((t) => (
                  <Card key={t.anchor} id={t.anchor} data-help-topic className="scroll-mt-24 p-6">
                    {t.aliases.map(alias => <span key={alias} id={alias} aria-hidden="true" />)}
                    <h3 className="text-lg font-semibold">{t.title}</h3>
                    <p className="mt-1 text-sm font-medium text-primary">{t.tip}</p>
                    <div className="mt-3 space-y-2 text-sm text-muted">
                      {t.body.map((p, i) => (
                        <p key={i}><DocInline text={p} /></p>
                      ))}
                    </div>
                    <Link to={`/docs/${t.article}`} className="mt-4 inline-block text-sm text-primary hover:underline">{i18nText("Read the complete guide")}</Link>
                  </Card>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
