import * as React from "react";
import { Link, useNavigate } from "react-router-dom";
import { Mail, Lock, KeyRound, ArrowRight, ArrowLeft } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { ActionButton } from "@/components/ActionButton";
import { AlertBanner } from "@/components/AlertBanner";
import { Logo } from "@/components/Logo";
import { useAuth } from "@/context/AuthContext";
import { useToast } from "@/context/ToastContext";
import { ApiError } from "@/lib/api";

/**
 * In-SPA password recovery (CX-001) — replaces the hard bounce to Odoo's
 * /web/reset_password. Step 1 requests an email code (account-enumeration
 * safe: the backend always reports "sent"); step 2 verifies the code, sets
 * the new password and signs the user in.
 */
export default function ForgotPassword() {
  const { resetStart, resetVerify } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();

  const [step, setStep] = React.useState<1 | 2>(1);
  const [email, setEmail] = React.useState("");
  const [otp, setOtp] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const requestCode = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!email.includes("@")) return setError("Please enter a valid email address.");
    setSubmitting(true);
    try {
      await resetStart(email.trim());
      toast.info("Check your email", "If an account exists, we sent a 6-digit code.");
      setStep(2);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't start the reset. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };

  const confirmReset = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!otp.trim()) return setError("Enter the 6-digit code from your email.");
    if (password.length < 8) return setError("Password must be at least 8 characters.");
    setSubmitting(true);
    try {
      const me = await resetVerify(email.trim(), otp.trim(), password);
      toast.success("Password updated", `You're signed in, ${me.name.split(" ")[0]}.`);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't reset your password. Please try again.");
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
        <Card glass className="p-8">
          <h1 className="text-2xl font-bold tracking-tight">Reset password</h1>
          <p className="mt-1.5 text-sm text-muted">
            {step === 1
              ? "Enter your email and we'll send you a verification code."
              : `Enter the code we sent to ${email} and choose a new password.`}
          </p>

          {error && (
            <AlertBanner
              className="mt-5"
              variant="danger"
              title="Couldn't continue"
              description={error}
              onDismiss={() => setError(null)}
            />
          )}

          {step === 1 ? (
            <form onSubmit={requestCode} className="mt-6 space-y-4">
              <div className="space-y-2">
                <Label htmlFor="email">Email</Label>
                <div className="relative">
                  <Mail className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
                  <Input
                    id="email"
                    type="email"
                    className="pl-9"
                    placeholder="you@company.com"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    autoComplete="email"
                    autoFocus
                  />
                </div>
              </div>
              <ActionButton type="submit" className="w-full" loading={submitting} loadingText="Sending…">
                Send code
                <ArrowRight />
              </ActionButton>
            </form>
          ) : (
            <form onSubmit={confirmReset} className="mt-6 space-y-4">
              <div className="space-y-2">
                <Label htmlFor="otp">Verification code</Label>
                <div className="relative">
                  <KeyRound className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
                  <Input
                    id="otp"
                    inputMode="numeric"
                    className="pl-9 tracking-[0.3em]"
                    placeholder="000000"
                    value={otp}
                    onChange={(e) => setOtp(e.target.value)}
                    autoComplete="one-time-code"
                    autoFocus
                  />
                </div>
              </div>
              <div className="space-y-2">
                <Label htmlFor="new-password">New password</Label>
                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
                  <Input
                    id="new-password"
                    type="password"
                    className="pl-9"
                    placeholder="At least 8 characters"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    autoComplete="new-password"
                  />
                </div>
              </div>
              <ActionButton type="submit" className="w-full" loading={submitting} loadingText="Updating…">
                Reset password
                <ArrowRight />
              </ActionButton>
              <button
                type="button"
                onClick={() => { setStep(1); setError(null); }}
                className="flex w-full items-center justify-center gap-1.5 text-xs text-muted hover:text-foreground"
              >
                <ArrowLeft className="size-3.5" />
                Use a different email
              </button>
            </form>
          )}

          <p className="mt-6 text-center text-sm text-muted">
            Remembered it?{" "}
            <Link to="/login" className="font-medium text-primary hover:underline">
              Back to sign in
            </Link>
          </p>
        </Card>
      </div>
    </div>
  );
}
