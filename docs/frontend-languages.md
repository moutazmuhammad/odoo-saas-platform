# Frontend languages

The public website and customer console support English and Arabic. The language button uses `/saas/language`, saves a one-year preference, and returns to the current page with its query and fragment. React pages keep their unprefixed URLs; native Odoo purchase pages use Odoo's normal language routing. The preference is shared by both.

`frontend/veltnex/src/i18n/index.ts` provides `i18nText`, `translateMessage`, and `getLocale`. English source text is the catalog key. Use `i18nText("Message {0}", [value])` for interpolated messages. Preserve identifiers, URLs, commands, permission codes, role codes, and customer-entered names. Never translate values sent to APIs. The language switch reloads the current page; the Arabic catalog loads before page modules initialize, so static navigation and documentation are localized too. English visits do not download the Arabic catalog.

The shared Arabic catalog is `control-plane/saas_website/static/src/i18n/ar.json`. Native page text also uses Odoo's `control-plane/saas_website/i18n/ar.po`. Native interactions and error banners use the shared catalog. The website migration activates Arabic, links English and Arabic to the websites, and loads standard Odoo Arabic translations. It does not upgrade customer instances or their modules.

Follow [the Arabic editorial glossary](arabic-localization-glossary.md) for terminology, names, and contextual writing. Review complete sentences rather than translating isolated fragments. Catalog regression checks protect brand names, technical values, units, markup, and interpolation placeholders, and identify untranslated visible JSX text.

Use logical CSS and Tailwind utilities (`start`, `end`, `ms`, `me`, `ps`, `pe`, `text-start`) for layouts. SQL, terminal output, logs, code, passwords and technical values retain LTR direction. Dates and currency use the selected locale while timestamps retain browser-local time zones.

Run `npm --prefix frontend/veltnex test` and `npm --prefix frontend/veltnex run build`. Catalog tests check all explicit messages and interpolation placeholders. Arabic IAM tests verify that translated controls still submit the original role, project and environment identifiers. Native `TestSpaShellRoutes` tests cover cookies, redirects, unprefixed SPA URLs, the Arabic hosting page, and translated error banners.
