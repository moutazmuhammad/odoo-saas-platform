import * as React from "react";
import { api } from "./api";
export const PermissionContext = React.createContext<{ id: number; permissions?: string[] } | null>(null);
export function usePermissions(id: number) {
  const context = React.useContext(PermissionContext);
  const [permissions, setPermissions] = React.useState<string[] | undefined>([]);
  React.useEffect(() => {
    if (context?.id === id) return;
    let cancelled = false;
    setPermissions([]);
    api.instance(id).then(i => { if (!cancelled) setPermissions(i.permissions); }).catch(() => {});
    return () => { cancelled = true; };
  }, [id, context?.id]);
  const effective = context?.id === id ? context.permissions : permissions;
  return (permission: string) => !!effective?.includes(permission);
}
export function hasPermission(permissions: string[] | undefined, permission: string) {
  return !!permissions?.includes(permission);
}
