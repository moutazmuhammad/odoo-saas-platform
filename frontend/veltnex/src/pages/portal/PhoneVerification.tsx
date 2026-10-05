import * as React from "react";
import { Smartphone } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { Input, Label } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { ActionButton } from "@/components/ActionButton";
import { AlertBanner } from "@/components/AlertBanner";

export default function PhoneVerification() {
  const { refresh } = useAuth();
  const [options, setOptions] = React.useState<Awaited<ReturnType<typeof api.iamPhoneSetup>> | null>(null);
  const [phone, setPhone] = React.useState("");
  const [country, setCountry] = React.useState(0);
  const [sentPhone, setSentPhone] = React.useState("");
  const [code, setCode] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const load = React.useCallback(async () => {
    setError("");
    try { const info = await api.iamPhoneSetup(); setOptions(info); setPhone(info.phone); setCountry(info.phone_country_id || 0); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Could not load mobile verification. Please try again."); }
  }, []);
  React.useEffect(() => { load(); }, [load]);
  const send = async () => {
    setBusy(true); setError(""); setSentPhone(""); setCode("");
    try { const result = await api.iamPhoneSend(phone, country); setSentPhone(result.phone); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Could not send the verification code. Please try again."); }
    finally { setBusy(false); }
  };
  const verify = async () => {
    setBusy(true); setError("");
    try { await api.iamPhoneVerify(code); await refresh(); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Could not verify your mobile number. Please try again."); }
    finally { setBusy(false); }
  };
  return <>
    <div><Smartphone className="mb-3 size-8 text-primary" /><p className="mb-2 text-xs font-medium text-muted">Step 2 of 2 · Password saved</p><h1 className="text-2xl font-bold">Verify your mobile number</h1><p className="mt-2 text-sm text-muted">Confirm the number provided by your team owner, or enter your own number. Verify it with a WhatsApp code to access your projects.</p></div>
    {error && <AlertBanner variant="danger" title="Mobile verification" description={error} />}
    {!options ? <Button variant="secondary" onClick={load}>Reload verification</Button> : sentPhone ? <>
      <p className="text-sm text-muted">A WhatsApp verification code was sent to <span className="font-medium text-foreground">{sentPhone}</span>.</p>
      <div><Label htmlFor="mobile-code">Verification code</Label><Input id="mobile-code" className="mt-2" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code} onChange={e => setCode(e.target.value.replace(/\D/g, ""))} /></div>
      <ActionButton className="w-full" loading={busy} disabled={!/^[0-9]{6}$/.test(code)} onClick={verify}>Verify mobile and continue</ActionButton>
      <div className="flex justify-between gap-3"><Button variant="ghost" disabled={busy} onClick={send}>Resend code</Button><Button variant="ghost" disabled={busy} onClick={() => { setSentPhone(""); setCode(""); setError(""); }}>Change number</Button></div>
    </> : <>
      <div><Label htmlFor="mobile-country">Phone country</Label><select id="mobile-country" className="mt-2 h-10 w-full rounded-sm border border-border bg-card px-3 text-sm" value={country || ""} onChange={e => setCountry(Number(e.target.value))}><option value="">Select country…</option>{options.phone_countries.map(c => <option key={c.id} value={c.id}>{c.name} (+{c.phone_code})</option>)}</select></div>
      <div><Label htmlFor="mobile-phone">Mobile number</Label><div className="mt-2 flex items-center gap-2">{!!country && !/^(\+|00)/.test(phone) && <span className="shrink-0 text-sm text-muted">+{options.phone_countries.find(c => c.id === country)?.phone_code}</span>}<Input id="mobile-phone" type="tel" autoComplete="tel-national" value={phone} onChange={e => setPhone(e.target.value)} /></div></div>
      <ActionButton className="w-full" loading={busy} disabled={!phone.trim() || !country} onClick={send}>Send verification code</ActionButton>
    </>}
  </>;
}
