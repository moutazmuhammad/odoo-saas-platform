
export type Language = "en" | "ar";
let translations: Record<string, string> = {};
let catalogPromise: Promise<void> | undefined;

export function loadLanguage(): Promise<void> {
  if (getLanguage() !== "ar") return Promise.resolve();
  return catalogPromise ??= import("../../../../control-plane/saas_website/static/src/i18n/ar.json").then(module => {
    translations = module.default;
    messagePatterns = compileMessagePatterns();
  });
}

/** Read the same preference used by Odoo's native website pages. */
export function getLanguage(): Language {
  if (typeof document === "undefined") return "en";
  const cookie = document.cookie.match(/(?:^|;\s*)veltnex-language=(en|ar)(?:;|$)/);
  if (cookie) return cookie[1] as Language;
  const native = document.cookie.match(/(?:^|;\s*)frontend_lang=([^;]+)/)?.[1];
  if (native?.startsWith("ar")) return "ar";
  if (native?.startsWith("en")) return "en";
  try {
    const stored = localStorage.getItem("veltnex-language");
    if (stored === "en" || stored === "ar") return stored;
  } catch { /* Cookies still work when browser storage is unavailable. */ }
  return document.documentElement.lang.startsWith("ar") ? "ar" : "en";
}

export function getLocale(): string {
  return getLanguage() === "ar" ? "ar-EG" : "en-US";
}

/** English source keys keep missing/new messages readable; values never become HTML. */
export function i18nText(source: string, values: readonly unknown[] = []): string {
  const translated = getLanguage() === "ar" ? translations[source] ?? source : source;
  return translated.replace(/\{(\d+)\}/g, (token, index) =>
    Number(index) < values.length ? String(values[Number(index)] ?? "") : token,
  );
}

export function initializeLanguage(): void {
  const language = getLanguage();
  document.documentElement.lang = language;
  document.documentElement.dir = language === "ar" ? "rtl" : "ltr";
  try { localStorage.setItem("veltnex-language", language); } catch { /* optional */ }
}

/** Full navigation also updates native Odoo pages and server-rendered messages. */
export function languageUrl(language: Language, destination = location.pathname + location.search + location.hash): string {
  return `/saas/language?${new URLSearchParams({ lang: language, redirect: destination })}`;
}

const placeholderPattern = /\{\d+\}|%\([^)]+\)[sdf]|%[sdfdr]/g;
let messagePatterns: { regex: RegExp; tokens: string[]; target: string }[] = [];
function compileMessagePatterns() {
return Object.entries(translations).filter(([source]) => /\{\d+\}|%\([^)]+\)[sdf]|%[sdfdr]/.test(source)).map(([source, target]) => {
  const tokens = [...source.matchAll(placeholderPattern)].map(match => match[0]);
  const escape = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  let cursor = 0;
  const parts: string[] = [];
  for (const match of source.matchAll(placeholderPattern)) {
    parts.push(escape(source.slice(cursor, match.index)), "([\\s\\S]+?)");
    cursor = match.index! + match[0].length;
  }
  parts.push(escape(source.slice(cursor)));
  return { regex: new RegExp(`^${parts.join("")}$`), tokens, target };
});
}

/** Translate formatted API errors without changing interpolated names or details. */
export function translateMessage(message: string): string {
  if (getLanguage() !== "ar") return message;
  if (translations[message]) return translations[message];
  for (const { regex, tokens, target } of messagePatterns) {
    const match = regex.exec(message);
    if (!match) continue;
    const used = new Set<number>();
    return target.replace(placeholderPattern, token => {
      const index = tokens.findIndex((candidate, i) => candidate === token && !used.has(i));
      if (index === -1) return token;
      used.add(index);
      return match[index + 1];
    });
  }
  return message;
}
