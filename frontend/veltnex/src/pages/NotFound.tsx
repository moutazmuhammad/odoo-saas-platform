import { useNavigate } from "react-router-dom";
import { Compass } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Logo } from "@/components/Logo";

export default function NotFound() {
  const navigate = useNavigate();
  return (
    <div className="flex min-h-screen flex-col items-center justify-center px-4 text-center">
      <Logo />
      <span className="mt-10 flex size-16 items-center justify-center rounded-2xl bg-card border border-border text-primary">
        <Compass className="size-8" />
      </span>
      <h1 className="mt-6 text-5xl font-bold tracking-tight">404</h1>
      <p className="mt-3 max-w-sm text-muted">
        We couldn't find that page. It may have been moved or never existed.
      </p>
      <div className="mt-8 flex gap-3">
        <Button onClick={() => navigate("/")}>Go home</Button>
        <Button variant="secondary" onClick={() => navigate("/docs")}>
          Browse docs
        </Button>
      </div>
    </div>
  );
}
