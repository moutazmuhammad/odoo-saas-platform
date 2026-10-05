/** Align to the trigger's logical end and keep fixed menus inside the viewport. */
export function menuPosition(rect: { left: number; right: number; bottom: number }, viewportWidth: number, rtl: boolean, menuWidth = 176) {
  const preferred = rtl ? rect.left : rect.right - menuWidth;
  return { top: rect.bottom + 4, left: Math.max(8, Math.min(preferred, viewportWidth - menuWidth - 8)) };
}
