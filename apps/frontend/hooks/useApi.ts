"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/services/api";

/** Minimal data-fetching hook with optional polling. */
export function useApi<T = any>(path: string | null, refreshMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(!!path);
  const alive = useRef(true);
  const retry = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attempts = useRef(0);

  const load = useCallback(async () => {
    if (!path) return;
    try {
      const d = await api<T>(path);
      if (alive.current) {
        setData(d);
        setError(null);
        attempts.current = 0;
      }
    } catch (e: any) {
      if (alive.current) {
        setError(e?.message ?? String(e));
        // the backend may be restarting: a one-off load retries (2 s, 4 s, ... up to 30 s) instead of
        // leaving the page on "Failed to fetch"; polled loads simply try again on their next tick
        if (!refreshMs && !(e instanceof ApiError && e.status < 500) && attempts.current < 20) {
          const wait = Math.min(2000 * 2 ** attempts.current, 30000);
          attempts.current += 1;
          retry.current = setTimeout(() => { void load(); }, wait);
        }
      }
    } finally {
      if (alive.current) setLoading(false);
    }
  }, [path, refreshMs]);

  useEffect(() => {
    alive.current = true;
    attempts.current = 0;
    setLoading(!!path);
    load();
    let h: ReturnType<typeof setInterval> | undefined;
    if (refreshMs) h = setInterval(load, refreshMs);
    return () => {
      alive.current = false;
      if (h) clearInterval(h);
      if (retry.current) clearTimeout(retry.current);
    };
  }, [load, refreshMs, path]);

  return { data, error, loading, reload: load, setData };
}
