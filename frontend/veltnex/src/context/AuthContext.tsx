import * as React from "react";
import { api, ApiError, setUnauthorizedHandler, type ApiUser } from "@/lib/api";

interface RegisterForm {
  name: string;
  email: string;
  phone: string;
  company_name?: string;
  country_id: number | string;
  city: string;
  street?: string;
  password: string;
}

interface AuthContextValue {
  user: ApiUser | null;
  loading: boolean;
  isAuthenticated: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  // phone-OTP registration (mirrors the Odoo saas.registration.otp flow).
  // The code is delivered out-of-band (SMS) and never returned to the client.
  registerStart: (form: RegisterForm) => Promise<{ otp_sent: boolean }>;
  registerResend: (phone: string) => Promise<{ otp_sent: boolean }>;
  registerVerify: (form: RegisterForm & { otp: string }) => Promise<ApiUser>;
  // Password reset (in-SPA, email OTP). resetVerify signs the user in.
  resetStart: (email: string) => Promise<{ sent: boolean }>;
  resetVerify: (email: string, otp: string, password: string) => Promise<ApiUser>;
}

const AuthContext = React.createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = React.useState<ApiUser | null>(null);
  const [loading, setLoading] = React.useState(true);

  const refresh = React.useCallback(async () => {
    try {
      const me = await api.me();
      setUser(me);
    } catch (e) {
      // auth_required is expected when logged out — not an error.
      setUser(null);
      if (!(e instanceof ApiError) || e.code !== "auth_required") {
        // Network/server problems: stay logged out, surfaced by callers.
      }
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    refresh();
  }, [refresh]);

  // CX-002: any request that hits an expired session clears the user here,
  // so the route guard sends the customer to login instead of components
  // rendering a misleading "not found" state.
  React.useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    return () => setUnauthorizedHandler(null);
  }, []);

  const login = React.useCallback(async (email: string, password: string) => {
    const me = await api.login(email, password);
    setUser(me);
  }, []);

  const logout = React.useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setUser(null);
    }
  }, []);

  const registerStart = React.useCallback((form: RegisterForm) => {
    return api.registerStart(form as unknown as Record<string, unknown>);
  }, []);

  const registerResend = React.useCallback((phone: string) => {
    return api.registerResend(phone);
  }, []);

  const registerVerify = React.useCallback(
    async (form: RegisterForm & { otp: string }) => {
      const me = await api.registerVerify(form as unknown as Record<string, unknown>);
      setUser(me);
      return me;
    },
    []
  );

  const resetStart = React.useCallback((email: string) => {
    return api.resetStart(email);
  }, []);

  const resetVerify = React.useCallback(
    async (email: string, otp: string, password: string) => {
      const me = await api.resetVerify(email, otp, password);
      setUser(me);
      return me;
    },
    []
  );

  const value = React.useMemo<AuthContextValue>(
    () => ({
      user,
      loading,
      isAuthenticated: !!user,
      login,
      logout,
      refresh,
      registerStart,
      registerResend,
      registerVerify,
      resetStart,
      resetVerify,
    }),
    [user, loading, login, logout, refresh, registerStart, registerResend,
     registerVerify, resetStart, resetVerify]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = React.useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
