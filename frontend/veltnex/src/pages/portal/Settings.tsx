import * as React from "react";
import { useNavigate } from "react-router-dom";
import { User, CreditCard, Palette, LogOut, Trash2, Bell } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/EmptyState";
import { ThemeToggle } from "@/components/ThemeToggle";
import { PageHeader } from "@/components/PageHeader";
import { useAuth } from "@/context/AuthContext";
import { useToast } from "@/context/ToastContext";
import { api, ApiError, type ApiPaymentMethod } from "@/lib/api";

function Field({ label, value }: { label: string; value?: string }) {
  return (
    <div>
      <p className="text-xs text-muted">{label}</p>
      <p className="mt-0.5 text-sm font-medium">{value || "—"}</p>
    </div>
  );
}

function Section({
  icon: Icon,
  title,
  description,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <Card className="p-5">
      <div className="flex items-start gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-border bg-card text-muted">
          <Icon className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="font-semibold">{title}</h2>
          <p className="mt-0.5 text-sm text-muted">{description}</p>
          <div className="mt-4">{children}</div>
        </div>
      </div>
    </Card>
  );
}

export default function Settings() {
  const { user, logout } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const [methods, setMethods] = React.useState<ApiPaymentMethod[] | null>(null);

  React.useEffect(() => {
    api.paymentMethods().then(setMethods).catch(() => setMethods([]));
  }, []);

  const removeMethod = async (id: number) => {
    try {
      await api.removePaymentMethod(id);
      setMethods((m) => (m ? m.filter((x) => x.id !== id) : m));
      toast.success("Payment method removed");
    } catch (e) {
      toast.error("Couldn't remove", e instanceof ApiError ? e.message : "Please try again.");
    }
  };

  const handleLogout = () => {
    logout();
    toast.info("Signed out");
    navigate("/");
  };

  return (
    <div className="animate-fade-in">
      <PageHeader title="Settings" subtitle="Manage your account, billing, and preferences." />

      <div className="mt-6 space-y-4">
        <Section icon={User} title="Account" description="Your profile details. Contact support to change them.">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Name" value={user?.name} />
            <Field label="Email" value={user?.email} />
            <Field label="Company" value={user?.company} />
            <Field label="Phone" value={user?.phone} />
          </div>
        </Section>

        <Section icon={CreditCard} title="Payment methods" description="Saved cards used for renewals and one-click provisioning.">
          {methods === null ? (
            <div className="space-y-2">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          ) : methods.length === 0 ? (
            <EmptyState
              icon={CreditCard}
              title="No saved payment methods"
              description="A card is saved automatically the first time you pay an invoice."
            />
          ) : (
            <ul className="divide-y divide-border rounded-lg border border-border">
              {methods.map((m) => (
                <li key={m.id} className="flex items-center justify-between gap-3 p-3">
                  <div className="flex items-center gap-3">
                    <CreditCard className="size-4 text-muted" />
                    <div>
                      <p className="text-sm font-medium">{m.label}</p>
                      <p className="text-xs text-muted capitalize">
                        {m.provider}
                        {m.is_default ? " · default" : ""}
                      </p>
                    </div>
                  </div>
                  <Button size="sm" variant="ghost" onClick={() => removeMethod(m.id)} aria-label="Remove">
                    <Trash2 className="size-4 text-danger" />
                  </Button>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-3">
            <Button variant="secondary" size="sm" onClick={() => navigate("/my/billing")}>
              Go to billing
            </Button>
          </div>
        </Section>

        <Section icon={Bell} title="Notifications" description="How we reach you about renewals, backups, and deployments.">
          <p className="text-sm text-muted">
            Account notifications are sent to <span className="font-medium text-foreground">{user?.email}</span>.
            Granular channel controls are coming soon.
          </p>
        </Section>

        <Section icon={Palette} title="Appearance" description="Switch between light and dark themes.">
          <ThemeToggle />
        </Section>

        <Section icon={LogOut} title="Session" description="Sign out of this device.">
          <Button variant="danger" onClick={handleLogout}>
            <LogOut className="size-4" />
            Sign out
          </Button>
        </Section>
      </div>
    </div>
  );
}
