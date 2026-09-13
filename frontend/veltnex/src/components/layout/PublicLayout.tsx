import { Outlet, useLocation } from "react-router-dom";
import * as React from "react";
import { PublicNav } from "./PublicNav";
import { Footer } from "./Footer";

export function PublicLayout() {
  const { pathname } = useLocation();

  // Reset scroll on route change — avoids layout surprises between pages.
  React.useEffect(() => {
    window.scrollTo({ top: 0 });
  }, [pathname]);

  return (
    <div className="flex min-h-screen flex-col">
      <PublicNav />
      <main className="flex-1">
        <Outlet />
      </main>
      <Footer />
    </div>
  );
}
