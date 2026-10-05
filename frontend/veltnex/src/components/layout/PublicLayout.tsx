import { Navigate, Outlet, useLocation } from "react-router-dom";
import * as React from "react";
import { PublicNav } from "./PublicNav";
import { Footer } from "./Footer";
import { useAuth } from "@/context/AuthContext";

export function PublicLayout() {
  const { pathname } = useLocation();
  const { user, loading } = useAuth();

  // Reset scroll on route change — avoids layout surprises between pages.
  React.useEffect(() => {
    window.scrollTo({ top: 0 });
  }, [pathname]);

  if (!loading && (user?.must_change_password || user?.must_verify_phone)) {
    return <Navigate to="/my/change-password" replace />;
  }

  return (
    <div className="flex min-h-screen flex-col">
      <PublicNav />
      <main className="flex-1">
        <Outlet />
      </main>
      <Footer />
    </div>
  );
}
