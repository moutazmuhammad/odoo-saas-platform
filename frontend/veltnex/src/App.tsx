import { i18nText } from "@/i18n";
import { Routes, Route, Navigate, useSearchParams } from "react-router-dom";
import * as React from "react";
import { useAuth } from "./context/AuthContext";
import { Spinner } from "./components/Spinner";
import { PublicLayout } from "./components/layout/PublicLayout";
import { PortalLayout } from "./components/layout/PortalLayout";
import { ProtectedRoute } from "./components/layout/ProtectedRoute";
import { useSections } from "./lib/useSections";

import Home from "./pages/Home";
import Hosting from "./pages/Hosting";
const Docs = React.lazy(() => import("./pages/Docs"));
const DocArticle = React.lazy(() => import("./pages/DocArticle"));
import Help from "./pages/Help";
import Login from "./pages/Login";
import ForgotPassword from "./pages/ForgotPassword";
import Register from "./pages/Register";
import NotFound from "./pages/NotFound";

import Dashboard from "./pages/portal/Dashboard";
import Instances from "./pages/portal/Instances";
import InstanceLayout from "./components/layout/InstanceLayout";
import Databases from "./pages/portal/Databases";
import Environments from "./pages/portal/Environments";
import Code from "./pages/portal/Code";
import Logs from "./pages/portal/Logs";
import Backups from "./pages/portal/Backups";
import Snapshots from "./pages/portal/Snapshots";
import Metrics from "./pages/portal/Metrics";
import ShellPage from "./pages/portal/ShellPage";
import SqlPage from "./pages/portal/SqlPage";
import Invoices from "./pages/portal/Invoices";
import InvoiceDetail from "./pages/portal/InvoiceDetail";
import ProjectAccess from "./pages/portal/ProjectAccess";
import PasswordSetup from "./pages/portal/PasswordSetup";
import AcceptAccess from "./pages/portal/AcceptAccess";
import Settings from "./pages/portal/Settings";

// Hard guard: a disabled section's pages aren't reachable even by typing
// the URL. Sections default to enabled while loading, so we never flash a
// redirect before the config arrives.
function RequireSection({
  section,
  children,
}: {
  section: "hosting";
  children: React.ReactNode;
}) {
  const sections = useSections();
  if (!sections[section]) return <Navigate to="/" replace />;
  return <>{children}</>;
}

// A signed-in customer's home is their dashboard, not the marketing page.
// "/?site=1" (the portal's "Website" link) still shows the public site.
export function HomeOrDashboard() {
  const { user, loading } = useAuth();
  const [params] = useSearchParams();
  const showSite = params.has("site");
  // Don't paint the marketing page while the session check is in flight:
  // a signed-in customer would see it flash before the dashboard.
  if (loading && !showSite) return null;
  if (user && !showSite) return <Navigate to="/my/dashboard" replace />;
  return <Home />;
}

export default function App() {
  return (
    <Routes>
      {/* Public */}
      <Route element={<PublicLayout />}>
        <Route path="/" element={<HomeOrDashboard />} />
        <Route path="/register" element={<Register />} />
        <Route path="/hosting" element={<RequireSection section="hosting"><Hosting /></RequireSection>} />
        <Route path="/docs" element={<React.Suspense fallback={<Spinner label={i18nText("Loading…")} />}><Docs /></React.Suspense>} />
        <Route path="/docs/:slug" element={<React.Suspense fallback={<Spinner label={i18nText("Loading…")} />}><DocArticle /></React.Suspense>} />
        <Route path="/help" element={<Help />} />
        <Route path="/login" element={<Login />} />
        <Route path="/forgot-password" element={<ForgotPassword />} />
      </Route>

      {/* Authenticated portal. /my, /my/instances/* and /my/billing/*
          are served by Odoo as the SPA shell (see spa.py); ordering and
          checkout live on Odoo QWeb routes we navigate to directly. */}
      <Route element={<ProtectedRoute />}>
        <Route path="/my/change-password" element={<PasswordSetup />} />
        <Route path="/my/verify-profile" element={<PasswordSetup />} />
        <Route path="/my" element={<PortalLayout />}>
          {/* The summary dashboard is the signed-in customer's home. */}
          <Route index element={<Navigate to="/my/dashboard" replace />} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="home" element={<Navigate to="/my/dashboard" replace />} />
          <Route path="instances" element={<Instances />} />
          {/* One cohesive instance page: a persistent header (InstanceLayout)
              with the section content swapping in place below it. */}
          <Route path="instances/:id" element={<InstanceLayout />}>
            {/* A project opens straight into its Environments workspace. */}
            <Route index element={<Navigate to="environments" replace />} />
            <Route path="access" element={<ProjectAccess />} />
            <Route path="metrics" element={<Metrics />} />
            <Route path="environments" element={<Environments />} />
            <Route path="databases" element={<Databases />} />
            <Route path="code" element={<Code />} />
            <Route path="shell" element={<ShellPage />} />
            <Route path="sql" element={<SqlPage />} />
            <Route path="logs" element={<Logs />} />
            <Route path="backups" element={<Backups />} />
            <Route path="snapshots" element={<Snapshots />} />
          </Route>
          <Route path="access" element={<ProjectAccess />} />
          <Route path="access/accept" element={<AcceptAccess />} />
          <Route path="snapshots" element={<Snapshots />} />
          <Route path="billing" element={<Invoices />} />
          <Route path="billing/:id" element={<InvoiceDetail />} />
          <Route path="settings" element={<Settings />} />
        </Route>
      </Route>

      <Route path="/404" element={<NotFound />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
