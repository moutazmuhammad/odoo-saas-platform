import { i18nText } from "@/i18n";
import * as React from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { ArrowLeft, ArrowRight, FileText, Printer } from "lucide-react";
import { EmptyState } from "@/components/EmptyState";
import { DocContent } from "@/components/DocContent";
import { DOC_FOLDERS, docText, findArticle } from "@/lib/docs-content";
import { formatDate } from "@/lib/format";

export default function DocArticle() {
  const { slug = "" } = useParams();
  const { hash } = useLocation();
  const found = findArticle(slug);
  React.useEffect(() => {
    if (hash) document.getElementById(hash.slice(1))?.scrollIntoView();
    else window.scrollTo({ top: 0 });
    const previous = document.title;
    if (found) document.title = `${docText(found.article.title)} | VELTNEX`;
    return () => { document.title = previous; };
  }, [slug, hash]);
  if (!found) return <div className="mx-auto max-w-3xl px-4 py-16"><EmptyState icon={FileText} title={i18nText("Article not found")} description={i18nText("That documentation page doesn't exist.")} action={<Link to="/docs" className="text-primary hover:underline">{i18nText("Back to documentation")}</Link>} /></div>;
  const { folder, article } = found;
  const index = folder.articles.findIndex(a => a.id === article.id);
  const previous = folder.articles[index - 1];
  const next = folder.articles[index + 1];
  const toc = <ul className="space-y-2">{article.sections.map(section => <li key={section.id}><a href={`#${section.id}`} className="block border-s-2 border-border py-1 ps-3 text-sm text-muted hover:border-primary hover:text-primary">{docText(section.title)}</a></li>)}</ul>;
  return <div className="docs-article mx-auto max-w-[1500px] px-4 py-8 sm:px-6 lg:px-8">
    <div className="docs-navigation mb-6 flex items-center justify-between gap-3"><Link to="/docs" className="inline-flex items-center gap-2 text-sm text-muted hover:text-primary"><ArrowLeft className="size-4" />{i18nText("Documentation")}</Link><button onClick={() => window.print()} className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm text-muted hover:text-primary"><Printer className="size-4" />{i18nText("Print or save PDF")}</button></div>
    <div className="grid gap-8 lg:grid-cols-[220px_minmax(0,1fr)] xl:grid-cols-[220px_minmax(0,1fr)_200px]">
      <aside className="docs-navigation hidden lg:block"><nav aria-label={i18nText("Documentation navigation")} className="sticky top-24 max-h-[calc(100vh-7rem)] overflow-y-auto pe-3">{DOC_FOLDERS.map(group => <details key={group.id} open={group.id === folder.id} className="mb-3"><summary className="cursor-pointer py-2 text-sm font-semibold">{docText(group.title)}</summary><ul className="space-y-1">{group.articles.map(item => <li key={item.id}><Link to={`/docs/${item.id}`} aria-current={item.id === article.id ? "page" : undefined} className={`block rounded-md px-3 py-2 text-sm ${item.id === article.id ? "bg-primary/10 font-medium text-primary" : "text-muted hover:bg-card hover:text-foreground"}`}>{docText(item.title)}</Link></li>)}</ul></details>)}</nav></aside>
      <article className="min-w-0 max-w-4xl">
        <p className="text-sm font-medium text-primary">{docText(folder.title)}</p><h1 className="mt-2 text-3xl font-bold tracking-tight sm:text-4xl">{docText(article.title)}</h1><p className="mt-4 text-lg leading-8 text-muted">{docText(article.summary)}</p><p className="mt-4 text-xs text-muted">{i18nText("Reviewed on {0}", [formatDate(article.reviewed)])}</p>
        <details className="docs-navigation mt-6 rounded-lg border border-border p-4 xl:hidden"><summary className="cursor-pointer text-sm font-semibold">{i18nText("On this page")}</summary><div className="mt-3">{toc}</div></details>
        <div className="mt-8 space-y-10">{article.sections.map(section => <section id={section.id} key={section.id} className="scroll-mt-24"><h2 className="text-xl font-semibold">{docText(section.title)}</h2><div className="mt-4 space-y-4 text-[15px] leading-8">{section.blocks.map((block, i) => <DocContent key={i} block={block} />)}</div></section>)}</div>
        {article.related.length > 0 && <section className="mt-12 border-t border-border pt-6"><h2 className="text-lg font-semibold">{i18nText("Related guides")}</h2><ul className="mt-3 space-y-2">{article.related.map(id => { const related = findArticle(id); return related && <li key={id}><Link to={`/docs/${id}`} className="text-sm text-primary hover:underline">{docText(related.article.title)}</Link></li>; })}</ul></section>}
        <nav aria-label={i18nText("Previous and next articles")} className="docs-navigation mt-10 grid gap-3 border-t border-border pt-6 sm:grid-cols-2">{previous ? <Link to={`/docs/${previous.id}`} className="rounded-lg border border-border p-4 text-sm hover:border-primary"><span className="mb-2 flex items-center gap-2 text-xs text-muted"><ArrowLeft className="size-3" />{i18nText("Previous")}</span>{docText(previous.title)}</Link> : <span />}{next && <Link to={`/docs/${next.id}`} className="rounded-lg border border-border p-4 text-sm hover:border-primary"><span className="mb-2 flex items-center gap-2 text-xs text-muted">{i18nText("Next")}<ArrowRight className="size-3" /></span>{docText(next.title)}</Link>}</nav>
      </article>
      <aside className="docs-navigation hidden xl:block"><nav aria-label={i18nText("On this page")} className="sticky top-24"><p className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted">{i18nText("On this page")}</p>{toc}</nav></aside>
    </div>
  </div>;
}
