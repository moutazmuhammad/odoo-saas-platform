# VELTNEX — The Logic of Stability

A production-quality, **frontend-only** SaaS platform UI. Marketing site +
authenticated customer portal for enterprise-grade managed Odoo hosting.
Everything is mocked locally — no backend, no API calls.

## Stack

- **React + TypeScript** (Vite)
- **React Router** (not Next.js)
- **Tailwind CSS** with custom design tokens
- **shadcn/ui-style** components (hand-built, no Radix dependency)
- **lucide-react** icons

## Getting started

```bash
npm install
npm run dev      # http://localhost:5173
npm run build    # type-check + production build
npm run preview  # preview the production build
```

## Mock credentials

- **Login:** any email + 6-char password (pre-filled on the login screen)
- **Register OTP code:** `123456` (use "Auto-fill demo code")

## Routes

**Public:** `/` · `/services` · `/services/:id` · `/services/register` ·
`/hosting` · `/hosting/configure` · `/docs` · `/login`

**Portal (auth-gated, redirects to `/login`):** `/my` · `/my/instances` ·
`/my/instances/:id` · `/my/instances/:id/{databases,logs,backups}` ·
`/my/invoices` · `/my/invoices/:id`

## Architecture notes

- **`src/lib/mock-data.ts`** — single source for users, services, instances,
  invoices, doc folders, and the **hidden pricing model**.
- **`src/lib/pricing.ts`** — the only place internal per-resource rates are used.
  The UI never renders per-resource pricing; it shows workers, storage, and one
  final total.
- **Contexts** — `AuthContext` (mock auth + localStorage), `ToastContext`
  (notifications), `InstancesContext` (live usage simulation, provisioning →
  running transitions, start/stop/restart, database & backup mutations).
- **Reusable components** — `InfoCard`, `AlertBanner`, `StatusBadge`,
  `ActionButton`, `BreakdownRow`, `SliderControl`, `EmptyState`, `Spinner`.

Backend behavior is simulated with `useState`/`useEffect`/`setTimeout`/`setInterval`.
