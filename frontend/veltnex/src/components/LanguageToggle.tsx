import { Languages } from "lucide-react";
import { getLanguage, languageUrl } from "@/i18n";

export function LanguageToggle() {
  const language = getLanguage();
  return (
    <a href={languageUrl(language === "en" ? "ar" : "en")}
       className="inline-flex shrink-0 items-center gap-1.5 rounded-md px-2 py-2 text-sm font-medium text-muted hover:bg-foreground/5 hover:text-foreground"
       aria-label={language === "en" ? "Switch to Arabic" : "التبديل إلى الإنجليزية"}
       lang={language === "en" ? "ar" : "en"} dir={language === "en" ? "rtl" : "ltr"}>
      <Languages className="size-4" />
      {language === "en" ? "العربية" : "English"}
    </a>
  );
}
