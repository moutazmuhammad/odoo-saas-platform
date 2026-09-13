import * as React from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Check, Boxes } from "lucide-react";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/EmptyState";
import { Spinner } from "@/components/Spinner";
import { AlertBanner } from "@/components/AlertBanner";
import { ServiceIcon } from "@/components/ServiceIcon";
import { api, ApiError, type ApiService } from "@/lib/api";

export default function Services() {
  const [services, setServices] = React.useState<ApiService[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    api
      .services()
      .then(setServices)
      .catch((e) =>
        setError(e instanceof ApiError ? e.message : "Could not load services.")
      );
  }, []);

  return (
    <div className="mx-auto max-w-7xl animate-fade-in px-4 py-16 sm:px-6 lg:px-8">
      <div>
        <p className="text-sm font-medium text-primary">Ready-Made Apps</p>
        <h1 className="mt-2 max-w-2xl text-4xl font-bold tracking-tight">
          Pre-built Odoo apps, running on day one
        </h1>
        <p className="mt-4 max-w-2xl text-muted">
          Pick a solution tuned to your industry — pharmacy management, retail,
          clinics and more. Each one boots with its modules and database
          already set up, ready to use and yours to customize. Open any app to
          see the full details and start a free trial.
        </p>
      </div>

      {error && <AlertBanner className="mt-8" variant="danger" title="Couldn't load services" description={error} />}

      {!services && !error && (
        <div className="mt-16 flex justify-center">
          <Spinner size="lg" label="Loading services…" />
        </div>
      )}

      {services && services.length === 0 && (
        <EmptyState
          className="mt-12"
          icon={Boxes}
          title="No services published yet"
          description="Check back soon — new services are on the way."
        />
      )}

      {services && services.length > 0 && (
        <div className="mt-10 grid gap-5 md:grid-cols-2 lg:grid-cols-3">
          {services.map((s) => (
            <Link key={s.id} to={`/services/${s.id}`}>
              <Card className="group flex h-full flex-col p-6 transition-all hover:-translate-y-0.5 hover:border-primary/40">
                <span className="flex size-11 items-center justify-center rounded-xl bg-primary/15 text-primary">
                  <ServiceIcon icon={s.icon} className="size-5" />
                </span>
                <h3 className="mt-5 text-lg font-semibold">{s.name}</h3>
                <p className="mt-2 text-sm text-muted">{s.tagline}</p>
                {s.highlights && s.highlights.length > 0 && (
                  <ul className="mt-4 space-y-2">
                    {s.highlights.map((h) => (
                      <li key={h} className="flex items-center gap-2 text-sm text-foreground/80">
                        <Check className="size-4 text-success" />
                        {h}
                      </li>
                    ))}
                  </ul>
                )}
                <span className="mt-5 inline-flex items-center gap-1 text-sm font-medium text-primary">
                  View plans <ArrowRight className="size-3.5 transition-transform group-hover:translate-x-0.5" />
                </span>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
