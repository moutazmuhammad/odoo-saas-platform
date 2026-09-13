import * as React from "react";
import { RefreshCw, Wallet, HardDrive, CreditCard, ArrowUpCircle, Plus } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { api, type ApiInstance } from "@/lib/api";
import { useToast } from "@/context/ToastContext";

function money(amount: number, currency = "USD") {
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amount);
  } catch {
    return `${currency} ${amount.toFixed(2)}`;
  }
}

const TONE_STYLES: Record<string, string> = {
  neutral: "border-border",
  info: "border-info/40 bg-info/5",
  warning: "border-warning/50 bg-warning/5",
  paused: "border-danger/40 bg-danger/5",
};

/**
 * Customer billing controls (v47): auto-renew + saved method, two-class
 * wallet ("Your balance" never expires / "Bonus credit" may), and the
 * capacity "upgrade experience" — always positive, always two clear
 * actions (Upgrade / Add storage), never punitive.
 */
export function BillingPanel({
  instance,
  onChange,
}: {
  instance: ApiInstance;
  onChange: () => void | Promise<void>;
}) {
  const toast = useToast();
  const navigate = useNavigate();
  const [saving, setSaving] = React.useState(false);
  const cap = instance.capacity;
  const wallet = instance.wallet;
  const method = instance.payment_method;
  const currency = cap?.currency || instance.currency || "USD";

  async function toggleAutoRenew() {
    setSaving(true);
    try {
      await api.setAutoRenew(instance.id, { subscription: !instance.auto_renew_subscription });
      toast.success(
        "Auto-renew updated",
        !instance.auto_renew_subscription
          ? "We'll renew automatically on your due date."
          : "Auto-renew is off — you'll pay each invoice yourself.",
      );
      await onChange();
    } catch {
      toast.error("Couldn't update auto-renew", "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  const upgrade = () =>
    navigate(`/my/instances/${instance.id}/${instance.is_trial ? "upgrade" : "change-plan"}`);

  async function addStorage() {
    const blockGb = cap?.block_gb || 0;
    if (!blockGb) {
      toast.error("Storage blocks unavailable", "Please upgrade your plan to add capacity.");
      return;
    }
    if (!window.confirm(
      `Add ${blockGb} GB of storage for ${money(cap!.block_price, currency)}/cycle?`,
    )) return;
    setSaving(true);
    try {
      const res = await api.addStorageBlock(instance.id, 1);
      if (res.activated) {
        toast.success("Storage added", "Your capacity is now expanded.");
        await onChange();
      } else if (res.checkout_url) {
        window.location.href = res.checkout_url;
      }
    } catch {
      toast.error("Couldn't add storage", "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mt-4 space-y-4">
      {/* Capacity ("upgrade experience") — only shown when not healthy */}
      {cap && cap.state !== "ok" && (
        <Card className={`p-5 ${TONE_STYLES[cap.tone] || "border-border"}`}>
          <div className="flex items-start gap-3">
            <HardDrive className="mt-0.5 size-5 text-primary" />
            <div className="flex-1">
              <p className="text-sm font-semibold">{cap.title}</p>
              <p className="mt-1 text-sm text-muted">{cap.message}</p>
              <div className="mt-3 h-2 w-full max-w-md overflow-hidden rounded-full bg-card ring-1 ring-border">
                <div
                  className={`h-full rounded-full ${cap.usage_pct >= 100 ? "bg-warning" : "bg-primary"}`}
                  style={{ width: `${Math.min(100, cap.usage_pct)}%` }}
                />
              </div>
              <p className="mt-1 text-xs text-muted">
                {cap.used_gb} GB of {cap.capacity_gb} GB used ({cap.usage_pct}%)
                {cap.grace_days_left != null && cap.grace_days_left >= 0
                  ? ` · ${cap.grace_days_left} day(s) to expand`
                  : ""}
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button onClick={upgrade}>
                  <ArrowUpCircle className="size-4" /> Upgrade plan
                </Button>
                <Button variant="secondary" onClick={addStorage}>
                  <Plus className="size-4" /> Add storage
                </Button>
              </div>
            </div>
          </div>
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        {/* Auto-renew + payment method */}
        <Card className="flex flex-col gap-3 p-5">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <RefreshCw className="size-4 text-primary" /> Auto-renew
          </div>
          <p className="text-xs text-muted">
            {instance.auto_renew_subscription
              ? "Your subscription renews automatically on the due date."
              : "Auto-renew is off — invoices are issued but you pay them yourself."}
          </p>
          <div className="flex items-center gap-2 text-xs text-muted">
            <CreditCard className="size-3.5" />
            {method ? (
              <span>{method.label}{method.provider ? ` · ${method.provider}` : ""}</span>
            ) : (
              <span>No saved payment method yet — added on your next payment.</span>
            )}
          </div>
          <Button
            variant={instance.auto_renew_subscription ? "secondary" : "default"}
            disabled={saving}
            onClick={toggleAutoRenew}
            className="mt-1 w-fit"
          >
            {instance.auto_renew_subscription ? "Turn off auto-renew" : "Turn on auto-renew"}
          </Button>
        </Card>

        {/* Wallet — two classes */}
        <Card className="flex flex-col gap-2 p-5 lg:col-span-2">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Wallet className="size-4 text-primary" /> Wallet
          </div>
          {wallet ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <p className="text-xs text-muted">Your balance</p>
                <p className="text-xl font-semibold tabular-nums">
                  {money(wallet.funded, currency)}
                </p>
                <p className="text-[11px] text-muted">Your money — never expires.</p>
              </div>
              <div>
                <p className="text-xs text-muted">Bonus credit</p>
                <p className="text-xl font-semibold tabular-nums">
                  {money(wallet.bonus, currency)}
                </p>
                <p className="text-[11px] text-muted">
                  {wallet.bonus > 0 && wallet.bonus_expiry
                    ? `Expires ${wallet.bonus_expiry} · used first, automatically.`
                    : "Promotional credit, applied before your own money."}
                </p>
              </div>
            </div>
          ) : (
            <p className="text-xs text-muted">No wallet credit yet.</p>
          )}
          <p className="mt-1 text-xs text-muted">
            Credit is applied to your invoices automatically.
          </p>
        </Card>
      </div>
    </div>
  );
}
