import excerpts from "./docs-help.json";
import { getLanguage } from "@/i18n";

export interface HelpTopic {
  anchor: string;
  article: string;
  category: string;
  title: string;
  tip: string;
  body: string[];
}
// Generated from the documentation catalog; stable anchors preserve existing
// question-mark links without maintaining a second set of product claims.
const language = getLanguage();
export const HELP_TOPICS: HelpTopic[] = excerpts.map(topic => ({
  anchor: topic.anchor,
  article: topic.article,
  category: topic.category[language],
  title: topic.title[language],
  tip: topic.tip[language],
  body: topic.body.map(text => text[language]),
}));
export function helpTip(anchor: string): string {
  return HELP_TOPICS.find(topic => topic.anchor === anchor)?.tip || "";
}
