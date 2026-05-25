"use client";

/**
 * Auth helpers — token storage + authenticated fetch wrapper.
 *
 * Token is kept in localStorage (simple, fine for local dev). For production
 * you'd use httpOnly cookies set by the backend; that's a v2 concern.
 */

const TOKEN_KEY = "anykb:token";
const USER_KEY = "anykb:user";

export type User = {
  id: string;
  email: string;
  display_name: string;
  created_at: string | null;
};

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function getUser(): User | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as User;
  } catch {
    return null;
  }
}

export function setUser(user: User): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function clearAuth(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(USER_KEY);
}

export function isAuthenticated(): boolean {
  return !!getToken();
}

/** fetch wrapper that auto-attaches Bearer token. */
export async function authFetch(input: RequestInfo, init: RequestInit = {}): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return fetch(input, { ...init, headers });
}

// ---------------------------------------------------------------------------
// Auth API
// ---------------------------------------------------------------------------
type AuthResponse = { token: string; user: User };

export async function login(email: string, password: string): Promise<AuthResponse> {
  const r = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({ detail: `HTTP ${r.status}` }));
    throw new Error(detail.detail ?? "login failed");
  }
  const data = (await r.json()) as AuthResponse;
  setToken(data.token);
  setUser(data.user);
  return data;
}

export async function register(
  email: string,
  password: string,
  display_name = ""
): Promise<AuthResponse> {
  const r = await fetch("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, display_name }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({ detail: `HTTP ${r.status}` }));
    throw new Error(detail.detail ?? "register failed");
  }
  const data = (await r.json()) as AuthResponse;
  setToken(data.token);
  setUser(data.user);
  return data;
}

export function logout(): void {
  clearAuth();
}

// ---------------------------------------------------------------------------
// v3-M5: profile editing
// ---------------------------------------------------------------------------
export async function updateProfile(displayName: string): Promise<User> {
  const r = await authFetch("/api/auth/me", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: displayName }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({ detail: `HTTP ${r.status}` }));
    throw new Error(detail.detail ?? "update profile failed");
  }
  const user = (await r.json()) as User;
  setUser(user);
  return user;
}

export async function changePassword(
  oldPassword: string,
  newPassword: string
): Promise<void> {
  const r = await authFetch("/api/auth/change-password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({ detail: `HTTP ${r.status}` }));
    throw new Error(detail.detail ?? "change password failed");
  }
}

export async function deleteAccount(): Promise<void> {
  const r = await authFetch("/api/auth/me", { method: "DELETE" });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({ detail: `HTTP ${r.status}` }));
    throw new Error(detail.detail ?? "delete account failed");
  }
  clearAuth();
}
