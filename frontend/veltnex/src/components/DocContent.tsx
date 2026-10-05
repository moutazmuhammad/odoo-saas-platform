import { DocInline } from "./DocInline";
import { Info, TriangleAlert } from "lucide-react";
import { docText, type DocBlock } from "@/lib/docs-content";

export function DocContent({ block }: { block: DocBlock }) {
  if (block.type === "paragraph") return <p className="text-muted"><DocInline text={docText(block.text)} /></p>;
  if (block.type === "code") return <div className="overflow-hidden rounded-lg border border-border bg-background"><div dir="ltr" className="border-b border-border px-4 py-2 text-xs text-muted">{block.language}</div><pre dir="ltr" data-technical className="overflow-x-auto p-4 font-mono text-sm leading-7 text-foreground"><code>{block.code}</code></pre></div>;
  if (block.type === "note") {
    const Icon = block.tone === "warning" ? TriangleAlert : Info;
    return <aside className={`flex gap-3 rounded-lg border p-4 text-sm ${block.tone === "warning" ? "border-warning/30 bg-warning/5" : "border-primary/20 bg-primary/5"}`}><Icon className="mt-1 size-5 shrink-0" /><p><DocInline text={docText(block.text)} /></p></aside>;
  }
  if (block.type === "list") {
    const Tag = block.ordered ? "ol" : "ul";
    return <Tag className={`ms-6 space-y-3 text-muted ${block.ordered ? "list-decimal" : "list-disc"}`}>{block.items.map((text, i) => <li key={i} className="ps-1"><DocInline text={docText(text)} /></li>)}</Tag>;
  }
  return <div className="overflow-x-auto rounded-lg border border-border"><table className="w-full min-w-[460px] border-collapse text-start text-sm"><thead className="bg-background"><tr>{block.headers.map((text, i) => <th key={i} scope="col" className="border-b border-border px-4 py-3 text-start font-semibold"><DocInline text={docText(text)} /></th>)}</tr></thead><tbody className="divide-y divide-border">{block.rows.map((row, i) => <tr key={i}>{row.map((text, j) => <td key={j} className="px-4 py-3 align-top text-muted"><DocInline text={docText(text)} /></td>)}</tr>)}</tbody></table></div>;
}
