import * as React from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ShieldCheck, CheckCircle2 } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { ActionButton } from "@/components/ActionButton";
import { AlertBanner } from "@/components/AlertBanner";

export default function VerifyProfile() {
  const { user, refresh, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const profile = user?.teammate_verification?.[0];
  const [phone, setPhone] = React.useState(profile?.phone || "");
  const [codes, setCodes] = React.useState({ email: "", phone: "" });
  const [sent, setSent] = React.useState({ email: false, phone: false });
  const [password, setPassword] = React.useState("");
  const [confirmation, setConfirmation] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const returnPath = location.state?.from;
  React.useEffect(() => {
    if (!profile) navigate(typeof returnPath === "string" && returnPath.startsWith("/my/") && returnPath !== "/my/verify-profile" ? returnPath : "/my/instances", { replace: true });
  }, [profile, navigate, returnPath]);
  React.useEffect(() => { setPhone(profile?.phone || ""); setSent({ email: false, phone: false }); setCodes({ email: "", phone: "" }); }, [profile?.id]);
  const act = async (operation: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await operation(); } catch (e) { setError(e instanceof ApiError ? e.message : "Could not complete verification. Please try again."); }
    finally { setBusy(false); }
  };
  if (!profile) return null;
  return <main className="flex min-h-screen items-center justify-center bg-background p-5"><Card className="w-full max-w-lg space-y-6 p-7">
    <Link to="/" className="text-sm font-semibold text-primary">Veltnex</Link>
    <div><ShieldCheck className="mb-3 size-8 text-primary" /><h1 className="text-2xl font-bold">Confirm your teammate profile</h1><p className="mt-2 text-sm text-muted">Verify your email and phone to activate the access assigned by {profile.customer}.</p></div>
    {error && <AlertBanner variant="danger" title="Verification" description={error} />}
    {(["email", "phone"] as const).map(channel => {
      const verified = channel === "email" ? profile.email_verified : profile.phone_verified;
      return <section key={channel} className="space-y-3 rounded-lg border border-border p-4"><h2 className="flex items-center gap-2 font-medium capitalize">{channel} {verified && <CheckCircle2 className="size-4 text-success" />}</h2>
        {channel === "email" ? <p className="text-sm text-muted">{profile.email}</p> : <><Label htmlFor="verify-phone">Phone number with country code</Label><Input id="verify-phone" value={phone} disabled={verified || busy} onChange={e => { setPhone(e.target.value); setSent(s => ({ ...s, phone: false })); }} placeholder="+201012345678" /></>}
        {verified ? <p className="text-sm text-success">Verified</p> : <>
          <Button variant="secondary" disabled={busy} onClick={() => act(async () => { await api.iamVerificationSend(profile.id, channel, channel === "phone" ? phone : undefined); setSent(s => ({ ...s, [channel]: true })); })}>{sent[channel] ? "Send another code" : "Send verification code"}</Button>
          {sent[channel] && <div className="flex items-end gap-2"><div className="flex-1"><Label htmlFor={`code-${channel}`}>Six-digit code</Label><Input id={`code-${channel}`} className="mt-2" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={codes[channel]} onChange={e => setCodes(c => ({ ...c, [channel]: e.target.value }))} /></div><Button disabled={busy || !/^\d{6}$/.test(codes[channel])} onClick={() => act(async () => { const result = await api.iamVerificationVerify(profile.id, channel, codes[channel]); if (!result.verified) throw new ApiError("This code is incorrect or expired. Request another code and try again."); await refresh(); })}>Verify</Button></div>}
        </>}
      </section>;
    })}
    {profile.needs_password && <section className="space-y-3"><p className="text-sm text-muted">Replace the temporary password with a password only you know.</p><Label htmlFor="own-password">New password (at least 12 characters)</Label><Input id="own-password" type="password" autoComplete="new-password" value={password} onChange={e => setPassword(e.target.value)} /><Label htmlFor="confirm-password">Confirm password</Label><Input id="confirm-password" type="password" autoComplete="new-password" value={confirmation} onChange={e => setConfirmation(e.target.value)} /></section>}
    <ActionButton className="w-full" loading={busy} disabled={!profile.email_verified || !profile.phone_verified || (profile.needs_password && (password.length < 12 || password !== confirmation))} onClick={() => act(async () => { await api.iamVerificationFinish(profile.id, password); await refresh(); })}>Continue to projects</ActionButton>
    <Button variant="ghost" className="w-full" disabled={busy} onClick={() => logout()}>Sign out</Button>
  </Card></main>;
}
