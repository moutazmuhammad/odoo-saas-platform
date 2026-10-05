import { getLocale } from "@/i18n";
import { i18nText } from "@/i18n";
import * as React from "react";
import { useNavigate } from "react-router-dom";
import { Receipt, ChevronRight, CircleDollarSign, CheckCircle2, Wallet } from "lucide-react";
import { Button } from "@/components/ui/button";
import { InfoCard } from "@/components/InfoCard";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState } from "@/components/EmptyState";
import { Spinner } from "@/components/Spinner";
import { AlertBanner } from "@/components/AlertBanner";
import { PageHeader } from "@/components/PageHeader";
import { DataTable, type Column } from "@/components/DataTable";
import { HelpHint } from "@/components/HelpHint";
import { api, ApiError, type ApiInvoice, type WalletData } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";

function money(n: number, c = "USD") {
  return new Intl.NumberFormat(getLocale(), { style: "currency", currency: c }).format(n);
}

const FILTERS = [
  { label: i18nText("All"), value: "all" },
  { label: i18nText("Open"), value: "open" },
  { label: i18nText("Paid"), value: "paid" },
  { label: i18nText("Overdue"), value: "overdue" },
] as const;

export default function Invoices() {
  const navigate = useNavigate();
  const [invoices, setInvoices] = React.useState<ApiInvoice[] | null>(null);
  const [walletData, setWalletData] = React.useState<WalletData | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [filter, setFilter] = React.useState<(typeof FILTERS)[number]["value"]>("all");

  React.useEffect(() => {
    api
      .invoices()
      .then(setInvoices)
      .catch((e) => setError(e instanceof ApiError ? e.message : i18nText("Could not load invoices.")));
    api.wallet().then(setWalletData).catch(() => setWalletData(null));
  }, []);

  const list = invoices ?? [];
  const filtered = filter === "all" ? list : list.filter((i) => i.status === filter);
  const outstanding = list
    .filter((i) => i.status === "open" || i.status === "overdue")
    .reduce((acc, i) => acc + i.residual, 0);
  const paid = list.filter((i) => i.status === "paid").reduce((acc, i) => acc + i.total, 0);
  const currency = list[0]?.currency || "USD";

  return (
    <div className="animate-fade-in">
      <PageHeader
        title={<>{i18nText("Billing")}<HelpHint anchor="invoice-status" className="ms-1.5" /></>}
        subtitle={i18nText("Invoices and outstanding balances.")}
      />

      {error && <AlertBanner className="mt-6" variant="danger" title={i18nText("Couldn't load invoices")} description={error} />}

      {!invoices && !error ? (
        <div className="mt-20 flex justify-center">
          <Spinner size="lg" label={i18nText("Loading invoices…")} />
        </div>
      ) : (
        <>
          <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <InfoCard label={i18nText("Outstanding")} value={money(outstanding, currency)} icon={CircleDollarSign} hint={`${list.filter((i) => i.status === "open" || i.status === "overdue").length} open`} />
            <InfoCard label={i18nText("Wallet balance")} value={money(walletData?.balance ?? 0, walletData?.currency || currency)} icon={Wallet} hint={walletData && walletData.balance_bonus > 0 ? `${money(walletData.balance_bonus, walletData.currency)} bonus` : undefined} />
            <InfoCard label={i18nText("Paid to date")} value={money(paid, currency)} icon={CheckCircle2} />
            <InfoCard label={i18nText("Invoices")} value={list.length} icon={Receipt} />
          </div>

          <DataTable<ApiInvoice>
            className="mt-6"
            rows={filtered}
            getKey={(inv) => inv.id}
            onRowClick={(inv) => navigate(`/my/billing/${inv.id}`)}
            initialSortKey="issued"
            initialSortDir="desc"
            toolbar={
              <div className="flex flex-wrap gap-1">
                {FILTERS.map((f) => (
                  <button
                    key={f.value}
                    onClick={() => setFilter(f.value)}
                    className={cn(
                      "rounded-sm px-3 py-1.5 text-sm font-medium transition-colors",
                      filter === f.value ? "bg-primary/15 text-primary" : "text-muted hover:text-foreground",
                    )}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
            }
            emptyState={
              <EmptyState className="m-0 py-14" icon={Receipt} title={i18nText("No invoices here")} description={i18nText("There are no invoices matching this filter.")} />
            }
            columns={[
              { key: "number", header: i18nText("Invoice"), sortValue: (i) => i.number, render: (i) => <span className="font-medium text-foreground">{i.number}</span> },
              { key: "project", header: i18nText("Project"), hideBelow: "sm", className: "text-muted", render: (i) => i.instance_name || "—" },
              { key: "issued", header: i18nText("Issued"), hideBelow: "md", className: "text-muted", sortValue: (i) => i.issued || "", render: (i) => (i.issued ? formatDate(i.issued) : "—") },
              { key: "status", header: i18nText("Status"), sortValue: (i) => i.status, render: (i) => <StatusBadge status={i.status} /> },
              { key: "amount", header: i18nText("Amount"), align: "right", sortValue: (i) => i.total, render: (i) => <span className="font-medium tabular-nums">{money(i.total, i.currency)}</span> },
              {
                key: "go",
                header: "",
                align: "right",
                width: "120px",
                render: (i) => (
                  <div className="flex items-center justify-end gap-2" onClick={(e) => e.stopPropagation()}>
                    {i.payable && i.portal_url && (
                      <Button size="sm" onClick={() => (window.location.href = i.portal_url)}>{i18nText("Pay")}</Button>
                    )}
                    <ChevronRight className="size-4 text-muted" />
                  </div>
                ),
              },
            ]}
          />
        </>
      )}
    </div>
  );
}
