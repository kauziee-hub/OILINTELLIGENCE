"use client";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const TOKEN_KEY = "oi_token";
const USER_KEY = "oi_user";

export interface Session {
  token: string;
  username: string;
  role: "admin" | "engineer" | "operator" | "viewer";
}

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function getSession(): Session | null {
  const s = storage();
  const t = s?.getItem(TOKEN_KEY);
  const u = s?.getItem(USER_KEY);
  if (t === null || t === undefined || !u) return null;   // "" = open-access session (no sign-in)
  try {
    return { token: t, ...(JSON.parse(u) as Omit<Session, "token">) };
  } catch {
    return null;
  }
}

export function setSession(sess: Session | null) {
  const s = storage();
  if (!s) return;
  if (!sess) {
    s.removeItem(TOKEN_KEY);
    s.removeItem(USER_KEY);
  } else {
    s.setItem(TOKEN_KEY, sess.token);
    s.setItem(USER_KEY, JSON.stringify({ username: sess.username, role: sess.role }));
  }
}

export const ROLE_RANK: Record<string, number> = { viewer: 0, operator: 1, engineer: 2, admin: 3 };

export function hasRole(min: keyof typeof ROLE_RANK): boolean {
  const s = getSession();
  return !!s && ROLE_RANK[s.role] >= ROLE_RANK[min];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T = any>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const sess = getSession();
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (sess?.token) headers["Authorization"] = `Bearer ${sess.token}`;
  let body = init.body;
  if (init.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  }
  const res = await fetch(`${API_URL}/api/v1${path}`, { ...init, headers, body });
  if (res.status === 401) {
    setSession(null);
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      window.location.href = "/login";
    }
    throw new ApiError(401, "Not authenticated");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(res.status, msg);
  }
  const ct = res.headers.get("content-type") ?? "";
  return (ct.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export interface AuthConfig {
  auth_required: boolean;
  username?: string;
  role?: Session["role"];
}

/** Whether this deployment uses a sign-in page (public endpoint). */
export async function authConfig(): Promise<AuthConfig> {
  const res = await fetch(`${API_URL}/api/v1/auth/config`);
  if (!res.ok) throw new ApiError(res.status, res.statusText);
  return res.json();
}

/** The current session; on an open-access deployment (internal company network, no sign-in) one is
 * created without a token, as the identity the server records in its audit log. */
export async function ensureSession(): Promise<Session | null> {
  const s = getSession();
  if (s) return s;
  const c = await authConfig();
  if (c.auth_required) return null;
  const sess: Session = { token: "", username: c.username ?? "control-room", role: c.role ?? "admin" };
  setSession(sess);
  return sess;
}

export async function login(username: string, password: string): Promise<Session> {
  const form = new URLSearchParams({ username, password });
  const res = await fetch(`${API_URL}/api/v1/auth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form,
  });
  if (!res.ok) throw new ApiError(res.status, res.status === 401 ? "Invalid username or password" : res.statusText);
  const j = await res.json();
  const sess: Session = { token: j.access_token, username: j.username, role: j.role };
  setSession(sess);
  return sess;
}

export function wsUrl(): string {
  const base = API_URL.replace(/^http/, "ws");
  const t = getSession()?.token ?? "";
  return `${base}/ws/live?token=${encodeURIComponent(t)}`;
}
