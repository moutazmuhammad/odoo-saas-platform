import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { getLanguage } from "@/i18n";
import { DOC_FOLDERS, findArticle, searchArticles } from "@/lib/docs-content";
import { DocInline } from "@/components/DocInline";
import Docs from "../Docs";
import DocArticle from "../DocArticle";
import Help from "../Help";
import { HELP_TOPICS } from "@/lib/helpTopics";

afterEach(() => { document.cookie = "veltnex-language=; max-age=0; path=/"; localStorage.clear(); document.documentElement.lang = "en"; });
function renderDocs(route: string) {
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  render(<MemoryRouter initialEntries={[route]}><Routes><Route path="/docs" element={<Docs />} /><Route path="/docs/:slug" element={<DocArticle />} /></Routes></MemoryRouter>);
}
describe("Customer documentation", () => {
  it("searches article bodies, technical identifiers, and both languages", () => {
    expect(searchArticles("requirements.txt").flatMap(f => f.articles).map(a => a.id)).toContain("python-dependencies");
    expect(searchArticles("الْهَاتِف").flatMap(f => f.articles).map(a => a.id)).toContain("teammate-onboarding");
    expect(searchArticles("10000", "observability").flatMap(f => f.articles).map(a => a.id)).toEqual(["sql-console"]);
  });
  it("preserves existing public article links and gives every article real related links", () => {
    for (const id of ["overview", "create-account", "launch-instance", "free-trial", "sizing", "region-version", "billing-options", "custom-code", "access-power", "change-plan", "monitoring", "reactivate", "manage-databases", "daily-backups", "restore", "ondemand-backups", "invoices", "optional-charges", "support-plans"]) expect(findArticle(id)).not.toBeNull();
    for (const a of DOC_FOLDERS.flatMap(f => f.articles)) for (const id of a.related) expect(findArticle(id), a.id).not.toBeNull();
  });
  it("filters and navigates from a search result to a complete article", async () => {
    renderDocs("/docs?q=requirements.txt&category=code");
    expect(screen.getByRole("link", { name: /Manage Python dependencies/ })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("link", { name: /Manage Python dependencies/ }));
    expect(screen.getByRole("heading", { level: 1, name: "Manage Python dependencies" })).toBeInTheDocument();
    expect(screen.getByText("phonenumbers==9.0.40", { exact: false }).closest("pre")).toHaveAttribute("dir", "ltr");
  });
  it("renders native Arabic content with the same section anchors and technical code", () => {
    document.cookie = "veltnex-language=ar; path=/";
    expect(getLanguage()).toBe("ar");
    renderDocs("/docs/sql-console");
    expect(screen.getByRole("heading", { level: 1, name: "تشغيل استعلامات SQL للقراءة فقط" })).toBeInTheDocument();
    expect(document.getElementById("before")).not.toBeNull();
    expect(screen.getByText(/SELECT id, name/).closest("pre")).toHaveAttribute("dir", "ltr");
  });
  it("keeps existing help anchors while showing each article excerpt only once", () => {
    render(<MemoryRouter><Help /></MemoryRouter>);
    for (const topic of HELP_TOPICS) expect(document.getElementById(topic.anchor)).not.toBeNull();
    expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(new Set(HELP_TOPICS.map(t => t.article)).size);
    expect(screen.getAllByRole("link", { name: "Read the complete guide" })).toHaveLength(new Set(HELP_TOPICS.map(t => t.article)).size);
  });
  it("renders a role table and treats HTML or unsafe links as plain text", () => {
    renderDocs("/docs/iam-roles");
    expect(screen.getAllByRole("row")).toHaveLength(17);
    const { container } = render(<MemoryRouter><DocInline text={'<script>alert(1)</script> [bad](javascript:alert)'} /></MemoryRouter>);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
  });
});
