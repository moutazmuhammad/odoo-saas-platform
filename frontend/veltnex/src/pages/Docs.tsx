import * as React from "react";
import { Link } from "react-router-dom";
import { Search, FileText, Clock, BookOpen, ArrowRight } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/EmptyState";
import { DOC_FOLDERS } from "@/lib/docs-content";

export default function Docs() {
  const [query, setQuery] = React.useState("");

  const folders = DOC_FOLDERS.map((f) => ({
    ...f,
    articles: f.articles.filter((a) =>
      a.title.toLowerCase().includes(query.toLowerCase())
    ),
  })).filter((f) => f.articles.length > 0 || query === "");

  const hasResults = folders.some((f) => f.articles.length > 0);

  return (
    <div className="mx-auto max-w-7xl animate-fade-in px-4 py-16 sm:px-6 lg:px-8">
      <div className="text-center">
        <span className="mx-auto flex size-12 items-center justify-center rounded-2xl bg-primary/15 text-primary">
          <BookOpen className="size-6" />
        </span>
        <h1 className="mt-5 text-4xl font-bold tracking-tight">Documentation</h1>
        <p className="mt-3 text-muted">
          Guides and references to get the most out of VELTNEX.
        </p>
      </div>

      <div className="relative mx-auto mt-8 max-w-xl">
        <Search className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-muted" />
        <Input
          className="h-12 pl-10"
          placeholder="Search the docs…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {!hasResults ? (
        <EmptyState
          className="mt-12"
          icon={FileText}
          title="No articles found"
          description={`Nothing matches "${query}". Try a different search term.`}
        />
      ) : (
        <div className="mt-12 grid gap-6 md:grid-cols-2">
          {folders.map((folder) => (
            <Card key={folder.id} className="p-6">
              <h2 className="text-lg font-semibold">{folder.title}</h2>
              <p className="mt-1 text-sm text-muted">{folder.description}</p>
              <ul className="mt-4 divide-y divide-border">
                {folder.articles.map((a) => (
                  <li key={a.id}>
                    <Link to={`/docs/${a.id}`} className="group flex w-full items-center justify-between gap-3 py-3 text-left">
                      <span className="flex items-center gap-3">
                        <FileText className="size-4 text-muted" />
                        <span className="text-sm transition-colors group-hover:text-primary">
                          {a.title}
                        </span>
                      </span>
                      <span className="flex items-center gap-3 text-xs text-muted">
                        <span className="hidden items-center gap-1 sm:flex">
                          <Clock className="size-3" />
                          {a.readMinutes} min
                        </span>
                        <ArrowRight className="size-3.5 opacity-0 transition-opacity group-hover:opacity-100" />
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
