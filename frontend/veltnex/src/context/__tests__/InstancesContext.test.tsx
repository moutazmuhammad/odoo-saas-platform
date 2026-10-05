import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { InstancesProvider, useInstances } from "../InstancesContext";
import { api } from "@/lib/api";
const auth = vi.hoisted(() => ({ isAuthenticated: true, user: { must_change_password: true, must_verify_phone: false } }));
vi.mock("../AuthContext", () => ({ useAuth: () => auth }));
vi.mock("@/hooks/usePolling", () => ({ usePolling: vi.fn() }));
vi.mock("@/lib/api", async original => ({ ...await original<typeof import("@/lib/api")>(), api: { instances: vi.fn() } }));
function View() { const { instances, error } = useInstances(); return <div>{instances.map(i => <span key={i.id}>{i.name}</span>)}{error}</div>; }
describe("Workspace cache during first login", () => {
  beforeEach(() => { vi.clearAllMocks(); auth.user.must_change_password = true; auth.user.must_verify_phone = false; });
  it("loads projects after changing the temporary password without needing a reload", async () => {
    vi.mocked(api.instances).mockResolvedValue([{ id: 13, name: "Customer project", state: "stopped" }] as Awaited<ReturnType<typeof api.instances>>);
    const view = render(<InstancesProvider><View /></InstancesProvider>);
    expect(api.instances).not.toHaveBeenCalled();
    auth.user.must_change_password = false;
    view.rerender(<InstancesProvider><View /></InstancesProvider>);
    await waitFor(() => expect(screen.getByText("Customer project")).toBeInTheDocument());
    expect(api.instances).toHaveBeenCalledTimes(1);
    auth.user.must_change_password = true;
    view.rerender(<InstancesProvider><View /></InstancesProvider>);
    await waitFor(() => expect(screen.queryByText("Customer project")).not.toBeInTheDocument());
  });
  it("does not fetch projects while mobile verification is pending", () => {
    auth.user.must_change_password = false;
    auth.user.must_verify_phone = true;
    render(<InstancesProvider><View /></InstancesProvider>);
    expect(api.instances).not.toHaveBeenCalled();
  });
});
