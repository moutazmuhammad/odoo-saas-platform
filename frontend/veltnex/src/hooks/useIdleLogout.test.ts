import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { useIdleLogout, IDLE_LOGOUT_MS, IDLE_WARNING_MS, ACTIVITY_STORAGE_KEY } from "./useIdleLogout";

describe("useIdleLogout", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps a session active beyond the old thirty-minute cutoff", () => {
    const idle = vi.fn();
    renderHook(() => useIdleLogout(true, vi.fn(), idle));
    vi.advanceTimersByTime(60 * 60 * 1000);
    expect(idle).not.toHaveBeenCalled();
  });

  it("does nothing while disabled", () => {
    const onWarn = vi.fn();
    const onIdle = vi.fn();
    renderHook(() => useIdleLogout(false, onWarn, onIdle));
    vi.advanceTimersByTime(IDLE_LOGOUT_MS + 1000);
    expect(onWarn).not.toHaveBeenCalled();
    expect(onIdle).not.toHaveBeenCalled();
  });

  it("warns before the idle timeout, then calls onIdle at the timeout", () => {
    const onWarn = vi.fn();
    const onIdle = vi.fn();
    renderHook(() => useIdleLogout(true, onWarn, onIdle));

    vi.advanceTimersByTime(IDLE_LOGOUT_MS - IDLE_WARNING_MS - 1);
    expect(onWarn).not.toHaveBeenCalled();

    vi.advanceTimersByTime(2);
    expect(onWarn).toHaveBeenCalledTimes(1);
    expect(onIdle).not.toHaveBeenCalled();

    vi.advanceTimersByTime(IDLE_WARNING_MS);
    expect(onIdle).toHaveBeenCalledTimes(1);
  });

  it("resets the timers on activity, so a warned-then-active user never gets logged out", () => {
    const onWarn = vi.fn();
    const onIdle = vi.fn();
    renderHook(() => useIdleLogout(true, onWarn, onIdle));

    vi.advanceTimersByTime(IDLE_LOGOUT_MS - IDLE_WARNING_MS + 10);
    expect(onWarn).toHaveBeenCalledTimes(1);

    window.dispatchEvent(new Event("keydown"));

    // Just short of the FULL idle window from the reset point — onIdle
    // must not have fired again yet (a second onWarn firing here would be
    // correct, expected behavior for renewed inactivity, not asserted).
    vi.advanceTimersByTime(IDLE_LOGOUT_MS - 100);
    expect(onIdle).not.toHaveBeenCalled();
  });

  it("stops tracking and clears pending timers when disabled mid-session", () => {
    const onWarn = vi.fn();
    const onIdle = vi.fn();
    const { rerender } = renderHook(
      ({ enabled }) => useIdleLogout(enabled, onWarn, onIdle),
      { initialProps: { enabled: true } }
    );

    vi.advanceTimersByTime(IDLE_LOGOUT_MS - IDLE_WARNING_MS - 10);
    rerender({ enabled: false });
    vi.advanceTimersByTime(IDLE_LOGOUT_MS + 1000);

    expect(onWarn).not.toHaveBeenCalled();
    expect(onIdle).not.toHaveBeenCalled();
  });

  it("extends an inactive tab when another tab reports activity", () => {
    const idle = vi.fn();
    renderHook(() => useIdleLogout(true, vi.fn(), idle));
    vi.advanceTimersByTime(IDLE_LOGOUT_MS - 1000);
    window.dispatchEvent(new StorageEvent("storage", { key: ACTIVITY_STORAGE_KEY, newValue: String(Date.now()) }));
    vi.advanceTimersByTime(2000);
    expect(idle).not.toHaveBeenCalled();
    vi.advanceTimersByTime(IDLE_LOGOUT_MS);
    expect(idle).toHaveBeenCalledOnce();
  });

  it("checks recent shared activity even if the storage event was missed in a background tab", () => {
    const idle = vi.fn();
    renderHook(() => useIdleLogout(true, vi.fn(), idle));
    vi.advanceTimersByTime(IDLE_LOGOUT_MS - 1000);
    localStorage.setItem(ACTIVITY_STORAGE_KEY, String(Date.now()));
    vi.advanceTimersByTime(2000);
    expect(idle).not.toHaveBeenCalled();
    vi.advanceTimersByTime(IDLE_LOGOUT_MS);
    expect(idle).toHaveBeenCalledOnce();
  });

  it("removes its activity listeners on unmount", () => {
    const addSpy = vi.spyOn(window, "addEventListener");
    const removeSpy = vi.spyOn(window, "removeEventListener");
    const { unmount } = renderHook(() => useIdleLogout(true, vi.fn(), vi.fn()));
    const addedEvents = addSpy.mock.calls.map((c) => c[0]);
    unmount();
    const removedEvents = removeSpy.mock.calls.map((c) => c[0]);
    for (const ev of addedEvents) {
      expect(removedEvents).toContain(ev);
    }
    addSpy.mockRestore();
    removeSpy.mockRestore();
  });
});
