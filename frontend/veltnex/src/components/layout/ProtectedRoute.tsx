import { i18nText } from "@/i18n";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { Spinner } from "@/components/Spinner";

/** Redirects unauthenticated visitors to /login, preserving intended path. */
export function ProtectedRoute() {
  const { isAuthenticated, loading, user } = useAuth();
  const location = useLocation();

  // Wait for the initial session check so we don't bounce a logged-in
  // user to /login on a hard refresh.
  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner size="lg" label={i18nText("Loading your workspace…")} />
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location.pathname + location.search }} replace />;
  }
  if ((user?.must_change_password || user?.must_verify_phone) && location.pathname !== "/my/change-password") {
    return <Navigate to="/my/change-password" state={{ from: location.pathname + location.search }} replace />;
  }
  return <Outlet />;
}
