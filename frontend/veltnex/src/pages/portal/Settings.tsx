import { i18nText } from "@/i18n";
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
      toast.success(i18nText("Payment method removed"));
    } catch (e) {
      toast.error(i18nText("Couldn't remove"), e instanceof ApiError ? e.message : i18nText("Please try again."));
    }
  };

  const handleLogout = () => {
    logout();
    toast.info(i18nText("Signed out"));
    navigate("/");
  };

  return (
    <div className="animate-fade-in">
      <PageHeader title={i18nText("Settings")} subtitle={i18nText("Manage your account, billing, and preferences.")} />

      <div className="mt-6 space-y-4">
        <Section icon={User} title={i18nText("Account")} description={i18nText("Your profile details. Contact support to change them.")}>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label={i18nText("Name")} value={user?.name} />
            <Field label={i18nText("Email")} value={user?.email} />
            <Field label={i18nText("Company")} value={user?.company} />
            <Field label={i18nText("Phone")} value={user?.phone} />
          </div>
        </Section>

        <Section icon={CreditCard} title={i18nText("Payment methods")} description={i18nText("The card you pay with is kept here and used for renewals, snapshots and add-ons, so you never re-enter it.")}>
          {methods === null ? (
            <div className="space-y-2">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          ) : methods.length === 0 ? (
            <EmptyState
              icon={CreditCard}
              title={i18nText("No saved payment methods")}
              description={i18nText("Your card is kept automatically the first time you pay. Until then, each invoice is paid from Billing.")}
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
                        {m.is_default ? i18nText(" · default") : ""}
                      </p>
                    </div>
                  </div>
                  <Button size="sm" variant="ghost" onClick={() => removeMethod(m.id)} aria-label={i18nText("Remove")}>
                    <Trash2 className="size-4 text-danger" />
                  </Button>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-3">
            <Button variant="secondary" size="sm" onClick={() => navigate("/my/billing")}>{i18nText("Go to billing")}</Button>
          </div>
        </Section>

        <Section icon={Bell} title={i18nText("Notifications")} description={i18nText("How we reach you about renewals, backups, and deployments.")}>
          <p className="text-sm text-muted">{i18nText("Account notifications are sent to ")}<span className="font-medium text-foreground">{user?.email}</span>{i18nText(". Granular channel controls are coming soon.")}</p>
        </Section>

        <Section icon={Palette} title={i18nText("Appearance")} description={i18nText("Switch between light and dark themes.")}>
          <ThemeToggle />
        </Section>

        <Section icon={LogOut} title={i18nText("Session")} description={i18nText("Sign out of this device.")}>
          <Button variant="danger" onClick={handleLogout}>
            <LogOut className="size-4" />{i18nText("Sign out")}</Button>
        </Section>
      </div>
    </div>
  );
}
