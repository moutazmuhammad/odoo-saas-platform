import { afterEach, beforeAll, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
import { getLanguage, getLocale, initializeLanguage, i18nText, languageUrl, loadLanguage, translateMessage } from ".";
import { LanguageToggle } from "@/components/LanguageToggle";
import arabic from "../../../../control-plane/saas_website/static/src/i18n/ar.json";

function language(value: string) {
  document.cookie = `veltnex-language=${value}; path=/`;
  initializeLanguage();
}

afterEach(() => {
  document.cookie = "veltnex-language=; max-age=0; path=/";
  document.cookie = "frontend_lang=; max-age=0; path=/";
  localStorage.removeItem("veltnex-language");
  document.documentElement.lang = "en";
  document.documentElement.dir = "ltr";
});

describe("English and Arabic", () => {
  beforeAll(async () => { language("ar"); await loadLanguage(); language("en"); });
  it("persists the server preference and applies RTL before rendering", () => {
    localStorage.setItem("veltnex-language", "en");
    language("ar");
    expect(getLanguage()).toBe("ar");
    expect(getLocale()).toBe("ar-EG");
    expect(document.documentElement.dir).toBe("rtl");
    expect(localStorage.getItem("veltnex-language")).toBe("ar");
    expect(i18nText("My projects")).toBe("مشاريعي");
    language("en");
    expect(document.documentElement.dir).toBe("ltr");
    expect(i18nText("My projects")).toBe("My projects");
  });

  it("respects native Odoo language cookies", () => {
    document.cookie = "frontend_lang=ar_001; path=/";
    expect(getLanguage()).toBe("ar");
  });

  it("preserves entered values, markup as text, and API error details", () => {
    language("ar");
    expect(i18nText("WhatsApp is not set up yet. Enter this code: {0}", ["123456"]))
      .toBe("لم يُعدّ WhatsApp بعد. أدخل هذا الرمز: 123456");
    expect(translateMessage("Database 'ensan_prod' already exists."))
      .toBe("قاعدة البيانات 'ensan_prod' موجودة بالفعل.");
    expect(translateMessage("Unrecognized server detail 192.0.2.1"))
      .toBe("Unrecognized server detail 192.0.2.1");
    expect(i18nText("Set a new admin password for {0}.", ["<script>alert(1)</script>"]))
      .toContain("<script>alert(1)</script>");
  });

  it("offers the other language and preserves the page, query and fragment", () => {
    language("ar");
    render(<BrowserRouter><LanguageToggle /></BrowserRouter>);
    expect(screen.getByRole("link", { name: "التبديل إلى الإنجليزية" })).toHaveTextContent("English");
    const url = new URL(languageUrl("en", "/my/instances/13/environments?tab=code#repo"), "https://example.com");
    expect(url.searchParams.get("redirect")).toBe("/my/instances/13/environments?tab=code#repo");
    expect(url.searchParams.get("lang")).toBe("en");
  });

  it("has translations for every explicit frontend message", () => {
    const files = import.meta.glob("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true });
    const missing = new Set<string>();
    for (const [file, content] of Object.entries(files)) {
      if (file.includes(".test.") || file.includes("/test/") || file.includes("__tests__")) continue;
      for (const match of String(content).matchAll(/i18nText\(("(?:\\.|[^"\\])*")/g)) {
        const source: string = JSON.parse(match[1]);
        if (!(source in arabic)) missing.add(source);
      }
    }
    expect([...missing]).toEqual([]);
  });

  it("retains every interpolation placeholder in the Arabic catalog", () => {
    const pattern = /\{\d+\}|%\([^)]+\)[sdf]|%[sdfdr]/g;
    for (const [source, target] of Object.entries(arabic)) {
      expect([...(target.match(pattern) || [])].sort(), source)
        .toEqual([...(source.match(pattern) || [])].sort());
    }
  });
});
