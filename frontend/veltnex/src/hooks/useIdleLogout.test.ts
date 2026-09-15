import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { useIdleLogout, IDLE_LOGOUT_MS, IDLE_WARNING_MS } from "./useIdleLogout";

describe("useIdleLogout", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
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
