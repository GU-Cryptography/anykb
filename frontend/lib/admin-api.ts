"use client";

import { authFetch } from "./auth";

/**
 * Admin dashboard API client (06-01).
 *
 * Mirrors backend/src/admin/routes.py. All calls go through authFetch (Bearer
 * token) and hit /api/admin/* — the backend rejects non-admins with 403.
 * Self-protection invariants surface as 400 (self) / 409 (last admin); we
 * throw the backend `detail` so the page can toast it verbatim.
 */

// ---------------------------------------------------------------------------
// Types — keep in sync with the backend response contract.
// ---------------------------------------------------------------------------
export type AdminStats = {
  users: {
    total: number;
    active: number;
    banned: number;
    admins: number;
    new_last_7d: number;
  };
  kbs: {
    total: number;
    system: number;
  };
  documents: number;
  conversations: number;
  messages: number;
};

export type AdminUser = {
  id: string;
  email: string;
  display_name: string;
  created_at: string | null;
  is_admin: boolean;
  is_active: boolean;
  byok_configured: boolean;
  kb_count: number;
  conversation_count: number;
};

export type AdminUserListResponse = {
  total: number;
  limit: number;
  offset: number;
  users: AdminUser[];
};

export type AdminKb = {
  id: string;
  name: string;
  description: string;
  owner_id: string;
  owner_email: string | null;
  is_system: boolean;
  documents_count: number;
  chunks_count: number;
  member_count: number;
  created_at: string | null;
};

export type AdminKbListResponse = {
  total: number;
  limit: number;
  offset: number;
  kbs: AdminKb[];
};

export class AdminApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function unwrap<T>(r: Response): Promise<T> {
  if (!r.ok) {
    let detail: unknown = null;
    let message = `HTTP ${r.status}`;
    try {
      const j = await r.json();
      detail = j.detail ?? j;
      if (typeof detail === "string") {
        message = detail;
      } else if (detail && typeof detail === "object") {
        const d = detail as { message?: string };
        message = typeof d.message === "string" ? d.message : JSON.stringify(detail);
      }
    } catch {
      /* keep default */
    }
    throw new AdminApiError(r.status, detail, message);
  }
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------
export async function getStats(): Promise<AdminStats> {
  return unwrap(await authFetch("/api/admin/stats"));
}

// ---------------------------------------------------------------------------
// Users
// ---------------------------------------------------------------------------
export async function listUsers(
  limit = 50,
  offset = 0
): Promise<AdminUserListResponse> {
  return unwrap(
    await authFetch(`/api/admin/users?limit=${limit}&offset=${offset}`)
  );
}

export async function getUser(id: string): Promise<AdminUser> {
  return unwrap(await authFetch(`/api/admin/users/${id}`));
}

export async function updateUser(
  id: string,
  body: { is_active?: boolean; is_admin?: boolean }
): Promise<AdminUser> {
  return unwrap(
    await authFetch(`/api/admin/users/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
  );
}

export async function resetUserPassword(
  id: string,
  newPassword: string
): Promise<{ ok: true }> {
  return unwrap(
    await authFetch(`/api/admin/users/${id}/reset-password`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_password: newPassword }),
    })
  );
}

export async function deleteUser(id: string): Promise<void> {
  await unwrap(await authFetch(`/api/admin/users/${id}`, { method: "DELETE" }));
}

// ---------------------------------------------------------------------------
// Knowledge bases
// ---------------------------------------------------------------------------
export async function listKbs(
  limit = 50,
  offset = 0
): Promise<AdminKbListResponse> {
  return unwrap(
    await authFetch(`/api/admin/kbs?limit=${limit}&offset=${offset}`)
  );
}

export async function deleteKb(id: string): Promise<void> {
  await unwrap(await authFetch(`/api/admin/kbs/${id}`, { method: "DELETE" }));
}
