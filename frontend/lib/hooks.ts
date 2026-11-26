"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";

export interface Query<T> {
  data: T | undefined;
  error: ApiError | null;
  loading: boolean;
  reload: () => Promise<void>;
  setData: (d: T) => void;
}

/** GET a path; re-runs when the path changes. Pass null to skip. Optional polling. */
export function useApi<T>(path: string | null, opts: { pollMs?: number } = {}): Query<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const current = useRef(path);
  current.current = path;

  const reload = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      const d = await api<T>(path);
      if (current.current === path) {
        setData(d);
        setError(null);
      }
    } catch (e) {
      if (current.current === path) setError(e instanceof ApiError ? e : new ApiError(0, String(e)));
    } finally {
      if (current.current === path) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    setData(undefined);
    void reload();
  }, [reload]);

  useEffect(() => {
    if (!opts.pollMs || !path) return;
    const t = setInterval(() => {
      void api<T>(path)
        .then((d) => current.current === path && setData(d))
        .catch(() => {});
    }, opts.pollMs);
    return () => clearInterval(t);
  }, [path, opts.pollMs]);

  return { data, error, loading, reload, setData };
}

/** Wrap an async action with pending + error state. */
export function useAction<A extends unknown[], R>(fn: (...args: A) => Promise<R>) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(
    async (...args: A): Promise<R | undefined> => {
      setPending(true);
      setError(null);
      try {
        return await fn(...args);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        return undefined;
      } finally {
        setPending(false);
      }
    },
    [fn],
  );
  return { run, pending, error, setError };
}
