import * as React from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Download, CreditCard, Receipt, Building2, CheckCircle2 } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { AlertBanner } from "@/components/AlertBanner";
import { StatusBadge } from "@/components/StatusBadge";
import { BreakdownRow } from "@/components/BreakdownRow";
import { EmptyState } from "@/components/EmptyState";
import { Spinner } from "@/components/Spinner";
import { Logo } from "@/components/Logo";
import { PortalBreadcrumb } from "@/components/layout/PortalLayout";
import { useAuth } from "@/context/AuthContext";
import { api, ApiError, type ApiInvoice } from "@/lib/api";
import { formatDate } from "@/lib/format";

export default function InvoiceDetail() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const [inv, setInv] = React.useState<ApiInvoice | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    api
      .invoice(Number(id))
      .then(setInv)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Invoice not found."));
  }, [id]);

  const money = (n: number) =>
    new Intl.NumberFormat("en-US", { style: "currency", currency: inv?.currency || "USD" }).format(n);

  if (error) {
    return (
      <EmptyState
        className="mt-10"
        icon={Receipt}
        title="Invoice not found"
        action={<Button onClick={() => navigate("/my/billing")}>Back to billing</Button>}
      />
    );
  }
  if (!inv) {
    return (
      <div className="flex min-h-[50vh] items-center justify-center">
        <Spinner size="lg" label="Loading invoice…" />
      </div>
    );
  }

  // Payment + official PDF live on Odoo's portal invoice page.
  const goPay = () => (window.location.href = inv.portal_url);

  return (
    <div className="animate-fade-in">
      <PortalBreadcrumb items={[{ label: "Billing", to: "/my/billing" }, { label: inv.number }]} />

      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-bold tracking-tight">{inv.number}</h1>
          <StatusBadge status={inv.status} />
        </div>
        <div className="flex gap-2">
          <a href={inv.portal_url} target="_blank" rel="noreferrer">
            <Button variant="secondary">
              <Download className="size-4" />
              View / PDF
            </Button>
          </a>
          {inv.payable && (
            <Button onClick={goPay}>
              <CreditCard className="size-4" />
              Pay {money(inv.residual)}
            </Button>
          )}
        </div>
      </div>

      {inv.status === "overdue" && (
        <AlertBanner
          className="mt-6"
          variant="danger"
          title="This invoice is overdue"
          description="Please settle the balance to avoid service suspension."
          action={<Button size="sm" onClick={goPay}>Pay now</Button>}
        />
      )}

      <Card className="mt-6 p-6 sm:p-10">
        <div className="flex flex-col gap-6 border-b border-border pb-8 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <Logo />
            <p className="mt-4 text-sm text-muted">VELTNEX, Inc.</p>
          </div>
          <div className="sm:text-right">
            <p className="text-sm font-medium">Invoice {inv.number}</p>
            {inv.issued && <p className="mt-2 text-sm text-muted">Issued {formatDate(inv.issued)}</p>}
            {inv.due && <p className="text-sm text-muted">Due {formatDate(inv.due)}</p>}
            {inv.instance_name && <p className="mt-2 text-sm text-muted">Instance: {inv.instance_name}</p>}
          </div>
        </div>

        <div className="flex items-start gap-3 py-8">
          <span className="flex size-10 items-center justify-center rounded-lg border border-border bg-card text-muted">
            <Building2 className="size-4" />
          </span>
          <div>
            <p className="text-xs uppercase tracking-wide text-muted">Billed to</p>
            <p className="mt-1 font-medium">{user?.company}</p>
            <p className="text-sm text-muted">{user?.name}</p>
            <p className="text-sm text-muted">{user?.email}</p>
          </div>
        </div>

        <div className="border-t border-border pt-2">
          {(inv.lines || []).map((line, i) => (
            <BreakdownRow
              key={i}
              label={line.description}
              sub={line.quantity > 1 ? `Quantity: ${line.quantity}` : undefined}
              value={money(line.total)}
            />
          ))}
        </div>

        <div className="ml-auto mt-2 max-w-xs">
          {inv.subtotal !== undefined && <BreakdownRow label="Subtotal" value={money(inv.subtotal)} />}
          {inv.tax !== undefined && <BreakdownRow label="Tax" value={money(inv.tax)} />}
          <BreakdownRow label="Total" value={money(inv.total)} emphasis />
          {inv.residual > 0 && inv.residual !== inv.total && (
            <BreakdownRow label="Amount due" value={money(inv.residual)} />
          )}
        </div>

        {inv.status === "paid" && (
          <div className="mt-6 flex items-center gap-2 rounded-lg border border-success/30 bg-success/10 px-4 py-3 text-sm text-success">
            <CheckCircle2 className="size-4" />
            Paid in full — thank you for your business.
          </div>
        )}
      </Card>
    </div>
  );
}
