import * as React from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  Mail,
  Lock,
  User,
  Building2,
  Phone,
  MapPin,
  ArrowRight,
  ShieldCheck,
  ArrowLeft,
} from "lucide-react";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { ActionButton } from "@/components/ActionButton";
import { AlertBanner } from "@/components/AlertBanner";
import { Logo } from "@/components/Logo";
import { useAuth } from "@/context/AuthContext";
import { useToast } from "@/context/ToastContext";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

const OTP_LENGTH = 6;
const RESEND_SECONDS = 120;

interface Form {
  name: string;
  email: string;
  phone: string;
  company_name: string;
  country_id: string;
  city: string;
  password: string;
}

const EMPTY: Form = {
  name: "",
  email: "",
  phone: "",
  company_name: "",
  country_id: "",
  city: "",
  password: "",
};

export default function Register() {
  const { registerStart, registerVerify, registerResend } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  // Where to go after sign-up. Mirrors the backend's _build_redirect_url
  // (registration.py): continue the funnel the user came from — the
  // hosting/service configure page — instead of dumping them on the
  // dashboard. Plain sign-ups (no funnel params) land on home.
  const postRegisterUrl = React.useCallback(() => {
    const p = searchParams;
    if (p.get("hosting") === "1") {
      const parts: string[] = [];
      for (const key of ["workers", "storage", "billing", "odoo_version_id"]) {
        const v = p.get(key);
        if (v) parts.push(`${key}=${encodeURIComponent(v)}`);
      }
      if (p.get("is_trial") === "1") parts.push("is_trial=1");
      return "/hosting/configure" + (parts.length ? "?" + parts.join("&") : "");
    }
    const productId = parseInt(p.get("product_id") || "0", 10);
    const planId = parseInt(p.get("plan_id") || "0", 10);
    if (productId && planId) {
      return `/services/${productId}/plans/${planId}/configure${p.get("is_trial") === "1" ? "?trial=1" : ""}`;
    }
    return "/";
  }, [searchParams]);

  // After sign-up: for the hosting funnel, place the order immediately
  // (no Review page) and go straight to payment, carrying every order
  // field through the URL. Other funnels use their configure page.
  const goAfterRegister = React.useCallback(async () => {
    const p = searchParams;
    if (p.get("hosting") === "1") {
      const fields: Record<string, string> = {};
      p.forEach((v, k) => {
        if (k !== "hosting" && v) fields[k] = v;
      });
      try {
        const { redirect_url } = await api.hostingOrder(fields);
        window.location.assign(redirect_url);
        return;
      } catch {
        navigate("/my/instances", { replace: true });
        return;
      }
    }
    const dest = postRegisterUrl();
    if (dest.includes("/configure")) window.location.assign(dest);
    else navigate(dest, { replace: true });
  }, [searchParams, postRegisterUrl, navigate]);

  const [step, setStep] = React.useState<1 | 2>(1);
  const [form, setForm] = React.useState<Form>(EMPTY);
  const [countries, setCountries] = React.useState<{ id: number; name: string }[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  React.useEffect(() => {
    api.meta().then((m) => setCountries(m.countries)).catch(() => {});
  }, []);

  const set = (key: keyof Form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  const handleAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!form.name.trim()) return setError("Please enter your full name.");
    if (!form.email.includes("@")) return setError("Please enter a valid email address.");
    if (!form.phone.trim()) return setError("Please enter your phone number.");
    if (!form.country_id) return setError("Please select your country.");
    if (!form.city.trim()) return setError("Please enter your city.");
    if (form.password.length < 8) return setError("Password must be at least 8 characters.");

    setSubmitting(true);
    try {
      await registerStart(form);
      toast.info("Verification sent", "We texted a 6-digit code to your phone.");
      setStep(2);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't start registration.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="relative flex min-h-[calc(100vh-4rem)] items-center justify-center overflow-hidden px-4 py-12">
      <div className="pointer-events-none absolute left-1/2 top-0 h-96 w-[700px] -translate-x-1/2 rounded-full bg-primary/15 blur-[130px]" />
      <div className="relative w-full max-w-md animate-scale-in">
        <div className="mb-8 flex justify-center">
          <Logo />
        </div>

        <div className="mb-6 flex items-center justify-center gap-2">
          {[1, 2].map((n) => (
            <div
              key={n}
              className={cn(
                "h-1.5 w-12 rounded-full transition-colors",
                step >= n ? "bg-primary" : "bg-border"
              )}
            />
          ))}
        </div>

        <Card glass className="p-8">
          {step === 1 ? (
            <>
              <h1 className="text-2xl font-bold tracking-tight">Create your account</h1>
              <p className="mt-1.5 text-sm text-muted">Step 1 of 2 — your details</p>

              {error && (
                <AlertBanner
                  className="mt-5"
                  variant="danger"
                  title="Please review your details"
                  description={error}
                  onDismiss={() => setError(null)}
                />
              )}

              <form onSubmit={handleAccount} className="mt-6 space-y-4">
                <Field icon={User} id="name" label="Full name" placeholder="Jane Cooper" value={form.name} onChange={set("name")} />
                <Field icon={Mail} id="email" label="Work email" type="email" placeholder="jane@company.com" value={form.email} onChange={set("email")} />
                <Field icon={Phone} id="phone" label="Phone" placeholder="+1 555 123 4567" value={form.phone} onChange={set("phone")} />
                <div className="space-y-2">
                  <Label htmlFor="country">Country</Label>
                  <select
                    id="country"
                    value={form.country_id}
                    onChange={set("country_id")}
                    className="flex h-10 w-full rounded-lg border border-border bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-glow/70"
                  >
                    <option value="">Select your country…</option>
                    {countries.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                </div>
                <Field icon={MapPin} id="city" label="City" placeholder="San Francisco" value={form.city} onChange={set("city")} />
                <Field icon={Building2} id="company" label="Company (optional)" placeholder="Acme Inc." value={form.company_name} onChange={set("company_name")} />
                <Field icon={Lock} id="password" label="Password" type="password" placeholder="••••••••" value={form.password} onChange={set("password")} />
                <ActionButton type="submit" className="w-full" loading={submitting} loadingText="Sending code…">
                  Continue
                  <ArrowRight />
                </ActionButton>
              </form>

              <p className="mt-6 text-center text-sm text-muted">
                Already have an account?{" "}
                <Link to="/login" className="font-medium text-primary hover:underline">
                  Sign in
                </Link>
              </p>
            </>
          ) : (
            <OtpStep
              form={form}
              onBack={() => setStep(1)}
              onVerify={async (otp) => {
                const me = await registerVerify({ ...form, otp });
                toast.success("Account created", `Welcome to VELTNEX, ${me.name.split(" ")[0]}.`);
                goAfterRegister();
              }}
              onResend={async () => {
                await registerResend(form.phone);
                toast.info("Code resent", "A new code is on its way.");
              }}
            />
          )}
        </Card>
      </div>
    </div>
  );
}

function Field({
  icon: Icon,
  id,
  label,
  ...props
}: {
  icon: typeof Mail;
  id: string;
  label: string;
} & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <div className="relative">
        <Icon className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
        <Input id={id} className="pl-9" {...props} />
      </div>
    </div>
  );
}

function OtpStep({
  form,
  onBack,
  onVerify,
  onResend,
}: {
  form: Form;
  onBack: () => void;
  onVerify: (otp: string) => Promise<void>;
  onResend: () => Promise<void>;
}) {
  const [digits, setDigits] = React.useState<string[]>(Array(OTP_LENGTH).fill(""));
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);
  const [seconds, setSeconds] = React.useState(RESEND_SECONDS);
  const inputs = React.useRef<(HTMLInputElement | null)[]>([]);

  React.useEffect(() => {
    inputs.current[0]?.focus();
  }, []);
  React.useEffect(() => {
    if (seconds <= 0) return;
    const t = setInterval(() => setSeconds((s) => s - 1), 1000);
    return () => clearInterval(t);
  }, [seconds]);

  const code = digits.join("");

  const setDigit = (index: number, value: string) => {
    const clean = value.replace(/\D/g, "");
    setError(null);
    if (clean.length > 1) {
      const chars = clean.slice(0, OTP_LENGTH).split("");
      const next = Array(OTP_LENGTH).fill("");
      chars.forEach((c, i) => (next[i] = c));
      setDigits(next);
      inputs.current[Math.min(chars.length, OTP_LENGTH - 1)]?.focus();
      return;
    }
    setDigits((prev) => {
      const next = [...prev];
      next[index] = clean;
      return next;
    });
    if (clean && index < OTP_LENGTH - 1) inputs.current[index + 1]?.focus();
  };

  const verify = React.useCallback(
    async (value: string) => {
      if (value.length !== OTP_LENGTH) return setError("Enter all six digits.");
      setSubmitting(true);
      setError(null);
      try {
        await onVerify(value);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Verification failed.");
        setDigits(Array(OTP_LENGTH).fill(""));
        inputs.current[0]?.focus();
      } finally {
        setSubmitting(false);
      }
    },
    [onVerify]
  );

  React.useEffect(() => {
    if (code.length === OTP_LENGTH && !submitting) verify(code);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  return (
    <>
      <button
        onClick={onBack}
        className="inline-flex items-center gap-1.5 text-sm text-muted transition-colors hover:text-foreground"
      >
        <ArrowLeft className="size-4" />
        Back
      </button>
      <div className="mt-4 flex items-center gap-3">
        <span className="flex size-11 items-center justify-center rounded-xl bg-primary/15 text-primary">
          <ShieldCheck className="size-5" />
        </span>
        <div>
          <h1 className="text-xl font-bold tracking-tight">Verify your phone</h1>
          <p className="text-sm text-muted">Step 2 of 2</p>
        </div>
      </div>

      <p className="mt-5 text-sm text-muted">
        Enter the 6-digit code we sent to{" "}
        <span className="font-medium text-foreground">{form.phone}</span>.
      </p>

      {error && (
        <AlertBanner className="mt-4" variant="danger" title="Verification failed" description={error} />
      )}

      <div className="mt-5 flex justify-between gap-2">
        {digits.map((d, i) => (
          <input
            key={i}
            ref={(el) => (inputs.current[i] = el)}
            inputMode="numeric"
            maxLength={1}
            value={d}
            onChange={(e) => setDigit(i, e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Backspace" && !digits[i] && i > 0) inputs.current[i - 1]?.focus();
            }}
            disabled={submitting}
            className={cn(
              "h-14 w-full rounded-lg border bg-background text-center text-xl font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-glow/70",
              error ? "border-danger" : "border-border focus-visible:border-primary-glow"
            )}
          />
        ))}
      </div>

      <ActionButton
        className="mt-6 w-full"
        loading={submitting}
        loadingText="Verifying…"
        onClick={() => verify(code)}
      >
        Verify & create account
      </ActionButton>

      <div className="mt-5 text-center text-sm">
        {seconds > 0 ? (
          <span className="text-muted">Resend code in {seconds}s</span>
        ) : (
          <button
            onClick={() => {
              setSeconds(RESEND_SECONDS);
              onResend();
            }}
            className="font-medium text-primary hover:underline"
          >
            Resend code
          </button>
        )}
      </div>
    </>
  );
}
