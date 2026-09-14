import * as React from "react";
import { render } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ToastProvider } from "@/context/ToastContext";

/**
 * Shared render helper for portal pages: wraps in the same Router +
 * ToastProvider context the real app tree provides. Pass `path` for a
 * page that reads its instance id from a route param (e.g. `/i/:id`)
 * rather than a prop.
 */
export function renderWithProviders(
  ui: React.ReactElement,
  { route = "/", path }: { route?: string; path?: string } = {},
) {
  const content = path ? (
    <Routes>
      <Route path={path} element={ui} />
    </Routes>
  ) : (
    ui
  );
  return render(
    <MemoryRouter initialEntries={[route]}>
      <ToastProvider>{content}</ToastProvider>
    </MemoryRouter>,
  );
}
