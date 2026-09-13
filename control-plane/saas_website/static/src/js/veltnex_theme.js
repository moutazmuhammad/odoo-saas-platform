/** @odoo-module ignore **/
/*
 * VELTNEX theme system — defense-in-depth.
 *
 * This is a second line of defense on top of the inline `<script>`
 * in `cloudodoo_theme_init` (saas_frontend_layout.xml). Why two
 * copies? Because the inline one only ships if the XML override
 * successfully applies in the running DB — a failed `-u saas_website`
 * (or a stale compiled view) leaves the page without it, and the
 * only symptom is "QWeb pages stay dark even after the SPA toggles
 * light". This file runs as part of web.assets_frontend so it loads
 * on every QWeb page regardless of view-inheritance state.
 *
 * The `@odoo-module ignore` directive at the top tells Odoo 18's
 * asset compiler NOT to wrap this file as an ES6 module — it stays
 * a plain top-level script that runs immediately.
 *
 * The localStorage key (`veltnex-theme`) is shared with the SPA so
 * a toggle on either side carries to the other.
 */
(function () {
    "use strict";

    function apply(t) {
        document.documentElement.setAttribute("data-theme", t);
        document.documentElement.classList.toggle("dark", t === "dark");
    }

    // Persist the choice both to localStorage AND a cookie:
    //  - localStorage handles client-side reads (cross-tab sync, etc.)
    //  - the cookie is sent on every request so the SERVER can read
    //    it and inject `data-theme` into `<html>` at render time,
    //    eliminating FOUC and any reliance on view-inheritance
    //    timing or assets-bundle loading order.
    function persist(t) {
        try { localStorage.setItem("veltnex-theme", t); } catch (e) { /* */ }
        // 1 year, available to the whole site, survives sub-navigation.
        document.cookie = "veltnex-theme=" + t
            + "; path=/; max-age=31536000; SameSite=Lax";
    }

    function toggle() {
        var current = document.documentElement.getAttribute("data-theme") === "dark"
            ? "light" : "dark";
        apply(current);
        persist(current);
    }

    try {
        // CRITICAL: trust the server's `data-theme`. The Odoo backend
        // stamps it onto <html> at render time (from the cookie) before
        // any byte hits the network, so the CSS is already painting
        // the correct palette. The job of this script on load is JUST
        // to keep localStorage + cookie in sync with that value — NOT
        // to re-decide and apply something different (which is what
        // caused the white→dark flash).
        var server = document.documentElement.getAttribute("data-theme");
        if (server === "light" || server === "dark") {
            persist(server);
            try { localStorage.setItem("veltnex-theme", server); } catch (e) { /* */ }
        } else {
            // Defensive fallback only — should rarely happen because
            // both spa.py and cloudodoo_html_theme always emit a
            // `data-theme` attribute.
            var t = localStorage.getItem("veltnex-theme");
            if (t !== "light" && t !== "dark") {
                t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches)
                    ? "light" : "dark";
            }
            apply(t);
            persist(t);
        }

        // Cross-tab live sync.
        window.addEventListener("storage", function (e) {
            if (e.key === "veltnex-theme"
                && (e.newValue === "light" || e.newValue === "dark")) {
                apply(e.newValue);
            }
        });

        // Re-sync when this tab returns to the foreground — `storage`
        // events sometimes don't fire for backgrounded tabs.
        document.addEventListener("visibilitychange", function () {
            if (document.hidden) return;
            try {
                var v = localStorage.getItem("veltnex-theme");
                if ((v === "light" || v === "dark")
                    && v !== document.documentElement.getAttribute("data-theme")) {
                    apply(v);
                }
            } catch (err) { /* ignore */ }
        });

        // Wire up `.vx-theme-toggle` buttons via event delegation —
        // works for both the navbar toggle (rendered server-side at
        // load time) and the login page toggle (rendered inline).
        // Using addEventListener avoids the CSP `unsafe-inline` (and
        // some browsers' "eval-equivalent") warning that the previous
        // `onclick="..."` attribute triggered.
        document.addEventListener("click", function (e) {
            var btn = e.target && e.target.closest
                ? e.target.closest(".vx-theme-toggle")
                : null;
            if (!btn) return;
            e.preventDefault();
            toggle();
        });
    } catch (e) {
        // localStorage unavailable (private mode etc.) — leave default.
    }
})();
