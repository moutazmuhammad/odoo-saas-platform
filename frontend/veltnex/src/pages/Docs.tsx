import { i18nText } from "@/i18n";
import { Link, useSearchParams } from "react-router-dom";
import { Search, FileText, BookOpen, ArrowRight } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/EmptyState";
import { DOC_FOLDERS, docText, searchArticles } from "@/lib/docs-content";

export default function Docs() {
  const [params, setParams] = useSearchParams();
  const query = params.get("q") || "";
  const category = params.get("category") || "all";
  const folders = searchArticles(query, category);
  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (!value || value === "all") next.delete(key); else next.set(key, value);
    setParams(next, { replace: true });
  };
  return <div className="mx-auto max-w-[1500px] px-4 py-10 sm:px-6 lg:px-8">
    <div className="max-w-3xl"><BookOpen className="size-8 text-primary" /><h1 className="mt-4 text-3xl font-bold tracking-tight sm:text-4xl">{i18nText("VELTNEX documentation")}</h1><p className="mt-3 leading-7 text-muted">{i18nText("Practical guides, permission references, and troubleshooting for every step of your Odoo hosting workflow.")}</p></div>
    <div className="mt-8 flex flex-col gap-8 lg:flex-row">
      <aside className="lg:w-60 lg:shrink-0"><nav aria-label={i18nText("Documentation categories")} className="flex gap-2 overflow-x-auto pb-2 lg:sticky lg:top-24 lg:flex-col">
        <button onClick={() => update("category", "all")} aria-pressed={category === "all"} className={`shrink-0 rounded-md px-3 py-2 text-start text-sm ${category === "all" ? "bg-primary/10 font-semibold text-primary" : "text-muted hover:bg-card"}`}>{i18nText("All topics")}</button>
        {DOC_FOLDERS.map(folder => <button key={folder.id} onClick={() => update("category", folder.id)} aria-pressed={category === folder.id} className={`shrink-0 rounded-md px-3 py-2 text-start text-sm ${category === folder.id ? "bg-primary/10 font-semibold text-primary" : "text-muted hover:bg-card"}`}>{docText(folder.title)}</button>)}
      </nav></aside>
      <div className="min-w-0 flex-1"><div className="relative"><Search className="absolute start-3.5 top-1/2 size-4 -translate-y-1/2 text-muted" /><Input aria-label={i18nText("Search documentation")} placeholder={i18nText("Search guides, actions, permissions, and errors…")} className="h-12 ps-10" value={query} onChange={e => update("q", e.target.value)} /></div>
        <p role="status" aria-live="polite" className="mt-3 text-sm text-muted">{i18nText("{0} articles", [folders.reduce((n, f) => n + f.articles.length, 0)])}</p>
        {folders.length === 0 ? <EmptyState className="mt-10" icon={FileText} title={i18nText("No articles found")} description={i18nText("Try another term or choose All topics.")} /> : <div className="mt-6 space-y-10">{folders.map(folder => <section key={folder.id}><h2 className="text-xl font-semibold">{docText(folder.title)}</h2><p className="mt-1 text-sm leading-6 text-muted">{docText(folder.description)}</p><div className="mt-4 grid gap-3 xl:grid-cols-2">{folder.articles.map(article => <Card key={article.id} className="h-full"><Link to={`/docs/${article.id}`} className="group block h-full p-5"><p className="text-xs font-medium text-primary">{i18nText(article.kind === "reference" ? "Reference" : article.kind === "troubleshooting" ? "Troubleshooting" : "How-to guide")}</p><div className="mt-2 flex items-start justify-between gap-3"><h3 className="font-semibold group-hover:text-primary">{docText(article.title)}</h3><ArrowRight className="mt-1 size-4 shrink-0 text-muted" /></div><p className="mt-2 text-sm leading-6 text-muted">{docText(article.summary)}</p></Link></Card>)}</div></section>)}</div>}
      </div>
    </div>
  </div>;
}
