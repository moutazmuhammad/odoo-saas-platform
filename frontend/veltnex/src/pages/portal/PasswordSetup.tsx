import * as React from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { KeyRound } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { ActionButton } from "@/components/ActionButton";
import { AlertBanner } from "@/components/AlertBanner";

export default function PasswordSetup() {
  const { user, refresh, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [password, setPassword] = React.useState("");
  const [confirmation, setConfirmation] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const returnPath = location.state?.from;
  React.useEffect(() => {
    if (!user?.must_change_password) navigate(typeof returnPath === "string" && returnPath.startsWith("/my/") && !["/my/change-password", "/my/verify-profile"].includes(returnPath) ? returnPath : "/my/instances", { replace: true });
  }, [user?.must_change_password, navigate, returnPath]);
  const save = async () => {
    setBusy(true); setError("");
    try { await api.iamPasswordChange(password); await refresh(); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Could not change your password. Please try again."); }
    finally { setBusy(false); }
  };
  if (!user?.must_change_password) return null;
  return <main className="flex min-h-screen items-center justify-center bg-background p-5"><Card className="w-full max-w-lg space-y-6 p-7">
    <Link to="/" className="text-sm font-semibold text-primary">Veltnex</Link>
    <div><KeyRound className="mb-3 size-8 text-primary" /><h1 className="text-2xl font-bold">Choose your own password</h1><p className="mt-2 text-sm text-muted">Replace the temporary password provided by your team owner before accessing your projects.</p><p className="mt-3 text-sm font-medium">{user.email}</p></div>
    {error && <AlertBanner variant="danger" title="Password change" description={error} />}
    <div className="space-y-3"><Label htmlFor="own-password">New password (at least 12 characters)</Label><Input id="own-password" type="password" autoComplete="new-password" value={password} onChange={e => setPassword(e.target.value)} /><Label htmlFor="confirm-password">Confirm password</Label><Input id="confirm-password" type="password" autoComplete="new-password" value={confirmation} onChange={e => setConfirmation(e.target.value)} />{confirmation && password !== confirmation && <p className="text-sm text-warning">Passwords don’t match.</p>}</div>
    <ActionButton className="w-full" loading={busy} disabled={password.length < 12 || password !== confirmation || password !== password.trim()} onClick={save}>Save password and continue</ActionButton>
    <Button variant="ghost" className="w-full" disabled={busy} onClick={() => logout()}>Sign out</Button>
  </Card></main>;
}
