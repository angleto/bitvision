"use client";

import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { ApiError, type Me, authApi, request } from "@/lib/api";

export interface RegisterResult {
  /** True when the backend requires the user to click the email link
   * before a session is available. In that case no token was stored. */
  verificationRequired: boolean;
}

interface AuthState {
  user: Me | null;
  status: "loading" | "ready";
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, displayName: string) => Promise<RegisterResult>;
  loginMfa: (email: string, password: string, totpCode: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<Me | null>(null);
  const [status, setStatus] = useState<"loading" | "ready">("loading");

  // Probe the session by calling /api/auth/me. The HttpOnly cookie
  // travels automatically via ``credentials: "include"``; a 401 means
  // the cookie is missing or expired, equivalent to "anonymous".
  const refresh = useCallback(async () => {
    try {
      const me = await authApi.me();
      setUser(me);
    } catch (err) {
      if (!(err instanceof ApiError) || err.status !== 401) {
        // 401 is the expected "not logged in" signal; anything else
        // is worth a console.warn so an oncall can spot it.
        // eslint-disable-next-line no-console
        if (process.env.NODE_ENV !== "production") console.warn("auth.me failed", err);
      }
      setUser(null);
    } finally {
      setStatus("ready");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Listen for the global ``bv:auth-401`` event dispatched by the
  // request wrapper whenever the server says the cookie is no longer
  // valid. Without this, the auth context kept ``user`` populated
  // even after a server-side revocation / expiry, so the SiteHeader
  // showed "Log out" while pages rendered "authentication required".
  useEffect(() => {
    if (typeof window === "undefined") return;
    const handler = () => {
      setUser(null);
      setStatus("ready");
    };
    window.addEventListener("bv:auth-401", handler);
    return () => window.removeEventListener("bv:auth-401", handler);
  }, []);

  const login = useCallback(
    async (email: string, password: string) => {
      // The backend sets the HttpOnly bvp_session cookie on the
      // response; nothing for us to persist client-side. Re-probe
      // /api/auth/me so the in-memory user state matches.
      await authApi.login(email, password);
      await refresh();
    },
    [refresh],
  );

  const loginMfa = useCallback(
    async (email: string, password: string, totpCode: string) => {
      await authApi.loginMfa(email, password, totpCode);
      await refresh();
    },
    [refresh],
  );

  const register = useCallback(
    async (email: string, password: string, displayName: string): Promise<RegisterResult> => {
      const resp = await authApi.register(email, password, displayName);
      if (resp.access_token) {
        await refresh();
      }
      return { verificationRequired: resp.email_verification_required };
    },
    [refresh],
  );

  const logout = useCallback(async () => {
    // Hit the backend so the cookie gets cleared server-side.
    try {
      await request<void>("/api/auth/logout", { method: "POST" });
    } catch {
      // Best-effort: even if the call fails, we still drop the local
      // user state so the chrome reflects the logout intent.
    }
    setUser(null);
  }, []);

  const value = useMemo<AuthState>(
    () => ({ user, status, login, loginMfa, register, logout }),
    [user, status, login, loginMfa, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
