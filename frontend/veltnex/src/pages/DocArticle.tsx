import * as React from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Clock, ArrowRight } from "lucide-react";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/EmptyState";
import { FileText } from "lucide-react";
import { findArticle } from "@/lib/docs-content";

// Render the tiny markup used in article bodies into grouped blocks.
function renderBody(lines: string[]) {
  const blocks: React.ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;

  const flush = () => {
    if (!list) return;
    const items = list.items;
    blocks.push(
      list.ordered ? (
        <ol key={blocks.length} className="ml-5 list-decimal space-y-2 text-muted">
          {items.map((t, i) => <li key={i}>{t}</li>)}
        </ol>
      ) : (
        <ul key={blocks.length} className="ml-5 list-disc space-y-2 text-muted">
          {items.map((t, i) => <li key={i}>{t}</li>)}
        </ul>
      )
    );
    list = null;
  };

  for (const raw of lines) {
    const line = raw.trim();
    const ordered = /^\d+\.\s/.test(line);
    const bullet = line.startsWith("- ");
    if (ordered || bullet) {
      const text = ordered ? line.replace(/^\d+\.\s/, "") : line.slice(2);
      if (!list || list.ordered !== ordered) {
        flush();
        list = { ordered, items: [] };
      }
      list.items.push(text);
      continue;
    }
    flush();
    if (line.startsWith("## ")) {
      blocks.push(
        <h2 key={blocks.length} className="mt-8 text-lg font-semibold text-foreground">
          {line.slice(3)}
        </h2>
      );
    } else {
      blocks.push(
        <p key={blocks.length} className="text-muted">{line}</p>
      );
    }
  }
  flush();
  return blocks;
}

export default function DocArticle() {
  const { slug = "" } = useParams();
  const found = findArticle(slug);

  React.useEffect(() => {
    window.scrollTo({ top: 0 });
  }, [slug]);

  if (!found) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-16 sm:px-6 lg:px-8">
        <EmptyState
          icon={FileText}
          title="Article not found"
          description="That documentation page doesn't exist."
          action={<Link to="/docs" className="text-primary hover:underline">Back to documentation</Link>}
        />
      </div>
    );
  }

  const { folder, article } = found;
  const siblings = folder.articles;
  const idx = siblings.findIndex((a) => a.id === article.id);
  const next = siblings[idx + 1];

  return (
    <div className="mx-auto max-w-3xl animate-fade-in px-4 py-12 sm:px-6 lg:px-8">
      <Link to="/docs" className="inline-flex items-center gap-1.5 text-sm text-muted transition-colors hover:text-foreground">
        <ArrowLeft className="size-4" />
        Documentation
      </Link>

      <div className="mt-6">
        <p className="text-sm font-medium text-primary">{folder.title}</p>
        <h1 className="mt-1 text-3xl font-bold tracking-tight">{article.title}</h1>
        <p className="mt-2 flex items-center gap-1.5 text-xs text-muted">
          <Clock className="size-3" />
          {article.readMinutes} min read
        </p>
      </div>

      {article.image && (
        <img
          src={`/saas_website/static/spa/docs-img/${article.image}`}
          alt={article.title}
          loading="lazy"
          onError={(e) => {
            (e.currentTarget as HTMLImageElement).style.display = "none";
          }}
          className="mt-8 w-full rounded-xl border border-border shadow-card"
        />
      )}

      <div className="mt-8 space-y-4 text-[15px] leading-relaxed">
        {renderBody(article.body)}
      </div>

      {next && (
        <Card className="mt-12 p-5">
          <p className="text-xs uppercase tracking-wide text-muted">Next</p>
          <Link
            to={`/docs/${next.id}`}
            className="mt-1 flex items-center justify-between gap-3 font-medium transition-colors hover:text-primary"
          >
            {next.title}
            <ArrowRight className="size-4" />
          </Link>
        </Card>
      )}
    </div>
  );
}
