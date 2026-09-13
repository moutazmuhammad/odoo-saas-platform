import * as React from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, ArrowRight, Check, PackageX, Server, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/EmptyState";
import { Spinner } from "@/components/Spinner";
import { ServiceIcon } from "@/components/ServiceIcon";
import { api, ApiError, type ApiService, type TrialInfo } from "@/lib/api";

export default function ServiceDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [service, setService] = React.useState<ApiService | null>(null);
  const [trial, setTrial] = React.useState<TrialInfo | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    api.meta().then((m) => setTrial(m.trial)).catch(() => {});
  }, []);

  React.useEffect(() => {
    setService(null);
    setError(null);
    api
      .service(Number(id))
      .then(setService)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Service not found."));
  }, [id]);

  // Ordering stays on Odoo (configure → checkout). Full navigation.
  const configure = (planId: number, trial = false) => {
    const base = `/services/${id}/plans/${planId}/configure`;
    window.location.href = trial ? `${base}?trial=1` : base;
  };

  if (error) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-24">
        <EmptyState
          icon={PackageX}
          title="Service not found"
          description="The service you're looking for doesn't exist or has moved."
          action={<Button onClick={() => navigate("/services")}>Back to services</Button>}
        />
      </div>
    );
  }

  if (!service) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center">
        <Spinner size="lg" label="Loading service…" />
      </div>
    );
  }

  const realPlans = (service.plans || []).filter((p) => !p.is_trial);
  const canTrial =
    !!service.trial_plan_id && !!trial?.services_available && (trial?.days ?? 0) > 0;

  return (
    <div className="animate-fade-in">
      <section className="relative overflow-hidden border-b border-border">
        <div className="pointer-events-none absolute left-1/2 top-0 h-72 w-[700px] -translate-x-1/2 rounded-full bg-primary/15 blur-[120px]" />
        <div className="relative mx-auto max-w-7xl px-4 py-16 sm:px-6 lg:px-8">
          <Link
            to="/services"
            className="inline-flex items-center gap-1.5 text-sm text-muted transition-colors hover:text-foreground"
          >
            <ArrowLeft className="size-4" />
            All services
          </Link>
          <div className="mt-8 max-w-2xl">
            <span className="flex size-14 items-center justify-center rounded-2xl bg-primary/15 text-primary">
              <ServiceIcon icon={service.icon} className="size-7" />
            </span>
            <h1 className="mt-6 text-4xl font-bold tracking-tight">{service.name}</h1>
            {service.tagline && (
              <p className="mt-3 text-lg text-primary">{service.tagline}</p>
            )}
            {canTrial && (
              <div className="mt-6">
                <Button onClick={() => configure(service.trial_plan_id!, true)}>
                  <Sparkles className="size-4" />
                  Start your {trial?.days}-day free trial
                </Button>
                <p className="mt-2 text-xs text-muted">No credit card required.</p>
              </div>
            )}
          </div>
        </div>
      </section>

      {service.description && (
        <section className="mx-auto max-w-3xl px-4 py-12 sm:px-6 lg:px-8">
          <div
            className="prose prose-invert max-w-none text-muted [&_h1]:text-foreground [&_h2]:text-foreground [&_h3]:text-foreground [&_a]:text-primary"
            dangerouslySetInnerHTML={{ __html: service.description }}
          />
        </section>
      )}

      {service.features && service.features.length > 0 && (
        <section className="mx-auto max-w-7xl px-4 pb-4 sm:px-6 lg:px-8">
          <h2 className="text-2xl font-bold tracking-tight">What's included</h2>
          <div className="mt-8 grid gap-5 md:grid-cols-3">
            {service.features.map((f, i) => (
              <Card key={i} className="p-6">
                <Check className="size-5 text-success" />
                <h3 className="mt-3 text-base font-semibold">{f.title}</h3>
                {f.description && <p className="mt-2 text-sm text-muted">{f.description}</p>}
              </Card>
            ))}
          </div>
        </section>
      )}

      <section className="mx-auto max-w-7xl px-4 py-16 sm:px-6 lg:px-8">
        <h2 className="text-2xl font-bold tracking-tight">Choose a plan</h2>
        {realPlans.length === 0 ? (
          <p className="mt-4 text-muted">
            Plans for this service are configured at checkout.{" "}
            {canTrial && "Start a free trial above to get going."}
          </p>
        ) : (
          <div className="mt-8 grid gap-5 md:grid-cols-3">
            {realPlans.map((p) => (
              <Card key={p.id} className="flex flex-col p-6">
                <h3 className="text-lg font-semibold">{p.name}</h3>
                <ul className="mt-4 space-y-2 text-sm text-muted">
                  <li className="flex items-center gap-2">
                    <Server className="size-4 text-primary" />
                    {p.workers} workers · {p.storage_gb} GB storage
                  </li>
                </ul>
                <Button className="mt-6 w-full" onClick={() => configure(p.id)}>
                  Get started
                  <ArrowRight />
                </Button>
              </Card>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
