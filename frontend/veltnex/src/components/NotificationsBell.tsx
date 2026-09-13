import * as React from "react";
import { useNavigate } from "react-router-dom";
import { Bell, CheckCircle2 } from "lucide-react";
import { api, type ApiInstance } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Note {
  id: string;
  text: string;
  to: string;
  tone: "warning" | "danger" | "info";
}

function link(i: ApiInstance) {
  return i.is_hosting ? `/my/instances/${i.id}/environments` : `/my/instances/${i.id}`;
}

/** Lightweight notifications derived from live account signals (unpaid
 *  invoices, suspended/failed/awaiting environments). No backend feed needed
 *  yet — the bell surfaces the same things the Overview action center does, so
 *  customers see them from anywhere. */
export function NotificationsBell({ className }: { className?: string }) {
  const navigate = useNavigate();
  const [notes, setNotes] = React.useState<Note[]>([]);
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    let alive = true;
    api
      .dashboard()
      .then((d) => {
        if (!alive) return;
        const out: Note[] = [];
        for (const inv of d.recent_invoices) {
          if (inv.status === "open" || inv.status === "overdue")
            out.push({ id: `inv-${inv.id}`, text: `Invoice ${inv.number} ${inv.status}`, to: `/my/billing/${inv.id}`, tone: "warning" });
        }
        for (const i of d.instances) {
          if (i.state === "suspended")
            out.push({ id: `s-${i.id}`, text: `${i.name} is suspended`, to: link(i), tone: "warning" });
          else if (i.state === "failed")
            out.push({ id: `f-${i.id}`, text: `${i.name} failed to deploy`, to: link(i), tone: "danger" });
          else if (i.state === "pending_payment")
            out.push({ id: `p-${i.id}`, text: `${i.name} awaiting payment`, to: `/my/instances/${i.id}/checkout`, tone: "info" });
        }
        setNotes(out);
      })
      .catch(() => setNotes([]));
    return () => {
      alive = false;
    };
  }, []);

  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const count = notes.length;

  return (
    <div ref={ref} className={cn("relative", className)}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="relative rounded-md p-2 text-muted transition-colors hover:bg-card hover:text-foreground"
        aria-label={`Notifications${count ? ` (${count})` : ""}`}
      >
        <Bell className="size-5" />
        {count > 0 && (
          <span className="absolute right-1.5 top-1.5 flex size-2 items-center justify-center">
            <span className="absolute inline-flex size-2 animate-ping rounded-full bg-warning/70" />
            <span className="relative inline-flex size-2 rounded-full bg-warning" />
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 z-50 mt-2 w-80 overflow-hidden rounded-xl border border-border bg-card shadow-2xl animate-fade-in">
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <p className="text-sm font-semibold">Notifications</p>
            {count > 0 && <span className="text-xs text-muted">{count} need action</span>}
          </div>
          {count === 0 ? (
            <div className="flex flex-col items-center gap-2 px-4 py-8 text-center">
              <CheckCircle2 className="size-6 text-success" />
              <p className="text-sm text-muted">You're all caught up.</p>
            </div>
          ) : (
            <ul className="max-h-80 divide-y divide-border overflow-y-auto">
              {notes.map((n) => (
                <li key={n.id}>
                  <button
                    onClick={() => {
                      setOpen(false);
                      navigate(n.to);
                    }}
                    className="flex w-full items-start gap-2.5 px-4 py-3 text-left text-sm transition-colors hover:bg-background/50"
                  >
                    <span
                      className={cn(
                        "mt-1.5 size-2 shrink-0 rounded-full",
                        n.tone === "danger" ? "bg-danger" : n.tone === "info" ? "bg-info" : "bg-warning",
                      )}
                    />
                    <span className="flex-1">{n.text}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
