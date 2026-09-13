import { Routes, Route, Navigate, useParams } from "react-router-dom";
import * as React from "react";
import { useInstances } from "./context/InstancesContext";
import { Spinner } from "./components/Spinner";
import { PublicLayout } from "./components/layout/PublicLayout";
import { PortalLayout } from "./components/layout/PortalLayout";
import { ProtectedRoute } from "./components/layout/ProtectedRoute";
import { useSections } from "./lib/useSections";

import Home from "./pages/Home";
import Services from "./pages/Services";
import ServiceDetail from "./pages/ServiceDetail";
import Hosting from "./pages/Hosting";
import Docs from "./pages/Docs";
import DocArticle from "./pages/DocArticle";
import Help from "./pages/Help";
import Login from "./pages/Login";
import ForgotPassword from "./pages/ForgotPassword";
import Register from "./pages/Register";
import NotFound from "./pages/NotFound";

import Instances from "./pages/portal/Instances";
import InstanceDetail from "./pages/portal/InstanceDetail";
import InstanceLayout from "./components/layout/InstanceLayout";
import Databases from "./pages/portal/Databases";
import Environments from "./pages/portal/Environments";
import Code from "./pages/portal/Code";
import Logs from "./pages/portal/Logs";
import Backups from "./pages/portal/Backups";
import Metrics from "./pages/portal/Metrics";
import ShellPage from "./pages/portal/ShellPage";
import SqlPage from "./pages/portal/SqlPage";
import Invoices from "./pages/portal/Invoices";
import InvoiceDetail from "./pages/portal/InvoiceDetail";
import Settings from "./pages/portal/Settings";

// Hard guard: a disabled section's pages aren't reachable even by typing
// the URL. Sections default to enabled while loading, so we never flash a
// redirect before the config arrives.
function RequireSection({
  section,
  children,
}: {
  section: "services" | "hosting";
  children: React.ReactNode;
}) {
  const sections = useSections();
  if (!sections[section]) return <Navigate to="/" replace />;
  return <>{children}</>;
}

// The instance landing page. Hosting projects open straight into the
// Environments workspace (the section tools live there as in-place tabs);
// managed Services keep the classic Overview.
function InstanceHome() {
  const { id = "" } = useParams();
  const { getInstance, loading } = useInstances();
  const inst = getInstance(Number(id));
  if (!inst && loading) {
    return (
      <div className="mt-20 flex justify-center">
        <Spinner size="lg" label="Loading…" />
      </div>
    );
  }
  if (inst?.is_hosting) return <Navigate to="environments" replace />;
  return <InstanceDetail />;
}

export default function App() {
  return (
    <Routes>
      {/* Public */}
      <Route element={<PublicLayout />}>
        <Route path="/" element={<Home />} />
        <Route path="/services" element={<RequireSection section="services"><Services /></RequireSection>} />
        {/* Generic sign-up — not section-gated, so it works hosting-only. */}
        <Route path="/register" element={<Register />} />
        <Route path="/services/register" element={<RequireSection section="services"><Register /></RequireSection>} />
        <Route path="/services/:id" element={<RequireSection section="services"><ServiceDetail /></RequireSection>} />
        <Route path="/hosting" element={<RequireSection section="hosting"><Hosting /></RequireSection>} />
        <Route path="/docs" element={<Docs />} />
        <Route path="/docs/:slug" element={<DocArticle />} />
        <Route path="/help" element={<Help />} />
        <Route path="/login" element={<Login />} />
        <Route path="/forgot-password" element={<ForgotPassword />} />
      </Route>

      {/* Authenticated portal. /my, /my/instances/* and /my/billing/*
          are served by Odoo as the SPA shell (see spa.py); ordering and
          checkout live on Odoo QWeb routes we navigate to directly. */}
      <Route element={<ProtectedRoute />}>
        <Route path="/my" element={<PortalLayout />}>
          {/* Overview merged into Projects — the projects list is the home. */}
          <Route index element={<Navigate to="/my/instances" replace />} />
          <Route path="home" element={<Navigate to="/my/instances" replace />} />
          <Route path="instances" element={<Instances />} />
          {/* One cohesive instance page: a persistent header (InstanceLayout)
              with the section content swapping in place below it. */}
          <Route path="instances/:id" element={<InstanceLayout />}>
            <Route index element={<InstanceHome />} />
            <Route path="metrics" element={<Metrics />} />
            <Route path="environments" element={<Environments />} />
            <Route path="databases" element={<Databases />} />
            <Route path="code" element={<Code />} />
            <Route path="shell" element={<ShellPage />} />
            <Route path="sql" element={<SqlPage />} />
            <Route path="logs" element={<Logs />} />
            <Route path="backups" element={<Backups />} />
          </Route>
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
