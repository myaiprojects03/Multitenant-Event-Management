"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, SESSION_KEY, setToken } from "./api";
import type { Session } from "./types";

interface SessionState {
  session: Session | null;
  ready: boolean;
  signIn: (email: string, eventId?: string) => Promise<Session>;
  signOut: () => void;
}

const Ctx = createContext<SessionState | null>(null);

function load(): Session | null {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Session;
    if (Date.now() > s.issued_at + s.expires_in * 1000 - 30_000) return null; // expired
    return s;
  } catch {
    return null;
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const s = load();
    setToken(s?.access_token ?? null);
    setSession(s);
    setReady(true);
  }, []);

  const signIn = useCallback(async (email: string, eventId?: string) => {
    const res = await api<Omit<Session, "email" | "issued_at">>("/auth/demo-login", {
      body: { email, ...(eventId ? { event_id: eventId } : {}) },
      token: null,
    });
    const s: Session = { ...res, email, issued_at: Date.now() };
    setToken(s.access_token);
    try {
      sessionStorage.setItem(SESSION_KEY, JSON.stringify(s));
    } catch {
      /* private mode: the session lives in memory only */
    }
    setSession(s);
    return s;
  }, []);

  const signOut = useCallback(() => {
    setToken(null);
    try {
      sessionStorage.removeItem(SESSION_KEY);
    } catch {
      /* ignore */
    }
    setSession(null);
  }, []);

  return <Ctx.Provider value={{ session, ready, signIn, signOut }}>{children}</Ctx.Provider>;
}

export function useSession(): SessionState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSession outside SessionProvider");
  return v;
}

/** The signed-in session; only call inside the console layout (which guarantees one). */
export function useRequiredSession(): Session {
  const { session } = useSession();
  if (!session) throw new Error("no session");
  return session;
}
