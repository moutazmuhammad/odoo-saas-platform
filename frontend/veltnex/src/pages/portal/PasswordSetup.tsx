import { i18nText } from "@/i18n";
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
import { LanguageToggle } from "@/components/LanguageToggle";
import PhoneVerification from "./PhoneVerification";

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
    if (!user?.must_change_password && !user?.must_verify_phone) navigate(typeof returnPath === "string" && returnPath.startsWith("/my/") && !["/my/change-password", "/my/verify-profile"].includes(returnPath) ? returnPath : "/my/instances", { replace: true });
  }, [user?.must_change_password, user?.must_verify_phone, navigate, returnPath]);
  const save = async () => {
    setBusy(true); setError("");
    try { await api.iamPasswordChange(password); await refresh(); }
    catch (e) { setError(e instanceof ApiError ? e.message : i18nText("Could not change your password. Please try again.")); }
    finally { setBusy(false); }
  };
  if (!user?.must_change_password && !user?.must_verify_phone) return null;
  return <main className="flex min-h-screen items-center justify-center bg-background p-5"><Card className="w-full max-w-lg space-y-6 p-7">
    <div dir="ltr" className="flex items-center justify-between"><Link to="/" className="text-sm font-semibold text-primary">{i18nText("Veltnex")}</Link><LanguageToggle /></div>
    {user.must_change_password ? <><div><KeyRound className="mb-3 size-8 text-primary" /><p className="mb-2 text-xs font-medium text-muted">{user.must_verify_phone ? i18nText("Step 1 of 2") : i18nText("Password setup")}</p><h1 className="text-2xl font-bold">{i18nText("Choose your own password")}</h1><p className="mt-2 text-sm text-muted">{user.must_verify_phone ? i18nText("Replace the temporary password provided by your team owner, then verify your mobile number.") : i18nText("Replace the temporary password provided by your team owner to access your projects.")}</p><p className="mt-3 text-sm font-medium">{user.email}</p></div>
    {error && <AlertBanner variant="danger" title={i18nText("Password change")} description={error} />}
    <div className="space-y-3"><Label htmlFor="own-password">{i18nText("New password (at least 12 characters)")}</Label><Input id="own-password" type="password" autoComplete="new-password" value={password} onChange={e => setPassword(e.target.value)} /><Label htmlFor="confirm-password">{i18nText("Confirm password")}</Label><Input id="confirm-password" type="password" autoComplete="new-password" value={confirmation} onChange={e => setConfirmation(e.target.value)} />{confirmation && password !== confirmation && <p className="text-sm text-warning">{i18nText("Passwords don’t match.")}</p>}</div>
    <ActionButton className="w-full" loading={busy} disabled={password.length < 12 || password !== confirmation || password !== password.trim()} onClick={save}>{i18nText("Save password and continue")}</ActionButton>
    </> : <PhoneVerification />}
    <Button variant="ghost" className="w-full" disabled={busy} onClick={() => logout()}>{i18nText("Sign out")}</Button>
  </Card></main>;
}
