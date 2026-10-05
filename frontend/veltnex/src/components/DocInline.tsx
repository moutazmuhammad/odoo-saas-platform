import * as React from "react";
import { Link } from "react-router-dom";

/** Deliberately limited inline markup: no HTML or executable links. */
export function DocInline({ text }: { text: string }) {
  const parts: React.ReactNode[] = [];
  const pattern = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)]+)\)/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    parts.push(text.slice(cursor, match.index));
    if (match[1]) parts.push(<code key={match.index} className="rounded bg-border/60 px-1 py-0.5 font-mono text-[0.9em] text-foreground" dir="ltr">{match[1]}</code>);
    else if (match[2]) parts.push(<strong key={match.index} className="font-semibold text-foreground">{match[2]}</strong>);
    else if (/^\/docs\/[a-z0-9-]+(?:#[a-z0-9-]+)?$/.test(match[4])) parts.push(<Link key={match.index} to={match[4]} className="text-primary underline underline-offset-4">{match[3]}</Link>);
    else parts.push(match[0]);
    cursor = match.index! + match[0].length;
  }
  parts.push(text.slice(cursor));
  return <>{parts}</>;
}
