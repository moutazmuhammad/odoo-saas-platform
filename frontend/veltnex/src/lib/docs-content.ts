import catalog from "./docs-catalog.json";
import { getLanguage } from "@/i18n";

export interface LocalizedText { en: string; ar: string }
export type DocBlock =
  | { type: "paragraph"; text: LocalizedText }
  | { type: "note"; tone: "info" | "warning"; text: LocalizedText }
  | { type: "list"; ordered: boolean; items: LocalizedText[] }
  | { type: "table"; headers: LocalizedText[]; rows: LocalizedText[][] }
  | { type: "code"; language: string; code: string };
export interface DocSection { id: string; title: LocalizedText; blocks: DocBlock[] }
export interface DocArticle {
  id: string;
  title: LocalizedText;
  summary: LocalizedText;
  kind: "guide" | "reference" | "troubleshooting";
  reviewed: string;
  sections: DocSection[];
  related: string[];
  sources: string[];
}
export interface DocFolder {
  id: string;
  title: LocalizedText;
  description: LocalizedText;
  articles: DocArticle[];
}

// One bilingual editorial source. The documentation routes load this separately
// from the application; generated help excerpts and Markdown use the same source.
export const DOC_FOLDERS = catalog as DocFolder[];
export function docText(value: LocalizedText): string { return value[getLanguage()]; }
export function findArticle(slug: string) {
  for (const folder of DOC_FOLDERS) {
    const article = folder.articles.find(a => a.id === slug);
    if (article) return { folder, article };
  }
  return null;
}
export function articleSearchText(article: DocArticle): string {
  const parts: string[] = [article.title.en, article.title.ar, article.summary.en, article.summary.ar];
  const add = (t: LocalizedText) => parts.push(t.en, t.ar);
  for (const section of article.sections) {
    add(section.title);
    for (const block of section.blocks) {
      if (block.type === "paragraph" || block.type === "note") add(block.text);
      else if (block.type === "list") block.items.forEach(add);
      else if (block.type === "table") { block.headers.forEach(add); block.rows.flat().forEach(add); }
      else parts.push(block.code);
    }
  }
  return normalizeSearch(parts.join(" "));
}
export function normalizeSearch(value: string): string {
  return value.toLocaleLowerCase().normalize("NFKD")
    .replace(/[\u064b-\u065f\u0670\u0640]/g, "").replace(/[أإآٱ]/g, "ا").trim();
}
export function searchArticles(query: string, category = "all") {
  const terms = normalizeSearch(query).split(/\s+/).filter(Boolean);
  return DOC_FOLDERS.filter(f => category === "all" || category === f.id).map(folder => ({
    ...folder, articles: folder.articles.filter(a => terms.every(term => articleSearchText(a).includes(term))),
  })).filter(f => f.articles.length > 0);
}
