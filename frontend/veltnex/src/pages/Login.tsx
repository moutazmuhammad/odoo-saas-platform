import { i18nText } from "@/i18n";
import * as React from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Mail, Lock, ArrowRight } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { ActionButton } from "@/components/ActionButton";
import { AlertBanner } from "@/components/AlertBanner";
import { Logo } from "@/components/Logo";
import { useAuth } from "@/context/AuthContext";
import { useToast } from "@/context/ToastContext";
import { ApiError } from "@/lib/api";

export default function Login() {
  const { login } = useAuth();
  const toast = useToast();
  const registerTo = "/register";
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();

  // Where to land after sign-in:
  //  - `state.from`  — set by ProtectedRoute / the navbar "Sign in" link;
  //    always an in-app SPA path, so we client-route to it.
  //  - `?redirect=`  — set by Odoo when it bounces a logged-out visitor
  //    from /web/login here; may be a backend/QWeb URL (e.g. /odoo), so
  //    it needs a full navigation. Only honoured if it's a safe relative
  //    path (guards against open-redirect).
  //  - otherwise the home page — never a forced detour through /my.
  const fromState = (location.state as { from?: string } | null)?.from ?? null;
  const redirectParam = searchParams.get("redirect");
  const safeRedirect =
    redirectParam && /^\/(?!\/)/.test(redirectParam) ? redirectParam : null;

  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    // Accept any login, not just emails — staff/admin sign in with a
    // plain username (e.g. "admin"). The backend authenticates by login.
    if (!email.trim() || !password) {
      setError(i18nText("Enter your email/username and password to continue."));
      return;
    }
    setLoading(true);
    try {
      const signedInUser = await login(email, password);
      toast.success(i18nText("Welcome back"), i18nText("You're signed in."));
      if (signedInUser.must_change_password || signedInUser.must_verify_phone) {
        navigate("/my/change-password", { replace: true, state: { from: fromState || safeRedirect } });
      } else if (fromState) navigate(fromState, { replace: true });
      else if (safeRedirect) window.location.assign(safeRedirect);
      else navigate("/", { replace: true });
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : i18nText("We couldn't sign you in just now. Please try again.")
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative flex min-h-[calc(100vh-4rem)] items-center justify-center overflow-hidden px-4 py-12">
      <div className="pointer-events-none absolute start-1/2 top-0 h-96 w-[700px] -translate-x-1/2 rounded-full bg-primary/15 blur-[130px]" />
      <div className="relative w-full max-w-md animate-scale-in">
        <div className="mb-8 flex justify-center">
          <Logo />
        </div>
        <Card glass className="p-8">
          <h1 className="text-2xl font-bold tracking-tight">{i18nText("Sign in")}</h1>
          <p className="mt-1.5 text-sm text-muted">{i18nText("Welcome back. Sign in to manage your instances.")}</p>

          {error && (
            <AlertBanner
              className="mt-5"
              variant="danger"
              title={i18nText("Sign-in failed")}
              description={error}
              onDismiss={() => setError(null)}
            />
          )}

          <form onSubmit={handleSubmit} className="mt-6 space-y-4">
            <div className="space-y-2">
              <Label htmlFor="email">{i18nText("Email or username")}</Label>
              <div className="relative">
                <Mail className="absolute start-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
                <Input
                  id="email"
                  type="text"
                  data-technical
                  className="ps-9"
                  placeholder={i18nText("you@company.com")}
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  autoComplete="username"
                />
              </div>
            </div>
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label htmlFor="password">{i18nText("Password")}</Label>
                <Link to="/forgot-password" className="text-xs text-primary hover:underline">{i18nText("Forgot?")}</Link>
              </div>
              <div className="relative">
                <Lock className="absolute start-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
                <Input
                  id="password"
                  type="password"
                  className="ps-9"
                  placeholder="••••••••"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                />
              </div>
            </div>
            <ActionButton
              type="submit"
              className="w-full"
              loading={loading}
              loadingText={i18nText("Signing in\u2026")}
            >{i18nText("Sign in")}<ArrowRight />
            </ActionButton>
          </form>

          <p className="mt-6 text-center text-sm text-muted">{i18nText("New to VELTNEX?")}{" "}
            <Link to={registerTo} state={{ from: fromState }} className="font-medium text-primary hover:underline">{i18nText("Create an account")}</Link>
          </p>
        </Card>
      </div>
    </div>
  );
}
